"""Prepare the live demo: real PyYAML and Werkzeug releases, authored apps, everything under ./demo.

Needs Docker running and network access (PyPI downloads, dependency install inside containers). Prints the
exact commands to run on camera. Nothing here calls a model; the commands it prints do.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

from upkeep.evalcases import _w
from upkeep.reallib import BY_NAME, prepare

CACHE = Path.home() / ".cache" / "upkeep" / "reallib"  # not /tmp: macOS clears old files there
DEMO = Path("demo")
CASES = {"pyyaml_load": "yaml", "werkzeug_urls": "werkzeug"}  # case -> import package


def link(dst: Path, src: Path) -> None:
    if dst.is_symlink() or dst.exists():
        dst.unlink() if dst.is_symlink() else shutil.rmtree(dst)
    dst.symlink_to(Path(src).resolve())


def main() -> int:
    for name, pkg in CASES.items():
        case = BY_NAME[name]
        print(f"preparing {name} ({case.dist} {case.old_ver} -> {case.new_ver}) ...", flush=True)
        prep = prepare(case, CACHE)
        d = DEMO / name
        if (d / "repo").exists():
            shutil.rmtree(d / "repo")
        _w(d / "repo", case.repo)  # the visible tests only; hidden tests stay out of the demo repo
        link(d / "old", prep.old_src); link(d / "new", prep.new_src)
        link(d / "deps_old", prep.old_deps); link(d / "deps_new", prep.new_deps)
        (d / "CHANGELOG.txt").write_text(prep.changelog or "")
        print(f"   ok -> {d}  (release notes: {'yes' if prep.changelog else 'none found'})")

    print("\n=== commands to run on camera (from the project root) ===\n")
    for name, pkg in CASES.items():
        case, d = BY_NAME[name], DEMO / name
        print(f"# {name}: {case.dist} {case.old_ver} -> {case.new_ver}")
        print(f"upkeep radar --repo {d}/repo --old {d}/old --new {d}/new --package {pkg}")
        flags = f"--behavior-check --old-deps {d}/deps_old"
        print(f"upkeep fix --repo {d}/repo --old {d}/old --new {d}/new --package {pkg} --changelog {d}/CHANGELOG.txt \\\n"
              f"  --test-backend docker --image python:3.9-slim --deps {d}/deps_new {flags} \\\n"
              f"  --old-version {case.old_ver} --new-version {case.new_ver} --out demo/out/{name}\n")
    print("upkeep dashboard --run demo/out/werkzeug_urls --evals eval_results --out demo/dashboard.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
