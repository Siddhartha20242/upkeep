"""Prove every authored case is sound before any model is judged on it."""
import shutil
import tempfile
from pathlib import Path

import pytest

from upkeep.evalcases import CASES, _w, materialize
from upkeep.evalcases_heldout import CASES as HELDOUT
from upkeep.radar import run_radar
from upkeep.sandbox import run_tests


def run(repo: Path, lib_root: Path, extra: dict[str, str] | None = None):
    work = Path(tempfile.mkdtemp()) / "repo"
    shutil.copytree(repo, work)
    if extra:
        _w(work, extra)
    r = run_tests(work, backend="local", allow_unsandboxed=True, extra_paths=[lib_root], with_coverage=False)
    shutil.rmtree(work.parent, ignore_errors=True)
    return r


@pytest.mark.parametrize("case", CASES + HELDOUT, ids=lambda c: c.name)
def test_case_is_sound(case, tmp_path):
    old, new, repo = materialize(case, tmp_path)
    assert run(repo, old, case.hidden).ok, "must pass with the OLD library (visible + hidden)"
    assert not run(repo, new).ok, "visible tests must fail with the NEW library before any fix"

    report = run_radar(repo, old, new, "lib")
    if case.expect == "undetectable":
        assert not report.affected
    else:
        assert report.affected, "the static radar must detect this break"

    if case.expect == "fix":
        fixed = Path(tempfile.mkdtemp()) / "repo"
        shutil.copytree(repo, fixed)
        _w(fixed, case.solution)
        assert run(fixed, new, case.hidden).ok, "reference solution must pass visible + hidden tests"
        shutil.rmtree(fixed.parent, ignore_errors=True)
