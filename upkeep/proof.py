"""Proof bundle: the evidence attached to every change Upkeep proposes (or declines)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .charact import where
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
    behavior: dict | None = None

    def _probed(self) -> set[tuple]:
        return {tuple(x) for x in (self.behavior or {}).get("probed_sites", [])}

    def _unexercised(self):
        """Affected sites that neither a test nor a behavior probe executed."""
        probed = self._probed()
        return [s for s in self.radar.uncovered if (s.file, s.line) not in probed]

    def status(self) -> str:
        if self.declined_reason:
            return "declined"
        if self.behavior and self.behavior.get("mismatches"):
            return "declined"
        if self.tests_after is not None and self.tests_after.ok and not self._unexercised():
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
            probed = self._probed()
            for s in r.uncovered:
                if (s.file, s.line) in probed:
                    lines.append(f"- not run by the repo's tests, but exercised by behavior probes: `{s.file}:{s.line}`")
                else:
                    lines.append(f"- NOT covered: `{s.file}:{s.line}`")
        else:
            lines.append("- all affected sites are executed by existing tests")

        lines += ["", "## Test results"]
        for label, t in (("before", self.tests_before), ("after", self.tests_after)):
            lines.append(f"- {label}: " + (("pass" if t.ok else f"FAIL (exit {t.returncode})") + f" [{t.backend}]" if t else "not run"))

        lines += ["", "## Behavior check against the old library"]
        b = self.behavior
        if b is None:
            lines.append("- not run (only the repo's own tests were used)")
        else:
            if b.get("random_inputs"):
                lines.append(f"- includes up to {b['random_inputs']} seeded-random inputs per function (reproducible; not exhaustive)")
            lines.append(f"- {b['inputs_checked']} probe inputs over {b['functions_checked']} function(s); "
                         f"{len(b['mismatches'])} mismatch(es)")
            for m in b["mismatches"][:5]:
                lines.append(f"  - {where(m)}{m['function']}({', '.join(json.dumps(a) for a in m['input'])}): "
                             f"old library {m['old']} vs patched code on new library {m['new']}")
            lines += [f"  - skipped: {x}" for x in b["skipped"][:3]]
            lines.append("- probes are model-chosen: a clean result is evidence, not proof")

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
