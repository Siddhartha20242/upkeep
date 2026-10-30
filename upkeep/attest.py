"""Tamper-evident run output: a SHA-256 manifest of everything `upkeep fix` wrote, optionally signed with Ed25519.

What this proves: the files in the folder are exactly the ones Upkeep wrote (and, if signed, that the holder of one
specific private key vouches for them). What it does NOT prove: that the fix is correct. PROOF.md says that.
Hashing uses only the standard library. Signing needs the optional `cryptography` package (pip install upkeep[sign]).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path

from .pypi import release_digest

FILES = ("fix.patch", "PROOF.md", "result.json")
MANIFEST, SIGNATURE = "MANIFEST.json", "MANIFEST.sig"


class AttestError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def write_manifest(out: Path, package: str, old_version: str, new_version: str, status: str,
                   old_src: Path | None = None, new_src: Path | None = None) -> Path:
    manifest = {"schema": 1, "package": package, "old_version": old_version, "new_version": new_version, "status": status,
                "files": {n: sha256_file(out / n) for n in FILES if (out / n).is_file()}}
    rel = {k: d for k, d in (("old_release_sha256", release_digest(old_src) if old_src else None),
                             ("new_release_sha256", release_digest(new_src) if new_src else None)) if d}
    if rel:
        manifest["library_archives"] = rel
    (out / MANIFEST).write_bytes(_canonical(manifest))
    return out / MANIFEST


def _canonical(obj) -> bytes:
    return (json.dumps(obj, sort_keys=True, indent=2) + "\n").encode()


def verify_manifest(out: Path) -> list[str]:
    """Problems found (empty list = every file matches its recorded hash)."""
    mpath = Path(out) / MANIFEST
    if not mpath.is_file():
        return [f"{MANIFEST} not found"]
    try:
        manifest = json.loads(mpath.read_text())
        recorded = dict(manifest["files"])
    except (ValueError, KeyError, TypeError):
        return [f"{MANIFEST} is not a valid manifest"]
    problems = []
    for name, want in recorded.items():
        f = Path(out) / name
        if not f.is_file():
            problems.append(f"{name}: missing")
        elif not hmac.compare_digest(sha256_file(f), str(want)):
            problems.append(f"{name}: CHANGED since Upkeep wrote it")
    for name in FILES:
        if name not in recorded and (Path(out) / name).is_file():
            problems.append(f"{name}: present but not in the manifest")
    return problems


def _ed25519():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.exceptions import InvalidSignature
    except ImportError as e:
        raise AttestError("signing needs the optional 'cryptography' package: pip install cryptography") from e
    return serialization, ed25519, InvalidSignature


def _raw_pub(pub) -> bytes:
    ser, _, _ = _ed25519()
    return pub.public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw)


def fingerprint(pub_raw: bytes) -> str:
    return hashlib.sha256(pub_raw).hexdigest()[:16]


def generate_key(path: Path) -> str:
    """Write a new Ed25519 private key (PEM, mode 0600, never overwritten) and a .pub file; return the fingerprint."""
    ser, ed, _ = _ed25519()
    path = Path(path)
    if path.exists():
        raise AttestError(f"{path} already exists; refusing to overwrite a key")
    key = ed.Ed25519PrivateKey.generate()
    pem = key.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    pub = _raw_pub(key.public_key())
    path.with_suffix(path.suffix + ".pub").write_text(pub.hex() + "\n")
    return fingerprint(pub)


def sign_manifest(out: Path, key_path: Path) -> str:
    ser, ed, _ = _ed25519()
    try:
        key = ser.load_pem_private_key(Path(key_path).read_bytes(), password=None)
    except (TypeError, ValueError) as e:
        raise AttestError("could not load the signing key (a passphrase-protected or non-PEM key? "
                          "only unencrypted Ed25519 PEM keys from `upkeep keygen` are supported)") from e
    if not isinstance(key, ed.Ed25519PrivateKey):
        raise AttestError("the signing key is not an Ed25519 key")
    data = (Path(out) / MANIFEST).read_bytes()
    pub = _raw_pub(key.public_key())
    (Path(out) / SIGNATURE).write_text(json.dumps({"alg": "ed25519", "public_key": pub.hex(), "signature": key.sign(data).hex()}, indent=2) + "\n")
    return fingerprint(pub)


def verify_signature(out: Path, trusted_pub_hex: str | None = None) -> tuple[bool, str]:
    """(valid, message). With no trusted key the embedded one is used and the message says identity was NOT checked."""
    _, ed, InvalidSignature = _ed25519()
    sig_path, m_path = Path(out) / SIGNATURE, Path(out) / MANIFEST
    if not sig_path.is_file():
        return False, f"{SIGNATURE} not found (run was not signed)"
    try:
        sig = json.loads(sig_path.read_text())
        pub_hex, signature = sig["public_key"], bytes.fromhex(sig["signature"])
        if trusted_pub_hex and not hmac.compare_digest(trusted_pub_hex.strip().lower(), pub_hex.lower()):
            return False, "signed by a DIFFERENT key than the one you trust"
        ed.Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex)).verify(signature, m_path.read_bytes())
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return False, "signature is INVALID"
    fp = fingerprint(bytes.fromhex(pub_hex))
    return True, f"valid signature by key {fp}" + ("" if trusted_pub_hex else " (key identity NOT checked: pass --pubkey)")
