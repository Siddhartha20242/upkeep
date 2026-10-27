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
from collections import Counter

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
UNSAFE_YAML = {"Loader", "UnsafeLoader", "CLoader", "CUnsafeLoader"}
DENY_ATTRS = {"__path__", "__code__", "__file__", "__dict__", "__globals__", "__builtins__",
              "__subclasses__", "__class__", "__bases__", "__mro__", "__closure__", "__module__"}


def _denied_module(name: str) -> str | None:
    if name in ALLOWED_DOTTED or any(name.startswith(a + ".") for a in ALLOWED_DOTTED):
        return None
    root = name.split(".")[0]
    return root if root in DENY_MODULES else None


def _census(tree: ast.AST) -> Counter:
    c: Counter = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                d = _denied_module(a.name)
                if d:
                    c[f"import {d}"] += 1
        elif isinstance(node, ast.ImportFrom):
            if node.module:
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
            if isinstance(node.value, ast.Name) and node.value.id == "yaml" and node.attr in UNSAFE_YAML:
                c[f"unsafe deserialization yaml.{node.attr} (use yaml.SafeLoader, or decline)"] += 1
        elif isinstance(node, ast.Raise):
            c["raise statement"] += 1
    return c


def check_edit(old_text: str, new_text: str) -> tuple[bool, str]:
    try:
        new_tree = ast.parse(new_text)
    except SyntaxError as e:
        return False, f"edit leaves the file unparsable (line {e.lineno})"
    try:
        old = _census(ast.parse(old_text))
    except SyntaxError:
        old = Counter()
    added = _census(new_tree) - old
    if added:
        return False, "edit adds disallowed constructs: " + ", ".join(sorted(added))
    return True, ""
