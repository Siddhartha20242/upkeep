"""Behavior check against the OLD library (characterization by differential testing).

Passing tests only prove what the tests exercise. This check turns the old library
into an oracle for the functions the agent changed:

  1. find the module-level functions that contain an affected call site;
  2. ask a model for probe inputs (JSON literals only; never executed as code);
  3. run the ORIGINAL code on the OLD library and the PATCHED code on the NEW library
     with the same probes, in the sandbox;
  4. any difference in return value or exception type is a mismatch.

Limits (stated in the proof): only module-level functions are probed; probes are model-chosen,
so a clean result is evidence, not proof; functions whose arguments are not JSON-serialisable
are skipped; exception messages are ignored (types only).
"""

from __future__ import annotations

import ast
import hashlib
import json
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from .scan import CallSite

RESULT_TAG = "@@UPKEEP_RESULT@@"
Runner = Callable[[Path, str], tuple[bool, str]]


class _Chat(Protocol):
    def chat(self, messages: list[dict], **kw): ...


@dataclass
class Target:
    module: str
    name: str
    source: str
    params: list[str]
    file: str = ""
    start: int = 0
    end: int = 0


def where(m: dict) -> str:
    """'app/routing.py:42 (url_quote) ' for a mismatch, or '' when the call site is not known."""
    sites = m.get("sites") or []
    if not m.get("file") or not sites:
        return ""
    return f"{m['file']}:{sites[0]['line']} ({sites[0]['symbol']}) "


@dataclass
class BehaviorReport:
    functions_checked: int = 0
    inputs_checked: int = 0
    mismatches: list[dict] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    tokens: int = 0
    probed_sites: list[list] = field(default_factory=list)  # [file, line] of affected sites inside functions that were probed

    @property
    def ok(self) -> bool:
        return not self.mismatches

    random_inputs: int = 0  # seeded random probes per function added on top of the model's and the edge cases

    def summary(self) -> str:
        head = (f"behavior check: {self.inputs_checked} probe inputs over {self.functions_checked} function(s), "
                f"{len(self.mismatches)} mismatch(es)")
        lines = [head]
        for m in self.mismatches[:8]:
            lines.append(f"- {where(m)}{m['function']}({', '.join(json.dumps(a) for a in m['input'])}): "
                         f"old library -> {m['old']} ; new code -> {m['new']}")
        lines += [f"- skipped: {s}" for s in self.skipped[:5]]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"functions_checked": self.functions_checked, "inputs_checked": self.inputs_checked,
                "mismatches": self.mismatches, "skipped": self.skipped, "probed_sites": self.probed_sites,
                "random_inputs": self.random_inputs}


def find_targets(repo: Path, sites: list[CallSite]) -> list[Target]:
    seen: dict[tuple[str, str], Target] = {}
    for s in sites:
        path = repo / s.file
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.lineno <= s.line <= (node.end_lineno or node.lineno):
                module = ".".join(Path(s.file).with_suffix("").parts)
                params = [a.arg for a in node.args.posonlyargs + node.args.args]
                seen[(module, node.name)] = Target(module, node.name, ast.get_source_segment(text, node) or "", params,
                                                   s.file, node.lineno, node.end_lineno or node.lineno)
    return list(seen.values())


def _extract_json_list(text: str) -> list | None:
    for m in re.finditer(r"\[", text):
        try:
            val, _ = json.JSONDecoder().raw_decode(text[m.start():])
        except ValueError:
            continue
        if isinstance(val, list):
            return val
    return None


def propose_inputs(client: _Chat, target: Target, n: int = 8, temperature: float = 0.0) -> tuple[list[list], int]:
    prompt = (
        f"Here is a Python function from an application:\n\n{target.source}\n\n"
        f"Propose up to {n} diverse argument lists to test it, as ONE JSON array of arrays, one inner array per call "
        f"(positional arguments: {target.params}). Use only JSON literals (strings, numbers, booleans, null, lists, "
        "objects). Include edge cases that matter for what it does: empty values, unicode, special characters, "
        "separators, and typical values. Reply with the JSON array only."
    )
    reply = client.chat([{"role": "user", "content": prompt}], max_tokens=4000, temperature=temperature)
    tokens = int((getattr(reply, "usage", None) or {}).get("total_tokens", 0))
    data = _extract_json_list(getattr(reply, "text", "") or "")
    out: list[list] = []
    for item in data or []:
        if isinstance(item, list) and len(item) == len(target.params):
            out.append(item)
    return out[:n], tokens


EDGE_STR = ["", " ", "a b", "a/b:c", "ünï©ødé", "x" * 40, "!@#$%^&*()_+-=[]{}|;':\",./<>?", "line1\nline2", "%20", "+", "a=1&b=2"]
EDGE_INT = [0, 1, -1, 2 ** 31]


