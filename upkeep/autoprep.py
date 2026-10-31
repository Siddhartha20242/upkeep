"""One-command setup for `upkeep fix --package X --from A --to B`.

Downloads the two releases from PyPI (hash-checked), builds one dependency folder per version inside Docker, and finds the
release notes, so a person does not have to prepare source folders and dependency folders by hand.

The dependency step is the only one that runs with network access, so it is strict:
  * every requirement line from the repo is validated against a narrow pattern before it reaches `pip`
    (no options, no URLs, no local paths, no environment markers);
  * wheels only by default, so no package's build script runs; building from source needs an explicit flag.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .audit import MODULES, _all_requirements
from .pypi import PyPIError, changelog_section, fetch_sdist, sources_for
from .sandbox import TestResult, prepare_deps

DEFAULT_CACHE = Path.home() / ".cache" / "upkeep"
IMAGES = ["python:3.12-slim", "python:3.11-slim", "python:3.10-slim", "python:3.9-slim"]  # newest first: old pins often lack new wheels
MAX_REQUIREMENTS = 80

_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_SPEC = r"(?:==|>=|<=|~=|!=|<|>)\s*[A-Za-z0-9.*+!_-]+"
SAFE_REQUIREMENT = re.compile(rf"^({_NAME})(\[[A-Za-z0-9,._-]+\])?\s*((?:{_SPEC})(?:\s*,\s*{_SPEC})*)?$")


class PrepareError(RuntimeError):
    """Setup could not finish; the message says what to do about it."""


@dataclass
class Prepared:
    old_src: Path
    new_src: Path
    module: str
    changelog: Path | None
    deps_old: Path
    deps_new: Path
    image: str
    notes: list[str]


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def docker_ready() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def imported_roots(repo: Path, limit: int = 3000) -> set[str]:
    """Top-level module names imported anywhere in the repo's Python files (read with `ast`, nothing is run)."""
    roots: set[str] = set()
    for i, f in enumerate(sorted(Path(repo).rglob("*.py"))):
        if i >= limit or f.stat().st_size > 400_000:
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError, OSError):
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                roots |= {a.name.split(".")[0].lower() for a in n.names}
            elif isinstance(n, ast.ImportFrom) and not n.level and n.module:
                roots.add(n.module.split(".")[0].lower())
    return roots


def import_names(requirement_name: str) -> set[str]:
    """Plausible import names for a PyPI name: the name itself, python-/-python variants, and the known odd ones."""
    n = norm(requirement_name)
    names = {n.replace("-", "_"), n.replace("-", "")}
    for pre, suf in (("python-", ""), ("py-", ""), ("", "-python"), ("", "-py")):
        if n.startswith(pre) and n.endswith(suf) and (pre or suf):
            names.add(n[len(pre):len(n) - len(suf)].replace("-", "_"))
    if n in MODULES:
        names.add(MODULES[n].lower())
    return names


def repo_requirements(repo: Path, dist: str, only_imported: bool = False) -> tuple[list[str], list[str]]:
    """(requirements safe to install, notes about lines that were skipped). The upgraded library itself is excluded.

    ``only_imported`` keeps just the requirements whose import name appears in the repo's code: many projects list dozens of
    heavy packages their tests never touch, and installing all of them from wheels is the usual reason setup fails."""
    ok: list[str] = []
    notes: list[str] = []
    seen: set[str] = set()
    for line in _all_requirements(Path(repo)):
        text = line.split(";", 1)[0].strip()  # environment markers are dropped on purpose: they are free text
        m = SAFE_REQUIREMENT.match(text)
        if not m:
            notes.append(f"skipped a requirement line that is not a plain name/version spec: {line[:60]!r}")
            continue
        name = norm(m.group(1))
        if name == norm(dist) or name in seen:
            continue
        seen.add(name)
        ok.append(text)
    if only_imported:
        used = imported_roots(repo)
        keep = [r for r in ok if import_names(SAFE_REQUIREMENT.match(r).group(1)) & used]  # type: ignore[union-attr]
        if len(keep) < len(ok):
            notes.append(f"minimal dependencies: installing {len(keep)} of {len(ok)} requirements (the ones the code imports)")
        ok = keep
    if len(ok) > MAX_REQUIREMENTS:
        notes.append(f"{len(ok) - MAX_REQUIREMENTS} requirement(s) beyond the first {MAX_REQUIREMENTS} were not installed")
        ok = ok[:MAX_REQUIREMENTS]
    return ok, notes


