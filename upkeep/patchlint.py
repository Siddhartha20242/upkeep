"""Content check for model edits: a migration edit should not introspect, probe, or game tests.

The scope guard limits WHERE the model may edit. This limits WHAT it may add.
It compares the edited file with the original and rejects edits that ADD:
  * imports of modules used to inspect or escape the environment,
  * calls to dynamic / reflective builtins,
  * dunder introspection (``__code__``, ``__path__``, ...),
  * new ``raise`` statements (used to leak data into test output),
  * a file that no longer parses.

This is a heuristic denylist and is defence in depth only. The sandbox (no network,
no secrets, read-only dependencies) is the real boundary.
"""

from __future__ import annotations

import ast
import builtins
from collections import Counter
from pathlib import Path
from typing import Iterable

from .secrets import scan as scan_secrets

DENY_MODULES = {
    "os", "sys", "subprocess", "pathlib", "inspect", "importlib", "socket", "urllib", "http",
    "ctypes", "shutil", "builtins", "pickle", "marshal", "glob", "tempfile", "gc", "types",
    "requests", "pty", "signal", "multiprocessing", "threading",
}
# Pure string/number helpers are fine even though their package root is denied (urllib.request is not).
ALLOWED_DOTTED = {"urllib.parse"}
DENY_CALLS = {"eval", "exec", "compile", "__import__", "open", "getattr", "setattr", "delattr",
              "globals", "locals", "vars", "dir", "input", "breakpoint"}
# Unsafe deserializers: choosing one of these to silence an upgrade error is a security downgrade.
UNSAFE_YAML = {"Loader", "UnsafeLoader", "CLoader", "CUnsafeLoader", "unsafe_load", "unsafe_load_all",
               "FullLoader", "CFullLoader", "full_load", "full_load_all"}  # FullLoader was only made safe in 5.4 and can still build some Python objects
# Referring to a dangerous builtin without calling it (`f = eval; f(...)`) is as bad as calling it. `dir` and `input`
# are left out because they are common variable names; calling them is still denied.
DENY_REFS = (DENY_CALLS - {"dir", "input"}) | {"__builtins__"}
# String-driven attribute access that would bypass the dunder check.
DENY_NAMES_ANYWHERE = {"attrgetter", "methodcaller"}
DENY_ATTRS = {"__path__", "__code__", "__file__", "__dict__", "__globals__", "__builtins__",
              "__subclasses__", "__class__", "__bases__", "__mro__", "__closure__", "__module__",
              # frame and function internals: the usual ways to reach globals or builtins without a dunder
              "gi_frame", "gi_code", "cr_frame", "cr_code", "ag_frame", "ag_code", "f_globals", "f_locals", "f_builtins",
              "f_back", "f_code", "tb_frame", "__self__", "__loader__", "__spec__"}


# Deny by default. The denylists above name known-bad things; these two lists name the ONLY new things an edit may use, so a trick
# nobody thought of fails instead of passing. Attribute access cannot be allow-listed, so DENY_ATTRS stays a denylist.
ALLOWED_IMPORT_ROOTS = {
    "__future__", "re", "json", "math", "string", "textwrap", "html", "collections", "itertools", "functools", "typing", "dataclasses",
    "decimal", "fractions", "datetime", "enum", "abc", "operator", "copy", "numbers", "unicodedata", "base64", "binascii", "hashlib",
    "warnings", "contextlib", "uuid", "statistics", "bisect", "heapq", "calendar", "difflib", "zoneinfo",
}
SAFE_BUILTINS = {
    "abs", "all", "any", "ascii", "bin", "bool", "bytearray", "bytes", "callable", "chr", "classmethod", "complex", "dict", "divmod",
    "enumerate", "filter", "float", "format", "frozenset", "hash", "hex", "id", "int", "isinstance", "issubclass", "iter", "len", "list",
    "map", "max", "min", "next", "object", "oct", "ord", "pow", "print", "property", "range", "repr", "reversed", "round", "set", "slice",
    "sorted", "staticmethod", "str", "sum", "super", "tuple", "zip",
}
# Test infrastructure that sits next to the project's real dependencies: never a reason for a migration edit to import it.
INFRA_ROOTS = {"pytest", "_pytest", "coverage", "pluggy", "iniconfig", "packaging", "pip", "setuptools", "wheel", "pkg_resources",
               "_distutils_hack", "py", "exceptiongroup", "tomli", "attr", "attrs"}


def import_roots_from(*dirs: Path | None) -> set[str]:
    """Top-level importable names in the given folders (the library sources and the installed dependency folder)."""
    names: set[str] = set()
    for d in dirs:
        if d is None or not Path(d).is_dir():
            continue
        for e in Path(d).iterdir():
            n = e.name
            if n.startswith((".", "_")) or n.endswith((".dist-info", ".egg-info", "__pycache__")) or n in ("bin", "share"):
                continue
            if e.is_dir():
                names.add(n)
            elif e.suffix == ".py":
                names.add(e.stem)
    return names - INFRA_ROOTS


