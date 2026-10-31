"""Secret-like strings: find them, and cut them out before text leaves this machine.

`upkeep fix` sends code snippets and test output to a model provider. This module is the filter on that path, and also the
check on what the agent writes back. It is a pattern list, so it will miss some secrets and flag some harmless strings: it
reduces the chance of a leak, it does not remove it. Do not run `fix` on code you are not allowed to share.
"""

from __future__ import annotations

import re
import math
import os
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping

_PLACEHOLDER = re.compile(r"(example|changeme|change-me|placeholder|your[_-]|xxxx|\*\*\*|dummy|sample|test|redacted)", re.I)

# (kind, pattern). The first group of the last pattern is kept (the name and the quote), only the value is replaced.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("stripe-key", re.compile(r"\b[sr]k_live_[A-Za-z0-9]{16,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("private-key-block", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("pypi-token", re.compile(r"\bpypi-AgEI[A-Za-z0-9_-]{40,}\b")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("sendgrid-key", re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b")),
    ("twilio-key", re.compile(r"\bSK[0-9a-f]{32}\b")),
    ("discord-token", re.compile(r"\b[MNO][A-Za-z0-9_-]{23,25}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}\b")),
    ("azure-storage-key", re.compile(r"(?i)\bAccountKey=[A-Za-z0-9+/=]{40,}")),
    ("bearer-token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{24,}")),
    ("url-credentials", re.compile(r"(?<=://)[^\s/:@]+:[^\s/@]{3,}(?=@)")),
]
ASSIGNED = re.compile(
    r"""(?i)\b((?:api[_-]?key|secret(?:[_-]?key)?|access[_-]?token|auth[_-]?token|token|passw(?:or)?d|client[_-]?secret)\s*[:=]\s*)(["'])([^"'\s]{12,})\2""")

SENSITIVE_NAMES = {".env", ".netrc", ".pypirc", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials", "credentials.json",
                   "secrets.json", "secrets.yml", "secrets.yaml", "service-account.json"}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore", ".jks"}
SENSITIVE_DIRS = {".ssh", ".aws", ".gnupg", ".git", ".docker", ".kube"}
SAFE_ENV_TEMPLATES = {".env.example", ".env.sample", ".env.template", ".env.dist"}


def looks_sensitive(path: str) -> bool:
    """A file the model should never be shown: environment files, keys, credentials, VCS and cloud config folders."""
    p = PurePosixPath(path.replace("\\", "/"))
    name = p.name.lower()
    if name in SAFE_ENV_TEMPLATES:
        return False
    if name in SENSITIVE_NAMES or name.startswith(".env.") or p.suffix.lower() in SENSITIVE_SUFFIXES:
        return True
    return any(part.lower() in SENSITIVE_DIRS for part in p.parts)


SECRET_ENV_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.I)
MIN_KNOWN_LENGTH = 8      # shorter values would redact ordinary words
QUOTED = re.compile(r"""(["'])([A-Za-z0-9+/=_-]{32,})\1""")


def known_secret_values(repo: Path | None = None, environ: Mapping[str, str] | None = None) -> set[str]:
    """Exact values that ARE secrets here: environment variables named like secrets, and values in the repo's .env files.

    Pattern matching cannot know the shape of a token it has never seen. These values are known, so they are redacted by
    exact match wherever they appear, whatever their format.
    """
    values: set[str] = set()
    for name, value in (os.environ if environ is None else environ).items():
        if SECRET_ENV_NAME.search(name) and len(value) >= MIN_KNOWN_LENGTH:
            values.add(value)
    if repo is not None:
        for f in Path(repo).glob(".env*"):
            if f.is_file() and f.name.lower() not in SAFE_ENV_TEMPLATES:
                try:
                    lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue
                for line in lines:
                    key, sep, val = line.partition("=")
                    val = val.strip().strip("\"'")
                    if sep and not line.lstrip().startswith("#") and len(val) >= MIN_KNOWN_LENGTH:
                        values.add(val)
    return values


def _entropy(s: str) -> float:
    return -sum(n / len(s) * math.log2(n / len(s)) for n in Counter(s).values())


def _looks_random(s: str) -> bool:
    """A long mixed-case-and-digits string with high entropy: typical of an API key, unlike a hash, an identifier or prose."""
    return (len(set(s)) >= 16 and any(c.islower() for c in s) and any(c.isupper() for c in s)
            and any(c.isdigit() for c in s) and _entropy(s) >= 4.3)


def redact(text: str, known: Iterable[str] = ()) -> tuple[str, int]:
    """(text with secret-like strings replaced by [REDACTED:kind], how many were replaced)."""
    count = 0
    for value in sorted(set(known), key=len, reverse=True):
        if value and value in text:
            count += text.count(value)
            text = text.replace(value, "[REDACTED:known-secret]")

    def sub_kind(kind: str):
        def f(m: re.Match[str]) -> str:
            nonlocal count
            count += 1
            return f"[REDACTED:{kind}]"
        return f

    for kind, pat in PATTERNS:
        text = pat.sub(sub_kind(kind), text)

    def assigned(m: re.Match[str]) -> str:
        nonlocal count
        if _PLACEHOLDER.search(m.group(3)):
            return m.group(0)
        count += 1
        return f"{m.group(1)}{m.group(2)}[REDACTED:assigned-secret]{m.group(2)}"
    text = ASSIGNED.sub(assigned, text)

    def quoted(m: re.Match[str]) -> str:
        nonlocal count
        if _PLACEHOLDER.search(m.group(2)) or not _looks_random(m.group(2)):
            return m.group(0)
        count += 1
        return f"{m.group(1)}[REDACTED:high-entropy-string]{m.group(1)}"
    return QUOTED.sub(quoted, text), count


def scan(text: str, known: Iterable[str] = ()) -> Counter:
    """Kinds of secret-like strings found in ``text`` (counts), without changing it."""
    found: Counter = Counter()
    for value in set(known):
        if value and value in text:
            found["known-secret"] += text.count(value)
    for kind, pat in PATTERNS:
        found[kind] += len(pat.findall(text))
    found["assigned-secret"] += sum(1 for m in ASSIGNED.finditer(text) if not _PLACEHOLDER.search(m.group(3)))
    found["high-entropy-string"] += sum(1 for m in QUOTED.finditer(text) if not _PLACEHOLDER.search(m.group(2)) and _looks_random(m.group(2)))
    return +found  # drop zero counts
