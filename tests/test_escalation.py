"""Escalation: the cheap open-weight model first; a stronger model only if the verifier says the first attempt failed."""

import json

import pytest

from tests.test_agent import FIX, Scripted, tc
from tests.test_cli_fix import args, build
from upkeep import cli


def named(model, name):
    model.model = name
    return model


def decline():
    return named(Scripted([[tc("finish", {"status": "decline", "summary": "cannot infer the exact rules"})]]), "meta-models/muse-spark-open-1026")


def fixer():
    return named(Scripted([[FIX], [tc("finish", {"status": "fixed", "summary": "x"})]]), "muse-spark-1.3")


def test_a_declined_first_attempt_escalates_and_the_proof_records_both(tmp_path, capsys):
    old, new, repo = build(tmp_path)
    out = tmp_path / "o"
    code = cli.main(args(old, new, repo, out, "--escalate", "meta:muse-spark-1.3"), client=decline(), escalator=fixer())
    assert code == 0
    text = capsys.readouterr().out
    assert "Escalating to muse-spark-1.3" in text and "the verifier still decides" in text
    result = json.loads((out / "result.json").read_text())
    assert [a["status"] for a in result["attempts"]] == ["declined", "verified"]
    assert [a["model"] for a in result["attempts"]] == ["meta-models/muse-spark-open-1026", "muse-spark-1.3"]
    assert result["attempts"][1]["explore"] is True and result["attempts"][1]["max_turns"] == 40 and result["status"] == "verified"
    assert result["model"] == "muse-spark-1.3" and result["provider"] == "meta"          # the headline model produced the final result
    assert result["tokens"] == sum(a["tokens"] for a in result["attempts"])
    proof = (out / "PROOF.md").read_text()
    assert "escalated" in proof and "attempt 1: `meta-models/muse-spark-open-1026`" in proof and "attempt 2: `muse-spark-1.3`" in proof
    assert "declined" in proof and "verified" in proof and "the verifier decided both times" in proof


def test_the_earlier_attempts_conversation_is_kept_in_the_audit_log(tmp_path):
    old, new, repo = build(tmp_path)
    out = tmp_path / "o"
    cli.main(args(old, new, repo, out, "--escalate", "meta:muse-spark-1.3"), client=decline(), escalator=fixer())
    log = json.loads((out / "model_transcript.json").read_text())
    assert len(log["earlier_attempts"]) == 1 and log["earlier_attempts"][0]["model"] == "meta-models/muse-spark-open-1026"
    assert log["earlier_attempts"][0]["agent_messages"] and log["agent_messages"]


def test_no_escalation_when_the_first_attempt_verifies(tmp_path):
    old, new, repo = build(tmp_path)
    out = tmp_path / "o"

    class Boom:
        def __getattr__(self, name):
            raise AssertionError("the escalation model must not be touched")
    code = cli.main(args(old, new, repo, out, "--escalate", "meta:muse-spark-1.3"),
                    client=named(Scripted([[FIX], [tc("finish", {"status": "fixed", "summary": "x"})]]), "m"), escalator=Boom())
    assert code == 0 and len(json.loads((out / "result.json").read_text())["attempts"]) == 1


def test_a_failed_escalation_is_reported_as_declined_with_both_attempts(tmp_path):
    old, new, repo = build(tmp_path)
    out = tmp_path / "o"
    second = named(Scripted([[tc("finish", {"status": "decline", "summary": "still cannot"})]]), "muse-spark-1.3")
    code = cli.main(args(old, new, repo, out, "--escalate", "meta:muse-spark-1.3"), client=decline(), escalator=second)
    assert code == 1
    result = json.loads((out / "result.json").read_text())
    assert [a["status"] for a in result["attempts"]] == ["declined", "declined"] and result["status"] == "declined"


@pytest.mark.parametrize("bad", ["muse-spark-1.3", "openai:gpt", "meta:", ":x"])
def test_a_malformed_escalation_spec_is_a_usage_error(tmp_path, bad, capsys):
    old, new, repo = build(tmp_path)
    assert cli.main(args(old, new, repo, tmp_path / "o", "--escalate", bad), client=decline()) == 2
    assert "PROVIDER:MODEL" in capsys.readouterr().err


def test_without_the_flag_there_is_exactly_one_attempt(tmp_path):
    old, new, repo = build(tmp_path)
    out = tmp_path / "o"
    cli.main(args(old, new, repo, out), client=decline())
    result = json.loads((out / "result.json").read_text())
    assert len(result["attempts"]) == 1 and result["status"] == "declined"
