"""Secret-like strings: find them, and cut them out before text leaves this machine.

`upkeep fix` sends code snippets and test output to a model provider. This module is the filter on that path, and also the
check on what the agent writes back. It is a pattern list, so it will miss some secrets and flag some harmless strings: it
reduces the chance of a leak, it does not remove it. Do not run `fix` on code you are not allowed to share.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import PurePosixPath

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


def redact(text: str) -> tuple[str, int]:
    """(text with secret-like strings replaced by [REDACTED:kind], how many were replaced)."""
    count = 0

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
    return ASSIGNED.sub(assigned, text), count


def scan(text: str) -> Counter:
    """Kinds of secret-like strings found in ``text`` (counts), without changing it."""
    found: Counter = Counter()
    for kind, pat in PATTERNS:
        found[kind] += len(pat.findall(text))
    found["assigned-secret"] += sum(1 for m in ASSIGNED.finditer(text) if not _PLACEHOLDER.search(m.group(3)))
    return +found  # drop zero counts
