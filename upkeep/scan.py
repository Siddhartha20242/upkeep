"""Find uses of library symbols in a repository (static, AST only).

Reports calls, ``from pkg import name`` imports, and decorator / base-class
references. Resolves ``import pkg``, ``import pkg as p``, ``from pkg import f
as g`` and dotted attribute chains. Calls on instances (``obj.method()``)
cannot be resolved without type information and are not reported.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", "build", "dist"}


@dataclass(frozen=True)
class CallSite:
    """A place in the repo that touches a library symbol.

    kind: "call" (invoked), "import" (``from pkg import name``: a removed name
    fails here, at import time), or "reference" (decorator or base class).
    """

    file: str
    line: int
    end_line: int
    symbol: str
    kind: str = "call"


class _Visitor(ast.NodeVisitor):
    def __init__(self, package: str, symbols: set[str], file: str):
        self.package = package
        self.symbols = symbols
        self.file = file
        self.aliases: dict[str, str] = {}  # local name -> dotted target
        self.sites: list[CallSite] = []

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            if a.name == self.package or a.name.startswith(self.package + "."):
                local = a.asname or a.name.split(".")[0]
                self.aliases[local] = a.name if a.asname else a.name.split(".")[0]
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level == 0 and node.module and (
            node.module == self.package or node.module.startswith(self.package + ".")
        ):
            for a in node.names:
                target = f"{node.module}.{a.name}"
                self.aliases[a.asname or a.name] = target
                if target in self.symbols:
                    self.sites.append(
                        CallSite(self.file, node.lineno, node.end_lineno or node.lineno, target, "import")
                    )
        self.generic_visit(node)

    def _reference(self, expr: ast.expr) -> None:
        if isinstance(expr, ast.Call):  # a decorator like @pkg.f(...) is already seen as a call
            return
        target = self._resolve(expr)
        if target in self.symbols:
            self.sites.append(
                CallSite(self.file, expr.lineno, expr.end_lineno or expr.lineno, target, "reference")
            )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for base in node.bases:
            self._reference(base)
        for dec in node.decorator_list:
            self._reference(dec)
        self.generic_visit(node)

    def _visit_func(self, node) -> None:
        for dec in node.decorator_list:
            self._reference(dec)
        self.generic_visit(node)

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    def _resolve(self, func: ast.expr) -> str | None:
        parts: list[str] = []
        cur = func
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if not isinstance(cur, ast.Name) or cur.id not in self.aliases:
            return None
        return ".".join([self.aliases[cur.id], *reversed(parts)])

    def visit_Call(self, node: ast.Call) -> None:
        target = self._resolve(node.func)
        if target in self.symbols:
            self.sites.append(
                CallSite(self.file, node.lineno, node.end_lineno or node.lineno, target)
            )
        self.generic_visit(node)


def find_call_sites(repo: Path, package: str, symbols: set[str]) -> list[CallSite]:
    repo = Path(repo)
    out: list[CallSite] = []
    for file in sorted(repo.rglob("*.py")):
        if any(part in SKIP_DIRS for part in file.relative_to(repo).parts):
            continue
        try:
            tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        except (SyntaxError, UnicodeDecodeError):
            continue
        v = _Visitor(package, symbols, str(file.relative_to(repo)))
        v.visit(tree)
        out.extend(v.sites)
    return out
