"""Fetch two releases of a PyPI package so `upkeep check` can run the radar with one command.

Only public https://pypi.org / files.pythonhosted.org downloads. Archives are extracted with traversal protection and are
only ever parsed (never imported or executed).
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import shutil
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

# Not the temp folder: macOS clears old files from temp folders and can leave empty directories behind, which is the most
# likely cause of the "library sources lost their .py files" incident described in the README (not proven).
DEFAULT_CACHE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "upkeep" / "pypi"


DIGEST_FILE = ".upkeep-sha256"


class PyPIError(RuntimeError):
    pass


def check_sha256(blob: bytes, entry: dict, label: str) -> str:
    """Compare a download with the SHA-256 PyPI publishes for it; fail closed. Returns the verified digest."""
    expected = ((entry.get("digests") or {}).get("sha256") or "").lower()
    if not expected:
        raise PyPIError(f"PyPI published no SHA-256 for {label}; refusing to use an unverifiable archive")
    actual = hashlib.sha256(blob).hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise PyPIError(f"SHA-256 mismatch for {label}: PyPI says {expected[:16]}..., downloaded {actual[:16]}...; "
                        "the download was corrupted or tampered with, nothing was extracted")
    return actual


def release_digest(path: Path) -> str | None:
    """SHA-256 of the archive this source tree came from (recorded at download), searching up a few folders."""
    p = Path(path).resolve()
    for d in [p, *list(p.parents)[:4]]:
        f = d / DIGEST_FILE
        if f.is_file():
            return f.read_text().strip() or None
    return None


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


def fetch_sdist(dist: str, version: str, cache: Path = DEFAULT_CACHE, max_bytes: int | None = None) -> Path:
    root = Path(cache) / f"{dist}-{version}"
    if root.exists() and any(root.rglob("*.py")):  # a cache of empty folders is NOT a cache (it has happened)
        return root
    shutil.rmtree(root, ignore_errors=True)
    try:
        meta = json.loads(_read(f"https://pypi.org/pypi/{dist}/{version}/json"))
    except ValueError as e:
        raise PyPIError(f"could not read PyPI metadata for {dist}=={version}") from e
    files = [u for u in meta.get("urls", []) if u.get("packagetype") == "sdist"]
    if not files:
        raise PyPIError(f"{dist}=={version} has no source archive on PyPI")
    urls = [files[0]["url"]]
    if max_bytes and int(files[0].get("size") or 0) > max_bytes:
        raise PyPIError(f"{dist}=={version} source archive is {int(files[0]['size']) // 1_000_000} MB (limit {max_bytes // 1_000_000} MB)")
    blob = _read(urls[0])
    actual = check_sha256(blob, files[0], f"{dist}=={version}")
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
    (tmp / DIGEST_FILE).write_text(actual + "\n")
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


def latest_version(dist: str) -> str:
    """The newest stable release PyPI lists (``info.version``)."""
    try:
        return str(json.loads(_read(f"https://pypi.org/pypi/{dist}/json"))["info"]["version"])
    except (ValueError, KeyError, TypeError) as e:
        raise PyPIError(f"could not read the latest version of {dist}") from e


def sources_for(dist: str, old: str, new: str, module: str | None = None, cache: Path = DEFAULT_CACHE,
                max_bytes: int | None = None) -> tuple[Path, Path, str]:
    mod = module or guess_module(dist)
    o, n = fetch_sdist(dist, old, cache, max_bytes), fetch_sdist(dist, new, cache, max_bytes)
    op, np_ = locate_package(o, mod), locate_package(n, mod)
    if not op or not np_:
        raise PyPIError(f"package folder {mod!r} not found in the {dist} archives; pass --module (for PyYAML it is 'yaml')")
    return op, np_, mod
