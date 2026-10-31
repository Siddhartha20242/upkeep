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
from typing import Any, Callable, Iterable, Protocol

from .scan import CallSite
from .secrets import redact

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
    required: int = -1            # how many leading positional parameters have no default (-1: all of them)
    kwonly: tuple[str, ...] = ()  # keyword-only parameter names
    varkw: bool = False           # accepts **kwargs

    @property
    def n_required(self) -> int:
        return len(self.params) if self.required < 0 else self.required


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
    coverage: dict[str, dict] = field(default_factory=dict)  # per function: lines the probes executed in the PATCHED code

    def summary(self) -> str:
        head = (f"behavior check: {self.inputs_checked} probe inputs over {self.functions_checked} function(s), "
                f"{len(self.mismatches)} mismatch(es)")
        lines = [head]
        for m in self.mismatches[:8]:
            lines.append(f"- {where(m)}{m['function']}({format_call_args(m['input'])}): "
                         f"old library -> {m['old']} ; new code -> {m['new']}")
        for name, c in self.coverage.items():
            lines.append(f"- probes executed {c['executed']} of {c['total']} lines of {name}()"
                         + (f" (not executed: lines {', '.join(map(str, c['missed'][:8]))})" if c["missed"] else ""))
        lines += [f"- skipped: {s}" for s in self.skipped[:5]]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"functions_checked": self.functions_checked, "inputs_checked": self.inputs_checked,
                "mismatches": self.mismatches, "skipped": self.skipped, "probed_sites": self.probed_sites,
                "random_inputs": self.random_inputs, "coverage": self.coverage}


def format_call_args(inp) -> str:
    """'1, "a", key=2' for a probe input: a positional list, or {"args": [...], "kwargs": {...}}."""
    if isinstance(inp, dict):
        return ", ".join([*(json.dumps(a) for a in inp.get("args", [])), *(f"{k}={json.dumps(v)}" for k, v in inp.get("kwargs", {}).items())])
    return ", ".join(json.dumps(a) for a in inp)


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
                                                   s.file, node.lineno, node.end_lineno or node.lineno,
                                                   required=len(params) - len(node.args.defaults),
                                                   kwonly=tuple(a.arg for a in node.args.kwonlyargs), varkw=node.args.kwarg is not None)
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


VALUE_HELP = ("Use JSON only (strings, numbers, booleans, null, lists, objects). Other Python types can be written as tagged objects: "
              'a tuple {"__tuple__": [...]}, a set {"__set__": [...]}, bytes {"__bytes__": "text"}, and {"__float__": "nan"} (or "inf", "-inf").')


def valid_input(target: Target, item) -> bool:
    """Is ``item`` a call the function's signature accepts? A positional list, or {"args": [...], "kwargs": {...}}."""
    if isinstance(item, list):
        return target.n_required <= len(item) <= len(target.params)
    if isinstance(item, dict) and set(item) <= {"args", "kwargs"}:
        args, kwargs = item.get("args", []), item.get("kwargs", {})
        if not isinstance(args, list) or not isinstance(kwargs, dict) or len(args) > len(target.params):
            return False
        names = set(target.params) | set(target.kwonly)
        if not target.varkw and not set(kwargs) <= names:
            return False
        provided = set(target.params[:len(args)]) | set(kwargs)
        return set(target.params[:target.n_required]) <= provided and not (set(kwargs) & set(target.params[:len(args)]))
    return False


def _ask(client: _Chat, target: Target, prompt: str, n: int, temperature: float, log: list[str] | None) -> tuple[list, int]:
    if log is not None:
        log.append(prompt)  # exactly what is sent, for the audit log written next to each run
    reply = client.chat([{"role": "user", "content": prompt}], max_tokens=4000, temperature=temperature)
    tokens = int((getattr(reply, "usage", None) or {}).get("total_tokens", 0))
    data = _extract_json_list(getattr(reply, "text", "") or "")
    return [item for item in (data or []) if valid_input(target, item)][:n], tokens


def _signature_help(target: Target) -> str:
    kw = f" Keyword-only parameters: {list(target.kwonly)}." if target.kwonly else ""
    return (f"Each call is either an array of positional arguments (parameters: {target.params}, at least {target.n_required} required)"
            f' or, when you need keyword arguments, an object {{"args": [...], "kwargs": {{...}}}}.{kw}')


def propose_inputs(client: _Chat, target: Target, n: int = 8, temperature: float = 0.0,
                   log: list[str] | None = None, secrets: Iterable[str] = ()) -> tuple[list, int]:
    prompt = (
        f"Here is a Python function from an application:\n\n{redact(target.source, secrets)[0]}\n\n"
        f"Propose up to {n} diverse calls to test it, as ONE JSON array. {_signature_help(target)} {VALUE_HELP} "
        "Include edge cases that matter for what it does: empty values, unicode, special characters, "
        "separators, None, and typical values. Reply with the JSON array only."
    )
    return _ask(client, target, prompt, n, temperature, log)