def _variants(value) -> list:
    """Deterministic edge cases of the same JSON type as ``value`` (the model's input is always kept first)."""
    if isinstance(value, bool):
        return [not value]
    if isinstance(value, str):
        return EDGE_STR
    if isinstance(value, int):
        return EDGE_INT
    if isinstance(value, float):
        return [0.0, -1.5, 1e10]
    if isinstance(value, list):
        return [[], value[:1], value + value[:1], [None], ["", " "]]
    if isinstance(value, dict):
        k = next(iter(value), "k")
        v = value.get(k, "v")
        return [{}, {**value, "extra": None}, {**value, "empty": ""}, {k: [v, v]}, {k: None}]
    return []


def expand_inputs(inputs: list[list], max_total: int = 28) -> list[list]:
    """The model's own probes first, then edge-case variants of the first few, varying ONE argument at a time.

    A model that only proposes happy-path inputs would make the oracle weak; this keeps the check from depending
    on the model's imagination alone (it is still not exhaustive)."""
    out: list[list] = []
    seen: set[str] = set()

    def add(args: list) -> None:
        key = json.dumps(args, sort_keys=True, default=str)
        if key not in seen and len(out) < max_total:
            seen.add(key)
            out.append(args)

    for a in inputs:
        add(a)
    for base in inputs[:3]:
        for i, val in enumerate(base):
            for var in _variants(val):
                add(base[:i] + [var] + base[i + 1:])
    return out


SPECIAL = list("/:?&=%+#@!~*'\"()[]{}<>\\;,.-_ \n\t") + ["ü", "é", "©", "日"]
WORD = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _mutate(rng: random.Random, value):
    """A random value of the same JSON type, often built from ``value`` (one character or element changed)."""
    if isinstance(value, bool):
        return rng.random() < 0.5
    if isinstance(value, str):
        if value and rng.random() < 0.6:  # tweak the model's own string: insert, replace or delete one character
            i = rng.randrange(len(value) + 1)
            op = rng.choice("ird")
            ch = rng.choice(SPECIAL if rng.random() < 0.7 else WORD)
            if op == "i" or i >= len(value):
                return value[:i] + ch + value[i:]
            return value[:i] + ch + value[i + 1:] if op == "r" else value[:i] + value[i + 1:]
        return "".join(rng.choice(SPECIAL if rng.random() < 0.4 else WORD) for _ in range(rng.randint(0, 16)))
    if isinstance(value, int):
        return rng.choice([rng.randint(-1000, 1000), rng.randint(-2 ** 33, 2 ** 33), 0, 1, -1])
    if isinstance(value, float):
        return round(rng.uniform(-1e6, 1e6), rng.randint(0, 6))
    if isinstance(value, list):
        out = [_mutate(rng, v) if rng.random() < 0.4 else v for v in value]
        if out and rng.random() < 0.3:
            out.pop(rng.randrange(len(out)))
        if rng.random() < 0.3:
            out.append(_mutate(rng, out[0] if out else ""))
        return out
    if isinstance(value, dict):
        d_out = {k: (_mutate(rng, v) if rng.random() < 0.4 else v) for k, v in value.items()}
        if d_out and rng.random() < 0.3:
            d_out.pop(rng.choice(list(d_out)))
        return d_out
    return value


