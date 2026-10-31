# Upkeep

![tests](https://github.com/Siddhartha20242/upkeep/actions/workflows/tests.yml/badge.svg)

**A verified, security-aware dependency upgrade agent for Python projects.**
When a library ships a breaking change, Upkeep finds what it breaks in your code, proposes a fix, and attaches the
evidence. **The code decides the verdict, not the model.** If it cannot show a fix is safe, it says so.

> Built for the Meta Global AI Developer Hackathon (Indie Developer track), Oct 26 – Nov 4, 2026.
> Everything in this repository was written during the event. The main agent loop runs on
> **Muse Spark Open** (`meta-models/muse-spark-open-1026`, served by Baseten).
> **Try it on your own project in 5 minutes, no API key:** [`docs/TRY_IT.md`](docs/TRY_IT.md).

## The short, honest version

- **The finding:** a naive upgrade fix can pass every visible test and still change behavior. On the real Werkzeug
  upgrade, **18 of 18 runs without the behavior check were green and wrong, across Muse Spark Open and Muse Spark 1.1,
  1.2 and 1.3.** No model version was safe on its own, so Upkeep puts a deterministic verifier around the model: the old
  library is the oracle, and the code (not the model) decides the verdict.
- **What works:** a static radar (64% of the relevant changed lines on a held-out set of real developer fixes; `upkeep audit` checks every pinned dependency of a repo in one command), an agent
  that edits only the affected lines, a no-network Docker sandbox, and a proof file that says what was not verified.
- **What does not:** the agent has never produced a *correct* Werkzeug fix with the check on (it declined 33 of 34 runs;
  1 wrong fix got through before edge-case probes were added, 0 of the 9 runs after). On real library releases it is mixed.
- **Not shown:** real users. The agent's apps and tests were written by me; the radar has also been run on 35 public repos I did not write (result 12), where it flagged few and was wrong about half of its first flags.
- **The Meta angle:** a cheap open model (Muse Spark Open) is usable *because* the verifier catches what it gets wrong;
  see the [scorecard](#muse-spark-scorecard) comparing Open with 1.1, 1.2 and 1.3.

## How it works

1. **Radar (no model).** Parse the old and new library source with `ast` (never executed) and diff the public API:
   removed symbols, removed/reordered/keyword-only parameters, required-argument changes, and **changed default
   values**. Then scan your repo for calls, imports, decorators and base classes. The radar is **argument-aware**: a call
   is flagged only if it actually trips the specific change (`yaml.load(x, Loader=L)` is not flagged; `yaml.load(x)` is).
   It also types instances in simple local patterns (`c = lib.C(); c.m()`, `with lib.C() as c`, `self.cli = lib.C()`,
   annotated parameters), and resolves methods inherited from base classes in the same package.
2. **Agent (Muse Spark Open).** Tools: `list_affected`, `read_file`, `replace_lines`, `run_tests`, `check_behavior`,
   `read_release_notes`, `finish`. Release notes are pulled on demand. A failed model call is retried once, then ends
   in an honest decline, never a crash.
3. **Guards enforced in code, not by prompting.**
   - *Scope guard:* an edit may only touch the affected lines.
   - *Content check:* no new `raise`, `eval`, `open`, dunder introspection, network/OS imports (`urllib.parse` is allowed),
     or unsafe deserializers such as `yaml.Loader` (it told the model to use `yaml.SafeLoader`).
   - *Untrusted input:* changelogs and test output are screened, wrapped as data, and never obeyed.
   - *Sandbox:* tests run in Docker with no network, no secrets, dropped capabilities, and a read-only dependency folder.
4. **Verdict by code.** The model's claim of success is ignored; the system re-runs the tests. "Verified" requires green
   tests, no behavior differences, and every affected site executed by a test **or by a behavior probe** (the proof says which).
5. **Behavior check.** For each function the agent changed, a model proposes probe inputs twice and the two sets are merged
   (JSON only, never run as code; an optional `--prober-model` lets a different model do this);
   Upkeep adds deterministic edge cases (empty, unicode, separators, `None` values, ...) one argument at a time, ignores
   probes whose old-library output is not stable, then runs the original code on the **old** library and the patched code
   on the **new** one. Any difference blocks "verified". `finish` is rejected while differences remain.
6. **Proof bundle (`PROOF.md`).** API diff, affected sites, which sites tests or probes execute, before/after results,
   behavior-check results, known vulnerabilities (OSV), untrusted-input warnings, and **what this does not prove**.

`upkeep fix` never modifies your repo: it writes `fix.patch`, `PROOF.md` and `result.json`, and shows each agent step
live. `--open-pr` proposes a **draft** pull request and is a dry run unless you add `--confirm`.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,sign]" coverage mypy
.venv/bin/python -m pytest -q                       # 293 tests (no network, Docker or keys needed); live OSV test: UPKEEP_LIVE=1
cp .env.example .env                                # add keys; .env is git-ignored, never commit keys
```
```bash
upkeep audit     --repo .                                                            # no key; every pinned dependency vs its newest release
upkeep check     --repo . --package PyYAML --from 5.4.1 --to 6.0.2 --module yaml      # no key; downloads the two releases
upkeep radar     --repo R --old OLD_SRC --new NEW_SRC --package lib
upkeep fix       --repo R --old OLD_SRC --new NEW_SRC --package lib --test-backend docker --deps DEPS --behavior-check --old-deps OLD_DEPS
upkeep vulns     --package requests --version 2.19.0
upkeep dashboard --run upkeep-out --evals eval_results --out dashboard.html
```
`--test-backend local --unsandboxed` runs the repo's tests on your machine without isolation; use it only on code you trust.
A rehearsed 3-minute walkthrough is in [`docs/DEMO.md`](docs/DEMO.md); an example CI workflow (not yet run on GitHub
Actions) is in [`docs/ci/upkeep-check.yml`](docs/ci/upkeep-check.yml).

## Muse Spark scorecard

The same harness, benchmarks and verifier run on **Muse Spark Open** (Baseten) and **Muse Spark 1.1 / 1.2 / 1.3** (Meta API).
Sources are the numbered results below; every cell comes from a file in `eval_results/`.

| | Open | 1.1 | 1.2 | 1.3 |
|---|---|---|---|---|
| Tool calls exactly right (512 calls, result 11) | 84% | 97% | 99% | 100% |
| Mean latency per call / tokens per call | 1.7 s / 1,059 | 2.3 s / 1,206 | 2.7 s / 1,200 | 3.3 s / 1,353 |
| Authored cases solved (result 1; all saturate) | 38/38 (2 runs) | 19/19 | 19/19 | 19/19 |
| Tokens / seconds per authored case | 13.4k / 11 | 13.8k / 13 | 15.4k / 20 | 11.2k / 10 |
| Werkzeug, no behavior check: wrong but called verified (result 3) | 12 of 12 | 2 of 2 | 2 of 2 | 2 of 2 |
| Own probes catch the subtle wrong fix / the naive one (result 10) | 3/6, 6/6 | 5/6, 5/6 | 4/6, 6/6 | 5/6, 5/6 |

How to read it: newer versions call tools more reliably but are slower and use more tokens; Open is the cheapest and
fastest per call but is the one that answers in text instead of calling `finish` (the agent's nudge exists for it);
**every version fails the Werkzeug trap**, so none of them is a substitute for the verifier. *Caveats: small samples,
Open and the Meta-API versions are served by different stacks, and the Werkzeug rows count only runs without the check.*

**Why Muse Spark Open for the main loop:** it is open-weights, it had the lowest tokens and latency per call here, and an
agent loop makes about 14 calls per fix. The measurement also showed what it needs: a nudge when it stops without
`finish`, and the verifier above, because its subtle-fix probes were the weakest (3/6).

## Results (read the caveat under each)

**1. Authored cases: 20 cases (19 detectable), tiny fake libraries, hidden tests.** 38/38 on detectable cases for Muse
Spark Open over two runs; one run each on the Meta API: `muse-spark-1.1` 19/19, `1.2` 19/19, `1.3` 19/19. Cost per case:
1.1 about 13.8k tokens / 13 s, 1.2 15.4k / 20 s, 1.3 11.2k / 10 s, Open 13.4k / 11 s.
*Caveat: I wrote them, tuned parts of the agent against some, and every model saturates them.*

**2. Real library releases, authored apps** (PyYAML 5.4.1 to 6.0.2, Jinja2 3.0.3 to 3.1.4, Werkzeug 2.3.8 to 3.0.3),
validated in Docker against the real libraries (passes on old, fails on new, reference fix passes).

| Setting (3 runs per case) | PyYAML | Jinja2 | Werkzeug | Total |
|---|---|---|---|---|
| first run (content check blocked `urllib`) | 3/3 | 3/3 | 0/3 wrong-but-green | 6/9 |
| `urllib.parse` allowed | 3/3 | 1/3 | 0/3 wrong-but-green | 4/9 |
| + behavior check (initial) | 3/3 | 2/3 | 0/3 declined | 5/9 |
| focused release notes pushed | 3/3 | 3/3 | 0/3 wrong-but-green | 6/9 |
| release notes pulled on demand | 3/3 | 0/3 | 0/3 wrong-but-green | 3/9 |
| Meta API `muse-spark-1.1` / `1.2` / `1.3` (2 runs each, no check) | 2/2 each | 2/2 each | 0/2 each wrong-but-green | 4/6 each |

Run-to-run variance is large (Jinja2 went 0/3 then 5/5 under the same setting), so differences of one or two runs mean
little. *Caveat: three libraries, apps written by me, small samples.*

**3. The Werkzeug trap, with and without the behavior check.**

| Werkzeug 2.3.8 to 3.0.3 | Runs | Wrong but called "verified" | Correct fix | Honest decline |
|---|---|---|---|---|
| check **off** (Open x 12, Meta API versions x 6) | 18 | **18** | 0 | 0 |
| check **on**, model-proposed probes only (Open x 16, Meta API versions x 9) | 25 | **1** | 0 | 24 |
| check **on**, with edge-case expansion (Open x 3, `muse-spark-1.1` x 3) | 6 | **0** | 0 | 6 |

The one wrong acceptance was `muse-spark-1.1` using `safe='/+'` where the old default was `safe='/:'`; neither the
visible tests nor that model's probes contained a colon. Replaying that exact patch against the real library versions:
accepted with four happy-path probes, **blocked** (6 mismatches in 23 probes) once edge cases are added. The expansion
was added after seeing that miss; 6 later runs are too few to call it solved. In 8 of the first 16 runs the check named
a concrete failing input (for example `None` values becoming the literal text `q=None`). No run produced a **correct**
fix: the models could not reconstruct Werkzeug's exact encoding rules from memory, and the system failed safe.

**4. Do release notes help?** Real Jinja2, 8 runs per setting: no notes **8/8**, raw notes pushed 4/8, focused notes
pushed 6/8, pulled on demand 5/8 (pooled "any notes" 15/24 vs 8/8, p about 0.07: suggestive, not conclusive). On my
authored libraries notes are essential (without them 15/19) because those facts cannot be known. Famous libraries are
already known to the model, and long notes add noise. Takeaway: notes help for private or novel APIs, not necessarily
for famous ones.

**5. Ablations on the authored cases** (single runs, notes pushed): full 19/19; no signatures 18/19; one test run instead
of three 19/19; no baseline test run 19/19; no notes 15/19. The benchmark is too easy to show the value of the retry
budget or the baseline run.

**6. Real developer fixes (retrospective, read-only).** For real public commits that fixed an upgrade break, I fetched
each changed `.py` file as it was **before** the fix (nothing executed), ran the radar, and sorted every
developer-changed line that mentions the library: *flagged* by the radar, *compliant* (the radar saw the symbol and judged
the call fine, usually a refactor), or *unseen*. Commits were split by hash parity; I studied misses only in the dev half.

| | Commits | Lines flagged | Compliant | Unseen | Commits with a flagged line |
|---|---|---|---|---|---|
| **held-out half** | 37 | **47 (64%)** | 11 (15%) | 15 (21%) | **28 (76%)** |
| dev half | 23 | 24 (46%) | 24 (46%) | 4 (8%) | 14 (61%) |
| all 60 | 60 | 71 (57%) | 35 (28%) | 19 (15%) | 42 (70%) |

The 35 "compliant" lines are calls like `yaml.load(f, Loader=yaml.FullLoader)` that developers changed to `safe_load` for
security, not because the upgrade broke them. **Without argument-awareness, 35 of the 106 lines the radar sees (33%)
would have been false alarms.** The "unseen" lines I inspected (dev half) were `import yaml` and similar, not real
breaks, so 15% overstates the true blind spot. *Caveats: 55 of the 60 commits are PyYAML (Jinja2 1, Werkzeug 1, Flask 3,
because those "fixes" are mostly version pins); commits come from keyword searches, so the sample favors well-described
fixes; flagged-in-a-changed-line is not proof the break was real.*

**7. Prevalence on public GitHub (read-only code search, 2026-10-28).** About **28,000** file matches across 8 searches
still import APIs removed by these upgrades (for example `from jinja2 import Markup` 6,256; `from werkzeug.urls import
url_encode` 8,576; `from flask import Markup` 1,956). *Caveat: counts are approximate, searches can overlap, and many of
those files pin old versions or are vendored copies, so they are not broken today.* See `eval_results/prevalence_code_search.json`.

**8. How big is the instance-method blind spot?** Counting **distinct** breaking changes across 12 real upgrades (chosen
before looking at the result): 412 in total, **38% functions, 41% class constructors, 20% methods**; 5 of the 12
upgrades have no method changes at all, and the per-upgrade share ranges from 0% to 59% (Flask 59%, redis 59%, click 40%).
My first count said 51% methods; that was inflated, first by an inheritance bug (redis showed 222 "removed" methods that
were only moved into base classes) and then by one changed base method being counted once per subclass. After later radar
changes the census briefly read 646 changes for the same reason applied to constructors and re-exported names (a changed
base constructor counted once per subclass, a re-exported function under two keys); it now counts each underlying
definition once, and the older 443 and 21% were re-measured as 412 and 20%. See `scripts/blindspot_census.py` and
`eval_results/blindspot_census.txt`. *This counts API changes, not how often your code
calls them.*

**9. Mined real downstream repositories: 0 of 4 usable.** The validator (clone, install dependencies in a container, run
tests with no network, require FAIL then PASS across the real fix commit) correctly rejected all four: an old dependency
that needs a C compiler, tests that cannot import `six`, a Juju charm library that no requirements file provides, and
tests containing scrubbed `***REMOVED***` placeholders. Rebuilding arbitrary environments is the fragile part of `fix`;
`check` and `radar` need no environment.

**10. Does an independent prober help? Asking twice does; a different model did not.** I replayed two known wrong
Werkzeug fixes (the subtle `safe='/+'` rewrite and the naive default `urllib` swap) against the real library versions,
with probe sets proposed by each of four Meta models (6 samples each, about 16 probes per set, **no** deterministic
expansion, so only the model's own imagination is tested).

| A model's own probes catch the wrong fix | `safe='/+'` rewrite | naive default swap |
|---|---|---|
| Muse Spark Open | 3/6 | 6/6 |
| `muse-spark-1.1` / `1.2` / `1.3` | 5/6 / 4/6 / 5/6 | 5/6 / 6/6 / 5/6 |
| **one probe set (all models)** | **71%** | **92%** |
| two samples, **same** model | **93%** | 100% |
| two samples, **different** models | **92%** | 100% |

Taking a second sample raised the catch rate from 71% to 93%; having it come from a *different* model added nothing
(92%). So probe generation now takes two samples by default, and `--prober-model` exists but is not claimed to help.
*Caveats: 24 probe sets per variant, one library, two wrong fixes; Open was weakest (3/6) on the subtle case, which
is too few samples to rank the models.* See `scripts/prober_experiment.py`, `eval_results/prober_experiment.json`.

**11. Tool-calling conformance across Muse Spark versions (`scripts/toolcall_bench.py`).** Eight small tests an agent
depends on (simple arguments, quotes/backslashes/newlines/unicode, an enum, nested arrays, several calls in one turn,
*not* calling a tool when none is needed, choosing among five tools, reproducing 17 lines of code exactly), 8 trials at
each of two temperatures: 128 calls per model, 512 in all.

| Model | Exactly right | Invalid-JSON args | Schema-invalid | Mean latency | Tokens per call |
|---|---|---|---|---|---|
| Muse Spark Open (Baseten) | 108/128 (84%) | 0 | 0 | 1.7 s | 1,059 |
| `muse-spark-1.1` | 124/128 (97%) | 0 | 0 | 2.3 s | 1,206 |
| `muse-spark-1.2` | 127/128 (99%) | 0 | 0 | 2.7 s | 1,200 |
| `muse-spark-1.3` | 128/128 (100%) | 0 | 0 | 3.3 s | 1,353 |

No model produced malformed JSON or a schema violation in 512 calls. Most of the gap is **one** test: asked to "end the
session", Muse Spark Open replied in plain text and **never called the `finish` tool** (0 of 16; confirmed by hand), while
the Meta-API versions called it (13/16, 16/16, 16/16). That is the same "stops talking without finishing" behavior the
agent's nudge exists for. Newer versions were slower and used more tokens. *Caveats: small prompts; one test accounts for
most of the difference and its wording matters; Open and the others are served by different stacks (Baseten vs the Meta API).*
See `eval_results/toolcall_bench.json`.

**12. `upkeep audit` on public repos I did not write (read-only, no model).** I took repos that pin an *old* version of
nine popular libraries (Flask, click, SQLAlchemy, requests, marshmallow, PyYAML, Jinja2, Werkzeug, pydantic), in the order
GitHub code search returned them, and ran `audit` against the newest release (`scripts/outside_audit.py`; only counts and
`file:line` are saved, never their code). **I labeled every flag by reading the line.**

| | Repos | Library pins analysed | Pins with a flagged line | Flagged sites |
|---|---|---|---|---|
| Set A (used to find and fix false alarms) | 17 | 74 | 6 | 52 |
| Set B (fresh: different pins, no code changed after I read it) | 18 | 58 | 4 | 12 |

- **Most pinned-old repos are not hit.** Only 10 of 132 pins (8%) had any flagged line; the rest are green (or the break
  is invisible to the radar, see below).
- **Two kinds of flag with very different precision.** *Will fail* (removed names, removed or newly required
  parameters): 41 of 42 flagged sites were real on set A (one I could not judge), for example Flask's `send_file(...,
  cache_timeout=0)`, marshmallow 4 ignoring `ValidationError(field_names=...)`, and `from pydantic import BaseSettings`.
  None appeared on set B. *Default changed* (a default value changed and the call relies on it): only 10 of 22 mattered,
  because 12 were `marshmallow.fields.Nested(many=...)` going from `False` to `None`, which behaves the same. `audit` now
  prints these separately as "will fail" and "default change to review".
- **The first run was bad, and that was the useful part.** On set A it flagged 299 lines, most of them false. After the
  first round of fixes it was 95 sites, which I labeled by hand at about **50% precision** (47 real). The remaining false
  alarms had four causes the benchmark never exercised: a base class imported under an alias (Flask 3: `class
  Blueprint(SansioBlueprint)`), names a library exposes lazily (pydantic 2), absolute and two-step re-exports
  (marshmallow 4, SQLAlchemy 2), and calls that still work after a *parameter rename* (positional arguments, bare
  decorators, subclass declarations, constructors inherited from private base classes). Each is fixed with a test; the
  same set then gave 52 sites at 94% precision. That number is on the set I tuned on. The fair test is set B, and it is
  small (12 sites: 2 real behavior changes, 0 will-fail).
- **Misses are not measured here.** Reading code to find breaks the radar missed is slow, so I did not do it, except by
  accident: in one earlier repo (`cristianzsh/freki`) a call passes `method="sha256"`, which Werkzeug 3 rejects, and the
  radar cannot see a parameter that stops accepting a value. The radar-versus-real-fixes numbers in result 6 are the
  recall evidence, and they were unchanged (64% held-out) after all of these fixes.
- *Caveats: 35 repos, nine libraries, GitHub search order, a single labeler (me), and labels for `ValidationError` sites
  came from the radar's argument evidence plus a 5-site spot check.* See `eval_results/outside_audit*.json`.

## Integrity of what it downloads and produces

- **Downloads are verified.** Every PyPI release is checked against the SHA-256 PyPI publishes for it. A mismatch, or a
  release with no published digest, is refused before anything is extracted (fail closed).
- **Runs are tamper-evident.** `upkeep fix` writes `MANIFEST.json` with the SHA-256 of `fix.patch`, `PROOF.md` and
  `result.json`. `upkeep verify --run upkeep-out` fails if any of them was edited afterwards.
- **Runs can be signed (optional).** `pip install -e .[sign]`, `upkeep keygen --key my.pem`, then
  `upkeep fix ... --sign-key my.pem`. `upkeep verify --run upkeep-out --pubkey my.pem.pub` checks the Ed25519 signature
  and that it came from the key you trust.
- What this does **not** do: it does not show the fix is correct (PROOF.md says what is and is not proven), and the
  hashes use the standard library only, so the default install still has zero dependencies. There is no encryption
  because Upkeep stores no secrets and no data at rest; adding some would be decoration.

## What went wrong, and what caught it

1. **A "must decline" case that leaked its own answer.** The visible test contained the removed function's whole formula,
   so copying it inline was a legitimate fix. Caught by reading the diff; the case now holds only literal values.
2. **My own safety rule caused wrong fixes.** The content check denied every `urllib` import (it contains
   `urllib.request`), so the model re-implemented `url_quote` inline from memory with the wrong default. Caught by a
   behavior-trap test; `urllib.parse` is now allowed.
3. **A claim that did not survive real libraries.** My toy ablation said release notes were essential; real Jinja2 said
   they did not help. Narrowed above; notes are pulled on demand.
4. **The radar over-reported.** It flagged every call to a changed function, including calls that already complied.
   Real fix commits showed 31% of its flags would have been false alarms; it is now argument-aware.
5. **An inheritance bug nearly shipped.** Methods moved into base classes looked "removed" (redis: 222 false removals), and
   my first blind-spot number (51%) was wrong. Caught by looking at the listing instead of the total.
6. **One malformed model reply crashed a whole benchmark run** (`muse-spark-1.1` returned an empty message and the next
   request got HTTP 400). Now: never send an empty assistant message, retry once, then decline honestly; results save as
   each case finishes.
7. **The behavior check let one wrong fix through** (a happy-path-only probe set). Added deterministic edge-case expansion.
8. **A "correct" reference fix that was not truly equivalent.** Stronger probes showed my toy library's reference solution
   only matched on narrow inputs; the case was rebuilt.
9. **Tooling bugs that invalidated runs.** Dangling symlinks crashed the copy step (twice), a dependency folder was
   reused across Python versions, and I edited a shell script while it was running.
10. **"Verified" would have been wrong** on the Werkzeug fixes. This is why the proof lists which sites tests or probes
    actually execute.
11. **Missing data silently looked like "nothing breaks".** The extracted library sources in my caches lost all their
    `.py` files (I did not know what removed them; the most likely cause, not proven, is macOS clearing old files from the temp folder where the cache lived and leaving empty directories, so the cache now lives in `~/.cache/upkeep`), and the radar reported **no changes** instead of failing. I only
    noticed because an experiment that depended on it returned zero probes. The radar now refuses to run on a library
    folder with no Python source (exit code 3), and a cache of empty folders is re-downloaded, not trusted.
12. **A hypothesis that did not hold.** I expected a different Meta model as the prober to catch more than the model that
    wrote the fix. It did not; a second sample from the same model did just as well. I kept the finding, not the idea.
13. **Running on real repos exposed false alarms my benchmarks never could.** The first `audit` over 17 repos flagged
    299 lines, most of them wrong: `flask.Blueprint.route` "removed" 124 times because of an aliased base class,
    every `class X(Schema)` flagged for constructor changes, and names pydantic exposes lazily reported as gone. Fixing
    these (inheritance through import aliases, lazy names, re-export chains, renamed parameters, bare decorators)
    took the same set from 299 lines to 95 sites at about 50% precision, then to 52 at 94%, and a fresh set of 18 repos is the check.
    The earlier "66% on a held-out set" number moved to 64% for the same reason: I removed an import-line flag.

14. **My own tests were too gentle.** A ground-truth test for the radar found 89% recall where I believed it was near
    complete, and adversarial edits found 13 ways around the "no unsafe YAML loader" rule I advertised in the README.
    Both are fixed and kept as tests; the second is why that rule now reads as a heuristic, not a guarantee.

## Known limitations and roadmap

- Python only. The radar sees signature-level breaks and literal default changes; a change that keeps the same
  signature and defaults (return type, internal behavior) is invisible to it (one authored case documents this).
- The radar cannot see a parameter that stops accepting a value it used to accept (Werkzeug 3 rejecting `method="sha256"`), and a flagged *default change* is often benign (12 of 22 on public repos).
- Instance-method calls are resolved only in simple local patterns. Not resolved: inheritance of your own classes,
  reassigned variables, objects passed between files, `getattr`, factories and other dynamic calls.
- The behavior check uses JSON-only probes of module-level functions, chosen by a model plus deterministic edge cases:
  a clean result is evidence, not proof. It does not compare side effects, state, or performance.
- The dependency-install step needs network access. It installs **wheels only** by default (no build scripts run); if no
  wheel exists it falls back to a source build inside the container and says so. Docker is a strong boundary, not a perfect one.
- The content check is a heuristic denylist; the sandbox is the real boundary.
- Results are small, authored by me, and noisy; there is no public benchmark and no real-world adoption evidence.
- **Built late, measured once:** `--fuzz-inputs N` adds seeded-random probe inputs per function (standard library, same
  seed gives the same inputs; default 100 in `upkeep fix`, 0 in the benchmark scripts so the published numbers above
  still describe the fixed probe set). A unit test shows it finding a change the fixed probe set misses. On the real
  Werkzeug case (3 runs, 150 random inputs, Muse Spark Open) it did **not** produce a correct fix: 2 runs declined on
  a behavior difference and 1 made no edit (`eval_results/reallib_baseten_behavior_fuzz150*.json`). What it did change is
  the decline: it now names the file, line and library symbol, and the exact input where old and new differ (for
  example `url_encode` giving `+` for a space on the old library and `%20` on the new one).
- **Roadmap (not built), in the order I would do it:**
  1. Let the agent see the diverging input during editing (today only the first difference is fed back, and the model
     still failed to use it on Werkzeug).
  2. Dynamic call tracing during a baseline test run, non-JSON probe inputs and stateful probing (WSGI-style contexts
     and other implicit state are invisible to the current check).
  3. Partial fixes (accept the verified functions, decline the rest), a real type checker for call resolution, a CI
     action that comments on pull requests, and automatic Docker environment recovery for failing installs.
  Not planned: whole-library control-flow or dataflow graphs. Finding behavior changes that keep the same signature is
  an open research problem, and I would rather be honest that the current check cannot see them.

## Prior art (stated carefully)

Dependabot and Renovate propose version bumps; codemod tools apply hand-written rules; general coding agents can edit
code. Research on library migration with language models exists. Upkeep's narrow claim is not a better editor: it ties
the verdict to deterministic checks (scope guard, sandboxed tests, coverage of affected sites, and a differential check
against the old library) and shows what was and was not verified. I have not benchmarked against those tools.

## Reproducing

Needs Docker and your own keys (`BASETEN_API_KEY`; `META_API_KEY` only for the Meta API comparisons). Results are in
`eval_results/`. Commands that call a model cost tokens.
```bash
.venv/bin/python -I scripts/run_eval.py --set all --config full --repeat 2                     # authored cases
.venv/bin/python -I scripts/run_reallib.py --selfcheck                                         # prove the real-library cases are sound
.venv/bin/python -I scripts/run_reallib.py --run --provider baseten --repeat 3 [--behavior]
.venv/bin/python -I scripts/run_eval.py --set all --provider meta --model muse-spark-1.3       # another model version
.venv/bin/python scripts/retro_eval.py --reuse                                                 # real fix commits (read-only, needs gh)
.venv/bin/python scripts/blindspot_census.py                                                   # method vs function share
bash scripts/real_validation.sh                                                                # mined repos (review the script first)
.venv/bin/python -I scripts/demo_replay.py                                                     # a recorded wrong Werkzeug fix vs the real libraries (no model)
.venv/bin/python -I scripts/outside_audit.py --per-pin 5 --show                                # `audit` on public repos (read-only, needs gh)
.venv/bin/python -I scripts/radar_oracle.py                                                    # radar vs Python's own call binding (synthetic)
```

## Testing

- **293 tests, about 90% line coverage, `mypy` clean** (`.github/workflows/tests.yml` runs both on Python 3.10, 3.11 and 3.12;
  the first run passed on GitHub with no Docker, network or keys). No test needs the network, Docker or a model key: model replies are scripted, Docker is faked, and
  the HTTP client is tested against a local server. The uncovered code is mostly the benchmark runners (`reallib.py`,
  `evalharness.py`), which need Docker and a model.
- **A ground-truth test for the radar** (`tests/radar_oracle_lib.py`, `scripts/radar_oracle.py`). It generates random
  function signatures, applies the kinds of change libraries really make (remove, rename, reorder, add a required
  parameter, make a parameter keyword-only, drop `**kwargs`, ...), draws call shapes that are valid for the old signature,
  and asks Python's own `inspect.Signature.bind` whether the call still works. On 5,000 signature changes (18,892 calls,
  5,893 of which stop working) the radar flagged every broken call and had no false hard flags, with 535 calls excluded as
  uncertain (a value that moves into `*args`/`**kwargs`, which a library may forward) and 1,125 flagged only for a
  changed default. **This is synthetic and was written alongside the fixes, so it checks the logic, not real-world
  accuracy.** It earned its keep: the first run showed 89% recall (calls using a removed `**kwargs`, removals that shift
  later arguments, and every reorder) and a rule I had added a day earlier was wrong for two of those cases. The test also
  proves it can fail: replace the radar with one that ignores removed parameters and it reports recall below 90%.
- **Adversarial tests for the content check** (44 edits a careless or hostile model might try, and 12 ordinary migration
  edits that must still pass). They found 13 bypasses, now closed: `f = eval; f(...)`, `__builtins__["eval"]`,
  `import yaml as y; y.Loader`, `from yaml import UnsafeLoader`, `yaml.unsafe_load`, `from yaml import *`, and
  `operator.attrgetter` string tricks. It is still a denylist; the sandbox is the boundary.
- **Boundary tests** for the Docker command line (no network, all capabilities dropped, read-only dependencies, never root,
  no host environment), hostile archives (zip slip, tar path traversal, absolute paths, escaping symlinks, oversize), a
  randomized scope-guard test (300 edits accepted exactly when they touch an affected line), and the failure paths of the
  model client (key redaction in error bodies, connection refused, malformed replies).

## Development notes

An AI coding assistant (Claude Code) was used to help write the code, tests and this document. Every number above comes
from runs recorded in `eval_results/`; the benchmarks and cases were designed to catch the assistant's and the agent's
mistakes, several of which are listed above.
