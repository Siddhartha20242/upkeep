from pathlib import Path

from upkeep.apidiff import diff_api, extract_api
from upkeep.guard import allowed_ranges, check_diff
from upkeep.radar import run_radar
from upkeep.scan import find_call_sites


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def make_lib(root: Path, version: int) -> None:
    write(root, "lib/__init__.py", "from .core import fetch, Client\n")
    if version == 1:
        write(
            root,
            "lib/core.py",
            "def fetch(url, timeout=10, retries=3):\n    pass\n\n"
            "def _private(x):\n    pass\n\n"
            "class Client:\n    def __init__(self, token, base='x'):\n        pass\n\n"
            "def stable(a, b):\n    pass\n",
        )
    else:
        write(
            root,
            "lib/core.py",
            "def fetch(url, *, timeout, retries=3):\n    pass\n\n"
            "class Client:\n    def __init__(self, token, base, region):\n        pass\n\n"
            "def stable(a, b):\n    pass\n",
        )


def test_extract_api_skips_private_and_resolves_reexports(tmp_path):
    make_lib(tmp_path, 1)
    api = extract_api(tmp_path, "lib")
    assert "lib.core.fetch" in api
    assert "lib.fetch" in api  # re-export
    assert "lib.core._private" not in api
    assert api["lib.Client"].is_class
    assert [p.name for p in api["lib.Client"].params] == ["token", "base"]