def propose_guided(client: _Chat, target: Target, annotated: str, n: int = 6, temperature: float = 0.0,
                   log: list[str] | None = None, secrets: Iterable[str] = ()) -> tuple[list, int]:
    """A second round: show the function with the lines that no earlier input executed, and ask for inputs that reach them."""
    prompt = (
        "Here is a Python function. Lines marked with '>>' were NOT executed by your earlier test inputs:\n\n"
        f"{redact(annotated, secrets)[0]}\n\n"
        f"Propose up to {n} more calls that would make those lines run, as ONE JSON array. {_signature_help(target)} {VALUE_HELP} "
        "Reply with the JSON array only."
    )
    return _ask(client, target, prompt, n, temperature, log)


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


def expand_inputs(inputs: list, max_total: int = 28) -> list:
    """The model's own probes first, then edge-case variants of the first few, varying ONE argument at a time.

    A model that only proposes happy-path inputs would make the oracle weak; this keeps the check from depending
    on the model's imagination alone (it is still not exhaustive)."""
    out: list[list] = []
    seen: set[str] = set()

    def add(args: Any) -> None:
        key = json.dumps(args, sort_keys=True, default=str)
        if key not in seen and len(out) < max_total:
            seen.add(key)
            out.append(args)

    for a in inputs:
        add(a)
    for base in inputs[:3]:
        if isinstance(base, dict):
            args, kw = list(base.get("args", [])), dict(base.get("kwargs", {}))
            for i, val in enumerate(args):
                for var in _variants(val):
                    add({"args": args[:i] + [var] + args[i + 1:], "kwargs": kw})
            for k, val in kw.items():
                for var in _variants(val):
                    add({"args": args, "kwargs": {**kw, k: var}})
            continue
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
    inputs = [i for i in inputs if isinstance(i, list)]  # random mutation works on positional calls
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


COVERAGE_TAG = "@@UPKEEP_COVERAGE@@"


def probe_script(target: Target, inputs: list) -> str:
    """A script that calls the function with every probe and prints the results (and which of its lines ran).

    Probe values are JSON. Tagged objects ({"__tuple__": [...]}, {"__set__": [...]}, {"__bytes__": "..."}, {"__float__": "nan"})
    are turned into the matching Python type by a tiny decoder that builds values and never evaluates anything."""
    return f"""
import dis, importlib, inspect, json, sys
sys.path.insert(0, ".")


def _dec(v):
    if isinstance(v, list):
        return [_dec(x) for x in v]
    if isinstance(v, dict):
        if len(v) == 1:
            (k, x), = v.items()
            if k == "__tuple__" and isinstance(x, list):
                return tuple(_dec(i) for i in x)
            if k == "__set__" and isinstance(x, list):
                return set(_dec(i) for i in x)
            if k == "__bytes__":
                return str(x).encode("latin-1", "replace")
            if k == "__float__" and x in ("nan", "inf", "-inf"):
                return float(x)
        return {{k: _dec(x) for k, x in v.items()}}
    return v


res = []
try:
    m = importlib.import_module({target.module!r})
    f = getattr(m, {target.name!r})
except BaseException as e:
    print({RESULT_TAG!r} + json.dumps({{"load_error": type(e).__name__}}))
    raise SystemExit(0)
try:
    _fc = inspect.unwrap(f).__code__
except BaseException:
    _fc = None
_seen = set()


def _tr(frame, event, arg):
    if frame.f_code is _fc:
        _seen.add(frame.f_lineno)

        def _local(fr, ev, a):
            if ev == "line":
                _seen.add(fr.f_lineno)
            return _local
        return _local
    return None


for item in json.loads({json.dumps(json.dumps(inputs))}):
    try:
        if isinstance(item, dict):
            a, k = _dec(item.get("args", [])), {{n: _dec(x) for n, x in item.get("kwargs", {{}}).items()}}
        else:
            a, k = _dec(item), {{}}
        sys.settrace(_tr)
        try:
            out = f(*a, **k)
        finally:
            sys.settrace(None)
        res.append({{"ok": repr(out)[:300]}})
    except BaseException as e:
        res.append({{"err": type(e).__name__}})
print({RESULT_TAG!r} + json.dumps(res))
if _fc is not None:
    print({COVERAGE_TAG!r} + json.dumps({{"executed": sorted(_seen), "total": sorted({{ln for _, ln in dis.findlinestarts(_fc)}})}}))
"""


