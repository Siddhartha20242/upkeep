"""The behavior check measures how much of each function its probes ran, aims a second round at the rest, and understands
keyword arguments and a few non-JSON value types."""

import json
from pathlib import Path

import pytest

from tests.test_agent import write
from upkeep.charact import (BehaviorChecker, Target, expand_inputs, format_call_args, probe_script, propose_guided, propose_inputs,
                            valid_input, _parse, _parse_cov)
from upkeep.llm import Reply
from upkeep.radar import run_radar
from upkeep.sandbox import run_python


class Sequenced:
    """A model that answers each call with the next canned reply, and remembers every prompt."""

    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def chat(self, messages, **kw):
        self.prompts.append(messages[-1]["content"])
        text = self.replies.pop(0) if self.replies else "[]"
        return Reply({"role": "assistant", "content": text}, {"total_tokens": 5})


def local(lib):
    return lambda d, script: run_python(d, script, backend="local", allow_unsandboxed=True, extra_paths=[lib])


def libs(tmp_path, old_src, new_src, app_src):
    old, new, repo = tmp_path / "old", tmp_path / "new", tmp_path / "repo"
    write(old, "lib/__init__.py", old_src)
    write(new, "lib/__init__.py", new_src)
    write(repo, "app.py", app_src)
    report = run_radar(repo, old, new, "lib")
    return old, new, repo, [s for _, ss in report.affected for s in ss]


# The app function has a branch that only a very specific value reaches (the built-in edge cases cannot guess it).
OLD = "def scale(x, factor=2):\n    return x * factor\n"
NEW = "def scale(x, *, factor=2):\n    return x * factor\n"
APP = ("import lib\n\n\ndef compute(x):\n    if x == 424242:\n        return lib.scale(x, 3)\n    return lib.scale(x)\n")
WRONG_FIX = ("import lib\n\n\ndef compute(x):\n    if x == 424242:\n        return lib.scale(x, factor=4)\n    return lib.scale(x)\n")   # 4, not 3


def checker(tmp_path, model):
    old, new, repo, sites = libs(tmp_path, OLD, NEW, APP)
    import shutil
    fixed = tmp_path / "patched"
    shutil.copytree(repo, fixed)
    (fixed / "app.py").write_text(WRONG_FIX)
    return BehaviorChecker(model, repo, sites, local(old), local(new), probe_samples=1), fixed


def test_probes_that_only_cover_one_branch_are_reported_as_partial_coverage(tmp_path):
    model = Sequenced("[[1], [2], [5]]", "[]")                    # only positive numbers; the guided round finds nothing
    chk, fixed = checker(tmp_path, model)
    rep = chk.check(fixed)
    c = rep.coverage["compute"]
    assert c["executed"] < c["total"] and c["missed"] and rep.ok       # the wrong fix is NOT caught: the branch never ran
    assert "not executed" in rep.summary()


def test_a_second_round_aimed_at_the_missed_lines_catches_the_branch_specific_bug(tmp_path):
    model = Sequenced("[[1], [2], [5]]", "[[424242], [7]]")          # round two reaches the special branch
    chk, fixed = checker(tmp_path, model)
    rep = chk.check(fixed)
    assert not rep.ok and rep.mismatches[0]["input"] == [424242]
    assert any("1272726" in m["old"] and "1696968" in m["new"] for m in rep.mismatches)
    c = rep.coverage["compute"]
    assert c["executed"] == c["total"] and c["guided_inputs"] == 2
    assert ">>" in model.prompts[1] and "lib.scale(x, factor=4)" in model.prompts[1]      # the prompt marks the unexecuted lines
    assert model.prompts[1].count("\n>>") >= 1


def test_the_guided_round_runs_once_per_function_not_on_every_check(tmp_path):
    model = Sequenced("[[1], [2]]", "[[424242]]")
    chk, fixed = checker(tmp_path, model)
    chk.check(fixed)
    asked = len(model.prompts)
    chk.check(fixed)
    chk.check(fixed)
    assert len(model.prompts) == asked == 2


def test_when_the_probes_already_cover_everything_no_second_round_is_asked(tmp_path):
    model = Sequenced("[[1], [424242]]")
    chk, fixed = checker(tmp_path, model)
    rep = chk.check(fixed)
    assert rep.coverage["compute"]["missed"] == [] and len(model.prompts) == 1


def test_the_proof_reports_coverage_and_the_second_round(tmp_path):
    from upkeep.proof import ProofBundle
    from upkeep.sandbox import TestResult
    model = Sequenced("[[1], [2]]", "[[424242]]")
    chk, fixed = checker(tmp_path, model)
    rep = chk.check(fixed)
    old, new, repo, sites = libs(tmp_path / "x", OLD, NEW, APP)
    report = run_radar(repo, old, new, "lib")
    md = ProofBundle("lib", "1", "2", report, tests_after=TestResult(True, 0, "", "local"), behavior=rep.to_dict()).to_markdown()
    assert "probes executed" in md and "lines of `compute()`" in md and "second round of 1 inputs" in md


# ---------------------------------------------------------------- inputs: keyword arguments and richer values
T = Target("m", "f", "def f(a, b=1, *, mode='x'): ...", ["a", "b"], required=1, kwonly=("mode",))


