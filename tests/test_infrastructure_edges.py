"""Failure paths in the sandbox runner and the PyPI client: each must fail loudly and honestly, never look like success."""

import subprocess
from pathlib import Path

import pytest

from upkeep import pypi, sandbox
from upkeep.sandbox import run_python, run_tests


def make_repo(tmp_path):
    (tmp_path / "test_x.py").write_text("def test_a():\n    assert True\n")
    return tmp_path


def test_docker_missing_or_timing_out_is_reported_as_failure_not_success(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)

    def boom(*a, **k):
        raise FileNotFoundError("docker")
    monkeypatch.setattr(subprocess, "run", boom)
    r = run_tests(repo, backend="docker")
    assert not r.ok and "docker not installed" in r.output

    def slow(*a, **k):
        raise subprocess.TimeoutExpired("docker", 1)
    monkeypatch.setattr(subprocess, "run", slow)
    r = run_tests(repo, backend="docker", timeout=1)
    assert not r.ok and "timed out" in r.output
    ok, out = run_python(repo, "print(1)", backend="docker", timeout=1)
    assert not ok


def test_unknown_backend_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="unknown backend"):
        run_tests(make_repo(tmp_path), backend="vm")


def test_local_backend_passes_and_fails_honestly_and_runs_on_a_copy(tmp_path):
    repo = make_repo(tmp_path)
    assert run_tests(repo, backend="local", allow_unsandboxed=True, with_coverage=False).ok
    (repo / "test_x.py").write_text("def test_a():\n    assert False\n")
    r = run_tests(repo, backend="local", allow_unsandboxed=True, with_coverage=False)
    assert not r.ok and r.returncode != 0
    (repo / "test_x.py").write_text("from pathlib import Path\n\n\ndef test_a():\n    Path('scratch.txt').write_text('x')\n")
    run_tests(repo, backend="local", allow_unsandboxed=True, with_coverage=False)
    assert not (repo / "scratch.txt").exists()   # tests ran in a copy: the repo itself is never modified


def test_output_is_truncated(tmp_path):
    (tmp_path / "test_x.py").write_text("def test_a():\n    print('x' * 200000)\n    assert False\n")
    r = run_tests(tmp_path, backend="local", allow_unsandboxed=True, with_coverage=False)
    assert len(r.output) <= sandbox.MAX_OUTPUT + 50


def test_corrupt_coverage_file_means_unknown_coverage_not_full_coverage(tmp_path):
    (tmp_path / "coverage.json").write_text("{not json")
    assert sandbox._read_coverage(tmp_path) is None
    assert sandbox._read_coverage(tmp_path / "missing") is None


def test_download_retries_then_gives_up_with_a_clear_error(monkeypatch):
    calls = []

    def fail(url, timeout=0):
        calls.append(url)
        raise OSError("connection reset")
    monkeypatch.setattr(pypi.urllib.request, "urlopen", fail)
    monkeypatch.setattr(pypi.time, "sleep", lambda s: None)
    with pytest.raises(pypi.PyPIError, match="download failed after 3 tries"):
        pypi._read("https://pypi.org/x", tries=3)
    assert len(calls) == 3


def test_unreadable_pypi_metadata_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(pypi, "_read", lambda url, **k: b"<html>not json</html>")
    with pytest.raises(pypi.PyPIError, match="could not read"):
        pypi.fetch_sdist("x", "1", cache=tmp_path)
    with pytest.raises(pypi.PyPIError, match="could not read the latest"):
        pypi.latest_version("x")


def test_release_without_a_source_archive_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(pypi, "_read", lambda url, **k: b'{"urls": [{"packagetype": "bdist_wheel", "url": "https://f/x.whl"}]}')
    with pytest.raises(pypi.PyPIError, match="no source archive"):
        pypi.fetch_sdist("x", "1", cache=tmp_path)


def test_locate_package_handles_src_layout_and_ignores_test_folders(tmp_path):
    (tmp_path / "src" / "lib").mkdir(parents=True)
    (tmp_path / "src" / "lib" / "__init__.py").write_text("")
    (tmp_path / "tests" / "lib").mkdir(parents=True)
    (tmp_path / "tests" / "lib" / "__init__.py").write_text("")
    assert pypi.locate_package(tmp_path, "lib") == tmp_path / "src"
    assert pypi.locate_package(tmp_path, "nothing") is None
    assert pypi.guess_module("My-Lib") == "my_lib"


def test_missing_package_folder_gives_an_actionable_message(monkeypatch, tmp_path):
    (tmp_path / "a").mkdir()
    monkeypatch.setattr(pypi, "fetch_sdist", lambda *a, **k: tmp_path / "a")
    with pytest.raises(pypi.PyPIError, match="pass --module"):
        pypi.sources_for("PyYAML", "1", "2")


def _http_error(code):
    import io
    return pypi.urllib.error.HTTPError("https://pypi.org/x", code, "msg", {}, io.BytesIO(b""))


def test_a_404_is_reported_at_once_and_a_500_is_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(pypi.time, "sleep", lambda s: None)

    def always(code):
        def f(url, timeout=0):
            calls.append(code)
            raise _http_error(code)
        return f
    monkeypatch.setattr(pypi.urllib.request, "urlopen", always(404))
    with pytest.raises(pypi.PyPIError, match="not found on PyPI"):
        pypi._read("https://pypi.org/x", tries=4)
    assert calls == [404]
    calls.clear()
    monkeypatch.setattr(pypi.urllib.request, "urlopen", always(503))
    with pytest.raises(pypi.PyPIError, match="download failed after 4 tries"):
        pypi._read("https://pypi.org/x", tries=4)
    assert calls == [503] * 4


def test_unusable_archives_become_pypi_errors_not_crashes(monkeypatch, tmp_path):
    import hashlib, io, json, tarfile
    link = tarfile.TarInfo("pkg-1.0/l")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        t.addfile(link)
    for blob in (buf.getvalue(), b"\x1f\x8b not really gzip"):
        meta = json.dumps({"urls": [{"packagetype": "sdist", "url": "https://f/x.tar.gz", "size": len(blob),
                                     "digests": {"sha256": hashlib.sha256(blob).hexdigest()}}]}).encode()
        monkeypatch.setattr(pypi, "_read", lambda url, b=blob, m=meta, **k: m if url.endswith("/json") else b)
        with pytest.raises(pypi.PyPIError, match="could not unpack"):
            pypi.fetch_sdist("x", "1", cache=tmp_path / "c")
        assert not list((tmp_path / "c").glob(".tmp-*"))     # nothing half-extracted is left behind


def test_two_fetches_never_share_a_temp_folder(monkeypatch, tmp_path):
    import hashlib, io, json, tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        i = tarfile.TarInfo("pkg-1.0/lib/__init__.py")
        i.size = 2
        t.addfile(i, io.BytesIO(b"x\n"))
    blob = buf.getvalue()
    meta = json.dumps({"urls": [{"packagetype": "sdist", "url": "https://f/x.tar.gz", "size": len(blob),
                                 "digests": {"sha256": hashlib.sha256(blob).hexdigest()}}]}).encode()
    monkeypatch.setattr(pypi, "_read", lambda url, **k: meta if url.endswith("/json") else blob)
    a = pypi.fetch_sdist("x", "1", cache=tmp_path / "c")
    import shutil
    shutil.rmtree(a)
    b = pypi.fetch_sdist("x", "1", cache=tmp_path / "c")
    assert a == b and any(b.rglob("*.py"))
