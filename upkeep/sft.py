"""Verified agent traces as training data (groundwork for fine-tuning an open-weight model; no training happens here).

A run folder from `upkeep fix` holds the full conversation (`model_transcript.json`, after secret redaction) and a verdict made by
code, not by the model. That pairing is useful: the conversations whose patch the verifier accepted are examples of the behavior
worth keeping. This module selects only runs that can be trusted as examples:

  * `result.json` says verified, and the folder's MANIFEST still matches (nobody edited the transcript or patch afterwards);
  * nothing in the run was redacted and the patch has no secret-like strings, so the text carries no placeholder or leaked value;
  * the example is the FINAL attempt's conversation; declined attempts are left out (or exported separately as negatives).

It writes chat-format JSONL. Whether the code in a run folder may be used for training is the owner's call: only export runs on code
you have the right to use that way.
"""

from __future__ import annotations

import json
from pathlib import Path

from .attest import verify_manifest


def reject_reason(run: Path) -> str | None:
    """Why this run folder is not exported, or None if it qualifies."""
    run = Path(run)
    try:
        result = json.loads((run / "result.json").read_text())
        log = json.loads((run / "model_transcript.json").read_text())
    except (OSError, ValueError):
        return "result.json or model_transcript.json missing or unreadable"
    if result.get("status") != "verified":
        return f"status is {result.get('status')!r}, not verified"
    problems = verify_manifest(run)
    if problems:
        return "manifest check failed: " + problems[0]
    if result.get("redactions"):
        return "text was redacted in this run (placeholders would end up in the training text)"
    if result.get("secrets_in_patch"):
        return "the patch contains secret-like strings"
    if not log.get("agent_messages"):
        return "no conversation recorded"
    return None


def export(runs: list[Path], out: Path, include_declined: bool = False) -> dict:
    """Write one JSON line per qualifying run (and, optionally, per declined earlier attempt, marked label=negative)."""
    kept = skipped = negatives = 0
    reasons: list[str] = []
    lines: list[str] = []
    for run in runs:
        why = reject_reason(Path(run))
        if why:
            skipped += 1
            reasons.append(f"{Path(run).name}: {why}")
            continue
        result = json.loads((Path(run) / "result.json").read_text())
        log = json.loads((Path(run) / "model_transcript.json").read_text())
        meta = {"model": result.get("model"), "provider": result.get("provider"), "attempts": len(result.get("attempts", [])) or 1,
                "tokens": result.get("tokens"), "run": Path(run).name}
        lines.append(json.dumps({"label": "verified", "messages": log["agent_messages"], "meta": meta}))
        kept += 1
        if include_declined:
            for e in log.get("earlier_attempts", []):
                lines.append(json.dumps({"label": "negative", "messages": e.get("agent_messages", []), "meta": {**meta, "model": e.get("model")}}))
                negatives += 1
    Path(out).write_text("\n".join(lines) + ("\n" if lines else ""))
    return {"exported": kept, "negatives": negatives, "skipped": skipped, "skipped_reasons": reasons}
