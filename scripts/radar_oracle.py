"""Radar vs Python's own call binding on random signature changes (synthetic; see tests/radar_oracle_lib.py).

Run:  .venv/bin/python -I scripts/radar_oracle.py [--pairs 5000] [--seed 2026]      No network, no model.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.radar_oracle_lib import run  # noqa: E402


def main() -> int:
    a = sys.argv[1:]
    pairs = int(a[a.index("--pairs") + 1]) if "--pairs" in a else 5000
    seed = int(a[a.index("--seed") + 1]) if "--seed" in a else 2026
    r = run(pairs, seed=seed)
    s = r["stats"]
    out = {"pairs": s["pairs"], "calls": s["calls"], "calls_that_stop_working": s["breaks"], "caught": s["caught"],
           "recall": round(s["caught"] / s["breaks"], 4), "hard_flags": s["flagged_hard"],
           "hard_flags_that_were_not_breaking": s["hard_not_breaking"],
           "precision": round(1 - s["hard_not_breaking"] / s["flagged_hard"], 4),
           "calls_flagged_only_for_a_default_change": s["soft_only"], "uncertain_not_counted": s["uncertain"],
           "seed": seed, "by_mutation": r["by_kind"], "first_misses": r["missed"][:5], "first_false_alarms": r["false_alarms"][:5]}
    Path("eval_results").mkdir(exist_ok=True)
    Path("eval_results/radar_oracle.json").write_text(json.dumps(out, indent=2))
    print(json.dumps({k: v for k, v in out.items() if k not in ("by_mutation", "first_misses", "first_false_alarms")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