def pip_errors(output: str) -> str:
    """The lines of pip output that say what went wrong, without the version-skip chatter and the upgrade notice."""
    lines = [ln.strip() for ln in output.splitlines() if "ERROR:" in ln and "Ignored the following" not in ln]
    return "\n".join(lines[-3:]) if lines else output.strip()[-300:]


def _deps_dir(cache: Path, image: str, packages: list[str]) -> Path:
    key = hashlib.sha256(json.dumps([image, sorted(packages)]).encode()).hexdigest()[:16]
    return cache / "deps" / key


def _install(deps: Path, packages: list[str], image: str, wheels_only: bool, installer: Callable[..., TestResult]) -> TestResult:
    if deps.exists() and any(deps.iterdir()):
        return TestResult(True, 0, "cached", "docker")
    scratch = deps.parent / f".scratch-{deps.name}"
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        res = installer(scratch, deps, packages, image=image, wheels_only=wheels_only)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if not res.ok:
        shutil.rmtree(deps, ignore_errors=True)  # never leave a half-installed folder that a later run would trust
    return res


def prepare_upgrade(
    repo: Path, dist: str, old: str, new: str, module: str | None = None, image: str | None = None,
    cache: Path | None = None, allow_source_builds: bool = False, max_mb: int = 40, only_imported: bool = False,
    log: Callable[[str], None] = lambda s: print(s, flush=True),
    installer: Callable[..., TestResult] = prepare_deps,
) -> Prepared:
    cache = Path(cache) if cache else DEFAULT_CACHE
    module = module or MODULES.get(norm(dist))
    log(f"upkeep: downloading {dist} {old} and {new} from PyPI (SHA-256 checked) ...")
    try:
        old_src, new_src, mod = sources_for(dist, old, new, module, cache=cache / "pypi", max_bytes=max_mb * 1_000_000)
        new_root = fetch_sdist(dist, new, cache / "pypi", max_mb * 1_000_000)
    except PyPIError as e:
        raise PrepareError(f"could not prepare the two releases: {e}") from e
    notes_text = changelog_section(new_root, new)
    changelog = None
    if notes_text:
        changelog = cache / "changelogs" / f"{norm(dist)}-{new}.txt"
        changelog.parent.mkdir(parents=True, exist_ok=True)
        changelog.write_text(notes_text)
    else:
        log("upkeep: no release notes found in the new archive; the agent will work without them")

    others, notes = repo_requirements(repo, dist, only_imported)
    for n in notes:
        log(f"upkeep: note: {n}")
    base = ["pytest", "coverage", *others]
    attempts = [image] if image else IMAGES
    last = ""
    for builds in ([False, True] if allow_source_builds else [False]):
        for img in attempts:
            log(f"upkeep: installing dependencies on {img} ({'building from source' if builds else 'wheels only'}; this step uses the network) ...")
            deps_old = _deps_dir(cache, img, [*base, f"{dist}=={old}"])
            deps_new = _deps_dir(cache, img, [*base, f"{dist}=={new}"])
            r_old = _install(deps_old, [*base, f"{dist}=={old}"], img, not builds, installer)
            r_new = _install(deps_new, [*base, f"{dist}=={new}"], img, not builds, installer) if r_old.ok else r_old
            if r_old.ok and r_new.ok:
                return Prepared(old_src, new_src, mod, changelog, deps_old, deps_new, img, notes)
            last = pip_errors((r_new if r_old.ok else r_old).output)
    hint = ("" if allow_source_builds else
            " Re-run with --allow-source-builds to build from source inside the container (that runs the packages' build "
            "scripts, with network access), or pass --image for a Python version that has wheels.")
    raise PrepareError(f"could not install the dependencies from wheels on {', '.join(attempts)}.{hint}\nlast error: {last}")

