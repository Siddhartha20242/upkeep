"""`upkeep audit` on many public repos I did not write (read-only; nothing from them is executed).

Selection (no cherry-picking): GitHub code search for requirements files that pin an old version of one of NINE popular
libraries, repos taken in the order GitHub returns them, forks and repos over 40 MB skipped, every processed repo reported.
Repos are split by a hash of their name: `dev` (I may read the flags and fix false alarms) and `heldout` (the number to quote).
Only counts and file:line references are saved, never other people's code.

Run:  .venv/bin/python -I scripts/outside_audit.py [--per-pin 6] [--show] [--show-heldout] [--set-b]       Needs: `gh` logged in, network. No model.
"""
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from scripts.outside_repos import fetch, gh  # noqa: E402  (same safe download/extract helpers)
from upkeep.audit import audit

PINS = ["Flask==1.1.2", "click==7.1.2", "SQLAlchemy==1.3.24", "requests==2.25.1", "marshmallow==3.13.0",
        "PyYAML==5.4.1", "Jinja2==3.0.3", "Werkzeug==2.0.3", "pydantic==1.10.2"]
PINS_B = ["Flask==2.0.1", "click==8.0.1", "SQLAlchemy==1.4.46", "requests==2.28.1", "marshmallow==3.18.0",
          "PyYAML==5.3.1", "Jinja2==2.11.3", "Werkzeug==1.0.1", "pydantic==1.9.0"]  # a second, disjoint set of pins
ONLY = {"flask", "click", "sqlalchemy", "requests", "marshmallow", "pyyaml", "jinja2", "werkzeug", "pydantic"}


def split_of(repo: str) -> str:
    return "dev" if int(hashlib.sha256(repo.encode()).hexdigest()[:8], 16) % 2 == 0 else "heldout"


def candidates(per_pin: int, pins: list[str], exclude: set[str]) -> list[str]:
    seen: list[str] = []
    for pin in pins:
        out = gh("search", "code", pin, "--filename", "requirements.txt", "--limit", "40", "--json", "repository")
        time.sleep(7)
        n = 0
        for r in json.loads(out or "[]"):
            repo = r["repository"]
            name = repo["nameWithOwner"]
            if repo.get("isFork") or repo.get("isPrivate") or name in seen or name in exclude:
                continue
            seen.append(name)
            n += 1
            if n >= per_pin:
                break
    return seen


def main() -> int:
    per_pin = int(sys.argv[sys.argv.index("--per-pin") + 1]) if "--per-pin" in sys.argv else 6
    second = "--set-b" in sys.argv
    out_file = Path("eval_results/outside_audit_B.json" if second else "eval_results/outside_audit.json")
    exclude = {r["repo"] for r in json.loads(Path("eval_results/outside_audit.json").read_text())} if second else set()
    rows = []
    for repo in candidates(per_pin, PINS_B if second else PINS, exclude):
        with tempfile.TemporaryDirectory() as d:
            root = fetch(repo, Path(d))
            if root is None:
                print(f"{repo}: skipped (too big or unreadable)", flush=True)
                continue
            for r in audit(root, only=ONLY):
                row = {"repo": repo, "split": "fresh" if second else split_of(repo), **r.to_dict()}
                rows.append(row)
                if r.status == "checked":
                    print(f"{repo} [{row['split']}] {r.dist} {r.old}->{r.new}: {r.risk}, {len(r.flagged)} flagged", flush=True)
                    if "--show" in sys.argv and (row["split"] != "heldout" or "--show-heldout" in sys.argv):  # terminal only, never saved
                        for f, ln, sym, kind in r.flagged:
                            try:
                                text = (root / f).read_text(errors="replace").splitlines()[ln - 1].strip()[:130]
                            except (OSError, IndexError):
                                text = "?"
                            print(f"      {f}:{ln} {sym} [{kind}]  {text}", flush=True)
    Path("eval_results").mkdir(exist_ok=True)
    out_file.write_text(json.dumps(rows, indent=2))
    checked = [r for r in rows if r["status"] == "checked"]
    print(f"\n{len({r['repo'] for r in rows})} repos, {len(checked)} (repo, library) pairs analysed, "
          f"{sum(1 for r in checked if r['flagged'])} with flagged lines, "
          f"{sum(1 for r in rows if r['status'] == 'skipped')} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
