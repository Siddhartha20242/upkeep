"""Static, self-contained HTML dashboard for one `upkeep fix` run plus the benchmark results.

Everything that comes from a repo, a model or a test run is HTML-escaped (it is untrusted text).
No scripts, no external requests: the page is a plain file you can open or screenshot.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

CSS = """
:root{--bg:#fafafa;--fg:#1b1f23;--mut:#5d6773;--card:#fff;--line:#e3e7ec;--ok:#1a7f4b;--warn:#a15c00;--bad:#b42318;--add:#e6f6ec;--del:#fdecea;--acc:#2457d6}
@media(prefers-color-scheme:dark){:root{--bg:#111418;--fg:#e8ebef;--mut:#9aa5b1;--card:#181c21;--line:#2a3038;--ok:#4cc38a;--warn:#e0a34a;--bad:#f2766b;--add:#12301f;--del:#3a1816;--acc:#7aa2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,system-ui,Segoe UI,sans-serif}
main{max-width:980px;margin:0 auto;padding:24px 16px 64px}h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 10px}
.sub{color:var(--mut);margin:0 0 20px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:10px 0}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-weight:600;font-size:13px;border:1px solid currentColor}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.mut{color:var(--mut)}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600}code,pre{font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}
pre{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;overflow:auto;margin:8px 0}
.add{background:var(--add)}.del{background:var(--del)}.step{display:flex;gap:10px;padding:5px 0;border-bottom:1px dashed var(--line)}
.step b{min-width:150px;color:var(--acc)}ul{margin:6px 0 6px 20px;padding:0}.kv{display:flex;gap:24px;flex-wrap:wrap}.kv div{min-width:130px}
.kv span{display:block;color:var(--mut);font-size:12px}.kv strong{font-size:20px}
"""

STATUS_CLASS = {"verified": "ok", "declined": "bad", "tests-pass-but-unverified-sites": "warn", "unverified": "warn"}
OUTCOME_CLASS = {"success": "ok", "undetected": "mut", "wrong_but_green": "bad", "declined_failure": "warn",
                 "unsafe": "bad", "missed": "bad"}


def e(x) -> str:
    return html.escape(str(x), quote=True)


def _inline(text: str) -> str:
    t = e(text)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    return re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", t)


def md_to_html(md: str) -> str:
    out, in_list = [], False
    for line in md.splitlines():
        if line.startswith("- ") or line.startswith("  - "):
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{_inline(line.lstrip(' -'))}</li>")
            continue
        if in_list:
            out.append("</ul>"); in_list = False
        if line.startswith("## "):
            out.append(f"<h3>{_inline(line[3:])}</h3>")
        elif line.startswith("# "):
            out.append(f"<h2>{_inline(line[2:])}</h2>")
        elif line.strip():
            out.append(f"<p>{_inline(line)}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def diff_html(patch: str) -> str:
    rows = []
    for line in patch.splitlines():
        cls = "add" if line.startswith("+") and not line.startswith("+++") else "del" if line.startswith("-") and not line.startswith("---") else ""
        rows.append(f'<span class="{cls}">{e(line)}</span>' if cls else e(line))
    return "<pre>" + "\n".join(rows) + "</pre>" if rows else '<p class="mut">No patch (nothing was changed).</p>'


def _step_text(step: dict) -> str:
    """One readable line per tool step: no raw JSON, no internal untrusted-data markers."""
    text = str(step.get("result", ""))
    if step.get("tool") == "list_affected":
        try:
            rows = json.loads(text) if text.rstrip().endswith("]") else None
        except ValueError:
            rows = None
        return f"{len(rows)} affected site(s) reported" if rows is not None else "affected sites listed"
    lines = [l for l in text.splitlines() if not l.startswith("<<<")]
    return " ".join(lines)[:150]


def run_section(result: dict, patch: str, proof_md: str) -> str:
    status = result.get("status", "unknown")
    proof_status = re.search(r"\*\*Status:\*\* `([^`]+)`", proof_md)
    shown = proof_status.group(1) if proof_status else status
    cls = STATUS_CLASS.get(shown, "mut")
    steps = "".join(f'<div class="step"><b>{e(s.get("tool"))}</b><span class="mut">{e(_step_text(s))}</span></div>'
                    for s in result.get("steps", []))
    flags = result.get("injection_flags") or []
    kv = (f'<div><span>model calls</span><strong>{e(result.get("model_calls", 0))}</strong></div>'
          f'<div><span>tokens</span><strong>{e(result.get("tokens", 0))}</strong></div>'
          f'<div><span>guard rejections</span><strong>{e(result.get("guard_rejections", 0))}</strong></div>'
          f'<div><span>untrusted-input flags</span><strong>{e(len(flags))}</strong></div>')
    return (f'<h2>Run</h2><div class="card"><span class="badge {cls}">{e(shown)}</span> '
            f'<span class="mut">{e(result.get("reason", ""))}</span><div class="kv" style="margin-top:12px">{kv}</div></div>'
            f'<h2>What the agent did</h2><div class="card">{steps or "<span class=mut>no steps recorded</span>"}</div>'
            f'<h2>Patch</h2>{diff_html(patch)}{behavior_section(result.get("behavior"))}<h2>Proof</h2><div class="card">{md_to_html(proof_md)}</div>')


def _pretty(raw) -> str:
    """Probe results are stored as JSON like {"ok": "'a=1'"} or {"err": "TypeError"}; show the value, not the wrapper."""
    try:
        d = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return str(raw)
    if isinstance(d, dict) and "ok" in d:
        return str(d["ok"])
    if isinstance(d, dict) and "err" in d:
        return f"raises {d['err']}"
    return str(raw)


def behavior_section(behavior: dict | None) -> str:
    if not behavior:
        return ""
    mm = behavior.get("mismatches", [])
    head = (f"<h2>Behavior check against the old library</h2><p class='mut'>{e(behavior.get('inputs_checked', 0))} probe inputs over "
            f"{e(behavior.get('functions_checked', 0))} function(s): "
            f"<b class='{'bad' if mm else 'ok'}'>{len(mm)} difference(s)</b></p>")
    if not mm:
        return head
    rows = "".join(
        f"<tr><td><code>{e(m.get('function'))}({e(', '.join(json.dumps(a) for a in m.get('input', [])))})</code></td>"
        f"<td class='ok'><code>{e(_pretty(m.get('old')))}</code></td><td class='bad'><code>{e(_pretty(m.get('new')))}</code></td></tr>"
        for m in mm[:8])
    return head + ("<div class='card'><table><tr><th>call</th><th>old library</th><th>patched code on the new library</th></tr>"
                   + rows + "</table></div>")


def bench_section(name: str, rows: list[dict]) -> str:
    if not rows:
        return ""
    cases = list(dict.fromkeys(r["name"] for r in rows))
    body = []
    for c in cases:
        rs = [r for r in rows if r["name"] == c]
        tally = ", ".join(f'<span class="{OUTCOME_CLASS.get(k, "mut")}">{e(k)} x{sum(r["outcome"] == k for r in rs)}</span>'
                          for k in sorted({r["outcome"] for r in rs}))
        body.append(f"<tr><td>{e(c)}</td><td class='mut'>{e(rs[0].get('expect', ''))}</td><td>{tally}</td>"
                    f"<td class='mut'>{round(sum(r.get('tokens', 0) for r in rs) / len(rs))}</td></tr>")
    det = [r for r in rows if r.get("expect") != "undetectable"]
    ok = sum(r["outcome"] == "success" for r in det)
    cfg = rows[0].get("config", "")
    return (f'<h3>{e(name)} <span class="mut">({e(cfg)}, {len(rows)} case-runs, success {ok}/{len(det)})</span></h3>'
            f'<div class="card"><table><tr><th>case</th><th>expected</th><th>outcome</th><th>avg tokens</th></tr>{"".join(body)}</table></div>')


def load_evals(folder: Path) -> dict[str, list[dict]]:
    out = {}
    for f in sorted(Path(folder).glob("*.json")):
        try:
            data = json.loads(f.read_text())
        except ValueError:
            continue
        if isinstance(data, list) and data and isinstance(data[0], dict) and "outcome" in data[0]:
            out[f.stem] = data
    return out


def render(result: dict | None, patch: str, proof_md: str, evals: dict[str, list[dict]]) -> str:
    parts = ["<h1>Upkeep</h1><p class='sub'>A verified, security-aware dependency upgrade agent. The code decides the verdict, not the model.</p>"]
    if result is not None:
        parts.append(run_section(result, patch, proof_md))
    if evals:
        parts.append("<h2>Benchmarks</h2><p class='mut'>Authored cases and authored apps on real libraries, <b>not</b> a public benchmark. "
                     "Read the README for what each one can and cannot show.</p>")
        parts += [bench_section(n, r) for n, r in evals.items()]
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Upkeep</title><style>{CSS}</style></head><body><main>{''.join(parts)}</main></body></html>")


def build(run_dir: Path | None, evals_dir: Path | None, out: Path) -> Path:
    result, patch, proof = None, "", ""
    if run_dir:
        rd = Path(run_dir)
        result = json.loads((rd / "result.json").read_text())
        patch = (rd / "fix.patch").read_text() if (rd / "fix.patch").exists() else ""
        proof = (rd / "PROOF.md").read_text() if (rd / "PROOF.md").exists() else ""
    evals = load_evals(evals_dir) if evals_dir else {}
    out = Path(out)
    out.write_text(render(result, patch, proof, evals), encoding="utf-8")
    return out
