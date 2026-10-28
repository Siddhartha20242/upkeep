import subprocess

from upkeep import reallib, sandbox


def test_wheels_only_adds_the_flag_and_default_does_not(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(sandbox.subprocess, "run", lambda argv, **kw: (seen.append(argv), subprocess.CompletedProcess(argv, 0, "", ""))[1])
    sandbox.prepare_deps(tmp_path, tmp_path / "d1", ["x==1"], wheels_only=True)
    sandbox.prepare_deps(tmp_path, tmp_path / "d2", ["x==1"])
    assert "--only-binary=:all:" in seen[0] and "--only-binary=:all:" not in seen[1]
    assert "--network" not in seen[0]  # this is the one networked step; running tests later uses network none


def test_source_build_fallback_is_used_only_when_no_wheel_exists_and_is_reported(monkeypatch, tmp_path, capsys):
    calls = []

    def fake_prepare(repo, deps, packages, **kw):
        calls.append(kw.get("wheels_only"))
        return sandbox.TestResult(kw.get("wheels_only") is False, 0 if kw.get("wheels_only") is False else 1, "no wheel", "docker")

    monkeypatch.setattr(reallib, "prepare_deps", fake_prepare)
    reallib.BUILT_FROM_SOURCE.clear()
    d = reallib._deps(["oldpkg==1.0"], tmp_path)
    assert calls == [True, False] and reallib.BUILT_FROM_SOURCE == [("oldpkg==1.0",)]
    assert "building from source" in capsys.readouterr().out and d.name.startswith("deps-cov-")
