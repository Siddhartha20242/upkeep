#!/usr/bin/env bash
# Full dry run of the 3-minute demo against the current code. Needs: Docker running, BASETEN_API_KEY set, network.
# Writes the transcript to docs/demo_rehearsal_output.txt (the fallback if the live run misbehaves on camera).
set -u
cd "$(dirname "$0")/.."
docker info >/dev/null 2>&1 || { echo "Docker is not running: open Docker Desktop and wait for it, then run this again."; exit 1; }
[ -n "${BASETEN_API_KEY:-}" ] || { echo "BASETEN_API_KEY is not set in this shell."; exit 1; }
.venv/bin/python -I scripts/demo_prepare.py >/dev/null || { echo "demo_prepare failed"; exit 1; }
rm -rf demo/out && mkdir -p demo/out
U=.venv/bin/upkeep
{
  echo '$ upkeep radar (PyYAML 5.4.1 -> 6.0.2)'
  $U radar --repo demo/pyyaml_load/repo --old demo/pyyaml_load/old --new demo/pyyaml_load/new --package yaml
  echo; echo '$ upkeep fix --behavior-check (PyYAML)'
  $U fix --repo demo/pyyaml_load/repo --old demo/pyyaml_load/old --new demo/pyyaml_load/new --package yaml \
    --changelog demo/pyyaml_load/CHANGELOG.txt --test-backend docker --image python:3.9-slim --deps demo/pyyaml_load/deps_new \
    --behavior-check --old-deps demo/pyyaml_load/deps_old --old-version 5.4.1 --new-version 6.0.2 --out demo/out/pyyaml_load
  echo; echo '$ upkeep fix --behavior-check (Werkzeug 2.3.8 -> 3.0.3)'
  $U fix --repo demo/werkzeug_urls/repo --old demo/werkzeug_urls/old --new demo/werkzeug_urls/new --package werkzeug \
    --changelog demo/werkzeug_urls/CHANGELOG.txt --test-backend docker --image python:3.9-slim --deps demo/werkzeug_urls/deps_new \
    --behavior-check --old-deps demo/werkzeug_urls/deps_old --old-version 2.3.8 --new-version 3.0.3 --out demo/out/werkzeug_urls
  echo; echo '$ python scripts/demo_replay.py   (recorded wrong fix, no model)'
  .venv/bin/python -I scripts/demo_replay.py
  echo; echo '$ upkeep verify --run demo/out/werkzeug_urls'
  $U verify --run demo/out/werkzeug_urls
} 2>&1 | tee docs/demo_rehearsal_output.txt
