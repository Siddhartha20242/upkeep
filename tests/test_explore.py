"""compare_old_new: the agent can try a snippet on the old and the new library, like a person at a REPL."""

import json

from tests.test_agent import FIX, Scripted, tc
from tests.test_library_tools import _agent_with
from upkeep.agent import Agent, EXPLORE_TOOL


def explorer_for(calls):
    def run(code):
        calls.append(code)
        return "'a%20b'", "'a+b'"
    return run


def names(a):
    return {t["function"]["name"] for t in a.tools}


def test_the_tool_exists_only_when_an_explorer_is_given(tmp_path):
    assert "compare_old_new" in names(_agent_with(Scripted([]), tmp_path, explorer=explorer_for([])))
    assert "compare_old_new" not in names(_agent_with(Scripted([]), tmp_path / "b"))
    assert EXPLORE_TOOL["function"]["name"] == "compare_old_new"


def test_it_returns_both_outputs_as_untrusted_data(tmp_path):
    calls = []
    a = _agent_with(Scripted([]), tmp_path, explorer=explorer_for(calls))
    out = a.t_compare_old_new("print(repr(quote('a b')))")
    assert calls == ["print(repr(quote('a b')))"]
    assert "OLD library:\n'a%20b'" in out and "NEW library:\n'a+b'" in out and "UNTRUSTED" in out and "library behavior" in out


def test_secret_like_text_in_the_output_is_redacted_before_the_model_sees_it(tmp_path):
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    a = _agent_with(Scripted([]), tmp_path, explorer=lambda code: (f"token={token}", "ok"))
    out = a.t_compare_old_new("print(1)")
    assert token not in out and "[REDACTED:" in out and a.redactions >= 1


def test_bad_snippets_and_the_budget_are_handled_without_crashing(tmp_path):
    calls = []
    a = _agent_with(Scripted([]), tmp_path, explorer=explorer_for(calls), max_explorations=2)
    assert a.t_compare_old_new("").startswith("error") and a.t_compare_old_new(None).startswith("error")
    assert a.t_compare_old_new(12).startswith("error") and a.t_compare_old_new("x" * 2001).startswith("error")
    assert calls == []                                                       # none of those reached the sandbox
    a.t_compare_old_new("print(1)")
    a.t_compare_old_new("print(2)")
    over = a.t_compare_old_new("print(3)")
    assert "budget used (2)" in over and "replace_lines" in over and len(calls) == 2


def test_the_first_message_tells_the_agent_to_look_before_it_edits(tmp_path):
    a = _agent_with(Scripted([]), tmp_path, explorer=explorer_for([]))
    msg = a._first_message()
    assert "compare_old_new" in msg and "8 uses" in msg


def test_the_agent_can_use_the_tool_in_a_normal_run(tmp_path):
    calls = []
    model = Scripted([[tc("compare_old_new", {"code": "print(1)"})], [FIX], [tc("finish", {"status": "fixed", "summary": "x"})]])
    res = _agent_with(model, tmp_path, explorer=explorer_for(calls)).run()
    assert res.status == "verified" and calls == ["print(1)"] and res.steps[0]["tool"] == "compare_old_new"


def test_max_turns_flag_limits_the_agent(tmp_path):
    from tests.test_cli_fix import args, build
    from upkeep import cli
    old, new, repo = build(tmp_path)
    never_finishes = Scripted([[tc("list_affected", {})]] * 10)
    code = cli.main(args(old, new, repo, tmp_path / "o", "--max-turns", "2"), client=never_finishes)
    assert code == 1 and len(never_finishes.seen) <= 3          # it stopped after the turns it was given, and declined
