from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agent import Agent
from .charact import BehaviorChecker
from .dashboard import build as build_dashboard
from .llm import client_for, load_env
from .pr import PRError, open_draft_pr
from .proof import ProofBundle
from .radar import run_radar
from .sandbox import SandboxRefused, run_python, run_tests
from .vuln import query_osv


def _radar(args) -> int:
    executed = None
    if args.coverage != "none":
        try:
            res = run_tests(
                args.repo,
                backend=args.coverage,
                extra_paths=[args.old],  # tests run against the CURRENT version
                allow_unsandboxed=args.unsandboxed,
            )
        except SandboxRefused as e:
            print(f"refused: {e}", file=sys.stderr)
            return 2
        if res.executed_lines is None:
            print(f"warning: no coverage data ({res.backend}): {res.output[-300:]}", file=sys.stderr)
        executed = res.executed_lines

    report = run_radar(args.repo, args.old, args.new, args.package, executed)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(f"{report.package}: {report.risk.upper()}" + ("" if report.coverage_known else "  (coverage not measured)"))
        for change, sites in report.affected:
            print(f"  {change.symbol}: {change.kind} ({change.detail})")
            for s in sites:
                mark = " [NOT covered by tests]" if s in report.uncovered else ""
                print(f"    used at {s.file}:{s.line}{mark}")
        unused = len(report.changes) - len(report.affected)
        if unused:
            print(f"  {unused} breaking change(s) do not touch code in this repo")
    return {"green": 0, "amber": 1, "red": 2}[report.risk]


def _vulns(args) -> int:
    try:
        advisories = query_osv(args.package, args.version)
    except Exception as e:  # network failure must not look like "no vulnerabilities"
        print(f"lookup failed: {e}", file=sys.stderr)
        return 3
    if not advisories:
        print(f"{args.package}=={args.version}: no advisories reported by OSV")
        return 0
    for a in advisories:
        fixed = ", ".join(a.fixed_versions) or "no fixed version listed"
        print(f"{a.id} [{a.severity or 'n/a'}] {a.summary} (fixed in {fixed})")
    return 1


def _fix(args, client=None) -> int:
    if args.test_backend == "local" and not args.unsandboxed:
        print("refused: --test-backend local runs this repo's tests on this machine, NOT isolated. "
              "Pass --unsandboxed to allow it, or use --test-backend docker.", file=sys.stderr)
        return 2
    report = run_radar(args.repo, args.old, args.new, args.package)
    if not report.affected:
        print(f"{args.package}: nothing to fix. The static radar found no breaking change touching this repo.")
        print("(Behavior changes that keep signatures identical are invisible to the radar.)")
        return 0

    new_paths = None if args.deps else [args.new]  # a deps folder already holds the new library

    def runner(w, coverage=False):
        return run_tests(w, backend=args.test_backend, allow_unsandboxed=args.unsandboxed,
                         extra_paths=new_paths, deps_dir=args.deps, image=args.image, with_coverage=coverage)

    # Measure which affected call sites the repo's own tests actually execute (a line counts as
    # covered even if the call raises). Without this, a green run can't be called "verified".
    base = runner(args.repo, coverage=True)
    report = run_radar(args.repo, args.old, args.new, args.package, executed_lines=base.executed_lines)
    runner_plain = lambda w: runner(w)  # noqa: E731
    changelog = args.changelog.read_text() if args.changelog else None
    client = client or client_for(args.provider, args.model)
    checker = None
    if args.behavior_check:
        old_paths = None if (args.old_deps or args.deps) else [args.old]
        sites = [s for _, ss in report.affected for s in ss]
        world = lambda paths, deps: (lambda d, script: run_python(  # noqa: E731
            d, script, backend=args.test_backend, allow_unsandboxed=args.unsandboxed,
            extra_paths=paths, deps_dir=deps, image=args.image))
        checker = BehaviorChecker(client, args.repo, sites, world(old_paths, args.old_deps), world(new_paths, args.deps))
    agent = Agent(client, args.repo, report, runner_plain, changelog=changelog, behavior_checker=checker)
    res = agent.run()

    bundle = ProofBundle(
        args.package, args.old_version, args.new_version, report,
        tests_before=agent.baseline_result, tests_after=res.final_tests,
        injection_flags=res.injection_flags,
        declined_reason=res.reason if res.status == "declined" else None, patch=res.diff,
        behavior=res.behavior,
    )
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "fix.patch").write_text(res.diff)
    md = bundle.to_markdown()
    md += ("\n## Agent run\n"
           f"- model calls: {res.model_calls}, tokens: {res.total_tokens}, guard rejections: {res.guard_rejections}\n"
           "- tool trace: " + " > ".join(s["tool"] for s in res.steps) + "\n")
    (args.out / "PROOF.md").write_text(md)
    (args.out / "result.json").write_text(json.dumps(
        {"status": res.status, "reason": res.reason, "model_calls": res.model_calls,
         "tokens": res.total_tokens, "guard_rejections": res.guard_rejections,
         "injection_flags": res.injection_flags, "steps": res.steps}, indent=2))
    print(f"{args.package}: {res.status.upper()}  ({res.reason})")
    if args.open_pr:
        status = bundle.status()
        try:
            plan = open_draft_pr(args.repo, args.out / "fix.patch", args.out / "PROOF.md", args.package, args.new_version,
                                 status, base=args.base, confirm=args.confirm)
            print(("OPENED " if plan.executed else "DRY RUN (add --confirm to do this): ") + plan.title)
            print("  " + "\n  ".join(plan.steps))
            if plan.url:
                print(f"  draft PR: {plan.url}")
        except PRError as e:
            print(f"PR not opened: {e}", file=sys.stderr)
    print(f"wrote {args.out}/fix.patch, PROOF.md, result.json  (the repo itself was NOT modified)")
    agent.cleanup()
    return 0 if res.status == "verified" else 1