def _bound_names(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, ast.alias):
            out.add((n.asname or n.name).split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
    return out


def _allowlist_census(tree: ast.AST, old_roots: set[str], extra_roots: set[str]) -> Counter:
    c: Counter = Counter()
    bound = _bound_names(tree)
    for n in ast.walk(tree):
        names: list[str] = []
        if isinstance(n, ast.Import):
            names = [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom) and not n.level and n.module:
            names = [f"{n.module}.{a.name}" if n.module in ("urllib",) else n.module for a in n.names]
        for full in names:
            root = full.split(".")[0]
            allowed = (root in old_roots or root in ALLOWED_IMPORT_ROOTS or root in extra_roots or full in ALLOWED_DOTTED
                       or any(full.startswith(a + ".") for a in ALLOWED_DOTTED))
            if not allowed:
                c[f"import of unlisted module {root}"] += 1
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and not n.id.startswith("_") and hasattr(builtins, n.id):
            obj = getattr(builtins, n.id)
            is_exception = isinstance(obj, type) and issubclass(obj, BaseException)
            if callable(obj) and n.id not in SAFE_BUILTINS and not is_exception and n.id not in bound:
                c[f"use of builtin {n.id}"] += 1
    return c


def _import_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            roots |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and not n.level and n.module:
            roots.add(n.module.split(".")[0])
    return roots


def _denied_module(name: str) -> str | None:
    if name in ALLOWED_DOTTED or any(name.startswith(a + ".") for a in ALLOWED_DOTTED):
        return None
    root = name.split(".")[0]
    return root if root in DENY_MODULES else None


def _root(node: ast.expr) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _census(tree: ast.AST) -> Counter:
    c: Counter = Counter()
    yaml_names = {"yaml"}  # `import yaml as y` makes `y` the same module
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yaml_names |= {a.asname for a in node.names if a.name.split(".")[0] == "yaml" and a.asname}
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module and node.module.split(".")[0] == "yaml":
            yaml_names |= {a.asname or a.name for a in node.names if a.name != "*"}  # `from yaml import loader` -> loader.Loader
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                d = _denied_module(a.name)
                if d:
                    c[f"import {d}"] += 1
        elif isinstance(node, ast.ImportFrom):
            if node.module and not node.level and node.module.split(".")[0] == "yaml":
                for a in node.names:
                    if a.name == "*":
                        c["star import of yaml (could pull in an unsafe loader)"] += 1
                    elif a.name in UNSAFE_YAML:
                        c[f"unsafe deserialization yaml.{a.name} (use yaml.SafeLoader, or decline)"] += 1
            if node.module == "operator":
                for a in node.names:
                    if a.name in DENY_NAMES_ANYWHERE:
                        c[f"operator.{a.name}"] += 1
            if node.module and not node.level:  # `from .http import X` is the repo's own module, not the stdlib one
                for a in node.names:  # check "module.name" so `from urllib import parse` is allowed, `request` is not
                    d = _denied_module(f"{node.module}.{a.name}")
                    if d:
                        c[f"import {d}"] += 1
        elif isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else None
            if name in DENY_CALLS:
                c[f"call {name}()"] += 1
        elif isinstance(node, ast.Attribute):
            if node.attr in DENY_ATTRS:
                c[f"attribute {node.attr}"] += 1
            if node.attr in UNSAFE_YAML and _root(node.value) in yaml_names:
                c[f"unsafe deserialization yaml.{node.attr} (use yaml.SafeLoader, or decline)"] += 1
            if node.attr in DENY_NAMES_ANYWHERE:
                c[f"operator.{node.attr}"] += 1
        elif isinstance(node, ast.Name):
            if node.id in DENY_REFS and isinstance(node.ctx, ast.Load):
                c[f"reference to {node.id}"] += 1
        elif isinstance(node, ast.Raise):
            c["raise statement"] += 1
    return c


def check_edit(old_text: str, new_text: str, allowed_roots: Iterable[str] = ()) -> tuple[bool, str]:
    try:
        new_tree = ast.parse(new_text)
    except SyntaxError as e:
        return False, f"edit leaves the file unparsable (line {e.lineno})"
    try:
        old_tree = ast.parse(old_text)
        old = _census(old_tree)
        old_extra = _allowlist_census(old_tree, _import_roots(old_tree), set())
        old_roots = _import_roots(old_tree)
    except SyntaxError:
        old, old_extra, old_roots = Counter(), Counter(), set()
    extra = set(allowed_roots)
    added = _census(new_tree) - old
    for key, n in (_allowlist_census(new_tree, old_roots, extra) - old_extra).items():  # deny by default: only listed things are new
        if key not in added:
            added[key] += n
    for kind, n in (scan_secrets(new_text) - scan_secrets(old_text)).items():  # a hard-coded credential in a "fix"
        added[f"secret-like string ({kind})"] += n
    if added:
        return False, "edit adds disallowed constructs: " + ", ".join(sorted(added))
    return True, ""
