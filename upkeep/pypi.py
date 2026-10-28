"""Fetch two releases of a PyPI package so `upkeep check` can run the radar with one command.

Only public https://pypi.org / files.pythonhosted.org downloads. Archives are extracted with traversal protection and are
only ever parsed (never imported or executed).
"""

from __future__ import annotations

import io
import json
import shutil
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

DEFAULT_CACHE = Path(tempfile.gettempdir()) / "upkeep-pypi-cache"


class PyPIError(RuntimeError):
    pass


def _read(url: str, timeout: int = 45, tries: int = 4) -> bytes:
    last: Exception | None = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 - https PyPI URLs only
                return r.read()
        except (TimeoutError, OSError) as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise PyPIError(f"download failed after {tries} tries: {url}: {last}")


def fetch_sdist(dist: str, version: str, cache: Path = DEFAULT_CACHE) -> Path:
    root = Path(cache) / f"{dist}-{version}"
    if root.exists() and any(root.rglob("*.py")):  # a cache of empty folders is NOT a cache (it has happened)
        return root
    shutil.rmtree(root, ignore_errors=True)
    try:
        meta = json.loads(_read(f"https://pypi.org/pypi/{dist}/{version}/json"))
    except ValueError as e:
        raise PyPIError(f"could not read PyPI metadata for {dist}=={version}") from e
    urls = [u["url"] for u in meta.get("urls", []) if u.get("packagetype") == "sdist"]
    if not urls:
        raise PyPIError(f"{dist}=={version} has no source archive on PyPI")
    blob = _read(urls[0])
    tmp = Path(cache) / f".tmp-{dist}-{version}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    if urls[0].endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            for n in z.namelist():
                if n.startswith("/") or ".." in Path(n).parts:
                    raise PyPIError("unsafe path in archive")
            z.extractall(tmp)
    else:
        with tarfile.open(fileobj=io.BytesIO(blob)) as t:
            t.extractall(tmp, filter="data")
    shutil.rmtree(root, ignore_errors=True)
    tmp.rename(root)
    return root


def locate_package(root: Path, module: str) -> Path | None:
    """The folder that CONTAINS ``module/__init__.py`` (handles src/ and lib/ layouts), shallowest first."""
    best = None
    for init in Path(root).rglob("__init__.py"):
        if init.parent.name == module and not any(p in ("tests", "test", "docs") for p in init.parts):
            depth = len(init.parent.relative_to(root).parts)
            if best is None or depth < best[0]:
                best = (depth, init.parent.parent)
    return best[1] if best else None


def guess_module(dist: str) -> str:
    return dist.lower().replace("-", "_")


def sources_for(dist: str, old: str, new: str, module: str | None = None, cache: Path = DEFAULT_CACHE) -> tuple[Path, Path, str]:
    mod = module or guess_module(dist)
    o, n = fetch_sdist(dist, old, cache), fetch_sdist(dist, new, cache)
    op, np_ = locate_package(o, mod), locate_package(n, mod)
    if not op or not np_:
        raise PyPIError(f"package folder {mod!r} not found in the {dist} archives; pass --module (for PyYAML it is 'yaml')")
    return op, np_, mod
