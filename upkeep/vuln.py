"""Known-vulnerability lookup via the public OSV API (https://osv.dev).

Only the package name and version are sent. Reachability is "lite": advisories
rarely say which functions are affected, so ``reachable`` returns None
(unknown) unless the advisory names symbols. Never treat unknown as safe.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .scan import CallSite, find_call_sites

OSV_URL = "https://api.osv.dev/v1/query"


@dataclass
class Advisory:
    id: str
    summary: str
    aliases: list[str] = field(default_factory=list)
    severity: str | None = None
    fixed_versions: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)


def _http_post(url: str, payload: dict, timeout: int = 20) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https URL
        return json.loads(r.read())


def _symbols(affected: dict) -> list[str]:
    eco = affected.get("ecosystem_specific") or {}
    out: list[str] = []
    out += [s for s in eco.get("affected_functions", []) if isinstance(s, str)]
    for imp in eco.get("imports", []) or []:
        out += [s for s in imp.get("symbols", []) if isinstance(s, str)]
    return out


def parse_advisories(response: dict, package: str) -> list[Advisory]:
    out: list[Advisory] = []
    for v in response.get("vulns", []):
        fixed: list[str] = []
        symbols: list[str] = []
        for a in v.get("affected", []):
            pkg = (a.get("package") or {}).get("name", "")
            if pkg.lower().replace("_", "-") != package.lower().replace("_", "-"):
                continue
            for r in a.get("ranges", []):
                fixed += [e["fixed"] for e in r.get("events", []) if "fixed" in e]
            symbols += _symbols(a)
        sev = (v.get("database_specific") or {}).get("severity")
        out.append(
            Advisory(
                id=v["id"],
                summary=v.get("summary", ""),
                aliases=v.get("aliases", []),
                severity=sev,
                fixed_versions=sorted(set(fixed)),
                symbols=sorted(set(symbols)),
            )
        )
    return out


def query_osv(
    package: str,
    version: str,
    post: Callable[[str, dict], dict] = _http_post,
) -> list[Advisory]:
    """Advisories affecting ``package==version`` (PyPI). Raises on network failure."""
    resp = post(OSV_URL, {"package": {"name": package, "ecosystem": "PyPI"}, "version": version})
    return parse_advisories(resp, package)


def reachable(advisory: Advisory, repo: Path, package: str) -> list[CallSite] | None:
    """Call sites of the advisory's named symbols, or None if it names none (unknown)."""
    if not advisory.symbols:
        return None
    wanted: set[str] = set()
    for s in advisory.symbols:
        wanted.add(s if s.startswith(package + ".") else f"{package}.{s}")
    return find_call_sites(repo, package, wanted)
