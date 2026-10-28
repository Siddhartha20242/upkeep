from upkeep.radar import run_radar

OLD = "class Client:\n    def __init__(self, host):\n        pass\n    def get(self, key, default=None):\n        pass\n    def put(self, key, value):\n        pass\n"
NEW = "class Client:\n    def __init__(self, host):\n        pass\n    def get(self, key, *, default):\n        pass\n    def put(self, key, value):\n        pass\n"

# (source line, is_it_flagged)
CASES = [
    ("c = lib.Client('h')", False),
    ("c.get('k', 1)", True),                       # module-level variable
    ("def f():", False),
    ("    return c.get('k', 2)", True),             # module variable used inside a function
    ("def g():", False),
    ("    x = lib.Client('h')", False),
    ("    x.get('k', default=3)", False),           # already compliant (keyword)
    ("    return x.get('k', 4)", True),             # local variable
    ("def w():", False),
    ("    with lib.Client('h') as y:", False),
    ("        y.get('k', 5)", True),                # context manager
    ("class A:", False),
    ("    def __init__(self):", False),
    ("        self.cli = lib.Client('h')", False),
    ("    def m(self):", False),
    ("        return self.cli.get('k', 6)", True),  # attribute assigned in the same class
    ("def h(cl: lib.Client):", False),
    ("    return cl.get('k', 7)", True),            # annotated parameter
    ("def amb():", False),
    ("    z = lib.Client('h')", False),
    ("    z = something_else()", False),
    ("    return z.get('k', 8)", False),            # reassigned: unknown, deliberately NOT flagged
    ("def other(q):", False),
    ("    return q.get('k', 9)", False),            # untyped parameter: unknown
    ("def unchanged():", False),
    ("    k = lib.Client('h'); return k.put('a', 'b')", False),  # method that did not change
]


def test_instance_calls_are_resolved_for_common_local_patterns_and_unknowns_stay_unflagged(tmp_path):
    old, new, repo = tmp_path / "old", tmp_path / "new", tmp_path / "repo"
    for root, text in ((old, OLD), (new, NEW)):
        (root / "lib").mkdir(parents=True)
        (root / "lib" / "__init__.py").write_text(text)
    repo.mkdir()
    (repo / "app.py").write_text("import lib\n" + "\n".join(s for s, _ in CASES) + "\n")
    report = run_radar(repo, old, new, "lib")
    flagged = {s.line for _, sites in report.affected for s in sites}
    expected = {i + 2 for i, (_, hit) in enumerate(CASES) if hit}
    wrong = sorted((CASES[ln - 2][0].strip(), "flagged" if ln in flagged else "missed") for ln in flagged ^ expected)
    assert not wrong, wrong
    assert {c.symbol for c, _ in report.affected} == {"lib.Client.get"}


def test_module_level_behavior_is_unchanged_when_no_classes_exist(tmp_path):
    from upkeep.scan import find_call_sites
    (tmp_path / "a.py").write_text("import lib\nc = lib.Client()\nc.get(1)\nlib.fn(2)\n")
    assert [s.symbol for s in find_call_sites(tmp_path, "lib", {"lib.fn", "lib.Client.get"})] == ["lib.fn"]  # no `classes`: no instance typing
