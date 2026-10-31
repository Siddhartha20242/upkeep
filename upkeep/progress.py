"""Live progress for `upkeep fix`: one readable line per agent step, printed as it happens.

The agent appends to ``agent.steps`` while it runs, so the CLI can watch that list from the main thread. Nothing here
changes agent behavior. Colors are used only on a real terminal (and never if NO_COLOR is set).
"""

from __future__ import annotations

import os
import sys
import threading
import time

C = {"red": "31", "green": "32", "yellow": "33", "dim": "2", "bold": "1", "cyan": "36"}


def paint(text: str, color: str, on: bool) -> str:
    return f"\033[{C[color]}m{text}\033[0m" if on else text


def use_color(stream=None) -> bool:
    stream = stream or sys.stdout
    return bool(getattr(stream, "isatty", lambda: False)()) and "NO_COLOR" not in os.environ


def format_step(step: dict, color: bool = False) -> str:
    tool, args, res = step.get("tool", "?"), step.get("args", {}) or {}, str(step.get("result", ""))
    first = res.splitlines()[0] if res else ""
    where = ""
    if "path" in args:
        where = f" {args['path']}" + (f":{args['start_line']}-{args['end_line']}" if "start_line" in args else "")
    p = lambda t, c: paint(t, c, color)  # noqa: E731
    if tool == "replace_lines":
        if first.startswith("ok"):
            return p("  edit      ", "cyan") + where.strip()
        return p("  REJECTED  ", "red") + first[:170]
    if tool == "run_tests":
        bad = "FAILED" in first
        return p("  tests     ", "red" if bad else "green") + first[:110]
    if tool == "check_behavior":
        bad = first.startswith("DIFFERENCES")
        return p("  behavior  ", "red" if bad else "green") + first[:110]
    if tool == "finish":
        bad = first.startswith("finish REJECTED")
        return p("  finish    ", "yellow" if bad else "dim") + (first[:110] if bad else str(args.get("status", "")))
    if tool == "read_release_notes":
        return p("  notes     ", "dim") + f"asked the release notes about {args.get('query') or 'the affected symbols'!r}"
    if tool == "read_file":
        return p("  read      ", "dim") + where.strip()
    if tool == "compare_old_new":
        return p("  explore   ", "dim") + "ran a snippet on the old and the new library"
    if tool == "library_source":
        return p("  library   ", "dim") + f"read the {args.get('version', '?')} source of {args.get('path', '?')}:{args.get('start_line', '?')}-{args.get('end_line', '?')}"
    if tool == "library_search":
        return p("  library   ", "dim") + f"searched the {args.get('version', '?')} source for {args.get('text', '')!r}"
    if tool == "list_affected":
        return p("  sites     ", "dim") + "listed the affected call sites"
    return p(f"  {tool:<9} ", "dim") + first[:100]


def run_with_progress(agent, out=None, interval: float = 0.25):
    """Run ``agent.run()`` in a worker thread and print each new step as it appears. Returns the AgentResult."""
    out = out or sys.stdout
    color, box, seen = use_color(out), {}, 0

    def work():
        try:
            box["res"] = agent.run()
        except BaseException as e:  # surfaced in the main thread
            box["err"] = e

    t = threading.Thread(target=work, daemon=True)
    t.start()
    while t.is_alive() or seen < len(agent.steps):
        while seen < len(agent.steps):
            print(format_step(agent.steps[seen], color), file=out, flush=True)
            seen += 1
        if t.is_alive():
            time.sleep(interval)
    t.join()
    if "err" in box:
        raise box["err"]
    return box["res"]
