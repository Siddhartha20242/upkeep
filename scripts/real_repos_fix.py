"""Real downstream projects I did not write: find them, prove the upgrade really breaks their OWN tests, then try `upkeep fix`.

Funnel, reported at every stage (most projects drop out, and the counts are the honest result):
  candidates   public repos found by GitHub code search: ones whose requirements pin an old PyYAML (5.x) or Jinja2 (3.0.x), or with
               --usage, ones that CALL the API that changes (`yaml.load(` ..., `from jinja2 import Markup` ...)
  has tests    the repo ships pytest-style tests
  deps         the project's dependencies install from wheels in Docker on the old AND the new library version
  radar hit    Upkeep's radar flags at least one line that will fail
  validated    the repo's own tests PASS on the old library and FAIL on the new one (a real, observed break)
  fixed        `upkeep fix` produced a patch that makes the repo's own tests pass on the new library, behavior check clean

Safety: read-only analysis of public code; the repos' tests run only inside the Docker sandbox (no network, no capabilities);
nothing is pushed anywhere and no issue or pull request is opened. Only counts and file:line references are saved.

Run:  .venv/bin/python -I scripts/real_repos_fix.py [--limit 30] [--max-fix 6] [--dry] [--usage]     Needs: gh, Docker, BASETEN_API_KEY (unless --dry)
"""
import json
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.outside_repos import fetch, gh  # noqa: E402
from upkeep import cli  # noqa: E402
from upkeep.audit import find_pin  # noqa: E402
from upkeep.autoprep import PrepareError, docker_ready, prepare_upgrade  # noqa: E402
from upkeep.llm import load_env  # noqa: E402
from upkeep.radar import run_radar  # noqa: E402
from upkeep.sandbox import run_tests  # noqa: E402

load_env(Path(".env"))
TARGETS = [  # (PyPI name, import name, accepted old versions, new version, search pins, usage searches, default old version)
    ("PyYAML", "yaml", lambda v: v.startswith("5."), "6.0.2", ["PyYAML==5.4.1", "PyYAML==5.3.1", "PyYAML==5.4"],
     ["yaml.load(", "yaml.load_all("], "5.4.1"),
    ("Jinja2", "jinja2", lambda v: v.startswith("3.0."), "3.1.4", ["Jinja2==3.0.3", "Jinja2==3.0.1", "Jinja2==3.0.2"],
     ["from jinja2 import Markup", "from jinja2 import escape", "jinja2.contextfunction", "jinja2.contextfilter"], "3.0.3"),
]


def candidates(limit: int, usage: bool = False) -> list[tuple[str, str]]:
    """Repos to try. By default: repos that PIN an old version. With --usage: repos that CALL the API that changes, so a break is
    plausible; the project's own tests then decide what the upgrade does to it."""
    seen, out = set(), []
    for dist, _, _, _, pins, usage_queries, _ in TARGETS:
        searches = [("search", "code", q, "--language", "python", "--limit", "60", "--json", "repository") for q in usage_queries] if usage else \
                   [("search", "code", pin, "--filename", "requirements.txt", "--limit", "60", "--json", "repository") for pin in pins]
        for q in searches:
            res = gh(*q)
            time.sleep(7)
            for r in json.loads(res or "[]"):
                repo = r["repository"]
                name = repo["nameWithOwner"]
                if repo.get("isFork") or repo.get("isPrivate") or name in seen:
                    continue
                seen.add(name)
                out.append((name, dist))
    return out[:limit]


def has_tests(root: Path) -> bool:
    return any(True for _ in root.rglob("test_*.py")) or any(True for _ in root.rglob("*_test.py"))


