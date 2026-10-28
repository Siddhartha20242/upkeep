"""ToolCallBench: how reliably do the Muse Spark versions call tools?

Eight small tests that mirror what an agent needs: simple args, tricky strings (quotes, backslashes, newlines, unicode),
enums, nested arrays/objects, several calls in one turn, NOT calling a tool when none is needed, choosing the right tool
among several, and reproducing a long code block exactly. Each test runs several times per model at two temperatures.

Per trial it records: arguments are valid JSON, arguments satisfy the tool's schema, the answer is exactly what was asked
for (strict equality for strings), and latency/tokens. Nothing about Upkeep is involved: this measures the models.

Run:  .venv/bin/python -I scripts/toolcall_bench.py [--trials 5] [--models open,1.1,1.2,1.3]
Needs BASETEN_API_KEY (Muse Spark Open) and/or META_API_KEY (1.1, 1.2, 1.3). Costs a small number of tokens.
"""

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from upkeep.llm import LLMError, client_for, load_env

load_env(Path(__file__).resolve().parent.parent / ".env")

LONG = "\n".join([
    "def migrate(config):",
    "    \"\"\"Apply the 2.0 settings; keep the old defaults.\"\"\"",
    "    out = {}",
    "    for key, value in sorted(config.items()):",
    "        if key.startswith('_'):",
    "            continue  # private: skip",
    "        if isinstance(value, str):",
    "            value = value.strip().replace('\\t', '    ')",
    "        elif isinstance(value, (list, tuple)):",
    "            value = [v for v in value if v is not None]",
    "        out[key] = value",
    "    out.setdefault('timeout', 10)",
    "    out.setdefault('retries', 3)",
    "    if out.get('mode') == 'strict':",
    "        raise ValueError(\"strict mode is not supported: %r\" % out['mode'])",
    "    return {k: v for k, v in out.items() if v != ''}  # drop empties",
])
TRICKY = "x = \"a\\nb\"  # it's 100%\nif y:\n    return 'é'"


def tool(name, props, required, desc="tool"):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}


S, I = {"type": "string"}, {"type": "integer"}
REPLACE = tool("replace_lines", {"path": S, "start_line": I, "end_line": I, "new_text": S}, ["path", "start_line", "end_line", "new_text"], "Replace lines of a file.")
FINISH = tool("finish", {"status": {"type": "string", "enum": ["fixed", "decline"]}, "summary": S}, ["status", "summary"], "End the session.")
READ = tool("read_file", {"path": S}, ["path"], "Read a file.")
RUN = tool("run_tests", {}, [], "Run the test suite.")
LISTA = tool("list_affected", {}, [], "List affected sites.")
ADD = tool("add", {"a": I, "b": I}, ["a", "b"], "Add two integers.")
REPORT = tool("report", {"sites": {"type": "array", "items": {"type": "object", "properties": {"file": S, "line": I}, "required": ["file", "line"]}},
                         "risk": {"type": "string", "enum": ["green", "amber", "red"]}}, ["sites", "risk"], "Report affected sites.")


def check(schema_tool, args):
    """Tiny schema validator: required keys, basic types, enums, nested arrays/objects."""
    def ok(val, sch):
        t = sch.get("type")
        if t == "integer":
            return isinstance(val, int) and not isinstance(val, bool)
        if t == "string":
            return isinstance(val, str) and ("enum" not in sch or val in sch["enum"])
        if t == "array":
            return isinstance(val, list) and all(ok(v, sch.get("items", {})) for v in val)
        if t == "object":
            props = sch.get("properties", {})
            return (isinstance(val, dict) and all(k in val for k in sch.get("required", []))
                    and all(ok(v, props[k]) for k, v in val.items() if k in props))
        return True
    return ok(args, schema_tool["function"]["parameters"])


def calls_of(msg):
    out = []
    for c in msg.get("tool_calls") or []:
        try:
            args = json.loads(c["function"].get("arguments") or "{}")
        except ValueError:
            args = None
        out.append((c["function"]["name"], args))
    return out


def norm(s):
    return s.rstrip("\n") if isinstance(s, str) else s


