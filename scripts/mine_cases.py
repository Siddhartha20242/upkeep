"""Mine real downstream fix commits for known breaking upgrades (read-only GitHub access).

Keeps a commit only if: it changes ONLY .py files, the diff is small, the patch
touches the breaking symbol, the repo license is permissive, and the parent
commit's tree contains tests. Everything is fetched through `gh` (read-only).

Run:  .venv/bin/python -I scripts/mine_cases.py [lib ...]
"""

import json
import re
import subprocess
import sys

PERMISSIVE = {"mit", "bsd-2-clause", "bsd-3-clause", "apache-2.0", "isc", "unlicense", "0bsd"}

# lib -> (search queries, regex the patch must contain on a removed or added line)
LIBS = {
    "pyyaml": (
        ["yaml.load Loader PyYAML 6", "yaml.load() missing Loader argument", "replace yaml.load with yaml.safe_load"],
        r"yaml\.(safe_)?load|Loader\s*=",
    ),
    "jinja2": (
        ["ImportError cannot import name escape from jinja2", "jinja2 Markup escape moved to markupsafe",
         "jinja2 contextfilter pass_context"],
        r"jinja2|markupsafe|contextfilter|pass_context|escape|Markup",
    ),
    "werkzeug": (
        ["cannot import name url_quote from werkzeug.urls", "werkzeug.urls url_quote url_encode removed",
         "werkzeug 3 import error url_decode"],
        r"werkzeug|url_quote|url_encode|url_decode|urllib",
    ),
}


def gh(*args: str):
    p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except ValueError:
        return None


def search(query: str, limit: int = 60):
    return gh("search", "commits", query, "--limit", str(limit), "--json", "repository,sha") or []


def inspect(repo: str, sha: str, patch_rx: re.Pattern):
    c = gh("api", f"repos/{repo}/commits/{sha}")
    if not c or not c.get("parents"):
        return None
    files = c.get("files", [])
    if not files or any(not f["filename"].endswith(".py") for f in files):
        return None  # pin/downgrade fixes touch requirements.txt etc.
    if sum(f.get("changes", 0) for f in files) > 40:
        return None
    patch = "\n".join(f.get("patch", "") for f in files)
    if not patch_rx.search(patch):
        return None
    info = gh("api", f"repos/{repo}") or {}
    lic = ((info.get("license") or {}).get("spdx_id") or "").lower()
    if lic not in PERMISSIVE:
        return None
    parent = c["parents"][0]["sha"]
    tree = gh("api", f"repos/{repo}/git/trees/{parent}?recursive=1") or {}
    paths = [t["path"] for t in tree.get("tree", [])]
    tests = [p for p in paths if re.search(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$)", p)]
    if not tests or tree.get("truncated"):
        return None
    return {
        "repo": repo, "sha": sha, "parent": parent, "license": lic,
        "stars": info.get("stargazers_count"), "size_kb": info.get("size"),
        "files": [f["filename"] for f in files], "changes": sum(f.get("changes", 0) for f in files),
        "n_tests": len(tests), "msg": c["commit"]["message"].split("\n")[0][:80],
    }


def main() -> int:
    libs = sys.argv[1:] or list(LIBS)
    seen, found = set(), []
    for lib in libs:
        queries, rx = LIBS[lib]
        rx = re.compile(rx)
        for q in queries:
            for hit in search(q):
                key = (hit["repository"]["fullName"], hit["sha"])
                if key in seen:
                    continue
                seen.add(key)
                r = inspect(*key, rx)
                if r:
                    r["lib"] = lib
                    found.append(r)
                    print(f"[{lib}] {r['repo']} {r['sha'][:8]} {r['license']} "
                          f"{r['changes']}ch {r['n_tests']}tests {r['size_kb']}KB  {r['msg']}", flush=True)
    print(f"\n{len(seen)} commits examined, {len(found)} usable candidates")
    json.dump(found, open("candidates.json", "w"), indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
