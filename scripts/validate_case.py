"""Validate a mined case: FAIL -> PASS across the real fix commit, with the NEW library installed.

For a candidate (repo, fix commit) this
  1. clones the repo and exports the parent commit and the fix commit (git only; nothing executed),
  2. installs the repo's requirements plus the NEW pinned library into a deps dir
     inside a container WITH network (the only networked step; the old pin is filtered out),
  3. runs the tests at the parent and at the fix commit in a container with NO network,
  4. calls the case valid if at least one test fails at the parent and passes at the fix.

Run:  .venv/bin/python -I scripts/validate_case.py WORKDIR CANDIDATES_JSON OWNER/REPO "PyYAML==6.0.2"
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from upkeep.sandbox import prepare_deps, run_tests

REQ_FILES = ["requirements.txt", "requirements-dev.txt", "requirements_dev.txt", "test-requirements.txt", "dev-requirements.txt"]
PYTEST_ARGS = ["-q", "-p", "no:cacheprovider", "-rfE", "--tb=no", "-x" if False else "--maxfail=200"]


def sh(*args, cwd=None, timeout=300) -> str:
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])} failed: {p.stderr[-300:]}")
    return p.stdout


def export(repo_dir: Path, sha: str, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    tar = subprocess.run(["git", "-C", str(repo_dir), "archive", "--format=tar", sha], capture_output=True, timeout=120)
    if tar.returncode != 0:
        raise RuntimeError(f"git archive failed: {tar.stderr[-200:]!r}")
    subprocess.run(["tar", "-x", "-C", str(dest)], input=tar.stdout, check=True, timeout=120)


def failures(output: str) -> set[str]:
    return {m.group(2).split(" - ")[0].strip() for m in re.finditer(r"^(FAILED|ERROR) (.+)$", output, re.M)}


def summary_line(output: str) -> str:
    lines = [l for l in output.strip().splitlines() if re.search(r"\d+ (passed|failed|error)", l)]
    return lines[-1] if lines else "(no pytest summary)"


def main() -> int:
    work, cands, name, pin = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
    image = sys.argv[5] if len(sys.argv) > 5 else "python:3.12-slim"
    pip_args = sys.argv[6].split() if len(sys.argv) > 6 and sys.argv[6] else []
    print(f"[{name}] python image: {image}  pip args: {pip_args or '-'}", flush=True)
    lib = re.split(r"[=<>!~]", pin)[0].lower().replace("_", "-")
    case = next(c for c in json.load(open(cands)) if c["repo"] == name)
    slug = name.replace("/", "__")
    base = work / slug
    base.mkdir(parents=True, exist_ok=True)

    clone = base / "clone"
    if not clone.exists():
        sh("git", "clone", "--quiet", f"https://github.com/{name}.git", str(clone), timeout=300)
    parent, fix = case["parent"], case["sha"]
    for label, sha in (("parent", parent), ("fix", fix)):
        d = base / label
        if not d.exists():
            export(clone, sha, d)

    # requirements: drop any line pinning the library under test; we force the new pin separately
    src = base / "depsrc"
    if src.exists():
        shutil.rmtree(src)
    shutil.copytree(base / "fix", src, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    req_name = None
    for rf in REQ_FILES:
        f = src / rf
        if f.exists():
            kept = [l for l in f.read_text().splitlines() if re.split(r"[=<>!~\[ ;#]", l.strip().lower().replace("_", "-"))[0] != lib]
            f.write_text("\n".join(kept) + "\n")
            req_name = rf
            break
    print(f"[{name}] requirements file: {req_name or 'none found'}", flush=True)

    deps = base / ("deps-" + re.sub(r"[^a-zA-Z0-9]+", "_", image))  # one deps dir per Python image
    if not deps.exists():
        res = prepare_deps(src, deps, ["pytest", pin], requirements_file=req_name, image=image, pip_args=pip_args)
        print(f"[{name}] deps install: {'ok' if res.ok else 'FAILED'}", flush=True)
        if not res.ok:
            print(res.output[-1200:])
            shutil.rmtree(deps, ignore_errors=True)
            print(json.dumps({"repo": name, "valid": False, "reason": "dependency install failed"}))
            return 1

    results = {}
    for label in ("parent", "fix"):
        r = run_tests(base / label, backend="docker", image=image, deps_dir=deps, pytest_args=PYTEST_ARGS,
                      with_coverage=False, timeout=600, memory="2g")
        results[label] = r
        print(f"[{name}] {label}: {summary_line(r.output)}", flush=True)

    pf, ff = failures(results["parent"].output), failures(results["fix"].output)
    fail_to_pass = sorted(pf - ff)
    valid = bool(fail_to_pass)
    out = {
        "repo": name, "fix": fix, "parent": parent, "pin": pin, "valid": valid,
        "fail_to_pass": fail_to_pass[:10], "n_fail_to_pass": len(fail_to_pass),
        "parent_failures": len(pf), "fix_failures": len(ff),
        "still_failing_after_fix": sorted(ff)[:5],
        "reason": "" if valid else "no test fails before the fix and passes after it",
    }
    if not valid and not results["parent"].output.strip():
        out["reason"] += " (empty output; container may have crashed)"
    print(json.dumps(out, indent=2))
    if not valid:
        # Show WHY the suite is red, so an unrelated failure is visible instead of guessed at.
        probe = run_tests(base / "parent", backend="docker", image=image, deps_dir=deps,
                          pytest_args=["-q", "-p", "no:cacheprovider", "--tb=line", "--maxfail=3"],
                          with_coverage=False, timeout=300, memory="2g")
        print("--- first errors at the parent commit ---")
        print("\n".join(probe.output.strip().splitlines()[-14:]))
    return 0 if valid else 1


if __name__ == "__main__":
    sys.exit(main())
