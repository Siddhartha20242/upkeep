"""Retrospective validation on REAL developer fixes (read-only; nothing from those repos is executed).

For real public commits that fixed an upgrade break (PyYAML 6, Jinja2 3.1, Werkzeug 3, Flask 2.3), fetch each changed
.py file as it was BEFORE the fix, run Upkeep's radar on it, and classify every line the developer removed or changed
that mentions the library:

  flagged    the radar flagged this line
  compliant  the radar saw a changed symbol here but judged the call already fine (usually a refactor, not a fix)
  unseen     the radar saw nothing here: a blind spot (aliases, method calls, string-based use, ...)

Only line numbers and categories are saved (never other people's code). Commits come from keyword searches, so the
sample is biased toward well-described fixes; read the caveats in the README.

Run:  PYTHONPATH=. python scripts/retro_eval.py [--max-per-lib 60] [--libs pyyaml,jinja2,...]
Needs: `gh` logged in (read-only API use), network for PyPI. No model is called.
"""

import base64
import json
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

from upkeep.apidiff import diff_api, extract_api
from upkeep.pypi import sources_for
from upkeep.radar import site_affected
from upkeep.scan import find_call_sites

LIBS = {
    "pyyaml": dict(dist="PyYAML", module="yaml", old="5.4.1", new="6.0.2", queries=[
        "yaml.load Loader PyYAML 6", "yaml.load() missing Loader argument", "replace yaml.load with yaml.safe_load",
        "fix yaml.load deprecation warning Loader", "PyYAML 6.0 TypeError load() missing 1 required positional argument Loader",
        "yaml.load without Loader fix"]),
    "jinja2": dict(dist="Jinja2", module="jinja2", old="3.0.3", new="3.1.4", queries=[
        "ImportError cannot import name escape from jinja2", "jinja2 Markup escape moved to markupsafe",
        "jinja2 contextfilter pass_context", "jinja2 3.1 removed contextfunction", "from jinja2 import Markup fix"]),
    "werkzeug": dict(dist="Werkzeug", module="werkzeug", old="2.3.8", new="3.0.3", queries=[
        "cannot import name url_quote from werkzeug.urls", "werkzeug.urls url_quote url_encode removed",
        "werkzeug 3 import error url_decode", "werkzeug.security safe_str_cmp removed", "from werkzeug.urls import url_parse fix"]),
    "flask": dict(dist="Flask", module="flask", old="2.2.5", new="2.3.3", queries=[
        "flask.escape removed use markupsafe", "from flask import Markup fix", "flask json JSONEncoder removed 2.3",
        "ImportError cannot import name escape from flask", "flask.json_available removed"]),
}


def gh_json(*args, raw=False):
    p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=90)
    if p.returncode != 0:
        return None
    if raw:
        return p.stdout
    try:
        return json.loads(p.stdout)
    except ValueError:
        return None


def removed_lines(patch: str) -> set[int]:
    """Old-file line numbers the developer removed or changed."""
    out, old = set(), 0
    for line in patch.splitlines():
        m = re.match(r"@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@", line)
        if m:
            old = int(m.group(1))
        elif line.startswith("-") and not line.startswith("---"):
            out.add(old)
            old += 1
        elif line.startswith(" "):
            old += 1
    return out


def old_source(repo: str, path: str, ref: str) -> str | None:
    j = gh_json("api", f"repos/{repo}/contents/{path}?ref={ref}")
    if not j or j.get("encoding") != "base64":
        return None
    try:
        return base64.b64decode(j["content"]).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return None


def mentions(text_lines: list[str], line_no: int, module: str, names: set[str]) -> bool:
    if not (1 <= line_no <= len(text_lines)):
        return False
    t = text_lines[line_no - 1]
    return bool(re.search(rf"(?<![\w.]){re.escape(module)}\b", t)) or any(re.search(rf"(?<![\w]){re.escape(n)}(?![\w])", t) for n in names)


def split_of(sha: str) -> str:
    """dev = even hash (I may study its misses); heldout = odd hash (the honest number)."""
    return "dev" if int(sha[:8], 16) % 2 == 0 else "heldout"


def classify_file(src: str, removed: set[int], module: str, changes, old_api, new_api, names, show=None):
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "f.py").write_text(src)
        sites = find_call_sites(Path(d), module, {c.symbol for c in changes})
    seen, flagged = set(), set()
    for s in sites:
        rng = range(s.line, s.end_line + 1)
        seen.update(rng)
        for c in changes:
            if c.symbol == s.symbol and site_affected(c, s, old_api.get(c.symbol), new_api.get(c.symbol)):
                flagged.update(rng)
    lines = src.splitlines()
    res = Counter()
    for ln in removed:
        if not mentions(lines, ln, module, names):
            continue
        cat = "flagged" if ln in flagged else "compliant" if ln in seen else "unseen"
        res[cat] += 1
        if show is not None and cat != "flagged":
            show.append((cat, ln, lines[ln - 1].strip()[:150]))
    return res, len(flagged - removed)