def test_diff_detects_breaking_changes(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    make_lib(old, 1)
    make_lib(new, 2)
    changes = diff_api(extract_api(old, "lib"), extract_api(new, "lib"))
    kinds = {(c.symbol, c.kind) for c in changes}
    assert ("lib.fetch", "param_kind_changed") in kinds
    assert ("lib.fetch", "param_became_required") in kinds
    assert ("lib.Client", "param_became_required") in kinds
    assert ("lib.Client", "param_added_required") in kinds
    assert not any(c.symbol.endswith("stable") for c in changes)


def test_removed_symbol(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    write(old, "lib/__init__.py", "def gone(a):\n    pass\n")
    write(new, "lib/__init__.py", "def other(a):\n    pass\n")
    changes = diff_api(extract_api(old, "lib"), extract_api(new, "lib"))
    assert [(c.symbol, c.kind) for c in changes] == [("lib.gone", "removed")]


def test_adding_optional_param_is_not_breaking(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    write(old, "lib/__init__.py", "def f(a):\n    pass\n")
    write(new, "lib/__init__.py", "def f(a, b=1):\n    pass\n")
    assert diff_api(extract_api(old, "lib"), extract_api(new, "lib")) == []


def test_scan_resolves_aliases_and_attribute_chains(tmp_path):
    write(
        tmp_path,
        "app/main.py",
        "import lib\nimport lib as L\nfrom lib import fetch as get\nfrom lib.core import stable\n\n"
        "lib.fetch('a')\nL.fetch('b')\nget('c')\nstable(1, 2)\nother.fetch('z')\n",
    )
    sites = find_call_sites(tmp_path, "lib", {"lib.fetch"})
    calls = [s for s in sites if s.kind == "call"]
    assert sorted(s.line for s in calls) == [6, 7, 8]
    assert [(s.line, s.kind) for s in sites if s.kind == "import"] == [(3, "import")]


def test_scan_skips_venv(tmp_path):
    write(tmp_path, ".venv/x.py", "import lib\nlib.fetch(1)\n")
    assert find_call_sites(tmp_path, "lib", {"lib.fetch"}) == []


def test_radar_red_and_green(tmp_path):
    old, new, repo = tmp_path / "old", tmp_path / "new", tmp_path / "repo"
    make_lib(old, 1)
    make_lib(new, 2)
    write(repo, "svc.py", "import lib\n\nlib.fetch('u', 5)\n")
    report = run_radar(repo, old, new, "lib")
    assert report.risk == "red"
    assert report.affected[0][1][0].line == 3

    write(repo, "svc.py", "import lib\n\nlib.stable(1, 2)\n")
    assert run_radar(repo, old, new, "lib").risk == "green"


DIFF = """--- a/svc.py
+++ b/svc.py
@@ -1,3 +1,3 @@
 import lib
-lib.fetch('u', 5)
+lib.fetch('u', timeout=5)
 x = 1
"""

OUT_OF_RANGE = """--- a/svc.py
+++ b/svc.py
@@ -1,3 +1,3 @@
 import lib
 lib.fetch('u', 5)
-x = 1
+x = 2
"""


def test_guard_accepts_in_scope_edit(tmp_path):
    write(tmp_path, "svc.py", "import lib\nlib.fetch('u', 5)\nx = 1\n")
    sites = find_call_sites(tmp_path, "lib", {"lib.fetch"})
    assert check_diff(DIFF, allowed_ranges(sites)).ok


def test_guard_rejects_other_files_and_lines(tmp_path):
    write(tmp_path, "svc.py", "import lib\nlib.fetch('u', 5)\nx = 1\n")
    sites = find_call_sites(tmp_path, "lib", {"lib.fetch"})
    allowed = allowed_ranges(sites)

    other_file = DIFF.replace("svc.py", "secrets.py")
    assert not check_diff(other_file, allowed).ok

    result = check_diff(OUT_OF_RANGE, allowed)
    assert not result.ok
    assert "svc.py:3" in result.reason

    assert not check_diff("", allowed).ok


def test_scan_finds_import_decorator_and_base_class_uses(tmp_path):
    write(
        tmp_path,
        "app/m.py",
        "from lib import Markup, escape, contextfilter\nimport lib\n\n\n"
        "@contextfilter\ndef f(ctx, v):\n    return escape(v)\n\n\n"
        "class Enc(lib.JSONEncoder):\n    pass\n\n\n"
        "x = Markup('a')\n",
    )
    syms = {"lib.Markup", "lib.escape", "lib.contextfilter", "lib.JSONEncoder"}
    got = {(s.symbol, s.kind, s.line) for s in find_call_sites(tmp_path, "lib", syms)}
    assert ("lib.Markup", "import", 1) in got
    assert ("lib.escape", "import", 1) in got
    assert ("lib.contextfilter", "reference", 5) in got   # decorator
    assert ("lib.JSONEncoder", "reference", 10) in got    # base class
    assert ("lib.escape", "call", 7) in got
    assert ("lib.Markup", "call", 14) in got


def test_removed_import_alone_makes_radar_red(tmp_path):
    old, new, repo = tmp_path / "old", tmp_path / "new", tmp_path / "repo"
    write(old, "lib/__init__.py", "def escape(s):\n    return s\n")
    write(new, "lib/__init__.py", "def other(s):\n    return s\n")
    write(repo, "a.py", "from lib import escape\n")  # imported, never called
    assert run_radar(repo, old, new, "lib").risk == "red"


def test_test_modules_are_not_public_api(tmp_path):
    write(tmp_path, "lib/__init__.py", "def f(a):\n    pass\n")
    write(tmp_path, "lib/tests/test_x.py", "def helper(a):\n    pass\n")
    api = extract_api(tmp_path, "lib")
    assert "lib.f" in api and not any("tests" in k for k in api)


def test_radar_reports_old_and_new_signatures(tmp_path):
    old, new, repo = tmp_path / "old", tmp_path / "new", tmp_path / "repo"
    write(old, "lib/__init__.py", "def area(width, height, unit='m'):\n    pass\n\ndef gone(x):\n    pass\n")
    write(new, "lib/__init__.py", "def area(height, width, *, unit='m'):\n    pass\n")
    write(repo, "a.py", "import lib\nlib.area(3, 4)\nlib.gone(1)\n")
    sigs = run_radar(repo, old, new, "lib").signatures
    assert sigs["lib.area"] == ("lib.area(width, height, unit=...)", "lib.area(height, width, *, unit=...)")
    assert sigs["lib.gone"] == ("lib.gone(x)", None)
