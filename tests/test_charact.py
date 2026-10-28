import json
from pathlib import Path

import pytest

from tests.test_agent import Scripted, tc
from upkeep.agent import Agent
from upkeep.charact import BehaviorChecker, find_targets, propose_inputs, Target
from upkeep.evalcases import _w, materialize
from upkeep.evalcases_behavior import CASES
from upkeep.llm import Reply
from upkeep.radar import run_radar
from upkeep.scan import CallSite
from upkeep.sandbox import run_python, run_tests


class FakeInputs:
    """Stands in for the model that proposes probe inputs."""

    def __init__(self, text):
        self.text, self.calls = text, 0

    def chat(self, messages, **kw):
        self.calls += 1
        return Reply({"role": "assistant", "content": self.text}, {"total_tokens": 7})


def world(case, tmp_path):
    old, new, repo = materialize(case, tmp_path)
    report = run_radar(repo, old, new, "lib")
    sites = [s for _, ss in report.affected for s in ss]
    local = lambda lib: (lambda d, script: run_python(d, script, backend="local", allow_unsandboxed=True, extra_paths=[lib]))  # noqa: E731
    runner = lambda w: run_tests(w, backend="local", allow_unsandboxed=True, extra_paths=[new], with_coverage=False)  # noqa: E731
    return old, new, repo, report, sites, local, runner


def patched(repo, tmp_path, files):
    p = tmp_path / "patched"
    import shutil
    shutil.copytree(repo, p)
    _w(p, files)
    return p


QUOTE, SLUG = CASES


def test_find_targets_locates_the_enclosing_function(tmp_path):
    old, new, repo, report, sites, local, runner = world(QUOTE, tmp_path)
    t = find_targets(repo, sites)
    assert [(x.module, x.name, x.params) for x in t] == [("app", "quote", ["s"])]


def test_propose_inputs_parses_noisy_output_and_checks_arity():
    t = Target("app", "f", "def f(a, b): ...", ["a", "b"])
    noisy = 'Sure! Here you go:\n```json\n[["x", 1], ["y"], ["z", 2, 3], ["q", null]]\n```\nHope that helps.'
    inputs, tokens = propose_inputs(FakeInputs(noisy), t)
    assert inputs == [["x", 1], ["q", None]] and tokens == 7
    assert propose_inputs(FakeInputs("no json at all"), t)[0] == []


def test_behavior_check_catches_the_naive_replacement(tmp_path):
    old, new, repo, report, sites, local, runner = world(QUOTE, tmp_path)
    naive = patched(repo, tmp_path, {"app.py": "from urllib.parse import quote as url_quote\n\n\ndef quote(s):\n    return url_quote(s)\n"})
    assert runner(naive).ok  # visible test is green
    chk = BehaviorChecker(FakeInputs('[["a b"], ["a b/c:d"], [""]]'), repo, sites, local(old), local(new))
    rep = chk.check(naive)
    assert not rep.ok and rep.mismatches[0]["input"] == ["a b/c:d"]
    assert "%3A" in rep.mismatches[0]["new"] and ":" in rep.mismatches[0]["old"]


def test_behavior_check_accepts_a_correct_fix_and_caches_the_old_run(tmp_path):
    old, new, repo, report, sites, local, runner = world(QUOTE, tmp_path)
    good = patched(repo, tmp_path, QUOTE.solution)
    model = FakeInputs('[["a b"], ["a b/c:d"], [""]]')
    chk = BehaviorChecker(model, repo, sites, local(old), local(new))
    assert chk.check(good).ok
    assert chk.check(good).ok and model.calls == chk.probe_samples == 2  # proposed once per sample, then cached


def test_old_version_that_cannot_run_probes_is_skipped_not_trusted(tmp_path):
    old, new, repo, report, sites, local, runner = world(QUOTE, tmp_path)
    chk = BehaviorChecker(FakeInputs('[["a", "b"]]'), repo, sites, local(old), local(new))  # wrong arity -> no inputs
    rep = chk.check(repo)
    assert rep.ok and rep.functions_checked == 0 and "no usable probe inputs" in rep.skipped[0]


def edit(path, a, b, text, i="1"):
    return tc("replace_lines", {"path": path, "start_line": a, "end_line": b, "new_text": text}, i)


NAIVE = [
    # quote: swap the import for urllib's quote under the old name (ignores the old safe-characters default)
    (QUOTE, [edit("app.py", 1, 1, "from urllib.parse import quote as url_quote")]),
    # slug: define the removed helper inline at the import line, forgetting .strip()
    (SLUG, [edit("app.py", 1, 1, "def slug(s):\n    return s.lower().replace(' ', '-')")]),
]