# (name, prompt, tools, expectation(calls, text) -> bool)
TESTS = [
    ("simple_args", "Use the add tool to add 17 and 25.", [ADD, READ],
     lambda c, t: len(c) >= 1 and c[0][0] == "add" and c[0][1] == {"a": 17, "b": 25}),
    ("tricky_string", "Call replace_lines on app.py, lines 3 to 4, with new_text exactly equal to the text between the markers, "
                      f"nothing added or removed.\n<<<\n{TRICKY}\n>>>", [REPLACE, READ],
     lambda c, t: len(c) >= 1 and c[0][0] == "replace_lines" and isinstance(c[0][1], dict) and norm(c[0][1].get("new_text")) == TRICKY
     and c[0][1].get("path") == "app.py" and c[0][1].get("start_line") == 3 and c[0][1].get("end_line") == 4),
    ("enum", "The task cannot be completed safely. End the session with an honest summary.", [FINISH, READ],
     lambda c, t: len(c) >= 1 and c[0][0] == "finish" and isinstance(c[0][1], dict) and c[0][1].get("status") == "decline"),
    ("nested", "Report two affected sites: app.py line 5 and lib/util.py line 12. The risk is red.", [REPORT, READ],
     lambda c, t: len(c) >= 1 and c[0][0] == "report" and isinstance(c[0][1], dict) and c[0][1].get("risk") == "red"
     and sorted((s.get("file"), s.get("line")) for s in c[0][1].get("sites", []) if isinstance(s, dict)) == [("app.py", 5), ("lib/util.py", 12)]),
    ("parallel_in_one_turn", "Read the three files a.py, b.py and c.py. Call the tool once per file, in your next reply.", [READ, RUN],
     lambda c, t: sorted(a.get("path") for n, a in c if n == "read_file" and isinstance(a, dict)) == ["a.py", "b.py", "c.py"]),
    ("no_tool_needed", "What is two plus two? Answer in one word and do not use any tool.", [RUN, READ, LISTA],
     lambda c, t: len(c) == 0 and ("four" in (t or "").lower() or "4" in (t or ""))),
    ("pick_the_right_tool", "Run the test suite now.", [READ, RUN, LISTA, REPLACE, FINISH],
     lambda c, t: len(c) >= 1 and c[0][0] == "run_tests"),
    ("long_exact_code", "Call replace_lines on lib.py, lines 1 to 17, with new_text exactly equal to the code between the markers, "
                        f"character for character.\n<<<\n{LONG}\n>>>", [REPLACE],
     lambda c, t: len(c) >= 1 and c[0][0] == "replace_lines" and isinstance(c[0][1], dict) and norm(c[0][1].get("new_text")) == LONG),
]
ALL_TOOLS = {"add": ADD, "replace_lines": REPLACE, "finish": FINISH, "read_file": READ, "run_tests": RUN, "list_affected": LISTA, "report": REPORT}

MODELS = {"open": ("baseten", None, "Muse Spark Open (Baseten)"), "1.1": ("meta", "muse-spark-1.1", "muse-spark-1.1"),
          "1.2": ("meta", "muse-spark-1.2", "muse-spark-1.2"), "1.3": ("meta", "muse-spark-1.3", "muse-spark-1.3")}


def trial(client, test, temp):
    name, prompt, tools, expect = test
    t0 = time.monotonic()
    try:
        r = client.chat([{"role": "user", "content": prompt}], tools=tools, temperature=temp, max_tokens=4000)
    except LLMError as e:
        return {"test": name, "temp": temp, "error": str(e)[:120]}
    calls = calls_of(r.message)
    json_ok = all(a is not None for _, a in calls)
    schema_ok = json_ok and all(n in ALL_TOOLS and check(ALL_TOOLS[n], a) for n, a in calls) if calls else None
    try:
        exact = bool(expect(calls, r.text))
    except Exception:  # a malformed call (missing or wrong-typed argument) is a FAILURE to record, never a crash
        exact = False
    return {"test": name, "temp": temp, "json_ok": json_ok, "schema_ok": schema_ok, "n_calls": len(calls),
            "exact": exact, "seconds": round(time.monotonic() - t0, 2),
            "tokens": int((r.usage or {}).get("total_tokens", 0))}


def run_model(key, trials):
    provider, model, label = MODELS[key]
    client = client_for(provider, model)
    jobs = [(t, temp) for t in TESTS for temp in (0.0, 0.7) for _ in range(trials)]
    res = [trial(client, t, temp) for t, temp in jobs]
    return key, label, res


def main() -> int:
    args = sys.argv[1:]
    trials = int(args[args.index("--trials") + 1]) if "--trials" in args else 5
    keys = args[args.index("--models") + 1].split(",") if "--models" in args else list(MODELS)
    with ThreadPoolExecutor(max_workers=len(keys)) as ex:
        results = list(ex.map(lambda k: run_model(k, trials), keys))
    out = {k: {"label": label, "trials": res} for k, label, res in results}
    Path("eval_results").mkdir(exist_ok=True)
    Path("eval_results/toolcall_bench.json").write_text(json.dumps(out, indent=2))
    names = [t[0] for t in TESTS]
    print(f"{'test':22}" + "".join(f"{MODELS[k][2][:20]:>24}" for k in keys))
    for n in names:
        row = f"{n:22}"
        for k, _, res in results:
            rs = [r for r in res if r["test"] == n]
            good = sum(r.get("exact", False) for r in rs)
            row += f"{good:>10}/{len(rs):<3} exact".rjust(24)
        print(row)
    print()
    for k, label, res in results:
        ok = [r for r in res if "error" not in r]
        bad_json = sum(1 for r in ok if not r["json_ok"])
        bad_schema = sum(1 for r in ok if r["schema_ok"] is False)
        errs = len(res) - len(ok)
        print(f"{label:28} exact {sum(r['exact'] for r in ok)}/{len(res)}  invalid-JSON args {bad_json}  schema-invalid {bad_schema}  "
              f"request errors {errs}  mean {sum(r['seconds'] for r in ok) / max(1, len(ok)):.1f}s  tokens/call {sum(r['tokens'] for r in ok) // max(1, len(ok))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
