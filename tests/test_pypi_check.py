import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from upkeep import cli, pypi


def sdist_bytes(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def fake_pypi(monkeypatch, tmp_path):
    archives = {
        "1.0": sdist_bytes({"pkg-1.0/src/mylib/__init__.py": "def fetch(url, timeout=10):\n    pass\n"}),
        "2.0": sdist_bytes({"pkg-2.0/src/mylib/__init__.py": "def fetch(url, *, timeout):\n    pass\n"}),
    }
    def fake_read(url, timeout=45, tries=4):
        if url.endswith("/json"):
            v = url.split("/")[-2]
            return json.dumps({"urls": [{"packagetype": "sdist", "url": f"https://files.pythonhosted.org/mylib-{v}.tar.gz",
                                         "digests": {"sha256": hashlib.sha256(archives[v]).hexdigest()}}]}).encode()
        return archives[url.split("mylib-")[1].replace(".tar.gz", "")]
    monkeypatch.setattr(pypi, "_read", fake_read)
    monkeypatch.setattr(pypi, "DEFAULT_CACHE", tmp_path / "cache")
    monkeypatch.setattr(pypi, "sources_for", pypi.sources_for)
    return tmp_path


def make_repo(tmp_path: Path, body: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(body)
    return repo


def test_check_finds_the_break_with_one_command(fake_pypi, capsys, monkeypatch):
    monkeypatch.setattr(cli, "sources_for", lambda *a, **k: pypi.sources_for(*a, cache=fake_pypi / "cache", **k))
    repo = make_repo(fake_pypi, "import mylib\n\n\ndef go():\n    return mylib.fetch('u', 5)\n")
    code = cli.main(["check", "--repo", str(repo), "--package", "MyLib", "--from", "1.0", "--to", "2.0", "--module", "mylib"])
    out = capsys.readouterr().out
    assert code == 2 and "RED" in out and "mylib.fetch" in out
    assert "app.py:5" in out and "param_kind_changed" in out and "1 breaking change(s) touch this repo; 1 do not" in out


def test_check_green_when_the_repo_does_not_use_the_changed_function(fake_pypi, capsys, monkeypatch):
    monkeypatch.setattr(cli, "sources_for", lambda *a, **k: pypi.sources_for(*a, cache=fake_pypi / "cache", **k))
    repo = make_repo(fake_pypi, "print('no library here')\n")
    assert cli.main(["check", "--repo", str(repo), "--package", "mylib", "--from", "1.0", "--to", "2.0"]) == 0
    assert "GREEN" in capsys.readouterr().out


def test_unsafe_archive_paths_are_refused(monkeypatch, tmp_path):
    evil = sdist_bytes({"../evil.py": "x=1"})
    # tarfile's data filter must block traversal when extracting
    monkeypatch.setattr(pypi, "_read", lambda url, **k: json.dumps({"urls": [{"packagetype": "sdist", "url": "https://x/a.tar.gz"}]}).encode()
                        if url.endswith("/json") else evil)
    with pytest.raises(Exception):
        pypi.fetch_sdist("evil", "1", tmp_path / "c")
    assert not (tmp_path / "evil.py").exists() and not (tmp_path / "c" / "evil.py").exists()


def test_helpful_error_when_the_module_name_is_wrong(fake_pypi, capsys, monkeypatch):
    monkeypatch.setattr(cli, "sources_for", lambda *a, **k: pypi.sources_for(*a, cache=fake_pypi / "cache", **k))
    repo = make_repo(fake_pypi, "x = 1\n")
    code = cli.main(["check", "--repo", str(repo), "--package", "mylib", "--from", "1.0", "--to", "2.0", "--module", "nothere"])
    assert code == 3 and "pass --module" in capsys.readouterr().err


def test_tampered_download_is_rejected_and_nothing_extracted(fake_pypi, monkeypatch):
    real = pypi._read

    def evil(url, timeout=45, tries=4):
        data = real(url, timeout, tries)
        return data if url.endswith("/json") else data + b"x"
    monkeypatch.setattr(pypi, "_read", evil)
    with pytest.raises(pypi.PyPIError, match="SHA-256 mismatch"):
        pypi.fetch_sdist("MyLib", "1.0", cache=fake_pypi / "cache")
    assert not (fake_pypi / "cache" / "MyLib-1.0").exists()


def test_archive_without_a_published_digest_is_refused(fake_pypi, monkeypatch):
    real = pypi._read

    def nodigest(url, timeout=45, tries=4):
        data = real(url, timeout, tries)
        if url.endswith("/json"):
            j = json.loads(data)
            j["urls"][0].pop("digests")
            return json.dumps(j).encode()
        return data
    monkeypatch.setattr(pypi, "_read", nodigest)
    with pytest.raises(pypi.PyPIError, match="no SHA-256"):
        pypi.fetch_sdist("MyLib", "1.0", cache=fake_pypi / "cache")


def test_good_download_records_its_digest(fake_pypi):
    root = pypi.fetch_sdist("MyLib", "1.0", cache=fake_pypi / "cache")
    assert len(pypi.release_digest(root)) == 64
