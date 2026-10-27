"""First contact with the model: slug, plain call, tool calling, one tiny code fix.

Run:  .venv/bin/python scripts/smoke.py
Prints results and latency only; never prints the API key.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from upkeep.llm import LLMError, client_for, load_env  # noqa: E402

load_env(Path(__file__).resolve().parent.parent / ".env")


def step(name: str) -> None:
    print(f"\n== {name}")


def main() -> int:
    try:
        provider = sys.argv[1] if len(sys.argv) > 1 else "baseten"
        c = client_for(provider, sys.argv[2] if len(sys.argv) > 2 else None)
    except LLMError as e:
        print(f"setup problem: {e}")
        return 2
    print(f"model slug under test: {c.model}")

    step("1. models visible to this key")
    try:
        ids = c.list_models()
        hits = [i for i in ids if "muse" in i.lower() or "meta" in i.lower()]
        print(f"{len(ids)} models listed; muse/meta matches: {hits or 'none'}")
        print(f"model under test present in list: {c.model in ids}")
    except LLMError as e:
        print(f"could not list models (not fatal): {e}")

    step("2. plain call")
    try:
        r = c.chat([{"role": "user", "content": "Reply with exactly one word: pong"}], max_tokens=2000)
    except LLMError as e:
        print(f"FAILED: {e}")
        return 1
    print(f"reply: {r.text!r}  latency: {r.latency_s:.1f}s  usage: {r.usage}")
    print(f"finish_reason: {r.finish_reason!r}  truncated: {r.truncated}  reasoning chars: {len(r.reasoning)}")

    step("3. tool calling")
    tools = [{
        "type": "function",
        "function": {
            "name": "apply_fix",
            "description": "Replace one line of a file.",
            "parameters": {
                "type": "object",
                "properties": {"file": {"type": "string"}, "line": {"type": "integer"}, "new_text": {"type": "string"}},
                "required": ["file", "line", "new_text"],
            },
        },
    }]
    r = c.chat(
        [{"role": "user", "content": "Call apply_fix for file app.py, line 5, new_text \"x = 1\"."}],
        tools=tools, max_tokens=2000,
    )
    print(f"latency: {r.latency_s:.1f}s  tool_calls: {json.dumps(r.tool_calls)[:300] or 'none'}")
    print("TOOL CALLING WORKS" if r.tool_calls else "no tool call returned (reply text: %r)" % r.text[:200])

    step("4. tiny real task: fix a call after a breaking API change")
    prompt = (
        "A library changed `fetch(url, timeout=10)` to `fetch(url, *, timeout)`: timeout is now "
        "keyword-only and required.\nFix this line so it works with the new version. "
        "Reply with ONLY the corrected line.\n\nlib.fetch('https://x', 5)"
    )
    r = c.chat([{"role": "user", "content": prompt}], max_tokens=3000)
    print(f"latency: {r.latency_s:.1f}s  finish_reason: {r.finish_reason!r}  reasoning chars: {len(r.reasoning)}")
    print(f"reply: {r.text.strip()[:300]}")
    ok = "timeout=5" in r.text.replace(" ", "")
    print("CORRECT" if ok else "check this answer by hand")
    return 0


if __name__ == "__main__":
    sys.exit(main())
