from pathlib import Path

import pytest

from upkeep.radar import run_radar
from upkeep.sandbox import (
    SandboxRefused,
    docker_command,
    prepare_deps_command,
    run_tests,
    site_is_covered,
)
from upkeep.scan import CallSite


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def make_versions(tmp_path: Path):
    old, new = tmp_path / "old", tmp_path / "new"
    write(old, "lib/__init__.py", "def fetch(url, timeout=10):\n    return 'ok'\n")
    write(new, "lib/__init__.py", "def fetch(url, *, timeout):\n    return 'ok'\n")
    return old, new


def make_repo(tmp_path: Path, covered: bool) -> Path:
    repo = tmp_path / "repo"
    write(
        repo,
        "app.py",
        "import lib\n\n\ndef go():\n    return lib.fetch('u', 5)\n\n\ndef never_tested():\n    return lib.fetch('v', 6)\n",
    )
    body = "import app\n\n\ndef test_go():\n    assert app.go() == 'ok'\n"
    if not covered:
        body = "def test_nothing():\n    assert True\n"
    write(repo, "test_app.py", body)
    return repo


# ---- sandbox ---------------------------------------------------------------

def test_local_backend_refused_by_default(tmp_path):
    with pytest.raises(SandboxRefused):
        run_tests(tmp_path, backend="local")


def test_docker_command_is_locked_down(tmp_path):
    argv = docker_command(tmp_path / "w", ["pytest"], extra_paths=[tmp_path / "old"])
    joined = " ".join(argv)
    assert "--network none" in joined
    assert "--cap-drop ALL" in joined
    assert "no-new-privileges" in joined
    assert "--memory" in argv and "--pids-limit" in argv
    assert ":/extra/0:ro" in joined
    assert "PYTHONPATH=/extra/0" in joined
    assert argv[-1] == "pytest"
    # nothing from the host environment is forwarded
    assert argv.count("-e") == 2  # HOME and PYTHONPATH only


def test_prepare_deps_is_the_only_networked_step(tmp_path):
    argv = prepare_deps_command(tmp_path, ["pytest", "coverage"])
    assert "--network" not in argv  # default docker networking, unlike run_tests which uses "none"
    assert argv[-5:] == ["pip", "install", "--target", "/work", "pytest"] or argv[-6:] == [
        "pip", "install", "--target", "/work", "pytest", "coverage"
    ]
    assert "none" not in argv


def test_local_run_passes_on_old_fails_on_new_and_hides_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("UPKEEP_FAKE_SECRET", "s3cret")
    old, new = make_versions(tmp_path)
    repo = make_repo(tmp_path, covered=True)
    write(
        repo,
        "test_env.py",
        "import os\n\n\ndef test_no_secret():\n    assert os.environ.get('UPKEEP_FAKE_SECRET') is None\n",
    )
    before = run_tests(repo, backend="local", allow_unsandboxed=True, extra_paths=[old], with_coverage=False)
    after = run_tests(repo, backend="local", allow_unsandboxed=True, extra_paths=[new], with_coverage=False)
    assert before.ok, before.output
    assert not after.ok
    assert "TypeError" in after.output


def test_coverage_lines_reported(tmp_path):
    old, _ = make_versions(tmp_path)
    repo = make_repo(tmp_path, covered=True)
    res = run_tests(repo, backend="local", allow_unsandboxed=True, extra_paths=[old])
    assert res.ok, res.output
    assert res.executed_lines is not None
    assert 5 in res.executed_lines["app.py"]      # inside go(), executed
    assert 9 not in res.executed_lines["app.py"]  # never_tested(), not executed


def test_site_is_covered_semantics():
    site = CallSite("app.py", 5, 5, "lib.fetch")
    assert site_is_covered(site, None) is None
    assert site_is_covered(site, {"app.py": {5}}) is True
    assert site_is_covered(site, {"app.py": {1}}) is False
    assert site_is_covered(site, {"other.py": {5}}) is False


# ---- radar levels ----------------------------------------------------------

def test_radar_amber_when_all_affected_sites_covered(tmp_path):
    old, new = make_versions(tmp_path)
    repo = tmp_path / "repo"
    write(repo, "app.py", "import lib\n\n\ndef go():\n    return lib.fetch('u', 5)\n")
    write(repo, "test_app.py", "import app\n\n\ndef test_go():\n    assert app.go() == 'ok'\n")
    res = run_tests(repo, backend="local", allow_unsandboxed=True, extra_paths=[old])
    report = run_radar(repo, old, new, "lib", executed_lines=res.executed_lines)
    assert report.risk == "amber"
    assert report.uncovered == []


def test_radar_red_when_a_site_is_uncovered(tmp_path):
    old, new = make_versions(tmp_path)
    repo = make_repo(tmp_path, covered=True)
    res = run_tests(repo, backend="local", allow_unsandboxed=True, extra_paths=[old])
    report = run_radar(repo, old, new, "lib", executed_lines=res.executed_lines)
    assert report.risk == "red"
    assert [s.line for s in report.uncovered] == [9]


def test_radar_red_when_coverage_unknown_and_green_when_unaffected(tmp_path):
    old, new = make_versions(tmp_path)
    repo = make_repo(tmp_path, covered=True)
    assert run_radar(repo, old, new, "lib").risk == "red"  # unknown coverage is never "safe"
    other = tmp_path / "other"
    write(other, "x.py", "print('no lib here')\n")
    assert run_radar(other, old, new, "lib").risk == "green"
