import hashlib
import json

import pytest

from tests.test_pypi_check import sdist_bytes
from upkeep import audit as au, cli, pypi


@pytest.fixture
def fake(monkeypatch, tmp_path):
    arch = {("mylib", "1.0"): sdist_bytes({"p-1.0/src/mylib/__init__.py": "def fetch(url, timeout=10):\n    pass\n"}),
            ("mylib", "2.0"): sdist_bytes({"p-2.0/src/mylib/__init__.py": "def fetch(url, *, timeout):\n    pass\n"}),
            ("huge", "1.0"): b"x", ("huge", "2.0"): b"x"}

    def read(url, timeout=45, tries=4):
        if url.endswith("/json"):
            parts = url.split("/")
            if len(parts) == 6:  # /pypi/<dist>/json
                return json.dumps({"info": {"version": {"mylib": "2.0", "huge": "2.0", "current": "1.0"}[parts[4]]}}).encode()
            dist, v = parts[4], parts[5]
            data = arch[(dist, v)]
            return json.dumps({"urls": [{"packagetype": "sdist", "url": f"https://f/{dist}-{v}.tar.gz", "size": 99_000_000 if dist == "huge" else len(data),
                                         "digests": {"sha256": hashlib.sha256(data).hexdigest()}}]}).encode()
        dist, v = url.split("/")[-1][:-7].rsplit("-", 1)
        return arch[(dist, v)]
    monkeypatch.setattr(pypi, "_read", read)
    monkeypatch.setattr(pypi, "DEFAULT_CACHE", tmp_path / "cache")
    monkeypatch.setattr(au, "sources_for", lambda *a, **k: pypi.sources_for(*a, cache=tmp_path / "cache", **k))
    return tmp_path


def repo_with(tmp_path, reqs, code):
    r = tmp_path / "repo"
    r.mkdir()
    (r / "requirements.txt").write_text(reqs)
    (r / "app.py").write_text(code)
    return r


def test_parse_pins_ignores_ranges_comments_and_options(tmp_path):
    r = repo_with(tmp_path, "# c\n-r other.txt\nFoo[extra]==1.2.3 ; python_version>'3'\nbar>=2\nBaz_Qux==0.1  # hi\nplain\n", "")
    assert au.parse_pins(r) == [("baz-qux", "0.1"), ("foo", "1.2.3")]


def test_audit_flags_skips_and_up_to_date_are_all_reported(fake):
    r = repo_with(fake, "mylib==1.0\nhuge==1.0\ncurrent==1.0\n", "import mylib\n\n\ndef go():\n    return mylib.fetch('u', 5)\n")
    res = {x.dist: x for x in au.audit(r, max_mb=15)}
    assert res["mylib"].status == "checked" and res["mylib"].risk == "red" and res["mylib"].flagged[0][:2] == ["app.py", 5]
    assert res["huge"].status == "skipped" and "limit" in res["huge"].reason
    assert res["current"].status == "up-to-date"
    assert au.worst(list(res.values())) == "red"


def test_cli_audit_exit_codes_and_no_pins(fake, capsys):
    r = repo_with(fake, "mylib==1.0\n", "import mylib\nmylib.fetch('u', 5)\n")
    assert cli.main(["audit", "--repo", str(r)]) == 2
    assert "app.py:2" in capsys.readouterr().out
    empty = fake / "empty"
    empty.mkdir()
    assert cli.main(["audit", "--repo", str(empty)]) == 3


def test_audit_separates_hard_breaks_from_default_changes_to_review(fake, capsys, monkeypatch):
    from tests.test_pypi_check import sdist_bytes as sb
    import hashlib, json as j
    arch = {("dlib", "1.0"): sb({"p-1.0/src/dlib/__init__.py": "def f(a, mode='x'):\n    pass\n\n\ndef g(a, old=1):\n    pass\n"}),
            ("dlib", "2.0"): sb({"p-2.0/src/dlib/__init__.py": "def f(a, mode='y'):\n    pass\n\n\ndef g(a):\n    pass\n"})}
    real = pypi._read

    def read(url, timeout=45, tries=4):
        if "dlib" in url:
            if url.endswith("/dlib/json"):
                return j.dumps({"info": {"version": "2.0"}}).encode()
            if url.endswith("/json"):
                v = url.split("/")[-2]
                return j.dumps({"urls": [{"packagetype": "sdist", "url": f"https://f/dlib-{v}.tar.gz", "size": 10,
                                          "digests": {"sha256": hashlib.sha256(arch[("dlib", v)]).hexdigest()}}]}).encode()
            return arch[("dlib", url.split("dlib-")[1][:-7])]
        return real(url, timeout, tries)
    monkeypatch.setattr(pypi, "_read", read)
    r = repo_with(fake, "dlib==1.0\n", "import dlib\n\n\ndef go():\n    dlib.f(1)\n    dlib.g(1, old=2)\n")
    res = au.audit(r)[0]
    assert (res.hard, res.review) == (1, 1)
    cli.main(["audit", "--repo", str(r)])
    out = capsys.readouterr().out
    assert "1 will fail, 1 default change(s) to review" in out and "review: default changed" in out
