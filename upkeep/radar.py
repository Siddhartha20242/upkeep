"""Upgrade Radar: what would upgrading a dependency break in this repo?

No model involved. Combines the static API diff with a scan of the repo's
call sites and (optionally) line coverage from the repo's own tests.

Risk levels:

  green - no breaking change touches a symbol the repo calls
  amber - breaking changes hit the repo, and every affected call site is
          covered by existing tests, so a fix can be verified by those tests
  red   - breaking changes hit the repo and at least one affected call site is
          NOT covered (or coverage is unknown), so existing tests cannot verify
          a fix; needs characterization tests or a human
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .apidiff import Change, diff_api, extract_api, format_signature
from .sandbox import site_is_covered
from .scan import CallSite, find_call_sites


@dataclass
class RadarReport:
    package: str
    risk: str
    changes: list[Change] = field(default_factory=list)
    affected: list[tuple[Change, list[CallSite]]] = field(default_factory=list)
    uncovered: list[CallSite] = field(default_factory=list)
    coverage_known: bool = False
    signatures: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    limitations: list[str] = field(
        default_factory=lambda: [
            "Only module-level functions and class constructors are analysed.",
            "Instance method calls and dynamic calls are not resolved.",
        ]
    )

    def to_dict(self) -> dict:
        return {
            "package": self.package,
            "risk": self.risk,
            "coverage_known": self.coverage_known,
            "breaking_changes": [c.__dict__ for c in self.changes],
            "affected": [
                {"change": c.__dict__, "call_sites": [s.__dict__ for s in sites]}
                for c, sites in self.affected
            ],
            "uncovered_call_sites": [s.__dict__ for s in self.uncovered],
            "signatures": {k: {"old": v[0], "new": v[1]} for k, v in self.signatures.items()},
            "limitations": self.limitations,
        }


def run_radar(
    repo: Path,
    old_root: Path,
    new_root: Path,
    package: str,
    executed_lines: dict[str, set[int]] | None = None,
) -> RadarReport:
    old_api = extract_api(old_root, package)
    new_api = extract_api(new_root, package)
    changes = diff_api(old_api, new_api)

    symbols = {c.symbol for c in changes}
    sites = find_call_sites(repo, package, symbols) if symbols else []
    by_symbol: dict[str, list[CallSite]] = {}
    for s in sites:
        by_symbol.setdefault(s.symbol, []).append(s)

    affected = [(c, by_symbol[c.symbol]) for c in changes if c.symbol in by_symbol]

    unique_sites = {(s.file, s.line, s.end_line, s.symbol): s for _, ss in affected for s in ss}.values()
    uncovered = [s for s in unique_sites if site_is_covered(s, executed_lines) is not True]

    if not affected:
        risk = "green"
    elif executed_lines is not None and not uncovered:
        risk = "amber"
    else:
        risk = "red"

    signatures = {
        c.symbol: (format_signature(old_api[c.symbol]), format_signature(new_api[c.symbol]) if c.symbol in new_api else None)
        for c in changes
        if c.symbol in old_api
    }
    return RadarReport(
        package=package,
        signatures=signatures,
        risk=risk,
        changes=changes,
        affected=affected,
        uncovered=uncovered,
        coverage_known=executed_lines is not None,
    )
