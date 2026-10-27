# Upkeep

**A verified, security-aware dependency upgrade agent for Python projects.**
When a library ships a breaking change, Upkeep finds what it breaks in your code, proposes a fix, and attaches the
evidence. **The code decides the verdict, not the model.** If it cannot show a fix is safe, it says so.

> Built for the Meta Global AI Developer Hackathon (Indie Developer track), Oct 26 – Nov 4, 2026.
> Everything in this repository was written during the event. The main agent loop runs on
> **Muse Spark Open** (`meta-models/muse-spark-open-1026`, served by Baseten).

## The short, honest version

- **What works:** a static radar that finds what an upgrade breaks, an agent that edits only those lines, a no-network
  Docker sandbox for tests, and a proof bundle that says what was and was not verified.
- **What the evidence shows:** on small benchmarks I wrote myself the agent is near-perfect (38/38 detectable cases in
  the last run), which mostly shows those benchmarks are easy. On **real library releases** (PyYAML, Jinja2, Werkzeug,
  apps and tests written by me) it is **mixed and noisy: 3 to 6 of 9 correct depending on configuration**, with the
  Werkzeug case wrong in 12 of 12 runs until the behavior check turned them into honest declines (0 of 16 accepted).
- **The result I trust most is a negative one:** a naive fix can pass every visible test and still change behavior.
  Passing tests only prove what the tests exercise. The behavior check (the old library as an oracle) turns those
  silent wrong fixes into honest declines.
- **What is not shown:** real-world adoption. No real user or real downstream repository has used it yet. My attempt to
  mine real fix commits from GitHub produced **0 of 4 usable cases** (their environments could not be rebuilt).

## How it works

1. **Radar (no model).** Parse the old and new library source with `ast` (never executed), diff the public API, and scan
   your repo for calls, imports, decorators and base classes that touch changed symbols. Risk is green, amber (every
   affected site is run by your tests) or red (some site is not, or coverage is unknown).
2. **Agent (Muse Spark Open).** Tools: `list_affected`, `read_file`, `replace_lines`, `run_tests`, `check_behavior`,
   `read_release_notes`, `finish`. Release notes are *pulled* on demand, not pushed into the prompt.
3. **Guards enforced in code, not by prompting.**
   - *Scope guard:* an edit may only touch the affected lines.
   - *Content check:* no new `raise`, `eval`, `open`, dunder introspection or network/OS imports (`urllib.parse` is allowed).
   - *Untrusted input:* changelogs and test output are screened, wrapped as data, and never obeyed.
   - *Sandbox:* tests run in Docker with no network, no secrets, dropped capabilities, and a read-only dependency folder.
4. **Verdict by code.** The model's claim of success is ignored. The system re-runs the tests; "verified" requires green
   tests, no behavior differences, and every affected site executed by a test.
5. **Behavior check.** For each function the agent changed, a model proposes probe inputs (JSON only, never executed as
   code). The original code runs on the old library and the patched code on the new one; any difference blocks "verified".
6. **Proof bundle (`PROOF.md`).** API diff, affected sites, which sites tests execute, before/after results,
   behavior-check results, known vulnerabilities (OSV), untrusted-input warnings, and **what this does not prove**.