def _parse_cov(out: str) -> dict | None:
    for line in out.splitlines():
        if line.startswith(COVERAGE_TAG):
            try:
                v = json.loads(line[len(COVERAGE_TAG):])
                return v if isinstance(v, dict) else None
            except ValueError:
                return None
    return None


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
                 run_old: Runner, run_new: Runner, n_inputs: int = 8, probe_samples: int = 2, n_random: int = 0,
                 secrets: Iterable[str] = ()):
        self.client, self.original, self.run_old, self.run_new, self.n = client, Path(original_repo), run_old, run_new, n_inputs
        self.probe_samples = max(1, probe_samples)  # measured: two samples catch 93% of a subtle wrong fix, one catches 71%
        self.n_random = max(0, n_random)  # 0 keeps the original fixed probe set (what the published benchmark numbers used)
        self.sites = list(sites)
        self.targets = find_targets(self.original, sites)
        self._inputs: dict[tuple[str, str], list[list]] = {}
        self._old: dict[tuple[str, str], list | None] = {}
        self._unstable: dict[tuple[str, str], set[int]] = {}
        self.tokens = 0
        self.prompts: list[str] = []
        self.secrets = set(secrets)
        self._expanded: dict[tuple[str, str], list] = {}
        self._guided: dict[tuple[str, str], int] = {}  # functions whose coverage-guided round was tried -> inputs it added

    def _run(self, t: Target, key: tuple[str, str], patched_repo: Path, inputs: list):
        """(old results, new results, new-world output, coverage of the patched function) for one batch of probes."""
        script = probe_script(t, inputs)
        if key not in self._old:  # the old world never changes, so run it twice, once
            ok, out = self.run_old(self.original, script)
            first = _parse(out) if ok else None
            ok2, out2 = self.run_old(self.original, script)
            second = _parse(out2) if ok2 else None
            self._old[key] = first
            self._unstable[key] = ({i for i, (a, b) in enumerate(zip(first, second)) if a != b}
                                   if isinstance(first, list) and isinstance(second, list) else set())
        ok, out = self.run_new(Path(patched_repo), script)
        return self._old[key], (_parse(out) if ok else None), out, (_parse_cov(out) if ok else None)

    def _annotate(self, t: Target, patched_repo: Path, missed: list[int]) -> str:
        """The patched function's source with the lines no probe executed marked with '>>'."""
        try:
            text = (Path(patched_repo) / t.file).read_text(encoding="utf-8")
            node = next(n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef) and n.name == t.name)
        except (OSError, SyntaxError, UnicodeDecodeError, StopIteration):
            return t.source
        lines = text.splitlines()
        miss = set(missed)
        return "\n".join(f"{'>>' if i in miss else '  '} {lines[i - 1]}" for i in range(node.lineno, (node.end_lineno or node.lineno) + 1))

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
                    got, used = propose_inputs(self.client, t, self.n, temperature=0.0 if i == 0 else 0.8, log=self.prompts, secrets=self.secrets)
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
            rep.random_inputs = max(rep.random_inputs, min(self.n_random, len(inputs)))
            at = [{"line": x.line, "symbol": x.symbol} for x in self.sites if x.file == t.file and t.start <= x.line <= t.end]
            loc = {"file": t.file, "sites": at[:3]}
            old, new, out, cov = self._run(t, key, patched_repo, inputs)
            missed = sorted(set(cov["total"]) - set(cov["executed"])) if cov else []
            if missed and key not in self._guided and isinstance(old, list) and isinstance(new, list):
                # evidence is only as strong as the lines it reached: ask once for inputs aimed at the lines that never ran
                self._guided[key] = 0
                extra, used = propose_guided(self.client, t, self._annotate(t, patched_repo, missed), temperature=0.0,
                                             log=self.prompts, secrets=self.secrets)
                self.tokens += used
                rep.tokens += used
                have = {json.dumps(a, sort_keys=True, default=str) for a in inputs}
                extra = [a for a in extra if json.dumps(a, sort_keys=True, default=str) not in have]
                if extra:
                    self._guided[key] = len(extra)
                    self._expanded[key] = inputs = inputs + extra
                    self._old.pop(key, None)
                    self._unstable.pop(key, None)
                    old, new, out, cov = self._run(t, key, patched_repo, inputs)
            if cov:
                miss = sorted(set(cov["total"]) - set(cov["executed"]))
                rep.coverage[t.name] = {"executed": len(cov["total"]) - len(miss), "total": len(cov["total"]), "missed": miss,
                                        "file": t.file, "guided_inputs": self._guided.get(key, 0)}
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