def main() -> int:
    args = sys.argv[1:]
    cap = int(args[args.index("--max-per-lib") + 1]) if "--max-per-lib" in args else 60
    libs = args[args.index("--libs") + 1].split(",") if "--libs" in args else list(LIBS)
    show_unseen = "--show-unseen" in args  # prints dev-split misses for my analysis; never saved to files
    cache = Path("data") / "retro_commits.json"
    cached = {}
    if "--reuse" in args and cache.exists():
        for r in json.loads(cache.read_text()):
            cached.setdefault(r["lib"], []).append((r["repo"], r["sha"]))
    summary, records, mined = {}, [], []
    for lib in libs:
        cfg = LIBS[lib]
        old_src, new_src, module = sources_for(cfg["dist"], cfg["old"], cfg["new"], cfg["module"])
        old_api, new_api = extract_api(old_src, module), extract_api(new_src, module)
        changes = diff_api(old_api, new_api)
        names = {c.symbol.split(".")[-1] for c in changes if len(c.symbol.split(".")[-1]) >= 5}
        print(f"[{lib}] {len(changes)} API changes between {cfg['old']} and {cfg['new']}", flush=True)

        seen_commits, commits = set(), []
        if lib in cached:
            commits = cached[lib]
            seen_commits = set(commits)
        for q in ([] if lib in cached else cfg["queries"]):
            hits = gh_json("search", "commits", q, "--limit", "100", "--json", "repository,sha") or []
            time.sleep(2.5)  # search API: 30 requests per minute
            for h in hits:
                key = (h["repository"]["fullName"], h["sha"])
                if key not in seen_commits:
                    seen_commits.add(key)
                    commits.append(key)
        print(f"[{lib}] {len(commits)} candidate commits from {len(cfg['queries'])} searches", flush=True)

        tot, n_commits, hit_commits, extra_flags = Counter(), 0, 0, 0
        for repo, sha in commits:
            if n_commits >= cap:
                break
            c = gh_json("api", f"repos/{repo}/commits/{sha}")
            if not c or not c.get("parents"):
                continue
            pyfiles = [f for f in c.get("files", []) if f["filename"].endswith(".py") and f.get("patch")]
            if not pyfiles or sum(f["changes"] for f in pyfiles) > 120:
                continue
            parent = c["parents"][0]["sha"]
            commit_counts = Counter()
            shown = [] if (show_unseen and split_of(sha) == "dev") else None
            for f in pyfiles[:4]:
                src = old_source(repo, f["filename"], parent)
                if src is None or len(src) > 400_000:
                    continue
                res, extra = classify_file(src, removed_lines(f["patch"]), module, changes, old_api, new_api, names, shown)
                for cat, ln, text in (shown or [])[-20:]:
                    print(f"   [{lib} {cat}] {repo}:{f['filename']}:{ln}: {text}", flush=True)
                if shown is not None:
                    shown.clear()
                commit_counts += res
                extra_flags += extra
            if not sum(commit_counts.values()):
                continue  # the commit never touched a line that mentions this library
            n_commits += 1
            hit_commits += commit_counts["flagged"] > 0
            tot += commit_counts
            mined.append({"lib": lib, "repo": repo, "sha": sha})
            records.append({"lib": lib, "repo": repo, "sha": sha, "split": split_of(sha), **commit_counts})
            if n_commits % 10 == 0:
                print(f"[{lib}] {n_commits} commits analysed ...", flush=True)
        summary[lib] = {"commits": n_commits, "commits_with_a_flagged_line": hit_commits, "lines": dict(tot),
                        "flagged_lines_the_developer_did_not_change": extra_flags}
        print(f"[{lib}] {summary[lib]}", flush=True)

    out = Path("eval_results")
    out.mkdir(exist_ok=True)
    (out / "retro_validation.json").write_text(json.dumps({"summary": summary, "commits": records}, indent=2))
    Path("data").mkdir(exist_ok=True)
    (Path("data") / "retro_commits.json").write_text(json.dumps(mined, indent=2))
    print("\n=== SUMMARY BY SPLIT (heldout is the number to quote) ===")
    for sp in ("dev", "heldout"):
        c, n_c, hit = Counter(), 0, 0
        for r in records:
            if r["split"] == sp:
                n_c += 1; hit += r.get("flagged", 0) > 0
                c += Counter({k: r.get(k, 0) for k in ("flagged", "compliant", "unseen")})
        n = sum(c.values()) or 1
        print(f"{sp:8} commits={n_c:3} with-a-flagged-line={hit:3}  lines: " + ", ".join(f"{k}={c[k]} ({100 * c[k] / n:.0f}%)" for k in ("flagged", "compliant", "unseen")))
    print("\n=== SUMMARY BY LIBRARY ===")
    allc = Counter()
    for lib, s in summary.items():
        allc += Counter(s["lines"])
        n = sum(s["lines"].values()) or 1
        print(f"{lib:9} commits={s['commits']:3}  hit-commits={s['commits_with_a_flagged_line']:3}  lines: " +
              ", ".join(f"{k}={v} ({100 * v / n:.0f}%)" for k, v in sorted(s["lines"].items())))
    n = sum(allc.values()) or 1
    print(f"{'ALL':9} lines: " + ", ".join(f"{k}={v} ({100 * v / n:.0f}%)" for k, v in sorted(allc.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
