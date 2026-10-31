# Demo script (3 minutes)

Every command below is real. The full dry run (`scripts/rehearse_demo.sh`) was last done on 2026-10-31, after the final radar changes; its transcript is `docs/demo_rehearsal_output.txt`. Re-run it right before recording.
Run `bash scripts/rehearse_demo.sh` first: it rebuilds the demo folders (`demo_prepare.py`), does the whole run once and saves the transcript to `docs/demo_rehearsal_output.txt` (Docker running, `BASETEN_API_KEY` set). It prints the exact
commands; they are also in the table below. Waiting time is cut in editing; show the clock once so it is honest.

## Before recording
- Docker Desktop running; `docker info` works. Keys in the environment, never on screen.
- `demo/` populated by `demo_prepare.py`. **Run it again right before you record**: the cached library sources were once emptied by something outside this project; the tools now detect that (the radar exits with code 3 instead of reporting green, and an empty cache is re-downloaded), but a fresh prepare costs a minute and removes the risk. Large terminal font, one terminal, dark theme.
- Dry-run both commands once so the images and dependency folders are warm (PyYAML ~75 s, Werkzeug ~90 s).
- Have `demo/dashboard.html` open in a browser tab for the last beat (see the command at the bottom).

## Timeline

| Time | On screen | Say (plain words) |
|---|---|---|
| 0:00 to 0:20 | The `app.py` using `yaml.load(text)`, and a one-line caption: "PyYAML 5.4.1 to 6.0.2, real release". | "Upgrading a dependency can break your code, and tests only catch what they happen to run. AI agents can edit code, but who checks the edit? Upkeep's rule: the code decides, not the model." |
| 0:20 to 0:40 | `upkeep radar ...` (about 1 s). Output: `yaml: RED`, two breaking changes, call sites at `app.py:5` and `app.py:9`. | "No model yet. It reads the old and new library source without running it, and finds exactly which of my lines the upgrade breaks." |
| 0:40 to 1:20 | `upkeep fix ... --behavior-check` for PyYAML. Cut the wait (about 75 s). The live trace shows release-note lookups, two edits, `tests PASSED`, `behavior NO DIFFERENCES FOUND`, `finish fixed`. End: `VERIFIED`, then the top of `PROOF.md`. | "The agent runs on Muse Spark Open. It edits only the lines the radar flagged. Then the code, not the model, re-runs the tests and compares the patched code on the new library against the original on the old one. No differences, so verified." |
| 1:20 to 2:15 | `python scripts/demo_replay.py` (5 s, no model, real Werkzeug 2.3.8 and 3.0.3 in Docker). It replays a **recorded** wrong fix: step 1 `tests ... PASSED`, step 2 `happy-path probes ... no differences found`, step 3 `4 difference(s)` with `app.py:9 (werkzeug.urls.url_encode) encode({"q": null})`: old `''`, patched `'q=None'`; then `werkzeug: DECLINED`. Optionally cut to one live `upkeep fix` run on Werkzeug (about 90 s, cut), but see the note below. | "Here is an edit a model really produced. It passes every test I have, and it even passes ordinary probe inputs. But a `None` value becomes the text 'None', and a colon gets encoded. The old library is the oracle, so Upkeep refuses and says exactly where. Without this check, all 18 of my live runs on this library went green and wrong. With it, 1 of 34 got through, and that one used happy-path probes; with edge-case probes added, 0 of 9." |
| 2:15 to 2:30 | `upkeep verify --run demo/out/werkzeug_urls` prints `OK`. Edit one character in `fix.patch`, run it again: `FAIL fix.patch: CHANGED`. | "Every run writes a hashed manifest, and library downloads are checked against PyPI's published hashes. Edit the patch afterwards and verify fails." |
| 2:30 to 2:50 | `demo/dashboard.html`: the run page, then the benchmark tables. | "Honest numbers: on libraries I faked it is near perfect, which just shows those are easy. On real releases it is mixed. The radar catches about two thirds of the lines real developers had to change, and I checked it on repos I did not write. The proof file always says what it does not prove." |
| 2:50 to 3:00 | Repo URL, `github.com/Siddhartha20242/upkeep`. | "Built during the hackathon on Muse Spark Open. Verification by code, not by trust." |

The Werkzeug scene uses the **replay** because the live agent is non-deterministic: in the last rehearsal it declined with
"no edit was made" (honest, but it shows no difference), and in earlier runs it declined naming a concrete input. If you also
show a live run, say which it is and do not re-run until a nicer result appears. **Do not promise a specific live scene**: the PyYAML run sometimes tries the unsafe `yaml.Loader`
first and the content check rejects it (a good moment if it happens, but it did not happen in the last rehearsal), and the
Werkzeug decline prints whichever difference the probes found first.

## What to point at for the judges
- **Meta tooling:** the agent loop, release-note tool calls and probe-input proposals all run on
  `meta-models/muse-spark-open-1026` (Baseten). Muse Spark 1.1, 1.2 and 1.3 on the Meta API are compared in the README's
  scorecard (tool-call reliability over 512 calls, authored cases, the Werkzeug trap, probe quality).
- **Technical depth:** static API diff, scope guard, content check, sandbox with no network, differential behavior check.
- **Honesty:** the README lists fourteen things that went wrong and what caught each.

## If something goes wrong on camera
- **Model slow or fails:** play the saved rehearsal transcript `docs/demo_rehearsal_output.txt` or open `demo/out/*/PROOF.md` and say on screen that it is from the rehearsal.
- **Docker not running:** `open -a Docker`, wait for `docker info`, retry.
- **Do not** re-run until a nicer result appears. The Werkzeug run is non-deterministic; its decline is the honest outcome
  (with the check on it has never produced a correct fix here; it declined in 33 of 34 runs and let one wrong fix through).

## Commands (from the project root)

```bash
upkeep radar --repo demo/pyyaml_load/repo --old demo/pyyaml_load/old --new demo/pyyaml_load/new --package yaml
upkeep fix --repo demo/pyyaml_load/repo --old demo/pyyaml_load/old --new demo/pyyaml_load/new --package yaml \
  --changelog demo/pyyaml_load/CHANGELOG.txt --test-backend docker --image python:3.9-slim --deps demo/pyyaml_load/deps_new \
  --behavior-check --old-deps demo/pyyaml_load/deps_old --old-version 5.4.1 --new-version 6.0.2 --out demo/out/pyyaml_load
upkeep fix --repo demo/werkzeug_urls/repo --old demo/werkzeug_urls/old --new demo/werkzeug_urls/new --package werkzeug \
  --changelog demo/werkzeug_urls/CHANGELOG.txt --test-backend docker --image python:3.9-slim --deps demo/werkzeug_urls/deps_new \
  --behavior-check --old-deps demo/werkzeug_urls/deps_old --old-version 2.3.8 --new-version 3.0.3 --out demo/out/werkzeug_urls
python scripts/demo_replay.py                         # the Werkzeug scene: a recorded wrong fix, no model needed
upkeep dashboard --run demo/out/werkzeug_urls --evals eval_results --out demo/dashboard.html
upkeep verify --run demo/out/werkzeug_urls            # OK; then edit fix.patch and run it again for FAIL
```
