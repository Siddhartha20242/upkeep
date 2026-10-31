"""Deny by default: a new import or builtin must be on a list, so a trick nobody thought of fails instead of passing."""

import pytest

from upkeep.patchlint import ALLOWED_IMPORT_ROOTS, SAFE_BUILTINS, check_edit, import_roots_from

BASE = "def f(x):\n    return x\n"
DEPS = {"yaml", "markupsafe"}


def edit(snippet, roots=DEPS):
    return check_edit(BASE, BASE + snippet, roots)


@pytest.mark.parametrize("snippet", [
    "import socketserver\n", "import smtplib\n", "import webbrowser\n", "import ftplib\n", "import telnetlib\n", "import code\n",
    "import runpy\n", "import zipimport\n", "import xmlrpc.client\n", "import http.server\n", "from asyncio import subprocess\n",
    "import sqlite3\n", "import logging\n", "import random\n", "import pkgutil\n", "import ssl\n", "import mmap\n",
])
def test_imports_nobody_listed_are_rejected(snippet):
    ok, why = edit(snippet)
    assert not ok and ("unlisted module" in why or "import " in why), snippet


@pytest.mark.parametrize("snippet", [
    "t = type('X', (), {})\n", "m = memoryview(b'x')\n", "h = help\n", "e = exit\n", "q = quit\n", "c = copyright\n",
    "x = hasattr(f, 'a')\n", "y = compile\n", "g = globals()\n", "d = dir(f)\n",
])
def test_builtins_nobody_listed_are_rejected_when_new(snippet):
    ok, why = edit(snippet)
    assert not ok, snippet


@pytest.mark.parametrize("snippet", ["a = frozenset([1])\n", "b = bytearray(2)\n", "c = sorted(set([2, 1]))\n", "d = super\n"])
def test_listed_builtins_pass(snippet):
    assert edit(snippet)[0], snippet


@pytest.mark.parametrize("snippet", [
    "import re, json, math\nr = re.compile('a')\n", "from collections import OrderedDict\n", "import urllib.parse\n",
    "from urllib.parse import quote\n", "import yaml\n", "from markupsafe import Markup\n", "from . import helpers\n", "from .utils import x\n",
    "x = sorted(map(str, range(3)))\n", "y = isinstance(3, int) and len('ab') > 1\n", "z = dict(a=1) | {'b': 2}\n",
    "try:\n    pass\nexcept ValueError:\n    pass\n", "print(repr(1), str(2), list(zip([1], [2])))\n",
])
def test_ordinary_things_pass(snippet):
    assert edit(snippet)[0], snippet


def test_a_dependency_must_be_declared_to_be_importable():
    assert not check_edit(BASE, BASE + "import yaml\n", ())[0]
    assert check_edit(BASE, BASE + "import yaml\n", {"yaml"})[0]


def test_an_import_the_file_already_had_is_not_blamed_on_the_edit():
    old = "import socketserver\n\n\ndef f():\n    return socketserver.TCPServer\n"
    assert check_edit(old, old + "\n\ndef g():\n    return 1\n", ())[0]
    assert not check_edit(old, old + "import smtplib\n", ())[0]


def test_a_name_the_file_binds_itself_is_fine_even_if_it_shadows_a_builtin():
    assert edit("def type(x):\n    return 1\n\n\nv = type(2)\n")[0]      # the file defines `type` itself, so this is not the builtin
    assert not edit("v = type(2)\n")[0]


def test_lists_are_sane():
    assert not ({"os", "sys", "subprocess", "socket", "pathlib", "importlib", "ctypes", "pickle"} & ALLOWED_IMPORT_ROOTS)
    assert not ({"eval", "exec", "open", "compile", "getattr", "setattr", "globals", "type", "vars", "__import__"} & SAFE_BUILTINS)


def test_import_roots_come_from_the_dependency_folder_without_test_infrastructure(tmp_path):
    (tmp_path / "markupsafe").mkdir()
    (tmp_path / "yaml").mkdir()
    (tmp_path / "pytest").mkdir()
    (tmp_path / "_pytest").mkdir()
    (tmp_path / "six.py").write_text("")
    (tmp_path / "PyYAML-6.0.dist-info").mkdir()
    (tmp_path / "bin").mkdir()
    assert import_roots_from(tmp_path, None, tmp_path / "missing") == {"markupsafe", "yaml", "six"}


def test_the_agent_rejects_an_unlisted_import_but_accepts_a_declared_one(tmp_path):
    from tests.test_agent import Scripted, write
    from tests.test_cli_fix import build
    from upkeep.agent import Agent
    from upkeep.radar import run_radar
    from upkeep.sandbox import run_tests
    old, new, repo = build(tmp_path)
    write(new, "extra/__init__.py", "")                      # a package that ships next to the new library: declared by the project
    report = run_radar(repo, old, new, "lib")
    runner = lambda w: run_tests(w, backend="local", allow_unsandboxed=True, extra_paths=[new], with_coverage=False)  # noqa: E731
    a = Agent(Scripted([]), repo, report, runner, import_roots=import_roots_from(new))
    bad = a.t_replace_lines("app.py", 5, 5, "    import smtplib\n    return lib.fetch('u', timeout=5)")
    assert bad.startswith("rejected by content check") and "unlisted module smtplib" in bad
    assert "rejected" not in a.t_replace_lines("app.py", 5, 5, "    import re\n    return lib.fetch('u', timeout=5)")   # a listed stdlib module
    good = a.t_replace_lines("app.py", 5, 5, "    import extra\n    return lib.fetch('u', timeout=5)")
    assert "rejected" not in good
