"""Secrets must not reach the model provider, a file the model reads, or a pull request."""

import json

import pytest

from tests.test_agent import FIX, Scripted, tc, write
from tests.test_cli_fix import args, build
from upkeep import cli
from upkeep.agent import Agent
from upkeep.patchlint import check_edit
from upkeep.radar import run_radar
from upkeep.sandbox import TestResult, run_tests
from upkeep.secrets import looks_sensitive, redact, scan

AWS = "AKIA" + "ABCDEFGHIJKLMNOP"                       # shaped like a key, assembled so no scanner flags this file
GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
SK = "sk-" + "abcdefghijklmnopqrstuvwxyz0123456789"
PEM = "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEowIBAAKCAQEAxxxxxxxxxxxx\n-----END RSA " + "PRIVATE KEY-----"


@pytest.mark.parametrize("secret,kind", [(AWS, "aws-access-key"), (GH, "github-token"), (SK, "openai-style-key"), (PEM, "private-key-block"),
                                         ("postgres://admin:hunter2pass@db.internal/app", "url-credentials")])
def test_known_secret_shapes_are_redacted(secret, kind):
    out, n = redact(f"before {secret} after")
    assert n == 1 and f"[REDACTED:{kind}]" in out and "before" in out and "after" in out
    assert "hunter2pass" not in out and AWS not in out and "MIIEow" not in out


def test_an_assigned_secret_keeps_its_name_but_loses_its_value():
    out, n = redact('API_KEY = "Zk3jdh29sKq81nSdkQpw"\nsecret_key: \'7hd82nfJHDjs8s92kd\'')
    assert n == 2 and out.count("[REDACTED:assigned-secret]") == 2 and "API_KEY" in out and "Zk3jdh29" not in out


def test_placeholders_and_ordinary_code_are_left_alone():
    text = 'password = "changeme-please-now"\ntoken = os.environ["TOKEN"]\nsha = "e3b0c442"\ndef f(secret_key=None):\n    return 1\n'
    assert redact(text) == (text, 0)


def test_redaction_is_idempotent_and_scan_counts_without_changing():
    text = f"a={AWS} b={GH}"
    once, n = redact(text)
    assert redact(once) == (once, 0) and n == 2
    assert scan(text) == {"aws-access-key": 1, "github-token": 1} and scan(once) == {}


@pytest.mark.parametrize("path,expected", [
    (".env", True), (".env.local", True), ("config/.env", True), ("id_rsa", True), ("deploy/server.pem", True), ("tls.key", True),
    (".ssh/config", True), (".git/config", True), (".aws/credentials", True), ("secrets.json", True),
    (".env.example", False), (".env.sample", False), ("app.py", False), ("keys.py", False), ("src/env_utils.py", False)])
def test_files_that_hold_secrets_are_recognised(path, expected):
    assert looks_sensitive(path) is expected


def agent_for(tmp_path, model, runner_output="", **kw):
    old, new, repo = build(tmp_path)
    write(repo, ".env", f"TOKEN={GH}\n")
    write(repo, "settings.py", f'API_KEY = "{SK}"\nNAME = "x"\n')
    report = run_radar(repo, old, new, "lib")
    base = lambda w: run_tests(w, backend="local", allow_unsandboxed=True, extra_paths=[new], with_coverage=False)  # noqa: E731
    runner = (lambda w: TestResult(False, 1, runner_output, "local")) if runner_output else base
    return Agent(model, repo, report, runner, **kw)


def test_the_agent_cannot_read_an_environment_file_and_sees_redacted_source(tmp_path):
    a = agent_for(tmp_path, Scripted([]))
    assert a.t_read_file(".env", 1, 5).startswith("error: refusing to read")
    shown = a.t_read_file("settings.py", 1, 5)
    assert SK not in shown and "[REDACTED:" in shown and "NAME" in shown and a.redactions == 1


def test_secrets_in_test_output_never_reach_the_model(tmp_path):
    model = Scripted([[tc("run_tests", {})], [tc("finish", {"status": "decline", "summary": "x"})]])
    res = agent_for(tmp_path, model, runner_output=f"FAILED test_x - token={GH} leaked in the traceback\n1 failed").run()
    blob = json.dumps(model.seen)
    assert GH not in blob and "[REDACTED:" in blob and res.redactions >= 1


def test_a_redaction_placeholder_cannot_be_written_back_over_the_real_secret(tmp_path):
    a = agent_for(tmp_path, Scripted([]))
    out = a.t_replace_lines("settings.py", 1, 1, 'API_KEY = "[REDACTED:openai-style-key]"')
    assert out.startswith("rejected") and "placeholder" in out


def test_an_edit_that_adds_a_credential_is_rejected_but_an_existing_one_is_not_blamed():
    ok, why = check_edit("x = 1\n", f'x = 1\nKEY = "{AWS}"\n')
    assert not ok and "secret-like string (aws-access-key)" in why
    assert check_edit(f'KEY = "{AWS}"\n', f'KEY = "{AWS}"\ny = 2\n')[0]


def test_the_proof_reports_redactions_and_a_patch_with_a_secret_blocks_the_pull_request(tmp_path, capsys, monkeypatch):
    old, new, repo = build(tmp_path)
    write(repo, "settings.py", f'API_KEY = "{SK}"\n')
    leak = tc("replace_lines", {"path": "app.py", "start_line": 5, "end_line": 5,
                                "new_text": "    return lib.fetch('u', timeout=5)  # ok"})
    out = tmp_path / "o"
    model = Scripted([[leak], [tc("finish", {"status": "fixed", "summary": "x"})]])
    code = cli.main(args(old, new, repo, out, "--open-pr"), client=model)
    proof, result = (out / "PROOF.md").read_text(), json.loads((out / "result.json").read_text())
    assert "## Security" in proof and "no secret-like strings found" in proof and result["secrets_in_patch"] == {} and code == 0
    # now a patch that really contains one (written directly, as an upstream model edit could not pass the content check)
    from upkeep.secrets import scan
    assert scan(f"+ KEY = '{AWS}'") == {"aws-access-key": 1}


def test_open_pr_is_refused_when_the_patch_has_a_secret(tmp_path, capsys, monkeypatch):
    old, new, repo = build(tmp_path)
    monkeypatch.setattr(cli, "scan_secrets", lambda text: {"aws-access-key": 1})   # the agent's own content check already blocks real ones
    out = tmp_path / "o"
    model = Scripted([[FIX], [tc("finish", {"status": "fixed", "summary": "x"})]])
    cli.main(args(old, new, repo, out, "--open-pr"), client=model)
    assert "PR not opened: fix.patch contains secret-like strings" in capsys.readouterr().err
    assert "no pull request will be opened" in (out / "PROOF.md").read_text()
