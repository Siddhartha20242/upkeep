"""Open a DRAFT pull request from a verified fix. Outward-facing, so: dry-run unless confirm=True.

Safety rules enforced here:
  * the working tree must be clean and a git repo must have an `origin` remote;
  * a branch is created from the current one; the user's branch is never modified;
  * the PR is always a draft and its body is the proof bundle, so a human reviews the evidence;
  * only a "verified" or "tests-pass-but-unverified-sites" result can be proposed (never a decline);
  * no commit trailer is added: attribution lines are the user's choice.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

OK_STATUSES = ("verified", "tests-pass-but-unverified-sites")
Run = Callable[..., subprocess.CompletedProcess]


class PRError(RuntimeError):
    pass


@dataclass
class PRPlan:
    branch: str
    title: str
    base: str
    steps: list[str] = field(default_factory=list)
    url: str | None = None
    executed: bool = False


def _git(run: Run, repo: Path, *args: str) -> str:
    p = run(["git", *args], cwd=repo, capture_output=True, text=True)
    if p.returncode != 0:
        raise PRError(f"git {' '.join(args[:2])} failed: {(p.stderr or p.stdout).strip()[:200]}")
    return p.stdout.strip()


def plan_pr(repo: Path, package: str, new_version: str, status: str, base: str = "main") -> PRPlan:
    if status not in OK_STATUSES:
        raise PRError(f"refusing to propose a PR for status {status!r}")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{package}-{new_version}").strip("-")
    title = f"Upgrade {package} to {new_version} (Upkeep: {status})"
    return PRPlan(branch=f"upkeep/{safe}", title=title, base=base)


def open_draft_pr(
    repo: Path,
    patch: Path,
    proof_md: Path,
    package: str,
    new_version: str,
    status: str,
    base: str = "main",
    confirm: bool = False,
    run: Run = subprocess.run,
) -> PRPlan:
    repo = Path(repo)
    plan = plan_pr(repo, package, new_version, status, base)
    if not Path(patch).read_text().strip():
        raise PRError("the patch is empty; nothing to propose")
    if _git(run, repo, "status", "--porcelain"):
        raise PRError("working tree is not clean; commit or stash your changes first")
    _git(run, repo, "remote", "get-url", "origin")  # fails if there is no origin
    original = _git(run, repo, "rev-parse", "--abbrev-ref", "HEAD")
    plan.steps = [
        f"create branch {plan.branch} from {original}",
        f"apply {Path(patch).name} and commit",
        f"push {plan.branch} to origin",
        f"open a DRAFT pull request into {plan.base} with {Path(proof_md).name} as the body",
    ]
    if not confirm:
        return plan  # dry run: nothing has been changed

    _git(run, repo, "checkout", "-b", plan.branch)
    try:
        _git(run, repo, "apply", "--index", str(Path(patch).resolve()))
        _git(run, repo, "commit", "-m", plan.title, "-m", f"Proposed by Upkeep. See the proof bundle in the pull request body.")
        _git(run, repo, "push", "-u", "origin", plan.branch)
        p = run(["gh", "pr", "create", "--draft", "--base", plan.base, "--head", plan.branch, "--title", plan.title,
                 "--body-file", str(Path(proof_md).resolve())], cwd=repo, capture_output=True, text=True)
        if p.returncode != 0:
            raise PRError(f"gh pr create failed: {(p.stderr or p.stdout).strip()[:200]}")
        plan.url = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else None
        plan.executed = True
    finally:
        run(["git", "checkout", original], cwd=repo, capture_output=True, text=True)  # always leave the user's branch checked out
    return plan
