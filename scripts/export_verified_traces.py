"""Turn verified `upkeep fix` run folders into chat-format JSONL (groundwork for fine-tuning; nothing is trained here).

Run:  .venv/bin/python -I scripts/export_verified_traces.py --out traces.jsonl [--with-declined] RUN_DIR [RUN_DIR ...]
Only runs that were verified, whose manifest still matches, and in which nothing was redacted are exported. See upkeep/sft.py.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from upkeep.sft import export  # noqa: E402


def main() -> int:
    a = sys.argv[1:]
    if "--out" not in a or not [x for x in a if not x.startswith("--")]:
        print(__doc__, file=sys.stderr)
        return 2
    out = Path(a[a.index("--out") + 1])
    runs = [Path(x) for x in a if not x.startswith("--") and x != str(out)]
    summary = export(runs, out, include_declined="--with-declined" in a)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
