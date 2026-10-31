import json

from tests.test_cli_fix import args, build
from tests.test_escalation import decline, fixer
from upkeep import cli
from upkeep.sft import export, reject_reason


def run_folder(tmp_path, name, **flags):
    old, new, repo = build(tmp_path / name)
    out = tmp_path / name / "out"
    cli.main(args(old, new, repo, out, *flags.get("extra", [])), client=flags["client"], escalator=flags.get("escalator"))
    return out


def test_a_verified_run_is_exported_as_one_chat_example(tmp_path):
    out = run_folder(tmp_path, "ok", client=fixer())
    assert reject_reason(out) is None
    dest = tmp_path / "t.jsonl"
    s = export([out], dest)
    rows = [json.loads(x) for x in dest.read_text().splitlines()]
    assert s["exported"] == 1 and len(rows) == 1 and rows[0]["label"] == "verified"
    assert rows[0]["messages"][0]["role"] == "system" and rows[0]["meta"]["attempts"] == 1


def test_a_declined_run_is_not_exported(tmp_path):
    out = run_folder(tmp_path, "no", client=decline())
    assert "not verified" in reject_reason(out)
    s = export([out], tmp_path / "t.jsonl")
    assert s["exported"] == 0 and s["skipped"] == 1


def test_a_run_edited_after_the_fact_is_not_exported(tmp_path):
    out = run_folder(tmp_path, "edited", client=fixer())
    (out / "model_transcript.json").write_text(json.dumps({"agent_messages": [{"role": "user", "content": "injected"}]}))
    assert "manifest check failed" in reject_reason(out)


def test_redacted_runs_are_skipped_and_declined_attempts_can_be_exported_as_negatives(tmp_path):
    out = run_folder(tmp_path, "esc", client=decline(), escalator=fixer(), extra=["--escalate", "meta:muse-spark-1.3"])
    dest = tmp_path / "t.jsonl"
    s = export([out], dest, include_declined=True)
    labels = [json.loads(x)["label"] for x in dest.read_text().splitlines()]
    assert s["exported"] == 1 and s["negatives"] == 1 and labels == ["verified", "negative"]
    r = json.loads((out / "result.json").read_text())
    r["redactions"] = 2
    (out / "result.json").write_text(json.dumps(r))
    assert reject_reason(out) is not None   # the manifest no longer matches (and redactions would also block it)


def test_the_redaction_and_secret_rules_block_export_even_when_the_manifest_is_fine(tmp_path, monkeypatch):
    out = run_folder(tmp_path, "red", client=fixer())
    monkeypatch.setattr("upkeep.sft.verify_manifest", lambda run: [])
    r = json.loads((out / "result.json").read_text())
    r["redactions"] = 2
    (out / "result.json").write_text(json.dumps(r))
    assert "redacted" in reject_reason(out)
    r["redactions"], r["secrets_in_patch"] = 0, {"jwt": 1}
    (out / "result.json").write_text(json.dumps(r))
    assert "secret-like" in reject_reason(out)
