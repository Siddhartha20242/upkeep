"""`upkeep audit`: for every pinned dependency in a repo, what would upgrading to the newest release break?

Reads ``name==version`` pins from requirements*.txt, asks PyPI for the latest release, and runs the radar between the
two source archives. No model, no execution of any library code. Dependencies it cannot analyse are LISTED with the
reason (no source archive, too big, package folder not found), never silently counted as green.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .pypi import PyPIError, latest_version, sources_for
from .radar import RadarInputError, run_radar

# import name differs from the PyPI name
MODULES = {"pyyaml": "yaml", "pillow": "PIL", "beautifulsoup4": "bs4", "scikit-learn": "sklearn", "opencv-python": "cv2",
           "python-dateutil": "dateutil", "python-dotenv": "dotenv", "pyjwt": "jwt", "attrs": "attr", "msgpack-python": "msgpack"}
PIN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*==\s*([0-9][A-Za-z0-9.+!_-]*)\s*(?:;.*)?(?:#.*)?$")
RISK_RANK = {"green": 0, "amber": 1, "red": 2}


@dataclass
class DepResult:
    dist: str
    old: str
    new: str = ""
    status: str = ""          # checked | up-to-date | skipped
    reason: str = ""
    risk: str = "green"
    flagged: list[list] = field(default_factory=list)   # [file, line, symbol, kind]
    n_changes: int = 0
    hard: int = 0             # flagged lines that will fail or misbehave (removed names, removed/required/reordered parameters)
    review: int = 0           # flagged lines where only a default value changed: behavior MAY differ, often benign

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def parse_pins(repo: Path) -> list[tuple[str, str]]:
    """Exact pins from requirements*.txt (top two folder levels). Ranges and unpinned names are not analysable."""
    pins: dict[str, str] = {}
    files = sorted({*Path(repo).glob("requirements*.txt"), *Path(repo).glob("*/requirements*.txt")})
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            m = PIN.match(line)
            if m:
                pins.setdefault(m.group(1).lower().replace("_", "-"), m.group(2))
    return sorted(pins.items())


def _ver(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v.split("+")[0])[:4])


def audit(repo: Path, max_mb: int = 15, only: set[str] | None = None, limit: int | None = None) -> list[DepResult]:
    out: list[DepResult] = []
    for dist, old in parse_pins(repo):
        if only and dist not in only:
            continue
        if limit is not None and len(out) >= limit:
            break
        r = DepResult(dist, old)
        out.append(r)
        try:
            r.new = latest_version(dist)
            if _ver(r.new) <= _ver(old):
                r.status = "up-to-date"
                continue
            old_src, new_src, module = sources_for(dist, old, r.new, MODULES.get(dist), max_bytes=max_mb * 1_000_000)
            rep = run_radar(Path(repo), old_src, new_src, module)
        except (PyPIError, RadarInputError) as e:
            r.status, r.reason = "skipped", str(e)[:140]
            continue
        r.status, r.risk, r.n_changes = "checked", rep.risk, len(rep.changes)
        found = sorted({(s.file, s.line, s.symbol, c.kind) for c, sites in rep.affected for s in sites})
        r.flagged = [list(x) for x in found]
        r.review = sum(1 for x in r.flagged if x[3] == "default_changed")
        r.hard = len(r.flagged) - r.review
    return out


def worst(results: list[DepResult]) -> str:
    return max((r.risk for r in results if r.status == "checked"), key=lambda k: RISK_RANK[k], default="green")
