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
    n_pos: int = 0                  # positional arguments passed (calls only)
    kwargs: tuple[str, ...] = ()    # keyword argument names passed
    star: bool = False              # call uses *args or **kwargs, so what it passes is unknown


class _Visitor(ast.NodeVisitor):
    """Resolves calls to library symbols. With ``classes`` it also resolves INSTANCE method calls for the common,
    local patterns (never across files, never through inheritance, never through reassignment):

      c = lib.Client(...); c.get(...)            local variable
      with lib.Client() as c: c.get(...)          context manager
      self.cli = lib.Client(...) ... self.cli.get(...)   attribute assigned anywhere in the same class
      def f(c: lib.Client): c.get(...)            annotated parameter
      client = lib.Client(...)  (module level) ... used inside functions
    A variable assigned two different things is treated as unknown (a deliberate false negative)."""

    def __init__(self, package: str, symbols: set[str], file: str, classes: set[str] | None = None):
        self.package = package
        self.symbols = symbols
        self.file = file
        self.classes = classes or set()
        self.aliases: dict[str, str] = {}  # local name -> dotted target
        self.sites: list[CallSite] = []
        self.module_vars: dict[str, str | None] = {}   # name -> class (None = ambiguous)
        self.scopes: list[dict[str, str | None]] = []  # function-local variables
        self.attr_types: list[dict[str, str | None]] = []  # per enclosing class: self.<attr> -> class

    # ---- instance typing helpers
    def _class_of_call(self, value: ast.expr) -> str | None:
        if isinstance(value, ast.Call):
            t = self._resolve(value.func)
            if t in self.classes:
                return t
        return None

    def _class_of_annotation(self, ann: ast.expr | None) -> str | None:
        if ann is None:
            return None
        t = self._resolve(ann)
        return t if t in self.classes else None

    def _bind(self, table: dict[str, str | None], name: str, cls: str | None) -> None:
        if name in table and table[name] != cls:
            table[name] = None  # assigned two different things: unknown
        else:
            table[name] = cls

    def _lookup(self, name: str) -> str | None:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return self.module_vars.get(name)

    def _instance_class(self, expr: ast.expr) -> str | None:
        if isinstance(expr, ast.Name):
            return self._lookup(expr.id)
        if (isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name) and expr.value.id == "self"
                and self.attr_types):
            return self.attr_types[-1].get(expr.attr)
        return None

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
        attrs: dict[str, str | None] = {}
        if self.classes:  # pre-scan: self.<attr> = lib.Class(...) anywhere in this class
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Assign, ast.AnnAssign)):
                    value = sub.value
                    targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
                    cls = self._class_of_call(value) if value is not None else None
                    if cls is None and isinstance(sub, ast.AnnAssign):
                        cls = self._class_of_annotation(sub.annotation)
                    for t in targets:
                        if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                            self._bind(attrs, t.attr, cls)
        self.attr_types.append(attrs)
        self.generic_visit(node)
        self.attr_types.pop()

    def _visit_func(self, node) -> None:
        for dec in node.decorator_list:
            self._reference(dec)
        scope: dict[str, str | None] = {}
        if self.classes:
            for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
                cls = self._class_of_annotation(a.annotation)
                if cls:
                    scope[a.arg] = cls
        self.scopes.append(scope)
        self.generic_visit(node)
        self.scopes.pop()

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

    def visit_Assign(self, node: ast.Assign) -> None:
        cls = self._class_of_call(node.value)
        for t in node.targets:
            if isinstance(t, ast.Name) and self.classes:
                self._bind(self.scopes[-1] if self.scopes else self.module_vars, t.id, cls)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        cls = self._class_of_call(node.value) if node.value else None
        cls = cls or self._class_of_annotation(node.annotation)
        if isinstance(node.target, ast.Name) and self.classes:
            self._bind(self.scopes[-1] if self.scopes else self.module_vars, node.target.id, cls)
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            cls = self._class_of_call(item.context_expr)
            if cls and isinstance(item.optional_vars, ast.Name):
                self._bind(self.scopes[-1] if self.scopes else self.module_vars, item.optional_vars.id, cls)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        target = self._resolve(node.func)
        if target is None and self.classes and isinstance(node.func, ast.Attribute):
            cls = self._instance_class(node.func.value)  # instance method call: x.method(...)
            if cls:
                target = f"{cls}.{node.func.attr}"
        if target in self.symbols:
            star = any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords)
            self.sites.append(
                CallSite(
                    self.file, node.lineno, node.end_lineno or node.lineno, target, "call",
                    n_pos=sum(not isinstance(a, ast.Starred) for a in node.args),
                    kwargs=tuple(k.arg for k in node.keywords if k.arg is not None),
                    star=star,
                )
            )
        self.generic_visit(node)


def find_call_sites(repo: Path, package: str, symbols: set[str], classes: set[str] | None = None) -> list[CallSite]:
    repo = Path(repo)
    out: list[CallSite] = []
    for file in sorted(repo.rglob("*.py")):
        if any(part in SKIP_DIRS for part in file.relative_to(repo).parts):
            continue
        try:
            tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        except (SyntaxError, UnicodeDecodeError):
            continue
        v = _Visitor(package, symbols, str(file.relative_to(repo)), classes)
        v.visit(tree)
        out.extend(v.sites)
    return out
