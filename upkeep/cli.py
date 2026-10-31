from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .agent import Agent
from .charact import BehaviorChecker
from .dashboard import build as build_dashboard
from .llm import client_for, load_env
from .pr import PRError, open_draft_pr
from .pypi import PyPIError, sources_for
from .progress import paint, run_with_progress, use_color
from .audit import audit, count_unpinned, worst
from .autoprep import PrepareError, docker_ready, prepare_upgrade
from .attest import AttestError, generate_key, sign_manifest, verify_manifest, verify_signature, write_manifest
from .proof import ProofBundle
from .pypi import release_digest
from .radar import RadarInputError, run_radar
from .sandbox import SandboxRefused, run_python, run_tests
from .secrets import scan as scan_secrets
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

    try:
        report = run_radar(args.repo, args.old, args.new, args.package, executed)
    except RadarInputError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
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


def _check(args) -> int:
    """One command: download two real releases from PyPI and show what the upgrade breaks in a repo."""
    try:
        old_src, new_src, module = sources_for(args.package, args.from_version, args.to_version, args.module)
    except PyPIError as e:
        print(f"could not prepare the two releases: {e}", file=sys.stderr)
        return 3
    try:
        report = run_radar(args.repo, old_src, new_src, module)
    except RadarInputError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(f"{args.package} {args.from_version} -> {args.to_version}: " + paint(report.risk.upper(), {"green": "green", "amber": "yellow", "red": "red"}[report.risk], use_color())
              + ("  (coverage not measured)" if not report.coverage_known else ""))
        for change, sites in report.affected:
            print(f"  {change.symbol}: {change.kind} ({change.detail})")
            for s in sites:
                print(f"    {s.kind:9} {s.file}:{s.line}")
        unused = len(report.changes) - len(report.affected)
        print(f"  {len(report.affected)} breaking change(s) touch this repo; {unused} do not."
              + ("" if report.affected else " Nothing to fix (behavior changes that keep signatures are invisible to a static check)."))
        for lim in report.limitations:
            print(f"  note: {lim}")
    return {"green": 0, "amber": 1, "red": 2}[report.risk]


