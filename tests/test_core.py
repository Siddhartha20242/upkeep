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
    assert ("lib.JSONEncoder", "base", 10) in got         # base class
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


def test_extract_api_can_include_methods_for_measuring_the_blind_spot(tmp_path):
    write(tmp_path, "lib/__init__.py",
          "class C:\n    def __init__(self, a):\n        pass\n    def run(self, x, y=1):\n        pass\n"
          "    @staticmethod\n    def make(a):\n        pass\n    def _private(self):\n        pass\n")
    plain = extract_api(tmp_path, "lib")
    assert "lib.C" in plain and "lib.C.run" not in plain  # default: functions and constructors only
    full = extract_api(tmp_path, "lib", include_methods=True)
    assert [p.name for p in full["lib.C.run"].params] == ["x", "y"] and full["lib.C.run"].is_method   # self is skipped
    assert [p.name for p in full["lib.C.make"].params] == ["a"]                                       # staticmethod keeps its first arg
    assert "lib.C._private" not in full


def test_methods_moved_to_a_base_class_are_not_reported_as_removed(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    write(old, "lib/__init__.py", "from .client import Client\n")
    write(old, "lib/client.py", "class Client:\n    def get(self, key, default=None):\n        pass\n    def put(self, key, value):\n        pass\n")
    write(new, "lib/__init__.py", "from .client import Client\n")
    write(new, "lib/client.py", "from .mixins import Commands\n\n\nclass Client(Commands):\n    pass\n")
    write(new, "lib/mixins.py", "class Commands:\n    def get(self, key, *, default):\n        pass\n    def put(self, key, value):\n        pass\n")
    changes = diff_api(extract_api(old, "lib", include_methods=True), extract_api(new, "lib", include_methods=True))
    got = {(c.symbol, c.kind) for c in changes}
    assert not any(k == "removed" for _, k in got), got                 # nothing was removed, only moved
    assert ("lib.client.Client.get", "param_kind_changed") in got       # a REAL change on the moved method is still found
    assert not any(s.endswith(".put") for s, _ in got)                  # an unchanged moved method is quiet
    assert any(s == "lib.Client.get" for s, _ in got)                   # and the public alias works too


def test_inheritance_cycles_and_unknown_bases_do_not_crash(tmp_path):
    write(tmp_path, "lib/__init__.py", "class A(B):\n    def x(self):\n        pass\nclass B(A):\n    def y(self):\n        pass\nclass C(Unknown, object):\n    def z(self):\n        pass\n")
    api = extract_api(tmp_path, "lib", include_methods=True)
    assert "lib.A.x" in api and "lib.C.z" in api


def test_inherited_methods_follow_an_aliased_import_of_a_same_named_base(tmp_path):
    """Flask 3 style: `from .sansio.bp import Blueprint as SansioBlueprint; class Blueprint(SansioBlueprint)`."""
    from upkeep.apidiff import extract_api
    pkg = tmp_path / "lib"
    (pkg / "sansio").mkdir(parents=True)
    (pkg / "__init__.py").write_text("from .bp import Blueprint\n")
    (pkg / "sansio" / "__init__.py").write_text("")
    (pkg / "sansio" / "scaffold.py").write_text("class Scaffold:\n    def route(self, rule, **options):\n        pass\n")
    (pkg / "sansio" / "bp.py").write_text("from .scaffold import Scaffold\n\n\nclass Blueprint(Scaffold):\n    def register(self, app):\n        pass\n")
    (pkg / "bp.py").write_text("from .sansio.bp import Blueprint as SansioBlueprint\n\n\nclass Blueprint(SansioBlueprint):\n    def send(self):\n        pass\n")
    api = extract_api(tmp_path, "lib", include_methods=True)
    for m in ("route", "register", "send"):
        assert f"lib.Blueprint.{m}" in api, m


def test_lazily_exposed_names_are_not_reported_as_removed(tmp_path):
    from upkeep.apidiff import diff_api, extract_api
    old, new = tmp_path / "old", tmp_path / "new"
    for d in (old, new):
        (d / "lib").mkdir(parents=True)
    (old / "lib" / "__init__.py").write_text("def root_validator(pre=False):\n    pass\n\n\nclass Extra:\n    pass\n\n\ndef gone():\n    pass\n")
    (new / "lib" / "__init__.py").write_text(
        "_dyn = {'root_validator': 'x', 'Extra': 'y'}\n\n\ndef __getattr__(name):\n    return _dyn[name]\n")
    changes = {c.symbol: c.kind for c in diff_api(extract_api(old, "lib"), extract_api(new, "lib"))}
    assert changes == {"lib.gone": "removed"}  # root_validator and Extra still exist; only the real removal is reported


def test_absolute_reexports_inside_the_package_are_followed(tmp_path):
    from upkeep.apidiff import diff_api, extract_api
    old, new = tmp_path / "old", tmp_path / "new"
    for d in (old, new):
        (d / "lib").mkdir(parents=True)
    (old / "lib" / "__init__.py").write_text("from .schema import Schema\n")
    (new / "lib" / "__init__.py").write_text("from lib.schema import Schema\n")
    for d in (old, new):
        (d / "lib" / "schema.py").write_text("class Schema:\n    def __init__(self, strict=False):\n        pass\n")
    assert diff_api(extract_api(old, "lib"), extract_api(new, "lib")) == []
    assert "lib.Schema" in extract_api(new, "lib")


def _two_versions(tmp_path, old_files, new_files):
    out = []
    for name, files in (("old", old_files), ("new", new_files)):
        root = tmp_path / name
        for rel, text in files.items():
            f = root / "lib" / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        out.append(root)
    return out


def test_constructor_is_inherited_from_a_private_base_class(tmp_path):
    from upkeep.apidiff import diff_api, extract_api
    old, new = _two_versions(
        tmp_path,
        {"__init__.py": "from .f import DateTime\n", "f.py": "class DateTime:\n    def __init__(self, format=None, **kw):\n        pass\n"},
        {"__init__.py": "from .f import DateTime\n",
         "f.py": "class _Temporal:\n    def __init__(self, format=None, **kw):\n        pass\n\n\nclass DateTime(_Temporal):\n    pass\n"})
    assert [p.name for p in extract_api(new, "lib")["lib.DateTime"].params] == ["format", "kw"]
    assert diff_api(extract_api(old, "lib"), extract_api(new, "lib")) == []


def test_reexport_chains_and_assignment_aliases_are_followed(tmp_path):
    from upkeep.apidiff import diff_api, extract_api
    old, new = _two_versions(
        tmp_path,
        {"__init__.py": "def create(url):\n    pass\n\n\nclass NoResult(Exception):\n    pass\n", "orm.py": ""},
        {"__init__.py": "from .engine import create as create\nfrom .exc import NoResult as NoResult\n",
         "engine/__init__.py": "from .make import create as create\n", "engine/make.py": "def create(url):\n    pass\n",
         "exc.py": "class NoResult(Exception):\n    pass\n"})
    assert diff_api(extract_api(old, "lib"), extract_api(new, "lib")) == []   # two-hop re-export
    old2, new2 = _two_versions(
        tmp_path / "b",
        {"orm/__init__.py": "", "orm/exc.py": "class NoResultFound(Exception):\n    pass\n"},
        {"orm/__init__.py": "", "orm/exc.py": "from .. import exc as sa_exc\n\nNoResultFound = sa_exc.NoResultFound\n",
         "exc.py": "class NoResultFound(Exception):\n    pass\n", "__init__.py": ""})
    assert diff_api(extract_api(old2, "lib"), extract_api(new2, "lib")) == []   # assignment alias


def test_renamed_positional_parameter_and_bare_decorators_are_not_flagged():
    from upkeep.apidiff import Change, Param, Signature
    from upkeep.radar import site_affected
    from upkeep.scan import CallSite
    old = Signature("lib.validates", (Param("field_name", "pos", False),))
    new = Signature("lib.validates", (Param("field_names", "vararg", False),))
    ch = Change("lib.validates", "param_removed", "x", param="field_name")
    assert not site_affected(ch, CallSite("a.py", 1, 1, "lib.validates", "call", 1, (), False), old, new)       # validates('x')
    assert site_affected(ch, CallSite("a.py", 1, 1, "lib.validates", "call", 0, ("field_name",), False), old, new)  # keyword
    old2 = Signature("lib.cp", (Param("func", "pos", False),))
    new2 = Signature("lib.cp", (Param("fget", "pos", False),))
    bare = CallSite("a.py", 2, 2, "lib.cp", "reference", 0, (), False)
    assert not site_affected(Change("lib.cp", "param_removed", "x", param="func"), bare, old2, new2)
    assert not site_affected(Change("lib.cp", "param_added_required", "x", param="fget"), bare, old2, new2)


def test_generated_constructors_are_unknown_not_inherited_from_a_base(tmp_path):
    from upkeep.apidiff import diff_api, extract_api
    old, new = _two_versions(
        tmp_path,
        {"__init__.py": "from dataclasses import dataclass\n\n\nclass Base:\n    def __init__(self, x, y=1):\n        pass\n\n\n"
                        "@dataclass\nclass Cfg(Base):\n    a: int = 0\n\n\nclass Child(Cfg):\n    pass\n\n\nclass Plain(Base):\n    pass\n"},
        {"__init__.py": "from dataclasses import dataclass\n\n\nclass Base:\n    def __init__(self, x, y=1):\n        pass\n\n\n"
                        "@dataclass\nclass Cfg(Base):\n    a: int = 0\n    b: str = ''\n\n\nclass Child(Cfg):\n    pass\n\n\nclass Plain(Base):\n    pass\n"})
    api = extract_api(new, "lib")
    assert api["lib.Cfg"].ctor_unknown and api["lib.Child"].ctor_unknown          # no wrong signature is invented
    assert not api["lib.Plain"].ctor_unknown and [p.name for p in api["lib.Plain"].params] == ["x", "y"]
    assert diff_api(extract_api(old, "lib"), api) == []                           # the field change is unknown, so nothing false is reported


def test_a_generated_constructor_class_that_disappears_is_still_reported_removed(tmp_path):
    from upkeep.apidiff import diff_api, extract_api
    old, new = _two_versions(
        tmp_path, {"__init__.py": "from dataclasses import dataclass\n\n\n@dataclass\nclass Cfg:\n    a: int = 0\n"}, {"__init__.py": ""})
    assert [(c.symbol, c.kind) for c in diff_api(extract_api(old, "lib"), extract_api(new, "lib"))] == [("lib.Cfg", "removed")]


def test_proof_limitations_name_the_blind_spots():
    from upkeep.radar import RadarReport
    text = " ".join(RadarReport(package="x", risk="green").limitations)
    for phrase in ("stops accepting a value", "@dataclass", "lazily", "renamed parameter", "changed default"):
        assert phrase in text


def test_an_inherited_constructor_records_where_it_came_from(tmp_path):
    from upkeep.apidiff import extract_api
    old, _ = _two_versions(tmp_path, {"__init__.py": "class Base:\n    def __init__(self, x):\n        pass\n\n\nclass A(Base):\n    pass\n\n\nclass B(Base):\n    pass\n"}, {"__init__.py": ""})
    api = extract_api(old, "lib")
    assert api["lib.A"].ctor_from == api["lib.B"].ctor_from == "lib.Base" and api["lib.Base"].ctor_from == ""


def test_module_alias_import_makes_every_name_below_it_reachable_under_the_alias(tmp_path):
    """SQLAlchemy: `from .array import array as array` + `from . import array as arraylib` keeps `pg.arraylib.ARRAY` valid."""
    from upkeep.apidiff import diff_api, extract_api
    old, new = _two_versions(
        tmp_path,
        {"__init__.py": "", "pg/__init__.py": "", "pg/arraylib.py": "class ARRAY:\n    def bind(self, x):\n        pass\n"},
        {"__init__.py": "", "pg/__init__.py": "from .array import array as array\nfrom . import array as arraylib\n",
         "pg/array.py": "class array:\n    pass\n\n\nclass ARRAY:\n    def bind(self, x):\n        pass\n"})
    api = extract_api(new, "lib", include_methods=True)
    assert "lib.pg.arraylib.ARRAY" in api and "lib.pg.arraylib.ARRAY.bind" in api
    assert not any(c.kind == "removed" for c in diff_api(extract_api(old, "lib", include_methods=True), api))
