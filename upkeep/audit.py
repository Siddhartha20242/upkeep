"""`upkeep audit`: for every pinned dependency in a repo, what would upgrading to the newest release break?

Reads ``name==version`` pins from requirements*.txt, asks PyPI for the latest release, and runs the radar between the
two source archives. No model, no execution of any library code. Dependencies it cannot analyse are LISTED with the
reason (no source archive, too big, package folder not found), never silently counted as green.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .pypi import PyPIError, latest_version, sources_for
from .radar import RadarInputError, run_radar

# import name differs from the PyPI name
MODULES = {"pyyaml": "yaml", "pillow": "PIL", "beautifulsoup4": "bs4", "scikit-learn": "sklearn", "opencv-python": "cv2",
           "python-dateutil": "dateutil", "python-dotenv": "dotenv", "pyjwt": "jwt", "attrs": "attr", "msgpack-python": "msgpack"}
PIN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*==\s*([0-9][A-Za-z0-9.+!_-]*)\s*(?:;.*)?(?:#.*)?$")
RISK_RANK = {"green": 0, "amber": 1, "red": 2}
# `many=False` becoming `many=None` and similar: a falsy default turned into "unset". Usually equivalent, so it is counted
# separately (still listed, never hidden).
EQUIVALENT_DEFAULT = re.compile(r"default (?:False|0|0\.0|''|\"\"|\[\]|\{\}|\(\)) -> None$")


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
    minor: int = 0            # of those, how many are a falsy default becoming None (usually equivalent)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _requirement_lines(f: Path, seen: set[Path], depth: int = 0) -> list[str]:
    """Logical requirement lines of a file: continuations joined, comments removed, `-r other.txt` followed (3 levels)."""
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    seen.add(f.resolve())
    out: list[str] = []
    for line in text.replace("\\\r\n", " ").replace("\\\n", " ").splitlines():  # `foo==1.0 \` + `--hash=...` is ONE requirement
        line = line.strip()
        m = re.match(r"^(?:-r|--requirement)\s*=?\s*(\S+)", line)
        if m and depth < 3:
            inc = (f.parent / m.group(1)).resolve()
            if inc.is_file() and inc not in seen:
                out += _requirement_lines(inc, seen, depth + 1)
            continue
        if line and not line.startswith(("#", "-")):
            out.append(re.sub(r"\s+--?[A-Za-z][\w-]*(?:[= ]\S+)?(?=\s|$)", "", line))  # drop trailing options such as --hash=sha256:...
    return out


def _pyproject_requirements(repo: Path) -> list[str]:
    """Dependencies declared in pyproject.toml ([project] dependencies and the test/tests/dev groups). Needs Python 3.11+."""
    try:
        import tomllib
        data = tomllib.loads((Path(repo) / "pyproject.toml").read_text(encoding="utf-8"))
    except (ImportError, OSError, ValueError):
        return []
    proj = data.get("project", {}) if isinstance(data, dict) else {}
    lines = list(proj.get("dependencies", []))
    groups = proj.get("optional-dependencies", {})
    for group in ("test", "tests", "dev"):
        lines += groups.get(group, []) if isinstance(groups, dict) else []
    return [x.strip() for x in lines if isinstance(x, str) and x.strip()]


def _all_requirements(repo: Path) -> list[str]:
    seen: set[Path] = set()
    files = sorted({*Path(repo).glob("requirements*.txt"), *Path(repo).glob("*/requirements*.txt")})
    return [ln for f in files if f.resolve() not in seen for ln in _requirement_lines(f, seen)] + _pyproject_requirements(repo)


def parse_pins(repo: Path) -> list[tuple[str, str]]:
    """Exact pins from requirements*.txt (top two folder levels). Ranges and unpinned names are not analysable."""
    pins: dict[str, str] = {}
    for line in _all_requirements(repo):
        m = PIN.match(line)
        if m:
            pins.setdefault(m.group(1).lower().replace("_", "-"), m.group(2))
    return sorted(pins.items())


def find_pin(repo: Path, dist: str, accept=lambda v: True, depth: int = 4) -> str | None:
    """The first exact pin of ``dist`` that ``accept`` likes, in any requirements file up to ``depth`` folders down."""
    want = re.sub(r"[-_.]+", "-", dist).lower()
    seen: set[Path] = set()
    root = Path(repo)
    for f in sorted(root.rglob("*.txt")):
        rel = f.relative_to(root).parts
        if len(rel) > depth or "requirements" not in f.name.lower() and not any("requirements" in x.lower() or x.lower() in ("reqs", "deps") for x in rel[:-1]):
            continue
        if any(x in ("node_modules", ".git", "site-packages") for x in rel):
            continue
        for line in _requirement_lines(f, seen):
            m = PIN.match(line)
            if m and re.sub(r"[-_.]+", "-", m.group(1)).lower() == want and accept(m.group(2)):
                return m.group(2)
    return None


def count_unpinned(repo: Path) -> int:
    """Requirements that are NOT exact pins (ranges, bare names, VCS urls): listed by the CLI, never analysed."""
    names = {m.group(1).lower() for ln in _all_requirements(repo) if not PIN.match(ln) and (m := re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", ln))}
    return len(names - {n for n, _ in parse_pins(repo)})


_VERSION = re.compile(
    r"^(?:(?P<epoch>\d+)!)?(?P<rel>\d+(?:\.\d+)*)(?:[-_.]?(?P<pre>a|alpha|b|beta|c|rc|pre|preview)[-_.]?(?P<pren>\d*))?"
    r"(?:[-_.]?(?:post|rev|r)[-_.]?(?P<post>\d*))?(?:[-_.]?(?P<dev>dev)[-_.]?(?P<devn>\d*))?(?:\+.*)?$", re.I)
_PRE = {"a": -3, "alpha": -3, "b": -2, "beta": -2, "c": -1, "rc": -1, "pre": -1, "preview": -1}


def _ver(v: str) -> tuple:
    """Sort key following PEP 440 closely enough for pins: 2.0.0rc1 < 2.0.0 < 2.0.0.post1, epochs and dev releases included."""
    m = _VERSION.match(v.strip())
    if not m:
        return (0, tuple(int(x) for x in re.findall(r"\d+", v)), 0, 0, 0, 1)
    rel = [int(x) for x in m["rel"].split(".")]
    while len(rel) > 1 and rel[-1] == 0:
        rel.pop()  # 1.0 == 1.0.0
    pre = _PRE[m["pre"].lower()] if m["pre"] else (-4 if m["dev"] and m.group("post") is None else 0)  # 1.0.dev1 < 1.0a1
    return (int(m["epoch"] or 0), tuple(rel), pre, int(m["pren"] or 0) if m["pre"] else 0,
            (int(m["post"] or 0) + 1) if m.group("post") is not None else 0, 0 if m["dev"] else 1)


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
        by_site: dict[tuple, list[bool]] = {}
        for c, sites in rep.affected:
            if c.kind == "default_changed":
                for s in sites:
                    by_site.setdefault((s.file, s.line, s.symbol), []).append(bool(EQUIVALENT_DEFAULT.search(c.detail)))
        # a site counts as "equivalent" only if EVERY default change at it is (one real change must never hide behind a harmless one)
        r.minor = sum(1 for x in r.flagged if x[3] == "default_changed" and all(by_site.get((x[0], x[1], x[2]), [False])))
        r.hard = len(r.flagged) - r.review
    return out


def _skip_kind(reason: str) -> str:
    r = reason.lower()
    for needle, label in (("no source archive", "no source archive"), ("limit", "archive too big"), ("not found", "package folder or version not found"),
                          ("sha-256", "download failed the hash check"), ("could not unpack", "archive could not be unpacked"),
                          ("download failed", "network error")):
        if needle in r:
            return label
    return "other"


def share_summary(results: list[DepResult], version: str, python: str) -> str:
    """A block a tester can paste back: counts, library names and versions only. No code, file names, paths or repo names."""
    done = [r for r in results if r.status == "checked"]
    skipped = Counter(_skip_kind(r.reason) for r in results if r.status == "skipped")
    lines = [f"upkeep {version}, python {python}", f"pins found: {len(results)}; analysed: {len(done)}; already latest: "
             f"{sum(1 for r in results if r.status == 'up-to-date')}; skipped: {sum(skipped.values())}"
             + (" (" + ", ".join(f"{k} x{v}" for k, v in sorted(skipped.items())) + ")" if skipped else "")]
    for r in done:
        if r.hard or r.review:
            lines.append(f"  {r.dist} {r.old} -> {r.new}: {r.hard} line(s) will fail, {r.review} default change(s) to review ({r.minor} look equivalent)")
    if not any(r.hard or r.review for r in done):
        lines.append("  no flagged lines in the analysed libraries")
    return "\n".join(lines)


def worst(results: list[DepResult]) -> str:
    return max((r.risk for r in results if r.status == "checked"), key=lambda k: RISK_RANK[k], default="green")