`upkeep fix` never modifies your repo: it writes `fix.patch`, `PROOF.md` and `result.json`. `--open-pr` proposes a
**draft** pull request and is a dry run unless you add `--confirm`.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]" coverage mypy
.venv/bin/python -m pytest -q                       # live OSV test: UPKEEP_LIVE=1
cp .env.example .env                                # add keys; .env is git-ignored, never commit keys
```
```bash
upkeep radar     --repo R --old OLD_SRC --new NEW_SRC --package lib
upkeep fix       --repo R --old OLD_SRC --new NEW_SRC --package lib --test-backend docker --deps DEPS_DIR [--behavior-check]
upkeep vulns     --package requests --version 2.19.0
upkeep dashboard --run upkeep-out --evals eval_results --out dashboard.html
```
`--test-backend local --unsandboxed` runs the repo's tests on your machine without isolation; use it only on code you trust.

## Results (read the caveats under each)

**1. Authored cases: 20 cases (19 detectable) with tiny fake libraries and hidden tests.** 38/38 on the detectable cases
over two runs with release notes pulled on demand; earlier runs: 33/33 (dev set, 3 repeats) and 24/24 (held-out set, 3
repeats). *Caveat: I wrote them, tuned parts of the agent against some of them, and the model saturates them.*

**2. Real library releases, authored apps** (PyYAML 5.4.1→6.0.2, Jinja2 3.0.3→3.1.4, Werkzeug 2.3.8→3.0.3), validated in
Docker against the real libraries (passes on old, fails on new, reference fix passes).

| Setting (3 runs per case) | PyYAML | Jinja2 | Werkzeug | Total |
|---|---|---|---|---|
| first run (content check blocked `urllib`) | 3/3 | 3/3 | 0/3 wrong-but-green | 6/9 |
| `urllib.parse` allowed | 3/3 | 1/3 | 0/3 wrong-but-green | 4/9 |
| + behavior check | 3/3 | 2/3 | 0/3 declined | 5/9 |
| focused release notes pushed | 3/3 | 3/3 | 0/3 wrong-but-green | 6/9 |
| release notes pulled on demand | 3/3 | 0/3 | 0/3 wrong-but-green | 3/9 |

Run-to-run variance is large (Jinja2 went 0/3 then 5/5 under the same setting). With 3 runs per cell, differences of one
or two runs mean little.
*Caveat: three libraries, apps written by me, a small sample.*

**Werkzeug, with and without the behavior check (the clearest result in this repository).**

| Werkzeug 2.3.8 to 3.0.3 | Runs | Called "verified" but wrong | Correct fix | Honest decline |
|---|---|---|---|---|
| behavior check **off** (4 configurations x 3 runs) | 12 | **12** | 0 | 0 |
| behavior check **on** (4 configurations, 3 + 5 + 5 + 3 runs) | 16 | **0** | 0 | 16 |

Every naive replacement (`urllib.parse.quote`/`urlencode`, or a re-implementation from memory) passed all visible tests
and was wrong on a case only my hidden test covers. With the check on, nothing wrong was ever accepted. In 8 of the 16
runs the check named a concrete input where the new code differed from the old library (for example which characters
`url_encode` leaves unescaped); the other runs made no edit or ran out of attempts. Nudges, a gate that rejects
`finish` while differences remain, and a 30-turn budget did not produce a correct fix: **the model could not reconstruct
Werkzeug's exact encoding rules from memory, and the system failed safe instead of failing silently.** I count that as a
safety result, not a capability result. (The nudge, the finish gate and the turn budget were added after seeing this
case, so treat them as demonstrations, not measurements.)


**3. Do release notes help?** Real Jinja2, 8 runs per setting: no notes **8/8**, raw notes pushed 4/8, focused notes
pushed 6/8, notes pulled on demand 5/8 (pooled "any notes" 15/24 vs 8/8, p about 0.07: suggestive, not conclusive).
On my authored libraries, notes are essential (without them 15/19) because those facts cannot be known. The model already
knows well-known libraries, and long release notes add noise. Takeaway: notes help for private or novel APIs, not
necessarily for famous ones.

**4. Ablations on the authored cases** (single runs, notes pushed): full 19/19; no signatures 18/19; one test run
instead of three 19/19; no baseline test run 19/19; no notes 15/19. `muse-spark-1.3` (Meta API) scored 18/19, about the
same as Muse Spark Open, using roughly 20% fewer tokens. The benchmark is too easy to show the value of the retry budget
or the baseline run.

**5. Mined real downstream repositories: 0 of 4 usable.** The validator (clone, install dependencies in a container,
run tests with no network, require FAIL then PASS across the real fix commit) correctly rejected all four: an old
dependency that needs a C compiler, tests that cannot import `six`, a Juju charm library that no requirements file
provides, and tests containing scrubbed `***REMOVED***` placeholders.

## What went wrong, and what caught it

1. **A "must decline" case that leaked its own answer.** The visible test contained the removed function's whole formula,
   so copying it inline was a legitimate fix and my case was invalid. Caught by reading the diff; the case now holds only
   literal expected values.
2. **My own safety rule caused wrong fixes.** The content check denied every `urllib` import (it contains
   `urllib.request`). The model's natural fix was rejected, so it re-implemented `url_quote` inline from memory with the
   wrong default. Caught by a behavior-trap test; `urllib.parse` is now allowed.
3. **A claim that did not survive real libraries.** My toy ablation said release notes were essential. Real Jinja2 said
   they did not help and may hurt. The claim is narrowed above and notes are now pulled on demand.
4. **Tooling bugs that invalidated runs.** Dangling symlinks crashed the copy step (twice), a dependency folder was reused
   across Python versions, and I edited a shell script while it was running. Each was found by reading the failure,
   not by trusting a green summary.
5. **"Verified" would have been wrong.** All three Werkzeug runs went green and wrong. This is why the proof bundle lists
   which call sites tests actually execute, and why the behavior check exists.

## Known limitations

- Static analysis sees only signature-level breaks. A change that keeps signatures identical (return type, defaults) is
  invisible; one authored case documents this.
- Only module-level functions and class constructors are resolved. Instance-method calls are not.
- The behavior check probes with model-chosen inputs: a clean result is evidence, not proof. Only module-level functions
  are probed.
- Results are small, authored by me, and noisy. There is no public benchmark and no real-world adoption evidence.
- The content check is a heuristic denylist. The sandbox is the real boundary, and Docker is a strong boundary, not a
  perfect one (the dependency-install step runs third-party build scripts inside a container with network access).

## Prior art (stated carefully)

Dependabot and Renovate propose version bumps; codemod tools apply hand-written rules; general coding agents can edit
code. Research on library-migration with language models exists. Upkeep's narrow claim is not a better editor: it ties the
verdict to deterministic checks (scope guard, sandboxed tests, coverage of affected sites, and a differential check
against the old library) and shows what was and was not verified. I have not benchmarked against those tools.

## Reproducing

Needs Docker and your own keys (`BASETEN_API_KEY`; `META_API_KEY` only for the comparison). Results are in `eval_results/`.
```bash
.venv/bin/python -I scripts/run_eval.py --set all --config full --repeat 2          # authored cases
.venv/bin/python -I scripts/run_reallib.py --selfcheck                              # prove the real-library cases are sound
.venv/bin/python -I scripts/run_reallib.py --run --provider baseten --repeat 3 [--behavior]
bash scripts/real_validation.sh                                                    # mined repos (review the script first)
```

## Development notes

An AI coding assistant (Claude Code) was used to help write the code, tests and this document. Every number above comes
from runs recorded in `eval_results/`; the benchmarks and cases were designed to catch the assistant's and the agent's
mistakes, several of which are listed above.
