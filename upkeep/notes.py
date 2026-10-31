"""Release-note lines that mention names your code uses.

The radar compares signatures, so it cannot see a function that keeps its signature and behaves differently. Release notes
often say so in words. This module lists the lines of the newest release's notes that mention a name the repo imports or calls
from the library. It is a text match: it does not decide anything, never changes a risk level or an exit code, and a line it
lists may be irrelevant while a relevant change may not be written down at all.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

MIN_NAME = 4        # shorter names (`dump`, `load` are fine; `to`, `id`, `os`) match too much prose
MAX_LINES = 8


def used_names(repo: Path, module: str, limit: int = 3000) -> set[str]:
    """Names taken from ``module`` anywhere in the repo: `from module import a`, `module.a(...)`, `import module as m; m.a`."""
    root = module.split(".")[0]
    names: set[str] = set()
    for i, f in enumerate(sorted(Path(repo).rglob("*.py"))):
        if i >= limit or f.stat().st_size > 400_000 or any(p in {".venv", "venv", "site-packages", "node_modules"} for p in f.parts):
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError, OSError):
            continue
        aliases: set[str] = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                aliases |= {a.asname or a.name.split(".")[0] for a in n.names if a.name.split(".")[0] == root}
            elif isinstance(n, ast.ImportFrom) and not n.level and n.module and n.module.split(".")[0] == root:
                names |= {a.name for a in n.names if a.name != "*"}
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in aliases:
                names.add(n.attr)
    return {x for x in names if len(x) >= MIN_NAME and not x.startswith("__")}


def mentions(notes: str, names: set[str]) -> list[list[str]]:
    """[[name, line], ...]: note lines that contain a used name as a whole word (first match per line, at most MAX_LINES)."""
    if not names:
        return []
    pattern = re.compile(r"(?<!\w)(" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")(?![\w])")
    out: list[list[str]] = []
    for line in notes.splitlines():
        text = line.strip()
        m = pattern.search(text)
        if m and len(text) > len(m.group(1)) + 8:  # skip bare headings that are just a name
            out.append([m.group(1), text[:160]])
            if len(out) >= MAX_LINES:
                break
    return out