@pytest.mark.parametrize("item,ok", [
    ([1], True), ([1, 2], True), ([], False), ([1, 2, 3], False),
    ({"args": [1], "kwargs": {"mode": "y"}}, True), ({"args": [], "kwargs": {"a": 1, "mode": "y"}}, True),
    ({"args": [1], "kwargs": {"nope": 1}}, False), ({"args": [1, 2, 3]}, False), ({"kwargs": {"b": 2}}, False),
    ({"args": [1], "kwargs": {"a": 2}}, False), ({"args": "x"}, False), ({"other": 1}, False), ("text", False), (5, False),
])
def test_valid_input_follows_the_signature(item, ok):
    assert valid_input(T, item) is ok


def test_unknown_keywords_are_fine_when_the_function_takes_kwargs():
    t = Target("m", "f", "def f(a, **kw): ...", ["a"], required=1, varkw=True)
    assert valid_input(t, {"args": [1], "kwargs": {"anything": 2}})


def test_a_function_with_defaults_accepts_shorter_calls():
    t = Target("m", "f", "def f(a, b=1): ...", ["a", "b"], required=1)
    got, _ = propose_inputs(Sequenced('[["x"], ["x", 2], [], ["x", 2, 3]]'), t)
    assert got == [["x"], ["x", 2]]


def test_expand_inputs_varies_keyword_calls_one_value_at_a_time():
    base = {"args": ["a b"], "kwargs": {"mode": "x"}}
    out = expand_inputs([base])
    assert base in out and any(isinstance(o, dict) and o["kwargs"]["mode"] != "x" and o["args"] == ["a b"] for o in out)
    assert any(isinstance(o, dict) and o["args"] != ["a b"] and o["kwargs"] == {"mode": "x"} for o in out)


def test_format_call_args_reads_naturally():
    assert format_call_args(["a", 1]) == '"a", 1'
    assert format_call_args({"args": ["a"], "kwargs": {"mode": "y"}}) == '"a", mode="y"'


def test_keyword_calls_and_tagged_values_run_and_are_decoded_safely(tmp_path):
    src = ("def f(a, *, mode='x'):\n    return (type(a).__name__, mode)\n\n\n"
           "def g(v):\n    return repr(v)\n")
    write(tmp_path / "repo", "app.py", src)
    t = Target("app", "f", src, ["a"], required=1, kwonly=("mode",))
    inputs = [[1], {"args": [{"__tuple__": [1, 2]}], "kwargs": {"mode": "y"}}, [{"__set__": [3]}], [{"__bytes__": "hi"}],
              [{"__float__": "nan"}], [{"__tuple__": [{"__set__": [1]}]}]]
    ok, out = run_python(tmp_path / "repo", probe_script(t, inputs), backend="local", allow_unsandboxed=True)
    assert ok
    res = _parse(out)
    assert [r["ok"] for r in res] == ["('int', 'x')", "('tuple', 'y')", "('set', 'x')", "('bytes', 'x')", "('float', 'x')", "('tuple', 'x')"]


def test_a_tagged_object_cannot_run_code_it_is_only_ever_data(tmp_path):
    src = "def g(v):\n    return repr(v)\n"
    write(tmp_path / "repo", "app.py", src)
    t = Target("app", "g", src, ["v"])
    evil = [[{"__bytes__": "__import__('os').system('echo pwned > pwned.txt')"}], [{"__float__": "__import__('os')"}],
            [{"__tuple__": "__import__('os')"}]]
    ok, out = run_python(tmp_path / "repo", probe_script(t, evil), backend="local", allow_unsandboxed=True)
    res = _parse(out)
    assert "b\"__import__(" in res[0]["ok"]                                                          # kept as bytes text, never evaluated
    assert not (tmp_path / "repo" / "pwned.txt").exists() and all("ok" in r or "err" in r for r in res)


def test_coverage_output_is_parsed_and_ignored_when_malformed():
    assert _parse_cov('x\n@@UPKEEP_COVERAGE@@{"executed": [1], "total": [1, 2]}\n') == {"executed": [1], "total": [1, 2]}
    assert _parse_cov("@@UPKEEP_COVERAGE@@not json") is None and _parse_cov("nothing") is None


def test_keyword_probes_catch_a_difference_that_positional_ones_cannot_express(tmp_path):
    import shutil
    old = "def render(text, *, mode='a', strict=False):\n    return text.upper() if mode == 'a' else text\n"
    new = "def render(text, *, mode='a'):\n    return text.upper()\n"                  # `strict` is gone and mode='b' is no longer respected
    app = "import lib\n\n\ndef show(text, *, mode='a'):\n    return lib.render(text, mode=mode, strict=True)\n"
    o, n, repo, sites = libs(tmp_path, old, new, app)
    fixed = tmp_path / "patched"
    shutil.copytree(repo, fixed)
    (fixed / "app.py").write_text("import lib\n\n\ndef show(text, *, mode='a'):\n    return lib.render(text, mode=mode)\n")
    model = Sequenced('[["x"], {"args": ["x"], "kwargs": {"mode": "b"}}]')
    rep = BehaviorChecker(model, repo, sites, local(o), local(n), probe_samples=1).check(fixed)
    # `mode` is keyword-only, so no positional list can set it: the difference is found only through the keyword call
    assert any(isinstance(m["input"], dict) and m["input"]["kwargs"] == {"mode": "b"} for m in rep.mismatches)
    assert "mode=\"b\"" in rep.summary()
