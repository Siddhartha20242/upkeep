"""Real-library benchmark.  Modes:  --selfcheck   validate every case in Docker against the real libraries
                                   --run [--provider baseten|meta] [--nochangelog] [--repeat N]
Needs Docker running. Library archives come from PyPI; dependencies install inside a container.
Authored downstream apps; real library releases.  Results: eval_results/reallib_*.json
"""
import json
import sys
import tempfile
from pathlib import Path

from upkeep.llm import client_for, load_env
from upkeep.reallib import CASES, prepare, run_agent_case, selfcheck

load_env(Path(__file__).resolve().parent.parent / ".env")
CACHE = Path.home() / ".cache" / "upkeep" / "reallib"  # not /tmp: macOS clears old files there


def opt(name, default):
    a = sys.argv
    return a[a.index(name) + 1] if name in a else default


def main() -> int:
    Path("eval_results").mkdir(exist_ok=True)
    only = opt("--only", "")
    cases = [c for c in CASES if not only or c.name == only]
    preps = {}
    for c in cases:
        print(f"preparing {c.name} ({c.dist} {c.old_ver} -> {c.new_ver}) ...", flush=True)
        preps[c.name] = prepare(c, CACHE)
        print(f"   changelog from release archive: {'yes' if preps[c.name].changelog else 'NOT FOUND'}", flush=True)
    if "--selfcheck" in sys.argv:
        results = [selfcheck(c, preps[c.name]) for c in cases]
        for r in results:
            print(json.dumps(r))
        Path("eval_results/reallib_selfcheck.json").write_text(json.dumps(results, indent=2))
        print("ALL SOUND" if all(r["sound"] for r in results) else "SOME CASES ARE UNSOUND")
        return 0
    provider, nochange, repeat = opt("--provider", "baseten"), "--nochangelog" in sys.argv, int(opt("--repeat", "1"))
    behavior = "--behavior" in sys.argv
    model = opt("--model", "")
    client = client_for(provider, model or None)
    pp = opt("--prober-provider", "")
    prober = client_for(pp, opt("--prober-model", "") or None) if pp else None
    esc = opt("--escalate", "")
    escalate = client_for(esc.partition(":")[0], esc.partition(":")[2] or None) if esc else None
    outs = []
    for i in range(repeat):
        for c in cases:
            o = run_agent_case(c, preps[c.name], client, use_changelog=not nochange, behavior_check=behavior,
                               focus_notes="--rawnotes" not in sys.argv,
                               notes_mode=opt("--notes", "tool"), max_turns=int(opt("--max-turns", "14")), prober_client=prober, n_random=int(opt("--fuzz", "0")), library_source="--libsrc" in sys.argv, explore="--explore" in sys.argv,
                               escalate_client=escalate, escalate_turns=int(opt("--escalate-turns", "40")))
            outs.append(o)
            print(f"[run {i + 1}] {o.name}: {o.outcome} ({o.model_calls} calls, {o.tokens} tokens, {o.seconds}s) {o.reason[:60]}", flush=True)
    Path(f"eval_results/reallib_{provider}{'_nochangelog' if nochange else ''}{'_' + model if model else ''}{'_behavior' if behavior else '_lint2'}{opt('--tag', '')}.json").write_text(
        json.dumps([o.to_dict() for o in outs], indent=2))
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
