import io
import json
import tarfile
from pathlib import Path

import pytest

from upkeep import cli, pypi, reallib
from upkeep.radar import RadarInputError, run_radar


def lib(root: Path, text="def f(a):\n    pass\n"):
    (root / "lib").mkdir(parents=True)
    (root / "lib" / "__init__.py").write_text(text)


def test_radar_refuses_to_report_when_library_source_is_missing_or_hollow(tmp_path):
    good, hollow, repo = tmp_path / "good", tmp_path / "hollow", tmp_path / "repo"
    lib(good)
    (hollow / "lib" / "sub").mkdir(parents=True)          # folders only, no .py files: the corrupted-cache shape
    repo.mkdir(); (repo / "app.py").write_text("import lib\nlib.f(1)\n")
    for old, new, label in ((hollow, good, "old"), (good, hollow, "new"), (tmp_path / "nope", good, "old")):
        with pytest.raises(RadarInputError, match=label):
            run_radar(repo, old, new, "lib")


def test_a_library_that_deliberately_defines_nothing_is_still_a_valid_input(tmp_path):
    old, new, repo = tmp_path / "old", tmp_path / "new", tmp_path / "repo"
    lib(old); lib(new, "")                                  # new version removed everything: a real, legitimate case
    repo.mkdir(); (repo / "app.py").write_text("import lib\nlib.f(1)\n")
    assert run_radar(repo, old, new, "lib").risk == "red"


def test_cli_exits_3_with_a_clear_message_instead_of_green(tmp_path, capsys):
    good, hollow, repo = tmp_path / "good", tmp_path / "hollow", tmp_path / "repo"
    lib(good); (hollow / "lib").mkdir(parents=True)
    repo.mkdir(); (repo / "app.py").write_text("import lib\nlib.f(1)\n")
    code = cli.main(["radar", "--repo", str(repo), "--old", str(hollow), "--new", str(good), "--package", "lib"])
    assert code == 3 and "refusing to report a result from missing data" in capsys.readouterr().err


def sdist(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, text in files.items():
            data = text.encode(); info = tarfile.TarInfo(name); info.size = len(data); t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.mark.parametrize("module,attr", [(pypi, "_read"), (reallib, "_urlopen_read")])
def test_a_cache_of_empty_folders_is_redownloaded_not_trusted(monkeypatch, tmp_path, module, attr):
    blob = sdist({"pkg-1.0/src/mylib/__init__.py": "def f():\n    pass\n"})
    downloads = []

    def fake(url, **kw):
        downloads.append(url)
        if url.endswith("/json"):
            return json.dumps({"urls": [{"packagetype": "sdist", "url": "https://files.example/p-1.0.tar.gz"}]}).encode()
        return blob

    monkeypatch.setattr(module, attr, fake)
    cache = tmp_path / "cache"
    (cache / "PkgX-1.0" / "pkg-1.0" / "src" / "mylib" / "sub").mkdir(parents=True)   # corrupted: dirs only
    fetch = pypi.fetch_sdist if module is pypi else reallib._download_sdist
    root = fetch("PkgX", "1.0", cache)
    assert downloads, "the hollow cache must trigger a fresh download"
    assert any(root.rglob("*.py"))
    downloads.clear()
    fetch("PkgX", "1.0", cache)
    assert not downloads, "a healthy cache is reused"