@pytest.mark.parametrize("case,edits", NAIVE, ids=["quote", "slug"])
def test_agent_is_not_verified_when_behavior_differs(tmp_path, case, edits):
    old, new, repo, report, sites, local, runner = world(case, tmp_path)
    probes = '[["a b"], ["a b/c:d"], ["  A B "], [""], ["X"]]'
    model = Scripted([edits, [tc("run_tests", {}, "8")], [tc("finish", {"status": "fixed", "summary": "x"}, "9")]])
    chk = BehaviorChecker(FakeInputs(probes), repo, sites, local(old), local(new))
    res = Agent(model, repo, report, runner, behavior_checker=chk).run()
    assert "rejected" not in " ".join(s["result"] for s in res.steps), "edits must stay inside the allowed lines"
    assert res.status == "declined" and res.reason.startswith("behavior differs from the old version"), res.reason
    assert res.behavior and res.behavior["mismatches"]


def test_agent_can_use_the_tool_to_self_correct(tmp_path):
    old, new, repo, report, sites, local, runner = world(QUOTE, tmp_path)
    model = Scripted([
        [edit("app.py", 1, 1, "from urllib.parse import quote as url_quote")],
        [tc("check_behavior", {}, "2")],
        [edit("app.py", 1, 1, "from urllib.parse import quote as _q", "3"), edit("app.py", 5, 5, "    return _q(s, safe='/:')", "4")],
        [tc("check_behavior", {}, "5")],
        [tc("finish", {"status": "fixed", "summary": "x"}, "6")],
    ])
    chk = BehaviorChecker(FakeInputs('[["a b"], ["a b/c:d"]]'), repo, sites, local(old), local(new))
    res = Agent(model, repo, report, runner, behavior_checker=chk).run()
    results = [s["result"] for s in res.steps if s["tool"] == "check_behavior"]
    assert "DIFFERENCES FOUND" in results[0] and "NO DIFFERENCES FOUND" in results[1], results
    assert res.status == "verified", res.reason
    assert "safe='/:'" in res.diff


def test_nudge_after_behavior_mismatch_lets_the_agent_recover(tmp_path):
    old, new, repo, report, sites, local, runner = world(QUOTE, tmp_path)
    model = Scripted([
        [edit("app.py", 1, 1, "from urllib.parse import quote as url_quote")],
        [tc("check_behavior", {}, "2")],
        [],  # the model stops talking without finishing
        [edit("app.py", 1, 1, "from urllib.parse import quote as _q", "3"), edit("app.py", 5, 5, "    return _q(s, safe='/:')", "4")],
        [tc("check_behavior", {}, "5")],
        [tc("finish", {"status": "fixed", "summary": "x"}, "6")],
    ])
    chk = BehaviorChecker(FakeInputs('[["a b"], ["a b/c:d"]]'), repo, sites, local(old), local(new))
    res = Agent(model, repo, report, runner, behavior_checker=chk).run()
    nudge = [m for m in model.seen[3] if m.get("role") == "user"][-1]["content"]
    assert "changes behavior" in nudge and "a b/c:d" in nudge
    assert res.status == "verified", res.reason


def test_nudge_when_no_edit_then_honest_decline(tmp_path):
    from tests.test_agent import FIX  # noqa: F401
    old, new, repo, report, sites, local, runner = world(QUOTE, tmp_path)
    model = Scripted([[], [], [], [tc("finish", {"status": "decline", "summary": "x"})]])
    agent = Agent(model, repo, report, runner)
    res = agent.run()
    assert res.status == "declined" and res.reason == "no edit was made"
    assert sum(1 for m in model.seen[-1] if m.get("role") == "user") == 3  # first message + 2 nudges, then it gave up


def test_finish_is_rejected_while_behavior_differs_then_accepted_after_a_fix(tmp_path):
    old, new, repo, report, sites, local, runner = world(QUOTE, tmp_path)
    model = Scripted([
        [edit("app.py", 1, 1, "from urllib.parse import quote as url_quote")],
        [tc("finish", {"status": "fixed", "summary": "done"}, "2")],        # rejected: behavior differs
        [edit("app.py", 1, 1, "from urllib.parse import quote as _q", "3"), edit("app.py", 5, 5, "    return _q(s, safe='/:')", "4")],
        [tc("finish", {"status": "fixed", "summary": "done"}, "5")],        # accepted
    ])
    chk = BehaviorChecker(FakeInputs('[["a b"], ["a b/c:d"]]'), repo, sites, local(old), local(new))
    res = Agent(model, repo, report, runner, behavior_checker=chk).run()
    finishes = [s["result"] for s in res.steps if s["tool"] == "finish"]
    assert finishes[0].startswith("finish REJECTED") and finishes[1] == "noted"
    assert res.status == "verified", res.reason


