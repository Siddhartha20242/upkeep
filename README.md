# Upkeep

**A verified, security-aware dependency upgrade agent for Python projects.**
When a library ships a breaking change, Upkeep finds what it breaks in your code, proposes a fix, and attaches
proof. If it cannot prove a fix is safe, it says so instead of guessing.

> Built for the Meta Global AI Developer Hackathon (Indie Developer track), Oct 26 – Nov 4, 2026.
> Everything in this repository was written during the event. **Status: work in progress.**

## How it works
1. **Radar** (no model): static diff of the old and new library source (parsed with `ast`, never executed),
   plus a scan of your repo for calls, imports, decorators and base classes that touch changed symbols.
2. **Agent** (Muse Spark Open via Baseten): reads the affected sites and edits them through tools.
3. **Code decides the verdict, not the model.** Edits must pass a scope guard (only affected lines) and a content
   check (no environment probing, no `raise`/`eval`/`open` tricks). Tests run in a no-network Docker sandbox.
   The model's own claim of success is ignored; the tests are re-run by the system.
4. **Proof bundle** (`PROOF.md`): API diff, affected call sites, which sites your tests actually execute,
   before/after test results, known vulnerabilities (OSV), untrusted-input warnings, and what this does **not** prove.

```bash
upkeep radar --repo R --old OLD_SRC --new NEW_SRC --package lib
upkeep fix   --repo R --old OLD_SRC --new NEW_SRC --package lib --test-backend docker --deps DEPS_DIR
upkeep vulns --package requests --version 2.19.0
```
`fix` never modifies your repo; it writes `fix.patch`, `PROOF.md` and `result.json`.

## Results so far (read the caveats)
| Benchmark | What it is | Result |
|---|---|---|
| UpkeepBench-Synth (dev + held-out) | 19 cases **authored by me** with tiny fake libraries and hidden tests | 19/19 on detectable cases, 3 repeats. Saturated, so it says little about real upgrades |
| Real libraries, authored apps | PyYAML 5.4.1→6.0.2, Jinja2 3.0.3→3.1.4, Werkzeug 2.3.8→3.0.3; apps and tests authored by me, validated in Docker against the real libraries | **6/9 correct; 3/9 green-but-wrong** (all Werkzeug: the model re-implemented `url_quote` with the wrong default `safe`, which only a hidden test catches) |
| Mined real downstream repos | 4 real GitHub fix commits | **0/4 usable** (environments could not be rebuilt; unrelated failures) |

Ablation (single runs, Muse Spark Open): removing the changelog dropped success to 15/19, removing signatures to 18/19;
one test run vs three and the baseline run made no difference on these easy cases. `muse-spark-1.3` (Meta API) scored 18/19.

## Known limitations
- Static analysis only sees signature-level breaks. A change that keeps signatures identical (return type, defaults) is invisible.
- Only module-level functions and class constructors are resolved; instance-method calls are not.
- **Passing tests only prove behavior the tests exercise.** Characterization tests (record the old library's behavior)
  are the planned mitigation, not yet built.
- Benchmarks are small and authored by the project author. No real-world adoption evidence yet.

## Prior art (to be expanded)
Dependabot and Renovate bump versions; codemod tools need hand-written rules; general coding agents can edit code but do
not tie a verdict to a deterministic check. This project's narrow claim: signature-diff driven, coverage-aware,
verdict decided by code, with a visible proof bundle and untrusted-input handling.

## Development
```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]" coverage mypy
.venv/bin/python -m pytest -q          # 88 tests; the live OSV test runs with UPKEEP_LIVE=1
cp .env.example .env                   # then add your keys; .env is git-ignored. Never commit keys.
```
