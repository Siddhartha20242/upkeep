"""Dataclass and NamedTuple constructors, checked against Python's own `inspect.signature` as ground truth."""

import inspect
import random
import textwrap
import typing  # noqa: F401  (used by exec'd sources)
from dataclasses import KW_ONLY, InitVar, dataclass, field  # noqa: F401
from typing import ClassVar, NamedTuple  # noqa: F401

import pytest

from tests.test_core import _two_versions
from upkeep.apidiff import diff_api, extract_api


def extracted(tmp_path, source, name):
    old, _ = _two_versions(tmp_path, {"__init__.py": source}, {"__init__.py": ""})
    return extract_api(old, "lib")[f"lib.{name}"]


def truth(source, name):
    ns: dict = {}
    exec(textwrap.dedent(source), {"dataclass": dataclass, "field": field, "KW_ONLY": KW_ONLY, "InitVar": InitVar,
                                   "ClassVar": ClassVar, "NamedTuple": NamedTuple, "__name__": "lib_truth"}, ns)
    sig = inspect.signature(ns[name])
    kinds = {inspect.Parameter.POSITIONAL_OR_KEYWORD: "pos", inspect.Parameter.KEYWORD_ONLY: "kwonly"}
    return [(p.name, kinds[p.kind], p.default is not inspect.Parameter.empty) for p in sig.parameters.values()]


def shape(sig):
    return [(p.name, p.kind, p.has_default) for p in sig.params]


CASES = {
    "plain": "@dataclass\nclass C:\n    a: int\n    b: str = 'x'\n",
    "default_factory": "@dataclass\nclass C:\n    a: int\n    b: list = field(default_factory=list)\n    c: int = field(default=3)\n",
    "init_false": "@dataclass\nclass C:\n    a: int\n    hidden: int = field(init=False, default=0)\n",
    "classvar": "@dataclass\nclass C:\n    a: int\n    K: ClassVar[int] = 5\n    b: int = 0\n",
    "kw_only_sentinel": "@dataclass\nclass C:\n    a: int\n    _: KW_ONLY\n    b: int = 1\n    c: int = 2\n",
    "kw_only_decorator": "@dataclass(kw_only=True)\nclass C:\n    a: int\n    b: int = 1\n",
    "kw_only_field": "@dataclass\nclass C:\n    a: int\n    b: int = field(default=1, kw_only=True)\n    c: int = 2\n",
    "frozen_and_order": "@dataclass(frozen=True, order=True)\nclass C:\n    a: int\n    b: int = 0\n",
    "initvar": "@dataclass\nclass C:\n    a: int\n    seed: InitVar[int] = 0\n",
    "inherit": "@dataclass\nclass A:\n    a: int\n    b: int = 1\n\n\n@dataclass\nclass C(A):\n    c: int = 2\n",
    "redefine_keeps_position": "@dataclass\nclass A:\n    a: int = 1\n    b: int = 2\n\n\n@dataclass\nclass C(A):\n    a: int = 9\n    z: int = 3\n",
    "plain_subclass": "@dataclass\nclass A:\n    a: int\n    b: int = 1\n\n\nclass C(A):\n    pass\n",
    "namedtuple": "class C(NamedTuple):\n    x: int\n    y: int = 0\n",
    "attribute_decorator": "import dataclasses\n\n\n@dataclasses.dataclass\nclass C:\n    a: int\n    b: int = 0\n",
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_extracted_signature_equals_pythons_own(tmp_path, name):
    src = "from dataclasses import dataclass, field, KW_ONLY, InitVar\nfrom typing import ClassVar, NamedTuple\n\n\n" + CASES[name]
    got = extracted(tmp_path, src, "C")
    assert not got.ctor_unknown
    assert shape(got) == truth(src, "C"), name


def test_init_false_or_a_base_from_outside_the_package_is_unknown_not_guessed(tmp_path):
    a = extracted(tmp_path, "from dataclasses import dataclass\n\n\n@dataclass(init=False)\nclass C:\n    a: int\n", "C")
    b = extracted(tmp_path / "b", "from dataclasses import dataclass\nfrom elsewhere import Mixin\n\n\n@dataclass\nclass C(Mixin):\n    a: int\n", "C")
    assert a.ctor_unknown and b.ctor_unknown


def test_an_explicit_init_wins_over_the_generated_one(tmp_path):
    src = "from dataclasses import dataclass\n\n\n@dataclass\nclass C:\n    a: int\n\n    def __init__(self, a, extra=1):\n        self.a = a\n"
    assert [p.name for p in extracted(tmp_path, src, "C").params] == ["a", "extra"]


def test_random_dataclasses_match_pythons_signature(tmp_path):
    rng = random.Random(3)
    for i in range(150):
        lines, names, seen_default = ["@dataclass(kw_only=%s)" % rng.random() < 0.2 if False else "@dataclass", "class C:"], [], False
        for j in range(rng.randint(1, 6)):
            n = f"f{j}"
            kind = rng.choice(["req", "def", "factory", "hidden", "kwonly_field"])
            if kind == "req" and not seen_default:
                lines.append(f"    {n}: int")
            elif kind == "factory":
                lines.append(f"    {n}: list = field(default_factory=list)")
                seen_default = True
            elif kind == "hidden":
                lines.append(f"    {n}: int = field(init=False, default=0)")
            elif kind == "kwonly_field":
                lines.append(f"    {n}: int = field(default=1, kw_only=True)")
            else:
                lines.append(f"    {n}: int = {rng.randint(0, 9)}")
                seen_default = True
        src = "from dataclasses import dataclass, field\n\n\n" + "\n".join(lines) + "\n"
        got = extracted(tmp_path / f"r{i}", src, "C")
        assert shape(got) == truth(src, "C"), src


def test_adding_a_required_field_removing_one_or_changing_a_default_is_now_visible(tmp_path):
    base = "from dataclasses import dataclass\n\n\n@dataclass\nclass C:\n    a: int\n    b: int = 1\n"
    for new_src, expect in [(base.replace("b: int = 1", "b: int = 1\n    c: int"), "param_added_required"),
                            (base.replace("    b: int = 1\n", ""), "param_removed"),
                            (base.replace("b: int = 1", "b: int = 2"), "default_changed")]:
        old, new = _two_versions(tmp_path / expect, {"__init__.py": base}, {"__init__.py": new_src})
        assert expect in {c.kind for c in diff_api(extract_api(old, "lib"), extract_api(new, "lib"))}
