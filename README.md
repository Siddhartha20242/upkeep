# Upkeep

![tests](https://github.com/Siddhartha20242/upkeep/actions/workflows/tests.yml/badge.svg)

Upkeep checks what a Python dependency upgrade would break in your code. If an AI agent proposes the fix, Upkeep tests that fix against the old version of the library before it trusts it. The code decides whether a fix is safe, and the model's opinion doesn't count.

I built it for the Meta Global AI Developer Hackathon (Indie Developer track, Oct 26 to Nov 4, 2026). The agent runs on Muse Spark Open (`meta-models/muse-spark-open-1026`), served through Baseten. I wrote the code during the event, with Claude Code helping.

## The problem

When a library ships a breaking change, your tests only tell you about the code they happen to run. AI agents can edit code, but nothing checks that an edit keeps the old behavior.

I tried it on a real upgrade, Werkzeug 2.3.8 to 3.0.3. With no behavior check, the agent's fix passed every test and was still wrong in 18 of 18 runs. That happened with Muse Spark Open and with Muse Spark 1.1, 1.2 and 1.3. So Upkeep ignores the agent's claim that it is done and compares the patched code with the old library instead.

## Try it

You don't need an API key for the first two commands. They only download the library releases from PyPI and read your repo locally.

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,sign]" coverage mypy
source .venv/bin/activate
upkeep audit --repo .                                                       # every pinned dependency vs its newest release
upkeep check --repo . --package PyYAML --from 5.4.1 --to 6.0.2 --module yaml # one library, two versions you choose
```

`audit` reads the exact pins (`name==version`) in your `requirements*.txt` and lists the lines of your code that each upgrade would break. It also lists the libraries it could not check, and why. It never counts those as safe. Add `--summary` to print a short block of counts and library names (no code, no paths) that you can send to someone.

The agent needs Docker and a key (`cp .env.example .env`, then add `BASETEN_API_KEY`). One command does the rest:

```bash
upkeep fix --repo . --package PyYAML --from 5.4.1 --to 6.0.2 --module yaml
upkeep vulns --package requests --version 2.19.0
upkeep dashboard --run upkeep-out --evals eval_results --out dashboard.html
```

`fix` downloads both releases, installs your dependencies for each version inside Docker (the only step that uses the network, wheels only), runs the agent and writes the result to `upkeep-out/`. The first run takes a few minutes. If you already have the library source and dependency folders, `--old`, `--new`, `--deps` and `--old-deps` do the same job by hand (see `docs/DEMO.md`).

`upkeep fix` never changes your repo. It writes `fix.patch`, `PROOF.md` and `result.json`, and prints each agent step as it happens. `--open-pr` proposes a draft pull request, and only does a dry run unless you add `--confirm`. `--test-backend local --unsandboxed` runs your tests on your own machine with no isolation, so only use it on code you trust. `upkeep audit --fail-on hard` is the form to use in CI.

[`docs/TRY_IT.md`](docs/TRY_IT.md) walks through trying it on your own project. [`docs/DEMO.md`](docs/DEMO.md) is the script for the 3-minute demo.

## How it works

1. The radar reads the old and new source of the library, without running it, and works out what changed in the public API: removed names, removed or reordered parameters, new required arguments, changed defaults. Then it finds the lines in your repo that use something that changed. It only flags a call that actually trips the change, so `yaml.load(x, Loader=L)` is left alone and `yaml.load(x)` is flagged. No model is involved in this step.
2. The agent (Muse Spark Open) reads the flagged lines, pulls up the release notes if it needs them, edits only those lines and runs your tests. It has tools for listing the affected sites, reading files, replacing lines, running tests, checking behavior and reading release notes.
3. Code enforces the rules, not the prompt. An edit can only touch the flagged lines. It cannot add `eval`, `open`, network or OS imports, or an unsafe YAML loader. Tests run in Docker with no network and no secrets. Release notes and test output are treated as data and never as instructions.
4. The behavior check runs the original code on the old library and the patched code on the new one, with the same inputs. A model suggests the inputs, and Upkeep adds edge cases such as empty strings, unicode, separators and `None`. Any difference blocks the fix.
5. `PROOF.md` records what was checked and what was not. If Upkeep can't show a fix is safe, it says so and declines.

## What I found

- Werkzeug is the hard case, and it splits the models. Muse Spark Open has never produced a correct fix (0 of 47 runs, even with library source, a compare tool and 40 turns). Muse Spark 1.3 with the compare tool fixed it 2 of 2 (hidden tests pass), at 338k to 574k tokens a run. Over all 64 runs with the behavior check on, 59 ended in a decline, 4 were verified and correct, and 1 was verified and wrong. That wrong one came from Muse Spark 1.1 before I added edge-case probes.
- `upkeep fix --escalate meta:muse-spark-1.3` uses that split. Open goes first because it is cheap and open-weight. If the verifier declines, a stronger Muse Spark model takes over, and the same verifier judges its patch. End to end on Werkzeug this verified a correct fix in 2 of 3 runs, and in the third the verifier declined the stronger model's patch (`docs/EVIDENCE.md`, result 16). The hard case is therefore solved by a model that is not open-weight.
- PyYAML and Jinja2 work. In the latest runs the agent fixed each of them correctly 5 times out of 5 (the hidden tests pass). Earlier Jinja2 runs varied a lot, from 0 of 3 to 3 of 3 with the same settings, so those older numbers are noisier than they look.
- The radar catches about two thirds of the lines that real developers had to change. I took 60 real commits that fixed an upgrade break, ran the radar on the code from before each fix, and set aside half of them as a held-out set. On that half it flagged 47 of 73 relevant lines (64%).
- I also ran it on 35 public repos I did not write. The first run flagged 299 lines and most were wrong. I fixed the causes one by one. On the repos I tuned on, 41 of 42 "will fail" flags were real. On a second set I never tuned on, it flagged 12 lines, and 2 of them mattered.
- The four Muse Spark versions call tools reliably. Over 128 tool calls each, they got everything exactly right 84% of the time (Open), 97% (1.1), 99% (1.2) and 100% (1.3). Muse Spark Open sometimes ends a turn right after reasoning without making the tool call, so the agent now asks again when a reply comes back empty.

[`docs/EVIDENCE.md`](docs/EVIDENCE.md) has all the tables, the caveats under each one, and the commands to repeat them.

## What does not work

- It has not been used by anyone but me. The apps and tests in my benchmarks are ones I wrote. I also tried to find real projects where an upgrade breaks their own tests: of 70 public repos (two search strategies), none passed every stage, so I have no downstream validation to show (`docs/EVIDENCE.md`, result 17).
- It only handles Python, and mostly sees changes to function signatures. It cannot see a function that keeps its signature and behaves differently. It also cannot see a parameter that stops accepting a value it used to accept (Werkzeug 3 rejects `method="sha256"`).
- A clean behavior check is evidence and falls short of proof. A model chooses the inputs, I add edge cases, and a second round targets lines the first inputs never reached. Inputs are JSON (plus tagged tuples, sets and bytes) and only module-level functions are probed. `PROOF.md` says how many lines of each function the probes executed, so you can see how thin the evidence is.
- Redaction knows the shape of common tokens, the exact values of your own environment secrets and `.env` files, and what random keys look like. It will still miss a secret of an unknown shape that is not in your environment. The content check now denies new imports and builtins by default, but attribute tricks are still a deny list, and I found 13 ways around an earlier version. Docker with no network is the real boundary.
- The samples are small, and some results moved a lot between runs.

## Security

Your code goes to a model provider when you run `fix`, so secret-like strings are redacted first, the agent cannot read `.env` or key files, and a patch containing a secret is never sent as a pull request. Tests run in Docker with no network. [`SECURITY.md`](SECURITY.md) lists what is protected and what is not.

## Checking a run

Every PyPI release is checked against the SHA-256 that PyPI publishes for it. A mismatch, or a release with no published hash, is refused before anything is unpacked.

`upkeep fix` also saves everything it sent to the model (`model_transcript.json`, after secret redaction) and writes a `MANIFEST.json` with the SHA-256 of that file, `fix.patch`, `PROOF.md` and `result.json`, and `upkeep verify --run upkeep-out` fails if any of them was edited afterwards. You can sign the manifest with an Ed25519 key (`upkeep keygen --key my.pem`, then `upkeep fix ... --sign-key my.pem`). `upkeep verify --run upkeep-out --pubkey my.pem.pub` checks that signature against the key you trust. None of this shows that a fix is correct. `PROOF.md` says what is and is not proven.

## Tests

There are 512 tests, with about 90% line coverage, and `mypy` reports no problems. GitHub Actions runs them on Python 3.10, 3.11 and 3.12. No test needs the network, Docker or a model key.

Two kinds of test found real bugs. The first is a ground-truth test for the radar. It generates thousands of random function signatures and changes, then asks Python's own `inspect.Signature.bind` whether each call still works. On its first run the radar missed 11% of the calls that broke, and I fixed that. The second is a set of adversarial edits for the content check. Details are in [`docs/EVIDENCE.md`](docs/EVIDENCE.md).

```bash
.venv/bin/python -m pytest -q
```

## Where things are

- `upkeep/` is the package: the radar (`radar.py`, `apidiff.py`, `scan.py`), the agent (`agent.py`), the guards (`guard.py`, `patchlint.py`, `untrusted.py`, `sandbox.py`) and the behavior check (`charact.py`).
- `scripts/` holds the benchmarks and experiments, and `eval_results/` holds their raw output.
- `tests/` holds the tests, and `docs/` holds the demo script, the try-it guide and the evidence.

## Credits

Dependabot and Renovate propose version bumps, codemod tools apply hand-written rules, and general coding agents can edit code. Upkeep does not try to be a better editor. It ties the verdict to deterministic checks and shows what was and was not verified. I have not benchmarked it against those tools.

I used Claude Code to help write the code, the tests and this page. Every number comes from a run recorded in `eval_results/`. MIT license.
