"""Deterministic demo scene: a RECORDED wrong Werkzeug fix replayed against the real libraries. No model is called.

The wrong fix (`safe='/+'` plus `urlencode(doseq=True)`) is plausible and is exactly the kind of edit a model produces.
This shows, step by step, why Upkeep does not trust visible tests:
  1. the repo's own tests PASS on the new Werkzeug,
  2. happy-path probes find NO difference (how the fix would be accepted without the full check),
  3. Upkeep's edge-case probes expose it, naming the file, line and input, and the verdict is DECLINED.

Run (Docker running, demo/ prepared by scripts/demo_prepare.py):  .venv/bin/python -I scripts/demo_replay.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

from upkeep.charact import BehaviorChecker, _parse, find_targets, probe_script, where
from upkeep.llm import Reply
from upkeep.radar import run_radar
from upkeep.sandbox import run_python, run_tests

D = Path("demo/werkzeug_urls")
IMAGE = "python:3.9-slim"
WRONG_FIX = (
    "from urllib.parse import urlencode, quote as _quote\n\n\n"
    "def quote(s):\n    return _quote(s, safe='/+')\n\n\n"
    "def encode(d):\n    return urlencode(d, doseq=True)\n"
)
HAPPY = {"quote": [["hello world"], ["a b"]], "encode": [[{"q": "hello", "page": "2"}]]}  # what a lazy prober proposes


class HappyPathProber:
    """Stands in for a model that only ever proposes ordinary inputs."""

    def chat(self, messages, **kw):
        prompt = messages[-1]["content"]
        name = "encode" if "def encode" in prompt else "quote"
        return Reply(message={"content": json.dumps(HAPPY[name])})


def world(deps):
    return lambda d, script: run_python(d, script, backend="docker", image=IMAGE, deps_dir=deps, timeout=120)


def main() -> int:
    if not (D / "repo").exists():
        print("run scripts/demo_prepare.py first", file=sys.stderr)
        return 2
    repo, old, new = D / "repo", D / "old", D / "new"
    work = Path(tempfile.mkdtemp()) / "repo"
    shutil.copytree(repo, work)
    (work / "app.py").write_text(WRONG_FIX)
    print("Replaying a RECORDED wrong fix (a plausible edit a model produced) against the real Werkzeug 2.3.8 -> 3.0.3\n")
    print("--- the edit ---\n" + WRONG_FIX)

    report = run_radar(repo, old, new, "werkzeug")
    sites = [s for _, ss in report.affected for s in ss]
    print(f"radar: {report.risk.upper()}, {len(sites)} affected call site(s) in the ORIGINAL code\n")

    tests = run_tests(work, backend="docker", image=IMAGE, deps_dir=D / "deps_new", with_coverage=False,
                      pytest_args=["-q", "-p", "no:cacheprovider"], memory="1g", timeout=300)
    print(f"1. the repo's own tests on the new Werkzeug:  {'PASSED' if tests.ok else 'FAILED'}")

    run_old, run_new = world(D / "deps_old"), world(D / "deps_new")
    targets = find_targets(repo, sites)
    diffs = 0
    for t in targets:
        probes = HAPPY[t.name]
        a, b = _parse(run_old(repo, probe_script(t, probes))[1]), _parse(run_new(work, probe_script(t, probes))[1])
        diffs += sum(1 for x, y in zip(a or [], b or []) if x != y)
    print(f"2. happy-path probes against the old library:  {'no differences found' if not diffs else str(diffs) + ' difference(s)'}"
          "  <- this is how the fix would slip through")

    rep = BehaviorChecker(HappyPathProber(), repo, sites, run_old, run_new, n_inputs=8, probe_samples=1).check(work)
    print(f"3. Upkeep's check (same probes, plus edge cases like ':' '+' and None values): "
          f"{len(rep.mismatches)} difference(s) in {rep.inputs_checked} probes")
    for m in rep.mismatches[:3]:
        args = ", ".join(json.dumps(x) for x in m["input"])
        print(f"   {where(m)}{m['function']}({args})\n     old library -> {m['old']}\n     patched code -> {m['new']}")
    verdict = "DECLINED  (behavior differs from the old version)" if rep.mismatches else "VERIFIED"
    print(f"\nwerkzeug: {verdict}")
    shutil.rmtree(work.parent, ignore_errors=True)
    return 1 if rep.mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