def main(argv: list[str] | None = None, client=None) -> int:
    p = argparse.ArgumentParser(prog="upkeep")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("radar", help="show what upgrading a package would break")
    r.add_argument("--repo", required=True, type=Path)
    r.add_argument("--old", required=True, type=Path, help="source root of the current version")
    r.add_argument("--new", required=True, type=Path, help="source root of the new version")
    r.add_argument("--package", required=True)
    r.add_argument("--coverage", choices=["none", "docker", "local"], default="none",
                   help="run the repo's tests under coverage to tell amber from red")
    r.add_argument("--unsandboxed", action="store_true",
                   help="allow --coverage local (runs repo tests on this machine, NOT isolated)")
    r.add_argument("--json", action="store_true")
    r.set_defaults(fn=_radar)

    f = sub.add_parser("fix", help="let the agent propose a verified fix (the repo is never modified)")
    f.add_argument("--repo", required=True, type=Path)
    f.add_argument("--old", required=True, type=Path, help="source root of the current version")
    f.add_argument("--new", required=True, type=Path, help="source root of the new version (also put on the test path)")
    f.add_argument("--package", required=True)
    f.add_argument("--changelog", type=Path, help="release notes file (treated as untrusted data)")
    f.add_argument("--out", type=Path, default=Path("upkeep-out"))
    f.add_argument("--old-version", default="old")
    f.add_argument("--new-version", default="new")
    f.add_argument("--provider", choices=["baseten", "meta"], default="baseten")
    f.add_argument("--model")
    f.add_argument("--test-backend", choices=["docker", "local"], default="docker")
    f.add_argument("--unsandboxed", action="store_true", help="allow --test-backend local (NOT isolated)")
    f.add_argument("--deps", type=Path, help="read-only dependency dir holding the NEW library, for the docker test run")
    f.add_argument("--old-deps", type=Path, help="dependency dir holding the OLD library (for --behavior-check)")
    f.add_argument("--behavior-check", action="store_true",
                   help="compare patched code on the new library with the original on the old one (needs probe inputs from the model)")
    f.add_argument("--image", default="python:3.12-slim")
    f.add_argument("--open-pr", action="store_true", help="propose the fix as a DRAFT pull request (dry run unless --confirm)")
    f.add_argument("--confirm", action="store_true", help="with --open-pr: really push a branch and open the draft PR")
    f.add_argument("--base", default="main", help="base branch for --open-pr")
    f.set_defaults(fn=lambda a: _fix(a, client))

    d = sub.add_parser("dashboard", help="write a self-contained HTML page for a fix run and/or benchmark results")
    d.add_argument("--run", type=Path, help="output folder of `upkeep fix` (result.json, fix.patch, PROOF.md)")
    d.add_argument("--evals", type=Path, help="folder with benchmark result JSON files")
    d.add_argument("--out", type=Path, default=Path("dashboard.html"))
    d.set_defaults(fn=lambda a: (print(f"wrote {build_dashboard(a.run, a.evals, a.out)}"), 0)[1])

    v = sub.add_parser("vulns", help="known vulnerabilities for a pinned version (OSV)")
    v.add_argument("--package", required=True)
    v.add_argument("--version", required=True)
    v.set_defaults(fn=_vulns)

    load_env(Path(".env"))
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
