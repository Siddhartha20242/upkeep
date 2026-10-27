"""Run the real agent (Muse Spark Open on Baseten) on small toy upgrades we wrote ourselves.

Fixture code is ours, so the local test runner is acceptable here. This is NOT the
gate test: toy cases are easy. It checks the plumbing end to end with a real model.

Run:  .venv/bin/python -I scripts/run_toy.py [case ...]
"""

import sys
import tempfile
import time
from pathlib import Path

from upkeep.agent import Agent
from upkeep.llm import Client, load_env
from upkeep.radar import run_radar
from upkeep.sandbox import run_tests

load_env(Path(__file__).resolve().parent.parent / ".env")


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


CASES = {
    # keyword-only argument, one call site
    "kwonly": dict(
        old="def fetch(url, timeout=10):\n    return 'ok:%s' % timeout\n",
        new="def fetch(url, *, timeout):\n    return 'ok:%s' % timeout\n",
        app="import lib\n\n\ndef go():\n    return lib.fetch('u', 5)\n",
        test="import app\n\n\ndef test_go():\n    assert app.go() == 'ok:5'\n",
    ),
    # removed function with a replacement, imported by name, two call sites
    "renamed": dict(
        old="def old_name(x):\n    return x * 2\n",
        new="def new_name(x):\n    return x * 2\n",
        app="from lib import old_name\n\n\ndef a(v):\n    return old_name(v)\n\n\ndef b(v):\n    return old_name(v) + 1\n",
        test="import app\n\n\ndef test_a():\n    assert app.a(2) == 4\n\n\ndef test_b():\n    assert app.b(2) == 5\n",
    ),
    # parameter reorder: positional arguments silently change meaning
    "reordered": dict(
        old="def area(width, height, unit='m'):\n    return '%s%s' % (width * height, unit)\n",
        new="def area(height, width, *, unit='m'):\n    return '%s%s|h=%s' % (width * height, unit, height)\n",
        app="import lib\n\n\ndef box():\n    return lib.area(3, 4, 'cm')\n",
        test="import app\n\n\ndef test_box():\n    assert app.box() == '12cm|h=4'\n",
    ),
}


def main() -> int:
    names = sys.argv[1:] or list(CASES)
    client = Client()
    for name in names:
        c = CASES[name]
        tmp = Path(tempfile.mkdtemp(prefix=f"toy-{name}-"))
        old, new, repo = tmp / "old", tmp / "new", tmp / "repo"
        write(old, "lib/__init__.py", c["old"])
        write(new, "lib/__init__.py", c["new"])
        write(repo, "app.py", c["app"])
        write(repo, "test_app.py", c["test"])

        report = run_radar(repo, old, new, "lib")
        runner = lambda w: run_tests(w, backend="local", allow_unsandboxed=True, extra_paths=[new], with_coverage=False)  # noqa: E731
        before = runner(repo)
        agent = Agent(client, repo, report, runner)
        t0 = time.monotonic()
        res = agent.run()
        print(f"\n=== {name}: tests before fix: {'pass' if before.ok else 'FAIL'}  ->  {res.status.upper()}  "
              f"({res.reason})")
        print(f"    {res.model_calls} model calls, {res.total_tokens} tokens, {time.monotonic() - t0:.1f}s, "
              f"guard rejections: {res.guard_rejections}")
        print("    tools:", " > ".join(s["tool"] for s in res.steps))
        print("    diff:\n" + "\n".join("      " + l for l in res.diff.splitlines()))
        agent.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
