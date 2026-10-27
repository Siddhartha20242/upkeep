"""Scan candidate (package, old, new) upgrades for real breaking API changes.

Downloads source archives from PyPI into a cache dir, extracts them safely
(no code is executed, only parsed with ast), runs upkeep's API diff, and prints
a table. Also records each release's declared license so we only use cases we
are allowed to redistribute / evaluate on.

Run:  .venv/bin/python -I scripts/find_cases.py CACHE_DIR [pkg==old..new ...]
"""

import json
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

from upkeep.apidiff import diff_api, extract_api

DEFAULT = [
    ("PyYAML", "yaml", "5.4.1", "6.0"),
    ("Werkzeug", "werkzeug", "2.3.8", "3.0.0"),
    ("Jinja2", "jinja2", "3.0.3", "3.1.0"),
    ("click", "click", "7.1.2", "8.0.0"),
    ("PyJWT", "jwt", "1.7.1", "2.0.0"),
    ("itsdangerous", "itsdangerous", "1.1.0", "2.0.0"),
    ("Flask", "flask", "2.2.5", "2.3.0"),
    ("Markdown", "markdown", "2.6.11", "3.0"),
    ("urllib3", "urllib3", "1.26.18", "2.0.0"),
    ("docutils", "docutils", "0.17.1", "0.18"),
    ("redis", "redis", "3.5.3", "4.0.0"),
    ("jsonschema", "jsonschema", "3.2.0", "4.0.0"),
    ("attrs", "attr", "21.4.0", "22.1.0"),
    ("tabulate", "tabulate", "0.8.10", "0.9.0"),
    ("toml", "toml", "0.10.0", "0.10.2"),
]


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310 - fixed https
        return json.loads(r.read())


def sdist_info(pkg: str, ver: str) -> tuple[str, str]:
    data = fetch_json(f"https://pypi.org/pypi/{pkg}/{ver}/json")
    info = data["info"]
    lic = info.get("license_expression") or (info.get("license") or "")[:60].replace("\n", " ")
    if not lic:
        lic = "; ".join(c.split("::")[-1].strip() for c in info.get("classifiers", []) if c.startswith("License"))
    for u in data["urls"]:
        if u["packagetype"] == "sdist":
            return u["url"], lic or "unknown"
    raise RuntimeError(f"no sdist for {pkg}=={ver}")


def safe_extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            for n in z.namelist():
                if n.startswith("/") or ".." in Path(n).parts:
                    raise RuntimeError(f"unsafe path in archive: {n}")
            z.extractall(dest)
    else:
        with tarfile.open(archive) as t:
            t.extractall(dest, filter="data")  # blocks traversal, links, special files


def prepare(pkg: str, ver: str, cache: Path) -> tuple[Path, str]:
    root = cache / f"{pkg}-{ver}"
    marker = cache / f"{pkg}-{ver}.license"
    if root.exists() and marker.exists():
        return root, marker.read_text()
    url, lic = sdist_info(pkg, ver)
    archive = cache / url.rsplit("/", 1)[-1]
    if not archive.exists():
        with urllib.request.urlopen(url, timeout=120) as r, open(archive, "wb") as f:  # noqa: S310
            f.write(r.read())
    safe_extract(archive, root)
    marker.write_text(lic)
    return root, lic


def locate(root: Path, package: str) -> Path | None:
    """Shallowest directory containing <package>/__init__.py (handles src/ and lib/ layouts)."""
    best = None
    for init in root.rglob("__init__.py"):
        if init.parent.name == package:
            depth = len(init.parent.relative_to(root).parts)
            if "test" in str(init).lower() or "docs" in init.parts:
                continue
            if best is None or depth < best[0]:
                best = (depth, init.parent.parent)
    return best[1] if best else None


def main() -> int:
    cache = Path(sys.argv[1])
    cache.mkdir(parents=True, exist_ok=True)
    rows = DEFAULT
    if len(sys.argv) > 2:
        rows = []
        for a in sys.argv[2:]:
            name, rng = a.split("==")
            old, new = rng.split("..")
            rows.append((name, name.lower(), old, new))

    results = []
    for dist, mod, old, new in rows:
        try:
            old_root, old_lic = prepare(dist, old, cache)
            new_root, new_lic = prepare(dist, new, cache)
            o, n = locate(old_root, mod), locate(new_root, mod)
            if not o or not n:
                print(f"{dist} {old}->{new}: package dir '{mod}' not found", flush=True)
                continue
            changes = diff_api(extract_api(o, mod), extract_api(n, mod))
        except Exception as e:  # keep scanning the rest
            print(f"{dist} {old}->{new}: ERROR {type(e).__name__}: {e}", flush=True)
            continue
        kinds: dict[str, int] = {}
        for c in changes:
            kinds[c.kind] = kinds.get(c.kind, 0) + 1
        results.append((dist, old, new, len(changes), kinds, new_lic, changes))
        print(f"{dist} {old}->{new}: {len(changes)} breaking  {kinds}  license={new_lic}", flush=True)

    print("\n=== top symbols per upgrade ===")
    for dist, old, new, n, kinds, lic, changes in results:
        if not n:
            continue
        syms = sorted({c.symbol for c in changes})[:6]
        print(f"{dist} {old}->{new}: {', '.join(syms)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