def random_inputs(inputs: list[list], n: int, seed_text: str) -> list[list]:
    """Up to ``n`` seeded-random argument lists, derived from the model's probes (types follow the originals).

    Deterministic: the same function name and probes always give the same list, so a verdict can be reproduced.
    Standard library only. Still not exhaustive, and it cannot prove equivalence."""
    if n <= 0 or not inputs:
        return []
    rng = random.Random(int(hashlib.sha256(seed_text.encode()).hexdigest()[:12], 16))
    out: list[list] = []
    seen: set[str] = set()
    for _ in range(n * 20):
        if len(out) >= n:
            break
        base = list(rng.choice(inputs))
        idxs = [rng.randrange(len(base))] if len(base) == 1 or rng.random() < 0.7 else list(range(len(base)))
        for i in idxs:
            base[i] = _mutate(rng, base[i])
        key = json.dumps(base, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            out.append(base)
    return out


def probe_script(target: Target, inputs: list[list]) -> str:
    return f"""
import importlib, json, sys
sys.path.insert(0, ".")
res = []
try:
    m = importlib.import_module({target.module!r})
    f = getattr(m, {target.name!r})
except BaseException as e:
    print({RESULT_TAG!r} + json.dumps({{"load_error": type(e).__name__}}))
    raise SystemExit(0)
for args in json.loads({json.dumps(json.dumps(inputs))}):
    try:
        res.append({{"ok": repr(f(*args))[:300]}})
    except BaseException as e:
        res.append({{"err": type(e).__name__}})
print({RESULT_TAG!r} + json.dumps(res))
"""


def _parse(out: str):
    for line in out.splitlines():
        if line.startswith(RESULT_TAG):
            try:
                return json.loads(line[len(RESULT_TAG):])
            except ValueError:
                return None
    return None


class BehaviorChecker:
    def __init__(self, client: _Chat, original_repo: Path, sites: list[CallSite],
                 run_old: Runner, run_new: Runner, n_inputs: int = 8, probe_samples: int = 2, n_random: int = 0):
        self.client, self.original, self.run_old, self.run_new, self.n = client, Path(original_repo), run_old, run_new, n_inputs
        self.probe_samples = max(1, probe_samples)  # measured: two samples catch 93% of a subtle wrong fix, one catches 71%
        self.n_random = max(0, n_random)  # 0 keeps the original fixed probe set (what the published benchmark numbers used)
        self.sites = list(sites)
        self.targets = find_targets(self.original, sites)
        self._inputs: dict[tuple[str, str], list[list]] = {}
        self._old: dict[tuple[str, str], list | None] = {}
        self._unstable: dict[tuple[str, str], set[int]] = {}
        self.tokens = 0
        self._expanded: dict[tuple[str, str], list[list]] = {}

    def check(self, patched_repo: Path) -> BehaviorReport:
        rep = BehaviorReport()
        if not self.targets:
            rep.skipped.append("no module-level function contains an affected call site")
        for t in self.targets:
            key = (t.module, t.name)
            if key not in self._inputs:
                merged: list[list] = []
                seen_probe: set[str] = set()
                for i in range(self.probe_samples):  # first sample deterministic, later ones sampled for diversity
                    got, used = propose_inputs(self.client, t, self.n, temperature=0.0 if i == 0 else 0.8)
                    self.tokens += used
                    rep.tokens += used
                    for a in got:
                        k = json.dumps(a, sort_keys=True, default=str)
                        if k not in seen_probe:
                            seen_probe.add(k)
                            merged.append(a)
                self._inputs[key] = merged
            inputs = self._inputs[key]
            if not inputs:
                rep.skipped.append(f"{t.name}: no usable probe inputs proposed")
                continue
            if key not in self._expanded:
                base = expand_inputs(inputs)
                seen_e = {json.dumps(a, sort_keys=True, default=str) for a in base}
                self._expanded[key] = base + [a for a in random_inputs(inputs, self.n_random, f"{t.module}.{t.name}")
                                              if json.dumps(a, sort_keys=True, default=str) not in seen_e]
            inputs = self._expanded[key]
            script = probe_script(t, inputs)
            rep.random_inputs = max(rep.random_inputs, min(self.n_random, len(inputs)))
            at = [{"line": x.line, "symbol": x.symbol} for x in self.sites if x.file == t.file and t.start <= x.line <= t.end]
            loc = {"file": t.file, "sites": at[:3]}
            if key not in self._old:  # the old world never changes, so run it twice, once
                ok, out = self.run_old(self.original, script)
                first = _parse(out) if ok else None
                ok2, out2 = self.run_old(self.original, script)
                second = _parse(out2) if ok2 else None
                self._old[key] = first
                self._unstable[key] = ({i for i, (a, b) in enumerate(zip(first, second)) if a != b}
                                       if isinstance(first, list) and isinstance(second, list) else set())
            old = self._old[key]
            ok, out = self.run_new(Path(patched_repo), script)
            new = _parse(out) if ok else None
            if not isinstance(old, list) or not old or all("err" in r for r in old):
                rep.skipped.append(f"{t.name}: the old version does not run these probes (cannot be used as an oracle)")
                continue
            if not isinstance(new, list):
                rep.mismatches.append({"function": t.name, "input": [], "old": "ran", "new": f"did not run ({str(new or out)[:80]})", **loc})
                continue
            unstable = self._unstable.get(key, set())
            if unstable:
                rep.skipped.append(f"{t.name}: {len(unstable)} probe(s) ignored because the OLD library gave different "
                                   "results on two identical runs (non-deterministic)")
            rep.functions_checked += 1
            rep.inputs_checked += len(inputs) - len(unstable)
            rep.probed_sites += [[s.file, s.line] for s in self.sites
                                 if s.file == t.file and t.start <= s.line <= t.end and [s.file, s.line] not in rep.probed_sites]
            for idx, (args, o, n) in enumerate(zip(inputs, old, new)):
                if idx in unstable:
                    continue
                if o != n:
                    rep.mismatches.append({"function": t.name, "input": args, "old": json.dumps(o), "new": json.dumps(n), **loc})
        return rep
