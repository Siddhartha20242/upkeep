import json

import pytest

from upkeep import attest, cli


def run_dir(tmp_path):
    d = tmp_path / "out"
    d.mkdir()
    (d / "fix.patch").write_text("diff\n")
    (d / "PROOF.md").write_text("# proof\n")
    (d / "result.json").write_text("{}")
    attest.write_manifest(d, "pkg", "1", "2", "verified")
    return d


def test_untouched_run_verifies(tmp_path):
    assert attest.verify_manifest(run_dir(tmp_path)) == []


def test_edited_patch_is_detected(tmp_path):
    d = run_dir(tmp_path)
    (d / "fix.patch").write_text("diff\n+evil\n")
    assert any("fix.patch" in p and "CHANGED" in p for p in attest.verify_manifest(d))


def test_missing_and_extra_files_detected(tmp_path):
    d = run_dir(tmp_path)
    (d / "PROOF.md").unlink()
    assert any("missing" in p for p in attest.verify_manifest(d))
    d2 = tmp_path / "o2"
    d2.mkdir()
    (d2 / "fix.patch").write_text("a")
    attest.write_manifest(d2, "p", "1", "2", "declined")
    (d2 / "PROOF.md").write_text("late addition")
    assert any("not in the manifest" in p for p in attest.verify_manifest(d2))


def test_no_manifest(tmp_path):
    assert attest.verify_manifest(tmp_path) == ["MANIFEST.json not found"]


def test_cli_verify_exit_codes(tmp_path, capsys):
    d = run_dir(tmp_path)
    assert cli.main(["verify", "--run", str(d)]) == 0
    (d / "result.json").write_text("{\"status\": \"verified\"}")
    assert cli.main(["verify", "--run", str(d)]) == 2


def test_signing_roundtrip_and_tamper(tmp_path):
    pytest.importorskip("cryptography")
    d = run_dir(tmp_path)
    key = tmp_path / "k.pem"
    attest.generate_key(key)
    assert oct(key.stat().st_mode & 0o777) == "0o600"
    with pytest.raises(attest.AttestError):
        attest.generate_key(key)  # never overwrite a key
    attest.sign_manifest(d, key)
    pub = (tmp_path / "k.pem.pub").read_text().strip()
    assert attest.verify_signature(d, pub)[0]
    assert not attest.verify_signature(d, "00" * 32)[0]  # a different key
    m = json.loads((d / "MANIFEST.json").read_text())
    m["status"] = "verified-lie"
    (d / "MANIFEST.json").write_text(json.dumps(m))
    assert not attest.verify_signature(d, pub)[0]
