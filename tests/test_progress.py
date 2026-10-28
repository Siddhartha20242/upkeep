import io

from upkeep.progress import format_step, paint, run_with_progress


def test_step_lines_are_readable_and_plain_without_color():
    f = lambda **kw: format_step(kw, color=False)  # noqa: E731
    assert f(tool="replace_lines", args={"path": "a.py", "start_line": 5, "end_line": 5}, result="ok: edit applied").strip() == "edit      a.py:5-5"
    assert "REJECTED" in f(tool="replace_lines", args={}, result="rejected by content check: unsafe deserialization yaml.Loader")
    assert "yaml.Loader" in f(tool="replace_lines", args={}, result="rejected by content check: unsafe deserialization yaml.Loader")
    assert "tests     tests PASSED" in f(tool="run_tests", args={}, result="tests PASSED: 1 passed\n<<<UNTRUSTED_CONTENT")
    assert "DIFFERENCES FOUND" in f(tool="check_behavior", args={}, result="DIFFERENCES FOUND: your edit changed behavior\nmore")
    assert "finish REJECTED" in f(tool="finish", args={"status": "fixed"}, result="finish REJECTED: x")
    assert "release notes about 'Markup'" in f(tool="read_release_notes", args={"query": "Markup"}, result="...")
    assert "\033" not in f(tool="read_file", args={"path": "a.py", "start_line": 1, "end_line": 9}, result="1: x")


def test_color_codes_only_when_enabled():
    assert paint("x", "red", False) == "x" and paint("x", "red", True).startswith("\033[31m")


class FakeAgent:
    def __init__(self):
        self.steps = []

    def run(self):
        import time
        for i in range(3):
            self.steps.append({"tool": "read_file", "args": {"path": f"f{i}.py", "start_line": 1, "end_line": 2}, "result": "x"})
            time.sleep(0.05)
        return "RESULT"


def test_progress_streams_every_step_in_order_and_returns_the_result():
    out = io.StringIO()
    assert run_with_progress(FakeAgent(), out=out, interval=0.01) == "RESULT"
    assert [l.split()[-1] for l in out.getvalue().splitlines()] == ["f0.py:1-2", "f1.py:1-2", "f2.py:1-2"]


def test_exceptions_in_the_agent_are_not_swallowed():
    class Boom(FakeAgent):
        def run(self):
            raise RuntimeError("boom")
    import pytest
    with pytest.raises(RuntimeError, match="boom"):
        run_with_progress(Boom(), out=io.StringIO(), interval=0.01)
