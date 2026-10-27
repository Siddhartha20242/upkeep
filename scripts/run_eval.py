"""Run UpkeepBench-Synth against the real model.  Authored cases, NOT a real-world benchmark.

Run:  .venv/bin/python -I scripts/run_eval.py [--config full|nosig|oneshot|nochangelog] [--repeat N] [--set dev|heldout|all] [--provider baseten|meta] [--model NAME] [case ...]
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

from upkeep.evalcases import BY_NAME as DEV_BY, CASES as DEV
from upkeep.evalcases_heldout import CASES as HELD
from upkeep.evalharness import run_case, summarize
from upkeep.llm import client_for, load_env

load_env(Path(__file__).resolve().parent.parent / ".env")


def pop_opt(args: list[str], flag: str, default: str) -> tuple[str, list[str]]:
    if flag in args:
        i = args.index(flag)
        return args[i + 1], args[:i] + args[i + 2:]
    return default, args


def main() -> int:
    args = sys.argv[1:]
    config, args = pop_opt(args, "--config", "full")
    repeat, args = pop_opt(args, "--repeat", "1")
    which, args = pop_opt(args, "--set", "dev")
    provider, args = pop_opt(args, "--provider", "baseten")
    model, args = pop_opt(args, "--model", "")
    pool = {"dev": DEV, "heldout": HELD, "all": DEV + HELD}[which]
    BY_NAME = {c.name: c for c in pool}
    names = args or [c.name for c in pool]
    client = client_for(provider, model or None)
    print(f"provider={provider} model={client.model} config={config}", flush=True)
    all_outcomes = []
    tally: dict[str, list[str]] = defaultdict(list)
    for r in range(int(repeat)):
        outcomes = []
        for n in names:
            o = run_case(BY_NAME[n], client, config)
            outcomes.append(o)
            tally[n].append(o.outcome)
            print(f"[{config} run {r + 1}] {o.name}: {o.outcome} ({o.model_calls} calls, {o.tokens} tokens, "
                  f"rejections {o.guard_rejections})", flush=True)
        all_outcomes += outcomes
        print("\n" + summarize(outcomes) + "\n", flush=True)
    print("=== per-case tally over", repeat, "run(s) ===")
    for n, outs in tally.items():
        print(f"{n:20} {BY_NAME[n].expect:13} " + ", ".join(f"{k}:{outs.count(k)}" for k in sorted(set(outs))))
    out = Path("eval_results")
    out.mkdir(exist_ok=True)
    (out / f"synth_{which}_{config}_{provider}.json").write_text(json.dumps([o.to_dict() for o in all_outcomes], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
