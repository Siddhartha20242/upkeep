"""CLI behavior that users and CI scripts depend on: output, exit codes and refusals."""

import json

import pytest

from tests.test_agent import FIX, Scripted, tc, write
from tests.test_cli_fix import args, build
from upkeep import cli
from upkeep.vuln import Advisory


def libs(tmp_path, old_src, new_src, repo_src):
    old, new, repo = tmp_path / "old", tmp_path / "new", tmp_path / "repo"
    write(old, "lib/__init__.py", old_src)
    write(new, "lib/__init__.py", new_src)
    write(repo, "app.py", repo_src)
    return old, new, repo


def test_radar_text_output_and_exit_codes(tmp_path, capsys):
    old, new, repo = libs(tmp_path, "def f(a, t=1):\n    pass\n", "def f(a, *, t):\n    pass\n", "import lib\nlib.f(1, 2)\n")
    cmd = ["radar", "--repo", str(repo), "--old", str(old), "--new", str(new), "--package", "lib", "--coverage", "none"]
    assert cli.main(cmd) == 2
    out = capsys.readouterr().out
    assert "lib: RED" in out and "used at app.py:2" in out
    (repo / "app.py").write_text("import lib\nlib.f(1, t=2)\n")
    assert cli.main(cmd) == 0
    assert "lib: GREEN" in capsys.readouterr().out


def test_radar_json_is_valid_json(tmp_path, capsys):
    old, new, repo = libs(tmp_path, "def f(a, t=1):\n    pass\n", "def f(a, *, t):\n    pass\n", "import lib\nlib.f(1, 2)\n")
    cli.main(["radar", "--repo", str(repo), "--old", str(old), "--new", str(new), "--package", "lib", "--coverage", "none", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["risk"] == "red" and data["affected"]


def test_radar_with_local_coverage_requires_the_unsandboxed_flag(tmp_path, capsys):
    old, new, repo = libs(tmp_path, "def f(a):\n    pass\n", "def f(a):\n    pass\n", "import lib\nlib.f(1)\n")
    base = ["radar", "--repo", str(repo), "--old", str(old), "--new", str(new), "--package", "lib", "--coverage", "local"]
    assert cli.main(base) == 2 and "refused" in capsys.readouterr().err


def test_radar_refuses_a_library_folder_without_python_source(tmp_path, capsys):
    repo, empty = tmp_path / "repo", tmp_path / "empty"
    write(repo, "app.py", "import lib\n")
    (empty / "lib").mkdir(parents=True)
    code = cli.main(["radar", "--repo", str(repo), "--old", str(empty), "--new", str(empty), "--package", "lib", "--coverage", "none"])
    assert code == 3 and "refusing to report" in capsys.readouterr().err


@pytest.mark.parametrize("result,code,text", [
    ([], 0, "no advisories"),
    ([Advisory("OSV-1", "bad thing", severity="HIGH", fixed_versions=["2.0"])], 1, "OSV-1"),
    (RuntimeError("offline"), 3, "lookup failed"),
])
def test_vulns_exit_codes_never_confuse_a_network_failure_with_no_vulnerabilities(monkeypatch, capsys, result, code, text):
    def fake(*a, **k):
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(cli, "query_osv", fake)
    assert cli.main(["vulns", "--package", "x", "--version", "1"]) == code
    cap = capsys.readouterr()
    assert text in cap.out + cap.err


def test_fix_can_sign_and_verify_detects_tampering(tmp_path, capsys):
    pytest.importorskip("cryptography")
    old, new, repo = build(tmp_path)
    key, out = tmp_path / "k.pem", tmp_path / "out"
    assert cli.main(["keygen", "--key", str(key)]) == 0
    assert cli.main(["keygen", "--key", str(key)]) == 1   # never overwrites a key
    model = Scripted([[FIX], [tc("finish", {"status": "fixed", "summary": "x"})]])
    assert cli.main(args(old, new, repo, out, "--sign-key", str(key)), client=model) == 0
    assert "signed manifest with key" in capsys.readouterr().out
    assert cli.main(["verify", "--run", str(out), "--pubkey", str(key) + ".pub"]) == 0
    assert "valid signature" in capsys.readouterr().out
    (out / "PROOF.md").write_text((out / "PROOF.md").read_text() + "\nforged\n")
    assert cli.main(["verify", "--run", str(out)]) == 2
    assert "CHANGED" in capsys.readouterr().out


def test_proof_contains_integrity_hashes_and_the_manifest_covers_it(tmp_path):
    old, new, repo = build(tmp_path)
    out = tmp_path / "out"
    model = Scripted([[FIX], [tc("finish", {"status": "fixed", "summary": "x"})]])
    cli.main(args(old, new, repo, out), client=model)
    proof = (out / "PROOF.md").read_text()
    assert "## Integrity" in proof and "fix.patch SHA-256" in proof
    manifest = json.loads((out / "MANIFEST.json").read_text())
    assert set(manifest["files"]) == {"fix.patch", "PROOF.md", "result.json"} and manifest["status"] == "verified"


def test_dashboard_command_writes_a_self_contained_escaped_page(tmp_path, capsys):
    old, new, repo = build(tmp_path)
    out = tmp_path / "out"
    cli.main(args(old, new, repo, out), client=Scripted([[FIX], [tc("finish", {"status": "fixed", "summary": "<script>alert(1)</script>"})]]))
    page = tmp_path / "d.html"
    assert cli.main(["dashboard", "--run", str(out), "--out", str(page)]) == 0
    html = page.read_text()
    assert "<script>alert(1)</script>" not in html and "http://" not in html.replace("http://www.w3.org", "")


def test_audit_json_lists_skipped_and_checked_dependencies(tmp_path, capsys, monkeypatch):
    from upkeep import audit as au
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "requirements.txt").write_text("alpha==1.0\nbeta==1.0\n")
    monkeypatch.setattr(au, "latest_version", lambda d: "2.0")

    def fake_sources(dist, old, new, module=None, **k):
        if dist == "beta":
            raise au.PyPIError("no source archive")
        raise au.PyPIError("module folder not found")
    monkeypatch.setattr(au, "sources_for", fake_sources)
    assert cli.main(["audit", "--repo", str(repo), "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {r["dist"]: r["status"] for r in rows} == {"alpha": "skipped", "beta": "skipped"}
    assert all(r["reason"] for r in rows)
