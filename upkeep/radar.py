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

from .apidiff import Change, Signature, diff_api, extract_api, format_signature
from .sandbox import site_is_covered
from .scan import CallSite, find_call_sites


class RadarInputError(RuntimeError):
    """The library source could not be read. Distinct from 'nothing breaks': never report green on missing data."""


def _require_source(root: Path, package: str, label: str) -> None:
    pkg = Path(root) / package
    if not pkg.is_dir() or not any(pkg.rglob("*.py")):
        raise RadarInputError(f"no Python source found for package {package!r} in the {label} library folder ({root}); "
                              "refusing to report a result from missing data")


def _pos_index(sig: Signature | None, name: str) -> int | None:
    if sig is None:
        return None
    names = [p.name for p in sig.params if p.kind in ("posonly", "pos")]
    return names.index(name) if name in names else None


def _pos_names(sig: Signature | None) -> list[str]:
    return [p.name for p in sig.params if p.kind in ("posonly", "pos")] if sig else []


def _fits_positionally(sig: Signature | None, n: int) -> bool:
    """Can the new signature take ``n`` positional arguments? Unknown signature: no (stay conservative)."""
    if sig is None:
        return False
    if any(p.kind == "vararg" for p in sig.params):
        return True
    return n <= len(_pos_names(sig))


def _is_rename(old_sig: Signature | None, new_sig: Signature | None, idx: int, n: int) -> bool:
    """A removed positional parameter that was only RENAMED: the same slot now holds a name that did not exist before
    (and nothing shifted), so a positional argument still reaches the same place."""
    if old_sig is None or new_sig is None:
        return False
    old_names, new_pos = {p.name for p in old_sig.params}, _pos_names(new_sig)
    if idx < len(new_pos):
        return new_pos[idx] not in old_names and _fits_positionally(new_sig, n)
    # the removed parameter was the LAST positional one and `*args` now takes its place (a rename into a var-positional)
    return idx == len(new_pos) and any(p.kind == "vararg" for p in new_sig.params)


PARAM_LEVEL = {"params_reordered", "param_became_required", "param_added_required", "default_changed", "param_removed",
               "param_kind_changed"}


def site_affected(change: Change, site: CallSite, old_sig: Signature | None, new_sig: Signature | None) -> bool:
    """Does THIS call actually trip THIS change? Conservative: when unsure, say yes."""
    if site.kind in ("import", "base") and change.kind in PARAM_LEVEL:
        # importing a function or naming a base class cannot trip a changed parameter; the calls are what matter
        # (found on real repos: every `class X(Schema)` line was flagged for each constructor change)
        return False
    if site.kind == "reference" and change.kind in PARAM_LEVEL:
        site = CallSite(site.file, site.line, site.end_line, site.symbol, "call", 1, (), False)  # `@dec` calls dec(func)
    if site.kind != "call" or change.kind == "removed" or site.star:
        return True  # decorators/bases of a changed symbol, removed symbols, and calls that use *args/**kwargs
    p, n, kws = change.param, site.n_pos, set(site.kwargs)

    def provided(sig: Signature | None) -> bool:
        idx = _pos_index(sig, p) if p else None
        return bool(p) and (p in kws or (idx is not None and n > idx))

    if change.kind == "params_reordered":
        po, pn = _pos_names(old_sig), _pos_names(new_sig)
        if not po or not pn:
            return n >= 2
        return any(i < len(po) and i < len(pn) and po[i] != pn[i] for i in range(n))  # a positional argument lands elsewhere
    if p is None:
        return True
    if change.kind == "param_removed" and old_sig is not None:
        kind = next((x.kind for x in old_sig.params if x.name == p), None)
        new_named = {x.name for x in (new_sig.params if new_sig else ()) if x.kind in ("posonly", "pos", "kwonly")}
        if kind == "varkw":  # `**kwargs` is gone: any keyword the new signature does not name now fails
            return any(k not in new_named for k in kws)
        if kind == "vararg":  # `*args` is gone: extra positional arguments now fail
            return n > len(_pos_names(new_sig))
    if change.kind in ("param_became_required",):
        return not provided(old_sig)
    if change.kind in ("param_added_required", "default_changed"):
        return not provided(new_sig)
    if change.kind == "param_removed":
        if p in kws:
            return True
        idx = _pos_index(old_sig, p)
        if idx is None or n <= idx:
            return False
        return not _is_rename(old_sig, new_sig, idx, n)  # a renamed parameter still takes the same positional argument
    if change.kind == "param_kind_changed":
        newp = next((x for x in (new_sig.params if new_sig else []) if x.name == p), None)
        if newp is not None and newp.kind == "posonly":
            return p in kws  # was keyword-capable, now positional-only
        idx = _pos_index(old_sig, p)
        return idx is not None and n > idx  # now keyword-only: breaks callers that pass it positionally
    return True


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
            "Instance method calls are resolved only for simple local patterns (variable, with-statement, self attribute, "
            "annotated parameter); inheritance, reassigned variables, other files and dynamic calls are not resolved.",
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
    _require_source(old_root, package, "old")
    _require_source(new_root, package, "new")
    old_api = extract_api(old_root, package, include_methods=True)
    new_api = extract_api(new_root, package, include_methods=True)
    changes = diff_api(old_api, new_api)

    symbols = {c.symbol for c in changes}
    classes = {k for k, v in old_api.items() if v.is_class}  # lets the scanner type instances: c = lib.C(); c.m()
    sites = find_call_sites(repo, package, symbols, classes) if symbols else []
    by_symbol: dict[str, list[CallSite]] = {}
    for s in sites:
        by_symbol.setdefault(s.symbol, []).append(s)

    affected = []
    for c in changes:
        hit = [s for s in by_symbol.get(c.symbol, []) if site_affected(c, s, old_api.get(c.symbol), new_api.get(c.symbol))]
        if hit:
            affected.append((c, hit))

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
