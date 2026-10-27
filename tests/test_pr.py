import subprocess
from pathlib import Path

import pytest

from upkeep.pr import PRError, open_draft_pr, plan_pr

PATCH = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"


def sh(*a, cwd):
    return subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    bare, work = tmp_path / "origin.git", tmp_path / "work"
    sh("git", "init", "-q", "--bare", str(bare), cwd=tmp_path)
    sh("git", "init", "-q", "-b", "main", str(work), cwd=tmp_path)
    for k, v in (("user.name", "t"), ("user.email", "t@example.com")):
        sh("git", "config", k, v, cwd=work)
    (work / "app.py").write_text("x = 1\n")
    sh("git", "add", ".", cwd=work); sh("git", "commit", "-qm", "init", cwd=work)
    sh("git", "remote", "add", "origin", str(bare), cwd=work); sh("git", "push", "-q", "-u", "origin", "main", cwd=work)
    (tmp_path / "fix.patch").write_text(PATCH); (tmp_path / "PROOF.md").write_text("# proof\n")
    return work, bare, tmp_path / "fix.patch", tmp_path / "PROOF.md"


class FakeGh:
    """Real git, fake `gh`: records the call instead of contacting GitHub."""
    def __init__(self): self.calls = []
    def __call__(self, cmd, **kw):
        if cmd[0] == "gh":
            self.calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "https://github.com/o/r/pull/1\n", "")
        return subprocess.run(cmd, **kw)


def test_dry_run_changes_nothing(repos):
    work, bare, patch, proof = repos
    gh = FakeGh()
    plan = open_draft_pr(work, patch, proof, "lib", "2.0", "verified", run=gh)
    assert not plan.executed and plan.branch == "upkeep/lib-2.0" and len(plan.steps) == 4 and gh.calls == []
    assert sh("git", "branch", "--list", "upkeep/*", cwd=work) == ""
    assert sh("git", "ls-remote", "--heads", str(bare), cwd=work).count("upkeep") == 0


def test_confirmed_pr_pushes_a_branch_opens_a_draft_and_restores_main(repos):
    work, bare, patch, proof = repos
    gh = FakeGh()
    plan = open_draft_pr(work, patch, proof, "lib", "2.0", "verified", confirm=True, run=gh)
    assert plan.executed and plan.url.endswith("/pull/1")
    cmd = gh.calls[0]
    assert "--draft" in cmd and "--body-file" in cmd and cmd[cmd.index("--base") + 1] == "main"
    assert "upkeep/lib-2.0" in sh("git", "ls-remote", "--heads", str(bare), cwd=work)
    assert sh("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=work) == "main"   # the user's branch is back
    assert (work / "app.py").read_text() == "x = 1\n"                           # and unchanged
    assert "Co-Authored-By" not in sh("git", "log", "-1", "--format=%B", "upkeep/lib-2.0", cwd=work)


def test_refusals(repos, tmp_path):
    work, bare, patch, proof = repos
    with pytest.raises(PRError, match="declined"):
        plan_pr(work, "lib", "2.0", "declined")
    (work / "dirty.txt").write_text("x")
    with pytest.raises(PRError, match="not clean"):
        open_draft_pr(work, patch, proof, "lib", "2.0", "verified", run=FakeGh())
    (work / "dirty.txt").unlink()
    empty = tmp_path / "empty.patch"; empty.write_text("")
    with pytest.raises(PRError, match="empty"):
        open_draft_pr(work, empty, proof, "lib", "2.0", "verified", run=FakeGh())


def test_failed_apply_still_returns_to_the_users_branch(repos):
    work, bare, patch, proof = repos
    patch.write_text("--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-nothing like the file\n+y\n")
    with pytest.raises(PRError):
        open_draft_pr(work, patch, proof, "lib", "2.0", "verified", confirm=True, run=FakeGh())
    assert sh("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=work) == "main"
