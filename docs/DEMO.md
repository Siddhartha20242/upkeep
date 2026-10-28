# Demo script (3 minutes)

Every command below is real and was rehearsed on 2026-10-28 against real PyYAML and Werkzeug releases.
Run `.venv/bin/python -I scripts/demo_prepare.py` first (Docker running, `BASETEN_API_KEY` set). It prints the exact
commands; they are also in the table below. Waiting time is cut in editing; show the clock once so it is honest.

## Before recording
- Docker Desktop running; `docker info` works. Keys in the environment, never on screen.
- `demo/` populated by `demo_prepare.py`. **Run it again right before you record**: the cached library sources were once emptied by something outside this project; the tools now detect that (the radar exits with code 3 instead of reporting green, and an empty cache is re-downloaded), but a fresh prepare costs a minute and removes the risk. Large terminal font, one terminal, dark theme.
- Dry-run both commands once so the images and dependency folders are warm (PyYAML ~37 s, Werkzeug ~70 s).
- Have `demo/dashboard.html` open in a browser tab for the last beat (see the command at the bottom).

## Timeline

| Time | On screen | Say (plain words) |
|---|---|---|
| 0:00 to 0:20 | The `app.py` using `yaml.load(text)`, and a one-line caption: "PyYAML 5.4.1 to 6.0.2, real release". | "Upgrading a dependency can break your code, and tests only catch what they happen to run. AI agents can edit code, but who checks the edit? Upkeep's rule: the code decides, not the model." |
| 0:20 to 0:45 | `upkeep radar ...` (about 1 s). Output: `yaml: RED`, two breaking changes, call sites at `app.py:5` and `app.py:9`. | "No model yet. It reads the old and new library source without running it, and finds exactly which of my lines the upgrade breaks." |
| 0:45 to 1:30 | `upkeep fix ... --behavior-check` for PyYAML. Cut the wait. Show the trace in the dashboard: one edit **rejected** (`unsafe deserialization yaml.Loader`), then `yaml.SafeLoader` accepted. End: `VERIFIED`, patch, then `PROOF.md`. | "The agent runs on Muse Spark Open. It first tried the unsafe loader and the content check rejected it, because silencing an error with an insecure fix is not an upgrade. The proof says one function has no test, but behavior probes against the old library exercised it: 16 probes, 0 differences." |
| 1:30 to 2:30 | `upkeep fix ... --behavior-check` for Werkzeug (about 70 s, cut). End: `DECLINED` and the line with the `colors` example. | "Here the obvious fix passes every visible test and is wrong. The old library encodes a list as `colors=red&colors=green&colors=blue`; the replacement turns the list into a string. The old library is the oracle, so Upkeep refuses. Without this check, all 18 of my runs on this library went green and wrong. With it, 1 of 31 got through, and that one used happy-path probes; with edge-case probes added, 0 of 6." |
| 2:30 to 2:50 | `demo/dashboard.html`: the run page, then the benchmark tables. | "Honest numbers: on libraries I faked it is near perfect, which just shows those are easy. On real releases it is mixed, 3 to 6 of 9. Mining real repos from GitHub gave 0 of 4 usable cases. The proof file always says what it does not prove." |
| 2:50 to 3:00 | Repo URL, `github.com/Siddhartha20242/upkeep`. | "Built during the hackathon on Muse Spark Open. Verification by code, not by trust." |

## What to point at for the judges
- **Meta tooling:** the agent loop, release-note tool calls and probe-input proposals all run on
  `meta-models/muse-spark-open-1026` (Baseten). `muse-spark-1.3` on the Meta API was used only as a comparison.
- **Technical depth:** static API diff, scope guard, content check, sandbox with no network, differential behavior check.
- **Honesty:** the README lists five things that went wrong and what caught each.

## If something goes wrong on camera
- **Model slow or fails:** use the recorded `demo/out/*/PROOF.md` and `result.json` from the rehearsal and say so on screen.
- **Docker not running:** `open -a Docker`, wait for `docker info`, retry.
- **Do not** re-run until a nicer result appears. The Werkzeug run is non-deterministic; its decline is the honest outcome
  (with the check on it has never produced a correct fix here; it declined in 30 of 31 runs and let one wrong fix through).

## Commands (from the project root)

```bash
upkeep radar --repo demo/pyyaml_load/repo --old demo/pyyaml_load/old --new demo/pyyaml_load/new --package yaml
upkeep fix --repo demo/pyyaml_load/repo --old demo/pyyaml_load/old --new demo/pyyaml_load/new --package yaml \
  --changelog demo/pyyaml_load/CHANGELOG.txt --test-backend docker --image python:3.9-slim --deps demo/pyyaml_load/deps_new \
  --behavior-check --old-deps demo/pyyaml_load/deps_old --old-version 5.4.1 --new-version 6.0.2 --out demo/out/pyyaml_load
upkeep fix --repo demo/werkzeug_urls/repo --old demo/werkzeug_urls/old --new demo/werkzeug_urls/new --package werkzeug \
  --changelog demo/werkzeug_urls/CHANGELOG.txt --test-backend docker --image python:3.9-slim --deps demo/werkzeug_urls/deps_new \
  --behavior-check --old-deps demo/werkzeug_urls/deps_old --old-version 2.3.8 --new-version 3.0.3 --out demo/out/werkzeug_urls
upkeep dashboard --run demo/out/werkzeug_urls --evals eval_results --out demo/dashboard.html
```
