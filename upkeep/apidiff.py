"""Static API signature extraction and diffing.

Parses source trees with `ast` and never imports or executes the package, so
untrusted library code is never run.

Scope (v0): top-level public functions and classes (constructor taken from
``__init__``), plus simple relative re-exports in ``__init__.py``. Instance
method calls need type information and are out of scope.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Param:
    name: str
    kind: str  # posonly | pos | vararg | kwonly | varkw
    has_default: bool
    default: str | None = None  # source text of a LITERAL default (None if there is none or it is not a literal)


@dataclass(frozen=True)
class Signature:
    qualname: str  # canonical dotted name, e.g. "pkg.mod.func"
    params: tuple[Param, ...]
    is_class: bool = False
    is_method: bool = False  # a method of a public class (only extracted with include_methods=True)
    opaque: bool = False  # exists (exposed lazily through a module-level __getattr__) but its signature is unknown
    ctor_unknown: bool = False  # a class whose constructor is GENERATED from fields (@dataclass, attrs, pydantic ...): not comparable
    ctor_from: str = ""  # for a class that INHERITS its constructor: the class that defines it (one change, many subclasses)


@dataclass(frozen=True)
class Change:
    symbol: str
    kind: str
    detail: str
    breaking: bool = True
    param: str | None = None  # the parameter this change is about, if any


def _lit(node) -> str | None:
    if node is None:
        return None
    try:
        ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None
    return ast.unparse(node)


def _params_from_args(args: ast.arguments, skip_first: bool = False) -> tuple[Param, ...]:
    out: list[Param] = []
    positional = [(a, "posonly") for a in args.posonlyargs] + [(a, "pos") for a in args.args]
    n_defaults = len(args.defaults)
    first_default_idx = len(positional) - n_defaults
    for i, (a, kind) in enumerate(positional):
        has = i >= first_default_idx
        out.append(Param(a.arg, kind, has, _lit(args.defaults[i - first_default_idx]) if has else None))
    if args.vararg:
        out.append(Param(args.vararg.arg, "vararg", True))
    for a, d in zip(args.kwonlyargs, args.kw_defaults):
        out.append(Param(a.arg, "kwonly", d is not None, _lit(d)))
    if args.kwarg:
        out.append(Param(args.kwarg.arg, "varkw", True))
    if skip_first and out and out[0].kind in ("pos", "posonly"):
        out = out[1:]
    return tuple(out)


_NON_API_PARTS = {"test", "tests", "testing", "conftest"}


def _is_public(dotted: str) -> bool:
    parts = dotted.split(".")
    return not any(part.startswith("_") or part in _NON_API_PARTS for part in parts)


_GENERATED_DECORATORS = {"dataclass", "define", "frozen", "mutable", "attrs", "attributes", "s"}
_GENERATED_BASES = {"BaseModel", "BaseSettings", "NamedTuple", "TypedDict", "Struct"}


def _generates_constructor(cls: ast.ClassDef) -> bool:
    """@dataclass / attrs / pydantic-style classes have no `__init__` in their body: the signature comes from the fields."""
    for d in cls.decorator_list:
        d = d.func if isinstance(d, ast.Call) else d
        name = d.id if isinstance(d, ast.Name) else d.attr if isinstance(d, ast.Attribute) else None
        if name in _GENERATED_DECORATORS:
            return True
    return any(_base_name(b) in _GENERATED_BASES for b in cls.bases)


def _base_name(b: ast.expr) -> str | None:
    """Simple name of a base class expression: ``Base``, ``mod.Base`` and ``Base[T]`` (generic)."""
    e: ast.expr = b.value if isinstance(b, ast.Subscript) else b
    return e.id if isinstance(e, ast.Name) else e.attr if isinstance(e, ast.Attribute) else None


def _module_name(root: Path, file: Path) -> str:
    rel = file.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _method_sigs(cls: ast.ClassDef, qual: str) -> dict[str, Signature]:
    out: dict[str, Signature] = {}
    for item in cls.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) or item.name.startswith("_"):
            continue
        decos = {d.id for d in item.decorator_list if isinstance(d, ast.Name)}
        skip_first = "staticmethod" not in decos  # self / cls
        out[f"{qual}.{item.name}"] = Signature(f"{qual}.{item.name}", _params_from_args(item.args, skip_first=skip_first),
                                               False, True)
    return out


def extract_api(root: Path, package: str, include_methods: bool = False) -> dict[str, Signature]:
    """Return {public dotted name: Signature} for ``package`` under ``root``.

    ``include_methods`` also records public methods as ``pkg.mod.Class.method`` (used to MEASURE how much a
    static scan could miss; the call-site scanner itself cannot resolve instance calls)."""
    root = Path(root)
    pkg_dir = root / package
    api: dict[str, Signature] = {}
    reexports: list[tuple[str, str]] = []  # (public alias, canonical name)
    class_bases: dict[str, list[str]] = {}            # class qualname -> simple names of its bases
    class_methods: dict[str, dict[str, Signature]] = {}  # class qualname -> its OWN public methods (by name)
    imported: dict[str, dict[str, str]] = {}          # module -> {local name: canonical dotted name} from `from x import y [as z]`
    own_init: dict[str, tuple[Param, ...]] = {}       # class qualname -> parameters of its OWN __init__
    generated: set[str] = set()                       # classes whose constructor is generated from fields
    init_trees: list[tuple[str, ast.Module]] = []     # parsed __init__.py files, reused by the lazy-name pass

    for file in sorted(pkg_dir.rglob("*.py")):
        try:
            tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        except (SyntaxError, UnicodeDecodeError):
            continue
        module = _module_name(root, file)
        is_init = file.name == "__init__.py"
        if is_init:
            init_trees.append((module, tree))
        here = module if is_init else module.rsplit(".", 1)[0] if "." in module else ""
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.names:
                if node.level:
                    parts = here.split(".") if here else []
                    up = parts[: len(parts) - (node.level - 1)] if node.level - 1 <= len(parts) else []
                    src = ".".join([*up, *([node.module] if node.module else [])])
                else:
                    src = node.module or ""
                for a in node.names:
                    if a.name != "*" and src:
                        imported.setdefault(module, {})[a.asname or a.name] = f"{src}.{a.name}"

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{module}.{node.name}"
                if _is_public(qual):
                    api[qual] = Signature(qual, _params_from_args(node.args))
            elif isinstance(node, ast.ClassDef):
                qual = f"{module}.{node.name}"
                params: tuple[Param, ...] = ()
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                        params = _params_from_args(item.args, skip_first=True)
                        own_init[qual] = params
                if qual not in own_init and _generates_constructor(node):
                    generated.add(qual)
                if _is_public(qual):
                    api[qual] = Signature(qual, params, is_class=True, ctor_unknown=qual in generated)
                class_bases[qual] = [n for n in map(_base_name, node.bases) if n]
                if include_methods:  # private classes too: a public class inherits their public methods
                    class_methods[qual] = {k.rsplit(".", 1)[1]: v for k, v in _method_sigs(node, qual).items()}
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) \
                    and isinstance(node.value, (ast.Name, ast.Attribute)):
                # `NoResultFound = sa_exc.NoResultFound` / `Alias = imported_name`: the public name is an alias
                chain: list[str] = []
                v: ast.expr = node.value
                while isinstance(v, ast.Attribute):
                    chain.append(v.attr)
                    v = v.value
                if isinstance(v, ast.Name) and v.id in imported.get(module, {}):
                    reexports.append((f"{module}.{node.targets[0].id}", ".".join([imported[module][v.id], *reversed(chain)])))
            elif isinstance(node, ast.ImportFrom):
                # `from .x import Y [as Z]` or `from pkg.x import Y` inside the package: Y is also public as this module's Z
                for alias in node.names:
                    canonical = imported.get(module, {}).get(alias.asname or alias.name)
                    if alias.name != "*" and canonical and canonical.startswith(package + "."):
                        reexports.append((f"{module}.{alias.asname or alias.name}", canonical))

    by_simple: dict[str, list[str]] = {}
    for q in class_bases:
        by_simple.setdefault(q.rsplit(".", 1)[1], []).append(q)

    def base_quals(q: str) -> list[str]:
        """The classes in this package that ``q`` lists as bases (follows `from .x import Base as Alias` exactly)."""
        out: list[str] = []
        mod = q.rsplit(".", 1)[0]
        for base in class_bases.get(q, []):
            via_import = imported.get(mod, {}).get(base)
            cands = [via_import] if via_import in class_bases else by_simple.get(base, [])
            same = [c for c in cands if c.rsplit(".", 1)[0] == mod]  # prefer the same module, else the first match
            out += [c for c in (same or cands)[:1] if c != q]
        return out

    def inherited_init(q: str, seen: tuple[str, ...] = ()) -> tuple[tuple[Param, ...], str] | str | None:
        for b in base_quals(q):
            if b in seen:
                continue
            if b in own_init:
                return own_init[b], b
            if b in generated:
                return "unknown"  # the nearest constructor is generated from fields: do not pretend to know it
            got = inherited_init(b, seen + (q,))
            if got is not None:
                return got
        return None

    for q in class_bases:  # a class without its own __init__ takes the constructor of its nearest base that has one
        if q not in own_init and q not in generated and q in api:
            got = inherited_init(q)
            if got == "unknown":
                api[q] = Signature(q, (), is_class=True, ctor_unknown=True)
            elif got is not None and not isinstance(got, str):
                api[q] = Signature(q, got[0], is_class=True, ctor_from=got[1])

    if include_methods:
        def effective(q: str, seen: tuple[str, ...] = ()) -> dict[str, Signature]:
            """Own methods plus inherited ones from base classes defined in this package (nearest wins)."""
            out: dict[str, Signature] = {}
            for c in base_quals(q):
                if c not in seen:
                    out.update(effective(c, seen + (q,)))
            out.update(class_methods.get(q, {}))
            return out

        for q in class_methods:
            if not _is_public(q):
                continue
            for name, sig in effective(q).items():
                api[f"{q}.{name}"] = Signature(f"{q}.{name}", sig.params, False, True)

    for module, tree in init_trees:
        if any(isinstance(n, ast.FunctionDef) and n.name == "__getattr__" for n in tree.body):
            # lazily exposed names (e.g. pydantic 2) appear only as string constants
            for c in ast.walk(tree):
                if isinstance(c, ast.Constant) and isinstance(c.value, str) and c.value.isidentifier():
                    qual = f"{module}.{c.value}"
                    if qual not in api and _is_public(qual):
                        api[qual] = Signature(qual, (), opaque=True)

    by_prefix: dict[str, list[str]] = {}  # every dotted prefix -> the api keys below it (so an alias copies a subtree fast)

    def index(key: str) -> None:
        parts = key.split(".")
        for i in range(1, len(parts)):
            by_prefix.setdefault(".".join(parts[:i]), []).append(key)

    if include_methods:
        for k in api:
            index(k)
    for sweep in range(6):  # re-exports of re-exports (a -> b -> c): repeat until nothing new appears
        grew = False
        for public, canonical in reexports:
            if canonical in api and _is_public(public) and (sweep == 0 or public not in api):
                sig = api[canonical]
                fresh = public not in api
                api[public] = Signature(sig.qualname, sig.params, sig.is_class, sig.is_method, sig.opaque, sig.ctor_unknown, sig.ctor_from)
                grew = grew or sweep > 0
                if include_methods:
                    if fresh:
                        index(public)
                    if sig.is_class:  # `from . import array as arraylib` also makes every name under `array.` reachable as `arraylib.`
                        for k, m in [(k, api[k]) for k in by_prefix.get(canonical, [])]:
                            nk = public + k[len(canonical):]
                            was_new = nk not in api
                            api[nk] = m
                            if was_new:
                                index(nk)
        if not grew and sweep > 0:
            break
    return api


def format_signature(sig: Signature) -> str:
    """Human/model-readable signature, e.g. ``area(height, width, *, unit=...)``."""
    parts: list[str] = []
    saw_star = False
    for p in sig.params:
        if p.kind == "vararg":
            parts.append(f"*{p.name}")
            saw_star = True
        elif p.kind == "varkw":
            parts.append(f"**{p.name}")
        else:
            if p.kind == "kwonly" and not saw_star:
                parts.append("*")
                saw_star = True
            parts.append(f"{p.name}=..." if p.has_default else p.name)
    return f"{sig.qualname}({', '.join(parts)})"


def _positional_names(sig: Signature) -> list[str]:
    return [p.name for p in sig.params if p.kind in ("posonly", "pos")]


def diff_api(old: dict[str, Signature], new: dict[str, Signature]) -> list[Change]:
    """Breaking changes between two extracted APIs (symbols present in ``old``)."""
    changes: list[Change] = []
    for name, o in sorted(old.items()):
        if o.opaque:
            continue  # a name we only know from a string constant: not evidence of a real old API
        n = new.get(name)
        if n is None:
            changes.append(Change(name, "removed", "symbol no longer exists"))
            continue
        if n.opaque or o.ctor_unknown or n.ctor_unknown:
            continue  # lazily exposed, or a constructor generated from fields: it exists, but a static scan cannot compare it
        old_by = {p.name: p for p in o.params}
        new_by = {p.name: p for p in n.params}
        new_has_varkw = any(p.kind == "varkw" for p in n.params)

        for pname, op in old_by.items():
            if op.kind in ("vararg", "varkw"):
                if pname not in new_by and not any(p.kind == op.kind for p in n.params):
                    changes.append(Change(name, "param_removed", f"*{pname} removed", param=pname))
                continue
            np_ = new_by.get(pname)
            if np_ is None:
                if op.kind == "kwonly" and new_has_varkw:
                    continue
                changes.append(Change(name, "param_removed", f"parameter '{pname}' removed", param=pname))
                continue
            if op.kind != np_.kind:
                changes.append(
                    Change(name, "param_kind_changed", f"'{pname}': {op.kind} -> {np_.kind}", param=pname)
                )
            if op.has_default and not np_.has_default:
                changes.append(
                    Change(name, "param_became_required", f"'{pname}' lost its default", param=pname)
                )
            if op.has_default and np_.has_default and op.default is not None and np_.default is not None and op.default != np_.default:
                # a behavior change a signature CAN show: callers that omit the argument now get a different value
                changes.append(Change(name, "default_changed", f"'{pname}' default {op.default} -> {np_.default}",
                                      breaking=False, param=pname))

        for pname, np_ in new_by.items():
            if pname not in old_by and not np_.has_default and np_.kind not in ("vararg", "varkw"):
                changes.append(
                    Change(name, "param_added_required", f"new required parameter '{pname}'", param=pname)
                )

        old_pos = [p for p in _positional_names(o) if p in new_by]
        new_pos = [p for p in _positional_names(n) if p in old_by]
        if old_pos != new_pos and set(old_pos) == set(new_pos):
            changes.append(
                Change(name, "params_reordered", f"positional order {old_pos} -> {new_pos}")
            )
    return changes
