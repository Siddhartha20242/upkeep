"""Proof bundle: the evidence attached to every change Upkeep proposes (or declines)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .radar import RadarReport
from .sandbox import TestResult
from .vuln import Advisory


@dataclass
class ProofBundle:
    package: str
    old_version: str
    new_version: str
    radar: RadarReport
    tests_before: TestResult | None = None
    tests_after: TestResult | None = None
    advisories: list[Advisory] = field(default_factory=list)
    injection_flags: list[str] = field(default_factory=list)
    declined_reason: str | None = None
    patch: str | None = None

    def status(self) -> str:
        if self.declined_reason:
            return "declined"
        if self.tests_after is not None and self.tests_after.ok and not self.radar.uncovered:
            return "verified"
        if self.tests_after is not None and self.tests_after.ok:
            return "tests-pass-but-unverified-sites"
        return "unverified"

    def to_markdown(self) -> str:
        r = self.radar
        lines = [
            f"# Upkeep proof: {self.package} {self.old_version} -> {self.new_version}",
            "",
            f"**Status:** `{self.status()}`  |  **Radar risk:** `{r.risk}`",
        ]
        if self.declined_reason:
            lines += ["", f"**Declined:** {self.declined_reason}", "Handing off to a human."]

        lines += ["", "## Breaking API changes (static diff)"]
        lines += [f"- `{c.symbol}`: {c.kind} ({c.detail})" for c in r.changes] or ["- none found"]

        lines += ["", "## Affected call sites"]
        if r.affected:
            seen = set()
            for c, sites in r.affected:
                for s in sites:
                    key = (s.file, s.line, s.symbol, s.kind)
                    if key in seen:  # one site can be hit by several breaking changes
                        continue
                    seen.add(key)
                    why = [x.kind for x, ss in r.affected if s in ss and x.symbol == s.symbol]
                    lines.append(f"- `{s.file}:{s.line}` ({s.kind}) `{s.symbol}`: {', '.join(why)}")
        else:
            lines.append("- none")

        lines += ["", "## Test coverage of affected sites"]
        if not r.coverage_known:
            lines.append("- coverage unknown (tests not run under coverage)")
        elif r.uncovered:
            lines += [f"- NOT covered: `{s.file}:{s.line}`" for s in r.uncovered]
        else:
            lines.append("- all affected sites are executed by existing tests")

        lines += ["", "## Test results"]
        for label, t in (("before", self.tests_before), ("after", self.tests_after)):
            lines.append(f"- {label}: " + (("pass" if t.ok else f"FAIL (exit {t.returncode})") + f" [{t.backend}]" if t else "not run"))

        lines += ["", "## Known vulnerabilities in the current version"]
        if self.advisories:
            for a in self.advisories:
                fixed = ", ".join(a.fixed_versions) or "no fixed version listed"
                lines.append(f"- {a.id} ({a.severity or 'severity n/a'}): {a.summary} - fixed in {fixed}")
            lines.append("- reachability from this repo: unknown unless an advisory names functions")
        else:
            lines.append("- none reported by OSV")

        if self.injection_flags:
            lines += ["", "## Untrusted-input warnings", "- flags raised on changelog text: " + ", ".join(self.injection_flags)]

        lines += ["", "## What this does NOT prove"]
        lines += [f"- {x}" for x in r.limitations]
        lines.append("- passing tests only prove behavior the tests exercise")
        return "\n".join(lines) + "\n"