def test_finish_rejection_budget_ends_in_an_honest_decline(tmp_path):
    old, new, repo, report, sites, local, runner = world(QUOTE, tmp_path)
    naive = edit("app.py", 1, 1, "from urllib.parse import quote as url_quote")
    model = Scripted([[naive]] + [[tc("finish", {"status": "fixed", "summary": "done"}, str(i))] for i in range(2, 6)])
    chk = BehaviorChecker(FakeInputs('[["a b/c:d"]]'), repo, sites, local(old), local(new))
    res = Agent(model, repo, report, runner, behavior_checker=chk, max_finish_rejections=2).run()
    assert [s["result"][:15] for s in res.steps if s["tool"] == "finish"].count("finish REJECTED") == 2
    assert res.status == "declined" and res.reason.startswith("behavior differs")


def test_sites_exercised_by_probes_count_toward_verified_but_are_stated(tmp_path):
    from upkeep.proof import ProofBundle
    from upkeep.sandbox import TestResult
    old, new, repo, report, sites, local, runner = world(QUOTE, tmp_path)
    report.uncovered = [s for s in sites if s.kind == "call"]  # no test runs the call; the import line runs on any import
    report.coverage_known = True
    ok = TestResult(True, 0, "", "docker")
    good = patched(repo, tmp_path, QUOTE.solution)
    chk = BehaviorChecker(FakeInputs('[["a b"], ["a b/c:d"]]'), repo, sites, local(old), local(new))
    rep = chk.check(good)
    assert rep.ok and rep.probed_sites and all(isinstance(x, list) for x in rep.probed_sites)
    b = ProofBundle("lib", "1", "2", report, ok, ok, behavior=rep.to_dict())
    assert b.status() == "verified"
    assert "exercised by behavior probes" in b.to_markdown() and "NOT covered" not in b.to_markdown()
    # without probes the same situation is NOT verified
    assert ProofBundle("lib", "1", "2", report, ok, ok).status() == "tests-pass-but-unverified-sites"
    # and a mismatch always means declined
    bad = {"probed_sites": rep.probed_sites, "mismatches": [{"function": "quote", "input": [], "old": "a", "new": "b"}]}
    assert ProofBundle("lib", "1", "2", report, ok, ok, behavior=bad).status() == "declined"


def test_expansion_keeps_the_models_inputs_first_and_adds_edge_cases():
    from upkeep.charact import expand_inputs
    out = expand_inputs([["a b"], ["x", 3]])
    assert out[:2] == [["a b"], ["x", 3]]                         # the model's own probes come first
    assert ["a/b:c"] in out and [""] in out and ["x", 0] in out    # strings and ints are varied one argument at a time
    assert len(out) <= 28 and len({json.dumps(o) for o in out}) == len(out)
    assert expand_inputs([]) == [] and expand_inputs([[None]]) == [[None]]


def test_a_happy_path_only_model_still_cannot_get_a_wrong_fix_through(tmp_path):
    """The model proposes ONLY 'a b' (it never thinks of ':'). Expansion supplies it, so the naive fix is still caught."""
    old, new, repo, report, sites, local, runner = world(QUOTE, tmp_path)
    naive = patched(repo, tmp_path, {"app.py": "from urllib.parse import quote as url_quote\n\n\ndef quote(s):\n    return url_quote(s)\n"})
    rep = BehaviorChecker(FakeInputs('[["a b"]]'), repo, sites, local(old), local(new)).check(naive)
    assert not rep.ok and any(":" in json.dumps(m["input"]) for m in rep.mismatches)


def test_correct_fix_is_not_blocked_by_the_extra_probes(tmp_path):
    old, new, repo, report, sites, local, runner = world(QUOTE, tmp_path)
    good = patched(repo, tmp_path, QUOTE.solution)
    rep = BehaviorChecker(FakeInputs('[["a b"]]'), repo, sites, local(old), local(new)).check(good)
    assert rep.ok and rep.inputs_checked > 10


def test_nondeterministic_probes_are_ignored_not_reported_as_differences(tmp_path):
    from upkeep.evalcases import Case
    case = Case("flaky", "x", "fix", {"lib/__init__.py": "import random\n\ndef roll(s):\n    return s + str(random.random())\n"},
                {"lib/__init__.py": ""}, {"app.py": "from lib import roll\n\n\ndef go(s):\n    return roll(s)\n",
                                          "test_app.py": "def test_x():\n    assert True\n"}, {}, {})
    old, new, repo, report, sites, local, runner = world(case, tmp_path)
    fixed = patched(repo, tmp_path, {"app.py": "import random\n\n\ndef go(s):\n    return s + str(random.random())\n"})
    rep = BehaviorChecker(FakeInputs('[["a"]]'), repo, sites, local(old), local(new)).check(fixed)
    assert rep.ok and any("non-deterministic" in s for s in rep.skipped)


