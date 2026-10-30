"""Run the radar on public repos I did not write that PIN an old version of a library (read-only; nothing is executed).

Selection rule (so it is not cherry-picked): GitHub code search for a requirements file that pins the OLD version, take
repos in the order GitHub returns them, skip forks, skip repos over 40 MB, and report EVERY repo processed, including
those that never import the library (those count as "green").

For each repo: download the default-branch archive, extract it (path-traversal safe), run the radar between the pinned
version and the newer one. Only counts and `file:line` references are saved, never other people's code.

Run:  .venv/bin/python -I scripts/outside_repos.py [--per-lib 4]      Needs: `gh` logged in, network. No model is called.
"""

import io
import json
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from upkeep.pypi import sources_for
from upkeep.radar import RadarInputError, run_radar

LIBS = [
    dict(dist="PyYAML", module="yaml", old="5.4.1", new="6.0.2", pin="PyYAML==5.4.1"),
    dict(dist="Jinja2", module="jinja2", old="3.0.3", new="3.1.4", pin="Jinja2==3.0.3"),
    dict(dist="Werkzeug", module="werkzeug", old="2.3.8", new="3.0.3", pin="Werkzeug==2.3.8"),
]
MAX_KB, MAX_BYTES = 40_000, 60_000_000


def gh(*args: str, raw: bool = False):
    p = subprocess.run(["gh", *args], capture_output=True, timeout=180)
    if p.returncode != 0:
        return None
    return p.stdout if raw else p.stdout.decode()


def candidates(pin: str, want: int) -> list[str]:
    out = gh("search", "code", pin, "--filename", "requirements.txt", "--limit", "60", "--json", "repository")
    time.sleep(7)  # code search is rate limited
    seen: list[str] = []
    for r in json.loads(out or "[]"):
        repo = r["repository"]
        if repo.get("isFork") or repo.get("isPrivate") or repo["nameWithOwner"] in seen:
            continue
        seen.append(repo["nameWithOwner"])
    return seen[: want * 4]  # extra, because some will be too big or unreadable


def fetch(repo: str, dest: Path) -> Path | None:
    size = gh("api", f"repos/{repo}", "--jq", ".size")
    if not size or int(size.strip() or 0) > MAX_KB:
        return None
    blob = gh("api", f"repos/{repo}/tarball", raw=True)
    if not blob or len(blob) > MAX_BYTES:
        return None
    with tarfile.open(fileobj=io.BytesIO(blob)) as t:
        t.extractall(dest, filter="data")
    tops = [p for p in dest.iterdir() if p.is_dir()]
    return tops[0] if tops else None


def main() -> int:
    per_lib = int(sys.argv[sys.argv.index("--per-lib") + 1]) if "--per-lib" in sys.argv else 4
    rows = []
    for cfg in LIBS:
        old_src, new_src, module = sources_for(cfg["dist"], cfg["old"], cfg["new"], cfg["module"])
        done = 0
        for repo in candidates(cfg["pin"], per_lib):
            if done >= per_lib:
                break
            with tempfile.TemporaryDirectory() as d:
                root = fetch(repo, Path(d))
                if root is None:
                    print(f"[{cfg['dist']}] {repo}: skipped (too big or unreadable)", flush=True)
                    continue
                n_py = sum(1 for _ in root.rglob("*.py"))
                try:
                    rep = run_radar(root, old_src, new_src, module)
                except RadarInputError as e:
                    print(f"[{cfg['dist']}] {repo}: radar input error {e}", flush=True)
                    continue
                flagged = sorted({(s.file, s.line, s.symbol, c.kind) for c, sites in rep.affected for s in sites})
                row = {"lib": cfg["dist"], "from": cfg["old"], "to": cfg["new"], "repo": repo, "py_files": n_py,
                       "risk": rep.risk, "flagged": [list(x) for x in flagged]}
                rows.append(row)
                done += 1
                print(f"[{cfg['dist']}] {repo}: {n_py} py files, risk {rep.risk}, {len(flagged)} flagged", flush=True)
                if "--show" in sys.argv:  # terminal only, never saved: lets ME judge each flag by reading the line
                    for f, ln, sym, kind in flagged:
                        try:
                            text = (root / f).read_text(errors="replace").splitlines()[ln - 1].strip()[:140]
                        except (OSError, IndexError):
                            text = "?"
                        print(f"     {f}:{ln} {sym} [{kind}]  {text}", flush=True)
    Path("eval_results").mkdir(exist_ok=True)
    Path("eval_results/outside_repos.json").write_text(json.dumps(rows, indent=2))
    hit = [r for r in rows if r["flagged"]]
    print(f"\n{len(rows)} repos analysed, {len(hit)} with at least one flagged call site, "
          f"{sum(len(r['flagged']) for r in rows)} flagged sites in total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
