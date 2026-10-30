"""Ground truth for the radar from Python itself: random function signatures, random mutations of them (the kinds of change
real libraries make), random call shapes that are valid for the OLD signature, and ``inspect.Signature.bind`` as the oracle
for "does this call still work on the new signature?".

What it measures (synthetic, so read it as a unit-level check, not as real-world accuracy):
  recall     of calls that stop binding, how many does the radar flag?  (a miss here is a bug in the radar)
  precision  of calls the radar flags for a *hard* change, how many really stop binding?
Reorders and default changes can matter without breaking binding, so they are reported separately.
"""

from __future__ import annotations

import ast
import copy
import inspect
import random
from collections import Counter
from dataclasses import dataclass

from upkeep.apidiff import Signature, _params_from_args, diff_api
from upkeep.radar import site_affected
from upkeep.scan import CallSite

NAMES = ["a", "b", "c", "d", "e", "f"]


@dataclass
class Spec:
    pos: list[tuple[str, bool]]      # (name, has_default) in order; defaults must trail
    kwonly: list[tuple[str, bool]]
    vararg: bool = False
    varkw: bool = False

    def source(self, default_of=lambda n: "1") -> str:
        parts = [f"{n}={default_of(n)}" if d else n for n, d in self.pos]
        if self.vararg:
            parts.append("*args")
        elif self.kwonly:
            parts.append("*")
        parts += [f"{n}={default_of(n)}" if d else n for n, d in self.kwonly]
        if self.varkw:
            parts.append("**kwargs")
        return f"def f({', '.join(parts)}): pass"

    def names(self) -> list[str]:
        return [n for n, _ in self.pos] + [n for n, _ in self.kwonly]


def random_spec(rng: random.Random) -> Spec:
    names = rng.sample(NAMES, rng.randint(0, 5))
    n_pos = rng.randint(0, len(names))
    n_def = rng.randint(0, n_pos)
    pos = [(n, i >= n_pos - n_def) for i, n in enumerate(names[:n_pos])]
    kwonly = [(n, rng.random() < 0.5) for n in names[n_pos:]]
    return Spec(pos, kwonly, rng.random() < 0.15, rng.random() < 0.2)


def _valid(spec: Spec) -> bool:
    seen_default = False
    for _, d in spec.pos:
        if seen_default and not d:
            return False
        seen_default = seen_default or d
    try:
        compile(spec.source(), "<spec>", "exec")
    except SyntaxError:
        return False
    return True


def mutate(rng: random.Random, spec: Spec) -> tuple[str, Spec] | None:
    """One realistic API change. Returns (kind of change, new spec) or None when it does not apply."""
    s = copy.deepcopy(spec)
    free = [n for n in NAMES if n not in s.names()]
    kind = rng.choice(["remove", "rename", "add_required", "add_optional", "drop_default", "add_default", "make_kwonly",
                       "reorder", "add_varargs", "remove_varargs", "add_varkw", "remove_varkw", "default_value", "kwonly_to_pos"])
    if kind == "remove" and s.names():
        n = rng.choice(s.names())
        s.pos = [(x, d) for x, d in s.pos if x != n]
        s.kwonly = [(x, d) for x, d in s.kwonly if x != n]
    elif kind == "rename" and s.names() and free:
        n, new = rng.choice(s.names()), rng.choice(free)
        s.pos = [(new if x == n else x, d) for x, d in s.pos]
        s.kwonly = [(new if x == n else x, d) for x, d in s.kwonly]
    elif kind == "add_required" and free:
        if rng.random() < 0.5 and not any(d for _, d in s.pos):
            s.pos.append((free[0], False))
        else:
            s.kwonly.append((free[0], False))
    elif kind == "add_optional" and free:
        (s.pos if rng.random() < 0.5 else s.kwonly).append((free[0], True))
    elif kind == "drop_default" and s.pos and s.pos[-1][1]:
        s.pos[-1] = (s.pos[-1][0], False)
    elif kind == "add_default" and s.pos and not s.pos[-1][1]:
        s.pos[-1] = (s.pos[-1][0], True)
    elif kind == "make_kwonly" and s.pos:
        s.kwonly.insert(0, s.pos.pop())
    elif kind == "kwonly_to_pos" and s.kwonly and not s.vararg and not any(d for _, d in s.pos if False):
        n, d = s.kwonly.pop(0)
        s.pos.append((n, d))
    elif kind == "reorder" and len(s.pos) >= 2:
        i = rng.randrange(len(s.pos) - 1)
        s.pos[i], s.pos[i + 1] = s.pos[i + 1], s.pos[i]
    elif kind == "add_varargs" and not s.vararg:
        s.vararg = True
    elif kind == "remove_varargs" and s.vararg:
        s.vararg = False
    elif kind == "add_varkw" and not s.varkw:
        s.varkw = True
    elif kind == "remove_varkw" and s.varkw:
        s.varkw = False
    elif kind == "default_value":
        pass  # same shape, different default (handled by the default_of function in make_pair)
    else:
        return None
    return (kind, s) if _valid(s) else None


