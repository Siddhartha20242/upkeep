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


def test_pip_compile_style_pins_with_hashes_and_continuations_are_parsed(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    (r / "requirements.txt").write_text(
        "foo==1.0 \\\n    --hash=sha256:abc \\\n    --hash=sha256:def\nbar==2.0 --hash=sha256:def\nbaz==3.0\n"
        "qux>=1.0\n-r more.txt\n--index-url https://example.invalid/simple\n")
    (r / "more.txt").write_text("included==4.5  # from -r\nloose\n")
    assert au.parse_pins(r) == [("bar", "2.0"), ("baz", "3.0"), ("foo", "1.0"), ("included", "4.5")]
    assert au.count_unpinned(r) == 2     # qux (range) and loose (bare name) are reported, not silently ignored


def test_requirement_includes_cannot_loop_forever(tmp_path):
    (tmp_path / "requirements.txt").write_text("-r requirements-dev.txt\na==1\n")
    (tmp_path / "requirements-dev.txt").write_text("-r requirements.txt\nb==2\n")
    assert au.parse_pins(tmp_path) == [("a", "1"), ("b", "2")]


def test_version_ordering_follows_pep440_closely_enough_for_pins():
    order = ["1.0.dev1", "1.0a1", "1.0b2", "1.0rc1", "1.0", "1.0.post1", "1.1", "2.0.0rc1", "2.0.0", "1!0.5"]
    keys = [au._ver(v) for v in order]
    assert keys == sorted(keys)
    assert au._ver("1.0") == au._ver("1.0.0") and au._ver("2.0.0rc1") < au._ver("2.0.0")


def test_a_pinned_prerelease_is_audited_against_the_final_release(fake, monkeypatch):
    r = repo_with(fake, "mylib==1.0rc1\n", "import mylib\nmylib.fetch('u', 5)\n")
    monkeypatch.setattr(au, "latest_version", lambda d: "1.0")

    def no_sources(*a, **k):
        raise au.PyPIError("probe: the audit did try to analyse it")
    monkeypatch.setattr(au, "sources_for", no_sources)
    res = au.audit(r)[0]
    assert res.status == "skipped" and "did try" in res.reason   # i.e. it was NOT dismissed as already the latest


def test_fail_on_controls_the_exit_code_but_never_hides_a_total_failure(fake, capsys, monkeypatch):
    from tests.test_pypi_check import sdist_bytes as sb
    import hashlib, json as j
    arch = {"1.0": sb({"p-1.0/src/dlib/__init__.py": "def f(a, mode='x'):\n    pass\n"}),
            "2.0": sb({"p-2.0/src/dlib/__init__.py": "def f(a, mode='y'):\n    pass\n"})}   # only a DEFAULT changes
    real = pypi._read

    def read(url, timeout=45, tries=4):
        if "dlib" in url:
            if url.endswith("/dlib/json"):
                return j.dumps({"info": {"version": "2.0"}}).encode()
            if url.endswith("/json"):
                v = url.split("/")[-2]
                return j.dumps({"urls": [{"packagetype": "sdist", "url": f"https://f/dlib-{v}.tar.gz", "size": 10,
                                          "digests": {"sha256": hashlib.sha256(arch[v]).hexdigest()}}]}).encode()
            return arch[url.split("dlib-")[1][:-7]]
        return real(url, timeout, tries)
    monkeypatch.setattr(pypi, "_read", read)
    r = repo_with(fake, "dlib==1.0\n", "import dlib\n\n\ndef go():\n    return dlib.f(1)\n")
    assert cli.main(["audit", "--repo", str(r)]) == 2                              # default: any flagged line fails
    assert cli.main(["audit", "--repo", str(r), "--fail-on", "hard"]) == 0         # a changed default alone does not
    assert cli.main(["audit", "--repo", str(r), "--fail-on", "never"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(au, "sources_for", lambda *a, **k: (_ for _ in ()).throw(au.PyPIError("offline")))
    assert cli.main(["audit", "--repo", str(r), "--fail-on", "never"]) == 3        # but "nothing was analysed" is always an error


def test_pins_in_pyproject_are_read_when_there_is_no_requirements_file(tmp_path):
    import sys
    r = tmp_path / "p"
    r.mkdir()
    (r / "pyproject.toml").write_text('[project]\nname = "x"\ndependencies = ["PyYAML==5.4.1", "requests>=2", "six==1.16.0; python_version > \'3\'"]\n'
                                      '[project.optional-dependencies]\ndev = ["pytest==8.0.0"]\ndocs = ["sphinx==7.0"]\n')
    if sys.version_info < (3, 11):
        assert au.parse_pins(r) == []          # tomllib is not available before 3.11
        return
    assert au.parse_pins(r) == [("pyyaml", "5.4.1"), ("pytest", "8.0.0"), ("six", "1.16.0")] or \
        sorted(au.parse_pins(r)) == [("pyyaml", "5.4.1"), ("pytest", "8.0.0"), ("six", "1.16.0")] or \
        sorted(au.parse_pins(r)) == sorted([("pyyaml", "5.4.1"), ("pytest", "8.0.0"), ("six", "1.16.0")])
    assert au.count_unpinned(r) == 1           # requests>=2 is reported, docs-only sphinx is not read


def test_a_falsy_default_turning_into_none_is_counted_as_probably_equivalent(fake, monkeypatch, capsys):
    from tests.test_pypi_check import sdist_bytes as sb
    import hashlib, json as j
    arch = {"1.0": sb({"p-1.0/src/dlib/__init__.py": "def f(a, many=False, mode='x'):\n    pass\n"}),
            "2.0": sb({"p-2.0/src/dlib/__init__.py": "def f(a, many=None, mode='y'):\n    pass\n"})}
    real = pypi._read

    def read(url, timeout=45, tries=4):
        if "dlib" in url:
            if url.endswith("/dlib/json"):
                return j.dumps({"info": {"version": "2.0"}}).encode()
            if url.endswith("/json"):
                v = url.split("/")[-2]
                return j.dumps({"urls": [{"packagetype": "sdist", "url": f"https://f/dlib-{v}.tar.gz", "size": 10,
                                          "digests": {"sha256": hashlib.sha256(arch[v]).hexdigest()}}]}).encode()
            return arch[url.split("dlib-")[1][:-7]]
        return real(url, timeout, tries)
    monkeypatch.setattr(pypi, "_read", read)
    r = repo_with(fake, "dlib==1.0\n", "import dlib\n\n\ndef go():\n    return dlib.f(1)\n")
    res = au.audit(r)[0]
    assert (res.hard, res.review, res.minor) == (0, 1, 0)       # one site, two default changes: `mode` x->y is real, so NOT equivalent
    (r / "app.py").write_text("import dlib\n\n\ndef go():\n    return dlib.f(1, mode='x')\n")   # now only `many` False->None applies
    res = au.audit(r)[0]
    assert (res.hard, res.review, res.minor) == (0, 1, 1)
    cli.main(["audit", "--repo", str(r)])
    assert "1 of them look equivalent" in capsys.readouterr().out


def test_the_share_summary_has_counts_and_library_names_but_no_code_paths_or_files(fake, capsys):
    repo = repo_with(fake, "mylib==1.0\nhuge==1.0\ncurrent==1.0\n", "import mylib\n\n\ndef secret_business_logic():\n    return mylib.fetch('u', 5)\n")
    assert cli.main(["audit", "--repo", str(repo), "--summary"]) == 2
    out = capsys.readouterr().out
    block = out.split("--- paste this back")[1]
    assert "pins found: 3; analysed: 1; already latest: 1; skipped: 1 (archive too big x1)" in block
    assert "mylib 1.0 -> 2.0: 1 line(s) will fail" in block
    for private in ("app.py", "secret_business_logic", str(repo), "fetch('u'", "requirements.txt", str(fake)):
        assert private not in block, private


def test_the_share_summary_says_so_when_nothing_is_flagged():
    from upkeep.audit import DepResult, share_summary
    r = DepResult("requests", "2.0", "2.1", status="checked")
    assert "no flagged lines" in share_summary([r], "0.0.1", "3.12.0")


def test_find_pin_looks_in_requirements_files_several_folders_down(tmp_path):
    (tmp_path / "deploy" / "python" / "requirements").mkdir(parents=True)
    (tmp_path / "deploy" / "python" / "requirements" / "base.txt").write_text("PyYAML==5.4.1\nsix==1.16.0\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.txt").write_text("PyYAML==5.0\n")                       # not a requirements file: ignored
    assert au.find_pin(tmp_path, "pyyaml") == "5.4.1" and au.find_pin(tmp_path, "PyYAML", lambda v: v.startswith("5.4")) == "5.4.1"
    assert au.find_pin(tmp_path, "pyyaml", lambda v: v.startswith("6.")) is None and au.find_pin(tmp_path, "nothing") is None
    (tmp_path / "a" / "b" / "c" / "d" / "e").mkdir(parents=True)
    (tmp_path / "a" / "b" / "c" / "d" / "e" / "requirements.txt").write_text("deep==1.0\n")
    assert au.find_pin(tmp_path, "deep") is None                                         # deeper than the limit


def test_find_pin_normalises_names(tmp_path):
    (tmp_path / "requirements.txt").write_text("Zope_Interface==5.0\n")
    assert au.find_pin(tmp_path, "zope.interface") == "5.0"
