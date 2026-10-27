import json
from pathlib import Path

from tests.test_agent import FIX, Scripted, tc, write
from upkeep import cli


def build(tmp_path: Path):
    old, new, repo = tmp_path / "old", tmp_path / "new", tmp_path / "repo"
    write(old, "lib/__init__.py", "def fetch(url, timeout=10):\n    return 'ok'\n")
    write(new, "lib/__init__.py", "def fetch(url, *, timeout):\n    return 'ok'\n")
    write(repo, "app.py", "import lib\n\n\ndef go():\n    return lib.fetch('u', 5)\n")
    write(repo, "test_app.py", "import app\n\n\ndef test_go():\n    assert app.go() == 'ok'\n")
    return old, new, repo


def args(old, new, repo, out, *extra):
    return ["fix", "--repo", str(repo), "--old", str(old), "--new", str(new), "--package", "lib",
            "--out", str(out), "--test-backend", "local", "--unsandboxed", *extra]


def test_fix_writes_patch_and_proof_and_leaves_repo_untouched(tmp_path):
    old, new, repo = build(tmp_path)
    model = Scripted([[FIX], [tc("finish", {"status": "fixed", "summary": "x"})]])
    out = tmp_path / "out"
    code = cli.main(args(old, new, repo, out, "--old-version", "1.0", "--new-version", "2.0"), client=model)
    assert code == 0
    assert "timeout=5" in (out / "fix.patch").read_text()
    proof = (out / "PROOF.md").read_text()
    assert "lib 1.0 -> 2.0" in proof and "`verified`" in proof and "What this does NOT prove" in proof
    assert json.loads((out / "result.json").read_text())["status"] == "verified"
    assert "lib.fetch('u', 5)" in (repo / "app.py").read_text()


def test_fix_decline_exits_nonzero_and_says_why(tmp_path):
    old, new, repo = build(tmp_path)
    model = Scripted([[tc("finish", {"status": "decline", "summary": "cannot infer timeout"})]])
    out = tmp_path / "out"
    assert cli.main(args(old, new, repo, out), client=model) == 1
    assert "declined" in (out / "PROOF.md").read_text().lower()


def test_fix_refuses_unsandboxed_local_without_flag(tmp_path, capsys):
    old, new, repo = build(tmp_path)
    code = cli.main(["fix", "--repo", str(repo), "--old", str(old), "--new", str(new), "--package", "lib",
                     "--test-backend", "local"], client=Scripted([]))
    assert code == 2 and "NOT isolated" in capsys.readouterr().err


def test_fix_does_nothing_when_radar_is_green(tmp_path, capsys):
    old, new, repo = build(tmp_path)
    write(repo, "app.py", "print('no lib here')\n")
    assert cli.main(args(old, new, repo, tmp_path / "out"), client=Scripted([])) == 0
    assert "nothing to fix" in capsys.readouterr().out


def test_uncovered_site_is_reported_not_called_verified(tmp_path):
    old, new, repo = build(tmp_path)
    write(repo, "app.py", "import lib\n\n\ndef go():\n    return lib.fetch('u', 5)\n\n\ndef rare():\n    return lib.fetch('r', 7)\n")
    fix_rare = tc("replace_lines", {"path": "app.py", "start_line": 9, "end_line": 9,
                                    "new_text": "    return lib.fetch('r', timeout=7)"}, "3")
    model = Scripted([[FIX], [fix_rare], [tc("finish", {"status": "fixed", "summary": "x"})]])
    out = tmp_path / "out"
    assert cli.main(args(old, new, repo, out), client=model) == 0
    proof = (out / "PROOF.md").read_text()
    assert "`tests-pass-but-unverified-sites`" in proof
    assert "NOT covered: `app.py:9`" in proof