def test_two_probe_samples_are_merged_without_duplicates_and_cost_two_calls(tmp_path):
    old, new, repo, report, sites, local, runner = world(QUOTE, tmp_path)

    class Varied:
        def __init__(self):
            self.replies = ['[["a b"], ["x"]]', '[["x"], ["a b/c:d"]]']  # the second sample thinks of the colon
            self.calls = 0

        def chat(self, messages, **kw):
            text = self.replies[min(self.calls, 1)]
            self.calls += 1
            return Reply({"role": "assistant", "content": text}, {"total_tokens": 3})

    m = Varied()
    chk = BehaviorChecker(m, repo, sites, local(old), local(new), probe_samples=2)
    naive = patched(repo, tmp_path, {"app.py": "from urllib.parse import quote as url_quote\n\n\ndef quote(s):\n    return url_quote(s)\n"})
    assert not chk.check(naive).ok and m.calls == 2
    assert [a for a in chk._inputs[("app", "quote")] if a == ["x"]] == [["x"]]  # "x" appeared in both samples, kept once
    one = BehaviorChecker(Varied(), repo, sites, local(old), local(new), probe_samples=1)
    assert one.probe_samples == 1 and BehaviorChecker(Varied(), repo, sites, local(old), local(new), probe_samples=0).probe_samples == 1


def test_random_inputs_are_seeded_typed_and_deterministic():
    from upkeep.charact import random_inputs
    base = [["a b/c", 3, ["x"], {"k": "v"}, True]]
    a = random_inputs(base, 60, "mod.fn")
    assert a == random_inputs(base, 60, "mod.fn")          # reproducible
    assert a != random_inputs(base, 60, "mod.other")       # seed depends on the function
    assert len(a) == 60 and len({json.dumps(x) for x in a}) == 60
    for args in a:                                          # arity and JSON types follow the original probe
        assert len(args) == 5
        assert isinstance(args[0], str) and isinstance(args[1], int) and isinstance(args[2], list)
        assert isinstance(args[3], dict) and isinstance(args[4], bool)
        json.dumps(args)
    assert random_inputs(base, 0, "x") == [] and random_inputs([], 10, "x") == []


def test_random_inputs_catch_a_change_the_model_never_proposed(tmp_path):
    """The new library changed behavior only for the character '~'. The model proposed plain words and the fixed edge
    cases contain no '~', so only the seeded random inputs can find the difference."""
    repo, old, new = tmp_path / "repo", tmp_path / "old", tmp_path / "new"
    for d in (repo, old, new):
        d.mkdir()
    (old / "lib.py").write_text("def enc(s):\n    return s\n")
    (new / "lib.py").write_text("def enc(s):\n    return s.replace('~', '%7E')\n")
    (repo / "app.py").write_text("import lib\n\n\ndef go(s):\n    return lib.enc(s)\n")
    sites = [CallSite("app.py", 5, 5, "lib.enc", "call", 1, (), False)]
    local = lambda lib: (lambda d, script: run_python(d, script, backend="local", allow_unsandboxed=True, extra_paths=[lib]))  # noqa: E731
    plain = BehaviorChecker(FakeInputs('[["abc"], ["def"]]'), repo, sites, local(old), local(new))
    fuzzed = BehaviorChecker(FakeInputs('[["abc"], ["def"]]'), repo, sites, local(old), local(new), n_random=300)
    p = plain.check(repo)
    assert p.ok                                              # the fixed probe set is blind to this change
    rep = fuzzed.check(repo)
    assert not rep.ok and rep.inputs_checked > p.inputs_checked and rep.random_inputs > 0
    for m in rep.mismatches:                                 # every difference names the call site
        assert m["file"] == "app.py" and m["sites"][0]["line"] == 5


def test_mismatch_names_the_call_site_in_summary_and_proof(tmp_path):
    from upkeep.charact import BehaviorReport, where
    m = {"function": "go", "input": ["a b"], "old": '{"ok": "1"}', "new": '{"ok": "2"}', "file": "app/routing.py",
         "sites": [{"line": 42, "symbol": "utils.url_quote"}]}
    assert where(m) == "app/routing.py:42 (utils.url_quote) "
    assert where({"function": "go"}) == ""
    assert "app/routing.py:42 (utils.url_quote) go(" in BehaviorReport(mismatches=[m]).summary()
