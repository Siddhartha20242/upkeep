"""Scope guard: reject any patch that edits outside the allowed line ranges.

The model's output is never trusted. A unified diff is applied only if every
changed line of every file falls inside ranges derived from the scanned call
sites. This is enforced in code, not by prompting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .scan import CallSite

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    reason: str = ""


def allowed_ranges(sites: list[CallSite], margin: int = 0) -> dict[str, list[tuple[int, int]]]:
    ranges: dict[str, list[tuple[int, int]]] = {}
    for s in sites:
        ranges.setdefault(s.file, []).append((s.line - margin, s.end_line + margin))
    return ranges


def _strip_prefix(path: str) -> str:
    path = path.strip().split("\t")[0]
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def check_diff(diff: str, allowed: dict[str, list[tuple[int, int]]]) -> GuardResult:
    current: str | None = None
    old_line = 0
    touched_any = False
    for raw in diff.splitlines():
        if raw.startswith("--- "):
            continue
        if raw.startswith("+++ "):
            current = _strip_prefix(raw[4:])
            if current == "/dev/null" or current not in allowed:
                return GuardResult(False, f"file not in scope: {current}")
            continue
        m = _HUNK.match(raw)
        if m:
            old_line = int(m.group(1))
            continue
        if current is None:
            continue
        ranges = allowed[current]
        if raw.startswith("-"):
            touched_any = True
            if not any(lo <= old_line <= hi for lo, hi in ranges):
                return GuardResult(False, f"{current}:{old_line} is outside allowed ranges")
            old_line += 1
        elif raw.startswith("+"):
            touched_any = True
            anchor = max(old_line, 1)
            if not any(lo <= anchor <= hi or lo <= anchor - 1 <= hi for lo, hi in ranges):
                return GuardResult(False, f"insertion near {current}:{anchor} is out of scope")
        else:
            old_line += 1
    if not touched_any:
        return GuardResult(False, "diff changes nothing")
    return GuardResult(True)