def _audit(args) -> int:
    results = audit(args.repo, max_mb=args.max_mb, only={x.lower().replace("_", "-") for x in args.only} or None, limit=args.limit)
    if not results:
        print("no exact pins (name==version) found in requirements*.txt; nothing to audit", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        shade = {"green": "green", "amber": "yellow", "red": "red"}
        for r in results:
            if r.status == "checked":
                print(f"{r.dist} {r.old} -> {r.new}: " + paint(r.risk.upper(), shade[r.risk], use_color())
                      + f"  ({r.n_changes} breaking change(s) in the library; here: {r.hard} will fail, {r.review} default change(s) to review)")
                for f, ln, sym, kind in sorted(r.flagged, key=lambda x: x[3] == "default_changed")[:6]:
                    print(f"    {f}:{ln}  {sym}  [{'review: default changed' if kind == 'default_changed' else kind}]")
                if len(r.flagged) > 6:
                    print(f"    ... and {len(r.flagged) - 6} more")
            elif r.status == "up-to-date":
                print(f"{r.dist} {r.old}: already the latest")
            else:
                print(f"{r.dist} {r.old}: SKIPPED ({r.reason})")
        done = [r for r in results if r.status == "checked"]
        loose = count_unpinned(args.repo)
        if loose:
            print(f"\n{loose} requirement(s) without an exact pin (ranges, bare names) were not analysed.")
        print(f"\n{len(done)} of {len(results)} pinned dependencies analysed; "
              f"{sum(1 for r in done if r.hard)} have lines that will fail, {sum(1 for r in done if r.review and not r.hard)} only default changes "
              "to review. Skipped ones were NOT checked.")
    if not any(r.status == "checked" for r in results) and any(r.status == "skipped" for r in results):
        print("error: no dependency could be analysed (see the SKIPPED reasons above); this is not a clean result", file=sys.stderr)
        return 3
    if args.fail_on == "never":
        return 0
    if args.fail_on == "hard":  # default changes are listed for review but do not fail the build
        return 2 if any(r.hard for r in results if r.status == "checked") else 0
    return {"green": 0, "amber": 1, "red": 2}[worst(results)]


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


def _auto_prepare(args) -> int | None:
    """--from/--to: download the releases and build the dependency folders. Returns an exit code on failure, else None."""
    if (args.old is not None or args.new is not None) and (args.from_version or args.to_version):
        print("error: give either --old/--new (folders you prepared) or --from/--to (versions to download), not both", file=sys.stderr)
        return 2
    if (args.old is None) != (args.new is None):
        print("error: --old and --new go together (or use --from and --to instead)", file=sys.stderr)
        return 2
    if not (args.from_version and args.to_version):
        print("error: give --from and --to (for example --from 5.4.1 --to 6.0.2), or --old and --new source folders", file=sys.stderr)
        return 2
    if args.test_backend == "docker" and not docker_ready():
        print("error: Docker is not running (needed for the isolated test runs). Start Docker Desktop and try again.", file=sys.stderr)
        return 2
    try:
        prep = prepare_upgrade(args.repo, args.package, args.from_version, args.to_version, args.module, args.image,
                               allow_source_builds=args.allow_source_builds)
    except PrepareError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    args.dist, args.package = args.package, prep.module
    args.old, args.new, args.deps, args.old_deps, args.image = prep.old_src, prep.new_src, prep.deps_new, prep.deps_old, prep.image
    args.old_version, args.new_version = args.from_version, args.to_version
    args.behavior_check = not args.no_behavior_check
    if args.changelog is None:
        args.changelog = prep.changelog
    print(f"upkeep: ready ({prep.image}; behavior check {'on' if args.behavior_check else 'off'})\n", flush=True)
    return None


def _fix(args, client=None, prober=None) -> int:
    if args.test_backend == "local" and not args.unsandboxed:
        print("refused: --test-backend local runs this repo's tests on this machine, NOT isolated. "
              "Pass --unsandboxed to allow it, or use --test-backend docker.", file=sys.stderr)
        return 2
    if args.from_version or args.to_version or args.old is None or args.new is None:
        failed = _auto_prepare(args)
        if failed is not None:
            return failed
    if args.image is None:
        args.image = "python:3.12-slim"
    try:
        report = run_radar(args.repo, args.old, args.new, args.package)
    except RadarInputError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
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
        # An INDEPENDENT prober (a different model than the one editing) avoids shared blind spots.
        prober = prober or (client_for(args.prober_provider, args.prober_model) if args.prober_provider else None)
        checker = BehaviorChecker(prober or client, args.repo, sites, world(old_paths, args.old_deps), world(new_paths, args.deps),
                                  n_random=args.fuzz_inputs)
    agent = Agent(client, args.repo, report, runner_plain, changelog=changelog, behavior_checker=checker,
                  library_roots={"old": args.old, "new": args.new} if args.library_source else None)
    print(paint("\nupkeep: agent working (the model proposes; code decides)", "bold", use_color()), flush=True)
    res = run_with_progress(agent)
    print()

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
    model_name = getattr(client, "model", None) or "unknown (injected client)"
    provider = args.provider if getattr(client, "model", None) else "n/a"
    md += ("\n## Agent run\n"
           f"- model: `{model_name}` via provider `{provider}`"
           + (f"; probe inputs by `{getattr(prober, 'model', '?')}` via `{args.prober_provider}`" if prober is not None and args.prober_provider else "") + "\n"
           f"- model calls: {res.model_calls}, tokens: {res.total_tokens}, guard rejections: {res.guard_rejections}, empty model replies re-asked: {res.empty_replies}\n"
           "- tool trace: " + " > ".join(s["tool"] for s in res.steps) + "\n")
    leaks = scan_secrets(res.diff)
    md += ("\n## Security\n"
           f"- secret-like strings hidden from the model: {res.redactions}\n"
           + ("- fix.patch contains secret-like strings (" + ", ".join(f"{k} x{n}" for k, n in leaks.items()) + "); no pull request will be opened\n"
              if leaks else "- fix.patch: no secret-like strings found\n")
           + "- the model sees snippets of your code and test output; do not run `fix` on code you are not allowed to share\n")
    md += _integrity_section(res.diff, args.old, args.new)
    (args.out / "PROOF.md").write_text(md)
    (args.out / "result.json").write_text(json.dumps(
        {"status": res.status, "reason": res.reason, "model": model_name, "provider": provider, "model_calls": res.model_calls,
         "tokens": res.total_tokens, "guard_rejections": res.guard_rejections, "empty_replies": res.empty_replies,
         "redactions": res.redactions, "secrets_in_patch": dict(leaks),
         "injection_flags": res.injection_flags, "steps": res.steps, "behavior": res.behavior}, indent=2))
    write_manifest(args.out, args.package, args.old_version, args.new_version, bundle.status(), args.old, args.new)
    if args.sign_key:
        try:
            print(f"signed manifest with key {sign_manifest(args.out, args.sign_key)}")
        except (AttestError, OSError, ValueError) as e:
            print(f"not signed: {e}", file=sys.stderr)
    shade = {"verified": "green", "declined": "red"}.get(res.status, "yellow")
    print(f"{args.package}: " + paint(res.status.upper(), shade, use_color()) + f"  ({res.reason})")
    if args.open_pr and leaks:
        print("PR not opened: fix.patch contains secret-like strings (see PROOF.md). Remove them and open the PR yourself.", file=sys.stderr)
    elif args.open_pr:
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
    print(f"wrote {args.out}/fix.patch, PROOF.md, result.json, MANIFEST.json  (the repo itself was NOT modified)")
    agent.cleanup()
    return 0 if res.status == "verified" else 1


def _integrity_section(diff: str, old_src: Path, new_src: Path) -> str:
    lines = ["", "## Integrity", f"- fix.patch SHA-256: `{hashlib.sha256(diff.encode()).hexdigest()}`"]
    for label, src in (("current", old_src), ("new", new_src)):
        d = release_digest(src)
        lines.append(f"- {label} library archive SHA-256 (verified against PyPI at download): " + (f"`{d}`" if d else "not recorded"))
    lines.append("- `MANIFEST.json` hashes this file too; `upkeep verify --run <folder>` re-checks everything. "
                 "This shows the patch is the one that was checked, not that it is what you deploy.")
    return "\n".join(lines) + "\n"


def _verify(args) -> int:
    problems = verify_manifest(args.run)
    for x in problems:
        print(f"FAIL {x}")
    if problems:
        return 2
    print("OK   every file matches the SHA-256 manifest")
    if args.pubkey or (args.run / "MANIFEST.sig").exists():
        try:
            trusted = args.pubkey.read_text().strip() if args.pubkey else None
            ok, msg = verify_signature(args.run, trusted)
        except (AttestError, OSError) as e:
            print(f"FAIL {e}")
            return 2
        print(("OK   " if ok else "FAIL ") + msg)
        return 0 if ok else 2
    print("note: not signed, so this shows the files are unchanged, not who produced them")
    return 0


def _keygen(args) -> int:
    try:
        fp = generate_key(args.key)
    except (AttestError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"wrote {args.key} (private, keep secret) and {args.key}.pub (share this); key id {fp}")
    return 0


def main(argv: list[str] | None = None, client=None, prober=None) -> int:
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
    f.add_argument("--old", type=Path, help="source root of the current version (or use --from/--to and Upkeep downloads it)")
    f.add_argument("--new", type=Path, help="source root of the new version (or use --from/--to)")
    f.add_argument("--package", required=True,
                   help="with --from/--to: the PyPI name (PyYAML). With --old/--new: the import name (yaml)")
    f.add_argument("--from", dest="from_version", help="version you use now: Upkeep downloads both releases and prepares everything")
    f.add_argument("--to", dest="to_version", help="version to upgrade to")
    f.add_argument("--module", help="with --from/--to: import name if it differs from the PyPI name (PyYAML -> yaml)")
    f.add_argument("--allow-source-builds", action="store_true",
                   help="with --from/--to: if no wheels exist, build from source inside the container (runs build scripts, with network)")
    f.add_argument("--no-behavior-check", action="store_true", help="with --from/--to the behavior check is on by default; skip it")
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
    f.add_argument("--prober-provider", choices=["baseten", "meta"],
                   help="use a DIFFERENT model to propose probe inputs (default: the editing model)")
    f.add_argument("--prober-model", help="model name for --prober-provider")
    f.add_argument("--behavior-check", action="store_true",
                   help="compare patched code on the new library with the original on the old one (needs probe inputs from the model)")
    f.add_argument("--fuzz-inputs", type=int, default=100,
                   help="with --behavior-check: extra seeded-random probe inputs per function (0 = only the model's and edge cases)")
    f.add_argument("--library-source", action="store_true",
                   help="let the agent read the library's old and new source (6 reads). Off by default: measured no gain and about twice the tokens")
    f.add_argument("--image", help="Docker image for the test runs (default: python:3.12-slim; with --from/--to the newest "
                                   "Python 3.9 to 3.12 image that has wheels for your dependencies)")
    f.add_argument("--open-pr", action="store_true", help="propose the fix as a DRAFT pull request (dry run unless --confirm)")
    f.add_argument("--confirm", action="store_true", help="with --open-pr: really push a branch and open the draft PR")
    f.add_argument("--base", default="main", help="base branch for --open-pr")
    f.add_argument("--sign-key", type=Path, help="Ed25519 private key (from `upkeep keygen`) to sign the run manifest")
    f.set_defaults(fn=lambda a: _fix(a, client, prober))

    vf = sub.add_parser("verify", help="check that a `upkeep fix` output folder was not edited after it was written")
    vf.add_argument("--run", required=True, type=Path)
    vf.add_argument("--pubkey", type=Path, help="public key file you trust, to check WHO signed it")
    vf.set_defaults(fn=_verify)

    kg = sub.add_parser("keygen", help="create an Ed25519 signing key (needs the optional 'cryptography' package)")
    kg.add_argument("--key", required=True, type=Path)
    kg.set_defaults(fn=_keygen)

    d = sub.add_parser("dashboard", help="write a self-contained HTML page for a fix run and/or benchmark results")
    d.add_argument("--run", type=Path, help="output folder of `upkeep fix` (result.json, fix.patch, PROOF.md)")
    d.add_argument("--evals", type=Path, help="folder with benchmark result JSON files")
    d.add_argument("--out", type=Path, default=Path("dashboard.html"))
    d.set_defaults(fn=lambda a: (print(f"wrote {build_dashboard(a.run, a.evals, a.out)}"), 0)[1])

    k = sub.add_parser("check", help="download two PyPI releases and show what upgrading would break in your repo (no model)")
    k.add_argument("--repo", required=True, type=Path)
    k.add_argument("--package", required=True, help="PyPI name, e.g. PyYAML")
    k.add_argument("--from", dest="from_version", required=True)
    k.add_argument("--to", dest="to_version", required=True)
    k.add_argument("--module", help="import name if it differs from the PyPI name (PyYAML -> yaml)")
    k.add_argument("--json", action="store_true")
    k.set_defaults(fn=_check)

    au = sub.add_parser("audit", help="check every pinned dependency in a repo against its newest release (no model)")
    au.add_argument("--repo", required=True, type=Path)
    au.add_argument("--only", nargs="*", default=[], help="limit to these package names")
    au.add_argument("--limit", type=int, help="analyse at most this many pins")
    au.add_argument("--max-mb", type=int, default=15, help="skip libraries whose source archive is larger than this")
    au.add_argument("--fail-on", choices=["any", "hard", "never"], default="any",
                    help="exit code 2 when: any flagged line (default), only lines that will fail (hard), or never. "
                         "Exit 3 (nothing could be analysed) is always reported")
    au.add_argument("--json", action="store_true")
    au.set_defaults(fn=_audit)

    v = sub.add_parser("vulns", help="known vulnerabilities for a pinned version (OSV)")
    v.add_argument("--package", required=True)
    v.add_argument("--version", required=True)
    v.set_defaults(fn=_vulns)

    load_env(Path(".env"))
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
