import json
from pathlib import Path

from upkeep import cli
from upkeep.dashboard import build, md_to_html, render

EVIL = "<script>alert('x')</script>"


def test_untrusted_text_is_escaped_everywhere():
    result = {"status": "declined", "reason": EVIL, "steps": [{"tool": EVIL, "result": EVIL}], "injection_flags": [EVIL]}
    evals = {"b": [{"name": EVIL, "expect": EVIL, "outcome": EVIL, "config": EVIL, "tokens": 5}]}
    page = render(result, f"+{EVIL}\n-{EVIL}\n", f"# {EVIL}\n- {EVIL}\n", evals)
    assert "<script>" not in page and "&lt;script&gt;" in page


def test_markdown_conversion_is_minimal_and_safe():
    h = md_to_html("# T\n\n**Status:** `verified`\n\n## Sec\n- one `x`\n  - nested\n")
    assert "<h2>T</h2>" in h and "<b>Status:</b>" in h and "<code>verified</code>" in h and h.count("<li>") == 2


def test_build_from_a_real_fix_run_and_missing_files(tmp_path, capsys):
    run = tmp_path / "run"
    run.mkdir()
    (run / "result.json").write_text(json.dumps({"status": "verified", "reason": "ok", "model_calls": 4, "tokens": 900,
                                                  "guard_rejections": 0, "injection_flags": [], "steps": [{"tool": "run_tests", "result": "tests PASSED"}]}))
    (run / "PROOF.md").write_text("# Upkeep proof\n\n**Status:** `tests-pass-but-unverified-sites`  |  **Radar risk:** `red`\n")
    ev = tmp_path / "ev"
    ev.mkdir()
    (ev / "synth.json").write_text(json.dumps([{"name": "kwonly", "expect": "fix", "outcome": "success", "config": "full", "tokens": 100}]))
    (ev / "notes.json").write_text(json.dumps({"not": "a results list"}))
    out = build(run, ev, tmp_path / "d.html")
    page = out.read_text()
    assert "tests-pass-but-unverified-sites" in page and 'class="badge warn"' in page  # honest status, not "verified"
    assert "kwonly" in page and "notes" not in page and "No patch" in page
    assert cli.main(["dashboard", "--evals", str(ev), "--out", str(tmp_path / "e.html")]) == 0
    assert "Benchmarks" in (tmp_path / "e.html").read_text()


def test_step_trace_is_readable_not_raw():
    from upkeep.dashboard import _step_text
    assert _step_text({"tool": "list_affected", "result": '[{"file": "a.py"}, {"file": "b.py"}]'}) == "2 affected site(s) reported"
    out = _step_text({"tool": "run_tests", "result": "tests PASSED: 1 passed\n<<<UNTRUSTED_CONTENT source='x'>>>\nbody"})
    assert "UNTRUSTED" not in out and "tests PASSED" in out