def to_signature(spec: Spec, default_of=lambda n: "1") -> tuple[Signature, inspect.Signature]:
    src = spec.source(default_of)
    fn = ast.parse(src).body[0]
    ns: dict = {}
    exec(src, ns)  # a generated `def f(...): pass` of our own making: this is the oracle, not library code
    return Signature("lib.f", _params_from_args(fn.args)), inspect.signature(ns["f"])


def valid_calls(rng: random.Random, spec: Spec, isig: inspect.Signature, n: int) -> list[tuple[int, tuple[str, ...]]]:
    """Call shapes (positional count, keyword names) that bind on ``spec``."""
    out: set[tuple[int, tuple[str, ...]]] = set()
    kw_names = spec.names() + (["zz"] if spec.varkw else [])
    for _ in range(n * 8):
        n_pos = rng.randint(0, len(spec.pos) + (2 if spec.vararg else 0))
        kws = tuple(sorted(rng.sample(kw_names, rng.randint(0, len(kw_names))))) if kw_names else ()
        try:
            isig.bind(*range(n_pos), **{k: 0 for k in kws})
        except TypeError:
            continue
        out.add((n_pos, kws))
        if len(out) >= n:
            break
    return sorted(out)


def _binding(isig: inspect.Signature, n_pos: int, kws: tuple[str, ...]) -> dict[int, str] | None:
    """Which parameter NAME receives each passed value (values are unique ints); None if the call does not bind."""
    values = list(range(n_pos)) + [100 + i for i, _ in enumerate(kws)]
    try:
        ba = isig.bind(*values[:n_pos], **{k: v for k, v in zip(kws, values[n_pos:])})
    except TypeError:
        return None
    out: dict[int, str] = {}
    for name, val in ba.arguments.items():
        for v in (val if isinstance(val, (tuple, list)) else val.values() if isinstance(val, dict) else [val]):
            out[v] = name
    return out


def run(n_pairs: int = 400, seed: int = 7, calls_per_pair: int = 6) -> dict:
    rng = random.Random(seed)
    stats: Counter = Counter()
    missed: list[dict] = []
    false_alarms: list[dict] = []
    by_kind: dict[str, Counter] = {}
    done = 0
    while done < n_pairs:
        old = random_spec(rng)
        m = mutate(rng, old)
        if m is None:
            continue
        kind, new = m
        value_changed = kind == "default_value"
        old_sig, old_i = to_signature(old)
        new_sig, new_i = to_signature(new, (lambda n: "2") if value_changed else (lambda n: "1"))
        changes = diff_api({"lib.f": old_sig}, {"lib.f": new_sig})
        done += 1
        for n_pos, kws in valid_calls(rng, old, old_i, calls_per_pair):
            site = CallSite("a.py", 1, 1, "lib.f", "call", n_pos, kws, False)
            hard = [c for c in changes if c.kind != "default_changed" and site_affected(c, site, old_sig, new_sig)]
            soft = [c for c in changes if c.kind == "default_changed" and site_affected(c, site, old_sig, new_sig)]
            ob, nb = _binding(old_i, n_pos, kws), _binding(new_i, n_pos, kws)
            new_names, old_names = set(new.names()), set(old.names())
            # breaks = stops binding, or a value silently reaches a different, still-existing parameter
            # (a plain rename moves a value to a name that did not exist before: not a break)
            absorbed = ("args", "kwargs")  # swallowed by *args/**kwargs: a library may forward it, so this is NOT counted either way
            moved = [(v, name) for v, name in (ob or {}).items() if name not in absorbed and nb is not None and nb.get(v) != name]
            uncertain = nb is not None and any(nb.get(v) in absorbed for v, _ in moved)
            breaks = nb is None or any(nb.get(v) not in absorbed and (name in new_names or nb.get(v) in old_names) for v, name in moved)
            if uncertain and not breaks:
                stats["uncertain"] += 1
                continue
            k = by_kind.setdefault(kind, Counter())
            k["calls"] += 1
            stats["calls"] += 1
            if breaks:
                stats["breaks"] += 1
                k["breaks"] += 1
                if hard:
                    stats["caught"] += 1
                else:
                    stats["missed"] += 1
                    k["missed"] += 1
                    missed.append({"old": old.source(), "new": new.source(), "call": (n_pos, kws), "kind": kind})
            if hard:
                stats["flagged_hard"] += 1
                if not breaks:
                    stats["hard_not_breaking"] += 1
                    k["hard_not_breaking"] += 1
                    false_alarms.append({"old": old.source(), "new": new.source(), "call": (n_pos, kws), "kind": kind})
            if soft and not hard:
                stats["soft_only"] += 1
    for key in ("calls", "breaks", "caught", "missed", "flagged_hard", "hard_not_breaking", "soft_only", "uncertain"):
        stats[key] += 0
    stats["pairs"] = done
    return {"stats": dict(stats), "missed": missed, "false_alarms": false_alarms, "by_kind": {k: dict(v) for k, v in by_kind.items()}}
