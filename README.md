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

`audit` reads the exact pins (`name==version`) in your `requirements*.txt` and lists the lines of your code that each upgrade would break. It also lists the libraries it could not check, and why. It never counts those as safe.

The agent needs a key (`cp .env.example .env`, then add `BASETEN_API_KEY`) and Docker:

```bash
upkeep fix --repo R --old OLD_SRC --new NEW_SRC --package lib --test-backend docker --deps DEPS --behavior-check --old-deps OLD_DEPS
upkeep vulns --package requests --version 2.19.0
upkeep dashboard --run upkeep-out --evals eval_results --out dashboard.html
```

`upkeep fix` never changes your repo. It writes `fix.patch`, `PROOF.md` and `result.json` to a folder, and prints each agent step as it happens. `--open-pr` proposes a draft pull request, and only does a dry run unless you add `--confirm`. `--test-backend local --unsandboxed` runs your tests on your own machine with no isolation, so only use it on code you trust.

[`docs/TRY_IT.md`](docs/TRY_IT.md) walks through trying it on your own project. [`docs/DEMO.md`](docs/DEMO.md) is the script for the 3-minute demo.

## How it works

1. The radar reads the old and new source of the library, without running it, and works out what changed in the public API: removed names, removed or reordered parameters, new required arguments, changed defaults. Then it finds the lines in your repo that use something that changed. It only flags a call that actually trips the change, so `yaml.load(x, Loader=L)` is left alone and `yaml.load(x)` is flagged. No model is involved in this step.
2. The agent (Muse Spark Open) reads the flagged lines, pulls up the release notes if it needs them, edits only those lines and runs your tests. It has tools for listing the affected sites, reading files, replacing lines, running tests, checking behavior and reading release notes.
3. Code enforces the rules, not the prompt. An edit can only touch the flagged lines. It cannot add `eval`, `open`, network or OS imports, or an unsafe YAML loader. Tests run in Docker with no network and no secrets. Release notes and test output are treated as data and never as instructions.
4. The behavior check runs the original code on the old library and the patched code on the new one, with the same inputs. A model suggests the inputs, and Upkeep adds edge cases such as empty strings, unicode, separators and `None`. Any difference blocks the fix.
5. `PROOF.md` records what was checked and what was not. If Upkeep can't show a fix is safe, it says so and declines.

## What I found

- Werkzeug is still unsolved. With the behavior check on, 52 of 53 runs ended in a decline. One wrong fix got through before I added edge-case probes, and none of the 28 runs since. The agent has never produced a correct Werkzeug fix.
- PyYAML and Jinja2 work. In the latest runs the agent fixed each of them correctly 2 times out of 2. Earlier Jinja2 runs varied a lot, from 0 of 3 to 3 of 3 with the same settings, so I would not read much into small differences.
- The radar catches about two thirds of the lines that real developers had to change. I took 60 real commits that fixed an upgrade break, ran the radar on the code from before each fix, and set aside half of them as a held-out set. On that half it flagged 47 of 73 relevant lines (64%).
- I also ran it on 35 public repos I did not write. The first run flagged 299 lines and most were wrong. I fixed the causes one by one. On the repos I tuned on, 41 of 42 "will fail" flags were real. On a second set I never tuned on, it flagged 12 lines, and 2 of them mattered.
- The four Muse Spark versions call tools reliably. Over 128 tool calls each, they got everything exactly right 84% of the time (Open), 97% (1.1), 99% (1.2) and 100% (1.3). Muse Spark Open sometimes ends a turn right after reasoning without making the tool call, so the agent now asks again when a reply comes back empty.

[`docs/EVIDENCE.md`](docs/EVIDENCE.md) has all the tables, the caveats under each one, and the commands to repeat them.

## What does not work

- It has not been used by anyone but me. The apps and tests in my benchmarks are ones I wrote.
- It only handles Python, and mostly sees changes to function signatures. It cannot see a function that keeps its signature and behaves differently. It also cannot see a parameter that stops accepting a value it used to accept (Werkzeug 3 rejects `method="sha256"`).
- A clean behavior check is evidence and falls short of proof. The inputs are chosen by a model plus my edge cases, they are limited to JSON values, and only module-level functions are probed.
- The content check is a list of things to deny, and I found 13 ways around it while testing. Docker with no network is the real boundary.
- The samples are small, and some results moved a lot between runs.

## Checking a run

Every PyPI release is checked against the SHA-256 that PyPI publishes for it. A mismatch, or a release with no published hash, is refused before anything is unpacked.

`upkeep fix` also writes a `MANIFEST.json` with the SHA-256 of `fix.patch`, `PROOF.md` and `result.json`, and `upkeep verify --run upkeep-out` fails if any of them was edited afterwards. You can sign the manifest with an Ed25519 key (`upkeep keygen --key my.pem`, then `upkeep fix ... --sign-key my.pem`). `upkeep verify --run upkeep-out --pubkey my.pem.pub` checks that signature against the key you trust. None of this shows that a fix is correct. `PROOF.md` says what is and is not proven.

## Tests

There are 307 tests, with about 90% line coverage, and `mypy` reports no problems. GitHub Actions runs them on Python 3.10, 3.11 and 3.12. No test needs the network, Docker or a model key.

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
