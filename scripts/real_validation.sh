#!/bin/bash
# REVIEW BEFORE RUNNING. What this does, in order:
#   1. git-clones 4 public GitHub repos (read-only download; nothing is executed by git).
#   2. For each, installs the repo's requirements PLUS PyYAML==6.0.2 into a folder, inside a
#      Docker container WITH network access (third-party build scripts can run here, contained
#      in the container: non-root, capabilities dropped, memory-limited).
#   3. Runs each repo's tests TWICE inside Docker with NO network:
#        - at the commit BEFORE its real fix, and at the real fix commit.
#   4. A case is "valid" only if some test fails before the fix and passes after it.
# It never writes to the repos, never uses your GitHub credentials, and sends no data anywhere
# except the normal downloads (git clone, pip). Results: eval_results/real_validation.txt
set -u
# Optional: REPOS="owner/name ..." PIP_ARGS="--no-build-isolation" IMAGE=python:3.9-slim bash scripts/real_validation.sh   (older repos often need an older Python)
cd "$(dirname "$0")/.."
export PATH="$PATH:/Applications/Docker.app/Contents/Resources/bin"
docker info >/dev/null 2>&1 || { echo "Docker is not running. Start Docker Desktop first."; exit 1; }
WORK="${TMPDIR:-/tmp}/upkeep-real-validation"; mkdir -p "$WORK" eval_results
: > eval_results/real_validation.txt
for repo in ${REPOS:-NCI-GDC/gdc-client k4cg/heiko canonical/charm-nrpe AAFC-BICoE/seqdb-py}; do
  echo "=== $repo" | tee -a eval_results/real_validation.txt
  .venv/bin/python -I scripts/validate_case.py "$WORK" data/candidates.json "$repo" "PyYAML==6.0.2" "${IMAGE:-python:3.12-slim}" "${PIP_ARGS:-}" 2>&1 \
    | tee -a eval_results/real_validation.txt | tail -25
done
echo; echo "Done. Full log: eval_results/real_validation.txt"
