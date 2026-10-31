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
    monkeypatch.setattr(cli, "scan_secrets", lambda text, *a: {"aws-access-key": 1})   # the agent's own content check already blocks real ones
    out = tmp_path / "o"
    model = Scripted([[FIX], [tc("finish", {"status": "fixed", "summary": "x"})]])
    cli.main(args(old, new, repo, out, "--open-pr"), client=model)
    assert "PR not opened: fix.patch contains secret-like strings" in capsys.readouterr().err
    assert "no pull request will be opened" in (out / "PROOF.md").read_text()


@pytest.mark.parametrize("secret,kind", [
    ("pypi-" + "AgEIcHlwaS5vcmcCJGFiY2RlZmdoaWprbG1ub3BxcnN0dXZ3eHl6MDEyMzQ1Njc4OQ", "pypi-token"),
    ("npm_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8", "npm-token"),
    ("SG." + "abcdefghijklmnop" + "." + "qrstuvwxyz012345", "sendgrid-key"),
    ("SK" + "0123456789abcdef0123456789abcdef", "twilio-key"),
    ("AccountKey=" + "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG=", "azure-storage-key"),
    ("Authorization: Bearer " + "abcdefghijklmnopqrstuvwxyz012345", "bearer-token"),
])
def test_more_service_tokens_are_redacted(secret, kind):
    out, n = redact(f"x {secret} y")
    assert n >= 1 and f"[REDACTED:{kind}]" in out


@pytest.mark.parametrize("loader", ["yaml.FullLoader", "yaml.CFullLoader", "yaml.full_load", "yaml.full_load_all"])
def test_full_loader_is_no_longer_accepted_from_the_agent(loader):
    ok, why = check_edit("import yaml\nx = 1\n", f"import yaml\nx = 1\ny = {loader}\n")
    assert not ok and "SafeLoader" in why


def test_everything_sent_to_the_model_is_saved_redacted_and_covered_by_the_manifest(tmp_path):
    from upkeep.attest import verify_manifest
    old, new, repo = build(tmp_path)
    write(repo, "settings.py", f'API_KEY = "{SK}"\n')
    out = tmp_path / "o"
    model = Scripted([[tc("read_file", {"path": "settings.py", "start_line": 1, "end_line": 3})], [FIX], [tc("finish", {"status": "fixed", "summary": "x"})]])
    cli.main(args(old, new, repo, out), client=model)
    log = (out / "model_transcript.json").read_text()
    data = json.loads(log)
    assert SK not in log and "[REDACTED:" in log                       # what was sent, with the secret already cut out
    assert data["agent_messages"][0]["role"] == "system" and any(m["role"] == "tool" for m in data["agent_messages"])
    assert "model_transcript.json" in json.loads((out / "MANIFEST.json").read_text())["files"]
    assert verify_manifest(out) == []
    (out / "model_transcript.json").write_text("{}")
    assert any("model_transcript.json: CHANGED" in p for p in verify_manifest(out))


def test_probe_prompts_are_part_of_the_audit_log(tmp_path):
    from upkeep.charact import BehaviorChecker, Target, propose_inputs
    from upkeep.llm import Reply

    class Echo:
        def chat(self, messages, **kw):
            return Reply({"role": "assistant", "content": "[[1]]"}, {})
    log: list[str] = []
    secret_fn = f'def f(x):\n    k = "{SK}"\n    return x\n'
    propose_inputs(Echo(), Target("m", "f", secret_fn, ["x"]), log=log)
    assert len(log) == 1 and SK not in log[0] and "[REDACTED:" in log[0]


# ---------------------------------------------------------------- exact values and entropy (not only patterns)
ODD = "q8Zr-" + "t3Vw.k9Xm_L2pN"        # a made-up token shape that no pattern knows


def test_a_known_value_from_the_environment_is_redacted_wherever_it_appears():
    from upkeep.secrets import known_secret_values
    known = known_secret_values(None, {"MY_SERVICE_TOKEN": ODD, "HOME": "/home/someone", "SHORT_KEY": "abc", "EDITOR": "vim-with-a-long-name"})
    assert known == {ODD}                         # only secret-named variables, and only values long enough to be real
    out, n = redact(f"log line: connecting with {ODD} now; again {ODD}", known)
    assert ODD not in out and n == 2 and out.count("[REDACTED:known-secret]") == 2
    assert scan(f"x {ODD}", known) == {"known-secret": 1} and scan(f"x {ODD}") == {}


def test_values_in_env_files_are_known_but_templates_are_not(tmp_path):
    from upkeep.secrets import known_secret_values
    (tmp_path / ".env").write_text(f'# comment\nDB_PASSWORD="{ODD}"\nTINY=abc\nAPI_URL=https://example.test/v1\n')
    (tmp_path / ".env.example").write_text("DB_PASSWORD=replace-me-with-yours\n")
    known = known_secret_values(tmp_path, {})
    assert ODD in known and "abc" not in known and "replace-me-with-yours" not in known


def test_a_random_looking_key_of_unknown_shape_is_caught_but_hashes_and_prose_are_not():
    key = "Xk9mQ2vL8pR4tY7wZ1nB5cD3fG6hJ0sA"          # mixed case, digits, high entropy
    out, n = redact(f'client = Client("{key}")')
    assert n == 1 and key not in out and "[REDACTED:high-entropy-string]" in out
    for harmless in ("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",       # a sha256
                     "123e4567-e89b-12d3-a456-426614174000",                                     # a uuid
                     "this_is_a_long_but_ordinary_identifier_name_for_a_function",
                     "the quick brown fox jumps over the lazy dog again and again"):
        text = f'x = "{harmless}"'
        assert redact(text) == (text, 0), harmless


def test_the_agent_redacts_known_values_from_files_and_test_output(tmp_path):
    old, new, repo = build(tmp_path)
    write(repo, ".env", f"INTERNAL_SECRET={ODD}\n")
    write(repo, "settings.py", f'ENDPOINT = "https://{ODD}@internal.example/"\nNAME = "x"\n')   # not matched by any pattern except exact value
    report = run_radar(repo, old, new, "lib")
    model = Scripted([[tc("read_file", {"path": "settings.py", "start_line": 1, "end_line": 3})], [tc("run_tests", {})],
                      [tc("finish", {"status": "decline", "summary": "x"})]])
    runner = lambda w: TestResult(False, 1, f"FAILED test_x - assert '{ODD}' == 'y'\n1 failed", "local")  # noqa: E731
    res = Agent(model, repo, report, runner).run()
    assert ODD not in json.dumps(model.seen) and res.redactions >= 2
