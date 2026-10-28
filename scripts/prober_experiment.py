"""Does an INDEPENDENT prober catch more? Probe proposals from four Meta models, replayed against known wrong fixes.

For two wrong Werkzeug fixes (a plausible-but-wrong `safe='/+'` rewrite, and the naive default `urllib` swap), each model
proposes probe inputs (no deterministic expansion, so only the model's own imagination is tested; temperature 0.8) for
the two changed functions. We run each probe set against the REAL Werkzeug 2.3.8 (old) and 3.0.3 (new) in Docker and
record whether it exposes a difference. Then compare:
  one model alone            how often a single model's probes catch the wrong fix
  two samples, SAME model    union of two samples from one model (just "more tries")
  two samples, DIFFERENT     union of one sample each from two different models (independent eyes)

Run (Docker running, demo/ prepared by scripts/demo_prepare.py, keys set):
  .venv/bin/python -I scripts/prober_experiment.py [--samples 6]
"""
import itertools
import json
import shutil
import sys
import tempfile
from pathlib import Path

from upkeep.charact import _parse, find_targets, probe_script, propose_inputs
from upkeep.llm import client_for, load_env
from upkeep.radar import run_radar
from upkeep.sandbox import run_python

load_env(Path(__file__).resolve().parent.parent / ".env")
D = Path("demo/werkzeug_urls")
MODELS = {"Open": ("baseten", None), "1.1": ("meta", "muse-spark-1.1"), "1.2": ("meta", "muse-spark-1.2"), "1.3": ("meta", "muse-spark-1.3")}
HEAD = "from urllib.parse import urlencode, quote as _quote\n\n\n"
VARIANTS = {
    "safe='/+' + urlencode(doseq)": HEAD + "def quote(s):\n    return _quote(s, safe='/+')\n\n\ndef encode(d):\n    return urlencode(d, doseq=True)\n",
    "naive quote(s) + urlencode(d)": HEAD + "def quote(s):\n    return _quote(s)\n\n\ndef encode(d):\n    return urlencode(d)\n",
}


def world(deps):
    return lambda d, script: run_python(d, script, backend="docker", image="python:3.9-slim", deps_dir=deps, timeout=120)


def main() -> int:
    k = int(sys.argv[sys.argv.index("--samples") + 1]) if "--samples" in sys.argv else 6
    repo, old, new = D / "repo", D / "old", D / "new"
    report = run_radar(repo, old, new, "werkzeug")
    targets = find_targets(repo, [s for _, ss in report.affected for s in ss])
    run_old, run_new = world(D / "deps_old"), world(D / "deps_new")
    variants = {}
    for name, src in VARIANTS.items():
        v = Path(tempfile.mkdtemp()) / "repo"
        shutil.copytree(repo, v)
        (v / "app.py").write_text(src)
        variants[name] = v

    caught = {m: {v: [] for v in VARIANTS} for m in MODELS}   # model -> variant -> [bool per sample]
    probes_n = {m: [] for m in MODELS}
    for m, (prov, mod) in MODELS.items():
        client = client_for(prov, mod)
        for i in range(k):
            sets = {t.name: propose_inputs(client, t, 8, temperature=0.8)[0] for t in targets}
            probes_n[m].append(sum(len(x) for x in sets.values()))
            old_res = {t.name: _parse(run_old(repo, probe_script(t, sets[t.name]))[1]) for t in targets if sets[t.name]}
            for vname, vdir in variants.items():
                hit = False
                for t in targets:
                    if not sets[t.name] or not isinstance(old_res.get(t.name), list):
                        continue
                    new_res = _parse(run_new(vdir, probe_script(t, sets[t.name]))[1])
                    if isinstance(new_res, list) and any(a != b for a, b in zip(old_res[t.name], new_res)):
                        hit = True
                caught[m][vname].append(hit)
            print(f"[{m} sample {i + 1}/{k}] probes={probes_n[m][-1]} caught: " + ", ".join(f"{v[:14]}={caught[m][v][-1]}" for v in VARIANTS), flush=True)

    print("\n=== catch rate of a model's OWN probes (no expansion) ===")
    for v in VARIANTS:
        print(f"  {v}")
        for m in MODELS:
            c = caught[m][v]
            print(f"    {m:5} {sum(c)}/{len(c)}  (avg {sum(probes_n[m]) / len(probes_n[m]):.0f} probes)")
    print("\n=== independent eyes? union of two probe sets ===")
    summary = {}
    for v in VARIANTS:
        same, diff = [], []
        for a in MODELS:
            for i, j in itertools.combinations(range(k), 2):
                same.append(caught[a][v][i] or caught[a][v][j])
        for a, b in itertools.combinations(MODELS, 2):
            for i in range(k):
                for j in range(k):
                    diff.append(caught[a][v][i] or caught[b][v][j])
        single = [x for m in MODELS for x in caught[m][v]]
        summary[v] = {"single": sum(single) / len(single), "two_same_model": sum(same) / len(same), "two_different_models": sum(diff) / len(diff)}
        s = summary[v]
        print(f"  {v}: single {100 * s['single']:.0f}% | two samples, same model {100 * s['two_same_model']:.0f}% | two models {100 * s['two_different_models']:.0f}%")
    Path("eval_results/prober_experiment.json").write_text(json.dumps({"samples_per_model": k, "caught": caught, "probes": probes_n, "summary": summary}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