def main() -> int:
    a = sys.argv[1:]
    limit = int(a[a.index("--limit") + 1]) if "--limit" in a else 30
    usage = "--usage" in a
    max_fix = int(a[a.index("--max-fix") + 1]) if "--max-fix" in a else 6
    dry = "--dry" in a
    if not docker_ready():
        print("Docker is not running", file=sys.stderr)
        return 2
    funnel: Counter = Counter()
    rows: list[dict] = []
    fixes = 0
    for name, dist in candidates(limit, usage):
        funnel["candidates"] += 1
        row = {"repo": name, "lib": dist, "stage": "candidate"}
        rows.append(row)
        cfg = next(t for t in TARGETS if t[0] == dist)
        default_old = cfg[6]
        with tempfile.TemporaryDirectory() as d:
            root = fetch(name, Path(d))
            if root is None:
                row["stage"] = "skipped: too big or unreadable"
                print(f"{name}: {row['stage']}", flush=True)
                continue
            old = find_pin(root, dist, cfg[2]) or (default_old if usage else None)   # --usage: ask "what if it upgraded from here?"
            if old is None or not has_tests(root):
                row["stage"] = "skipped: no old pin" if old is None else "skipped: no tests"
                print(f"{name}: {row['stage']}", flush=True)
                continue
            funnel["has tests"] += 1
            try:
                prep = prepare_upgrade(root, dist, old, cfg[3], cfg[1], log=lambda s: None, only_imported=True)
            except PrepareError as e:
                row["stage"] = "skipped: dependencies did not install from wheels"
                print(f"{name}: {row['stage']} ({str(e)[:70]})", flush=True)
                continue
            funnel["deps"] += 1
            try:
                report = run_radar(root, prep.old_src, prep.new_src, prep.module)
            except Exception as e:  # noqa: BLE001 - one odd repo must not stop the run
                row["stage"] = f"skipped: radar error {type(e).__name__}"
                continue
            hard = [(s.file, s.line) for c, ss in report.affected for s in ss if c.kind != "default_changed"]
            if not hard:
                row["stage"] = "skipped: radar found nothing that will fail"
                print(f"{name}: {row['stage']}", flush=True)
                continue
            funnel["radar hit"] += 1
            row["flagged_lines"] = len(set(hard))
            args = dict(backend="docker", image=prep.image, with_coverage=False, pytest_args=["-q", "-x", "-p", "no:cacheprovider"], timeout=300)
            before = run_tests(root, deps_dir=prep.deps_old, **args)
            after = run_tests(root, deps_dir=prep.deps_new, **args)
            if not before.ok:
                row["stage"] = "skipped: tests do not pass on the OLD library in the sandbox"
                print(f"{name}: {row['stage']}", flush=True)
                continue
            if after.ok:
                row["stage"] = "skipped: the repo's tests do not notice the upgrade"
                print(f"{name}: {row['stage']}", flush=True)
                continue
            funnel["validated"] += 1
            row["stage"] = "validated: tests pass on old, FAIL on new"
            print(f"{name}: validated (flagged lines: {row['flagged_lines']})", flush=True)
            if dry or fixes >= max_fix:
                continue
            fixes += 1
            out = Path(d) / "upkeep-out"
            code = cli.main(["fix", "--repo", str(root), "--package", dist, "--from", old, "--to", cfg[3], "--module", cfg[1],
                             "--out", str(out), "--image", prep.image, "--minimal-deps"])
            result = json.loads((out / "result.json").read_text()) if (out / "result.json").exists() else {}
            row.update(status=result.get("status", f"exit {code}"), reason=str(result.get("reason", ""))[:160],
                       tokens=result.get("tokens"), model_calls=result.get("model_calls"))
            if result.get("status") == "verified":
                funnel["fixed"] += 1
            print(f"{name}: {row['status']}", flush=True)
    Path("eval_results").mkdir(exist_ok=True)
    Path("eval_results/real_repos_usage.json" if usage else "eval_results/real_repos.json").write_text(json.dumps({"funnel": dict(funnel), "repos": rows}, indent=2))
    print("\nFUNNEL:", dict(funnel))
    return 0


if __name__ == "__main__":
    sys.exit(main())
