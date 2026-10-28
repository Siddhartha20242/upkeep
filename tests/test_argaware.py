from pathlib import Path

from upkeep.radar import run_radar

OLD = """def load(stream, Loader=None):\n    pass\n
def kw(url, timeout=10):\n    pass\n
def rm(a, b, ssl=True):\n    pass\n
def reorder(w, h):\n    pass\n
def dflt(x, retries=3):\n    pass\n
def newreq(a):\n    pass\n
def pos(a, b):\n    pass\n"""
NEW = """def load(stream, Loader):\n    pass\n
def kw(url, *, timeout):\n    pass\n
def rm(a, b):\n    pass\n
def reorder(h, w):\n    pass\n
def dflt(x, retries=5):\n    pass\n
def newreq(a, *, region):\n    pass\n
def pos(a, b, /):\n    pass\n"""

# (call, is_it_affected)
CALLS = [
    ("lib.load(x)", True), ("lib.load(x, Loader=L)", False), ("lib.load(x, L)", False), ("lib.load(*args)", True),
    ("lib.load(x, **opts)", True),
    ("lib.kw('u', 5)", True), ("lib.kw('u', timeout=5)", False),
    ("lib.rm(1, 2, ssl=True)", True), ("lib.rm(1, 2, True)", True), ("lib.rm(1, 2)", False),
    ("lib.reorder(1, 2)", True), ("lib.reorder(h=1, w=2)", False), ("lib.reorder(1)", False),
    ("lib.dflt(1)", True), ("lib.dflt(1, retries=3)", False), ("lib.dflt(1, 3)", False),
    ("lib.newreq(1)", True), ("lib.newreq(1, region='x')", False),
    ("lib.pos(1, b=2)", True), ("lib.pos(1, 2)", False),
]


def test_each_call_is_flagged_only_if_it_trips_the_change(tmp_path):
    old, new, repo = tmp_path / "old", tmp_path / "new", tmp_path / "repo"
    for root, text in ((old, OLD), (new, NEW)):
        (root / "lib").mkdir(parents=True)
        (root / "lib" / "__init__.py").write_text(text)
    repo.mkdir()
    (repo / "app.py").write_text("import lib\n" + "".join(f"{c}\n" for c, _ in CALLS))
    report = run_radar(repo, old, new, "lib")
    flagged = {s.line for _, sites in report.affected for s in sites}
    expected = {i + 2 for i, (_, hit) in enumerate(CALLS) if hit}
    wrong = sorted((CALLS[ln - 2][0], "flagged" if ln in flagged else "missed") for ln in flagged ^ expected)
    assert not wrong, wrong


def test_default_change_is_reported_with_the_old_and_new_value(tmp_path):
    from upkeep.apidiff import diff_api, extract_api
    old, new = tmp_path / "old", tmp_path / "new"
    for root, text in ((old, OLD), (new, NEW)):
        (root / "lib").mkdir(parents=True)
        (root / "lib" / "__init__.py").write_text(text)
    changes = [c for c in diff_api(extract_api(old, "lib"), extract_api(new, "lib")) if c.kind == "default_changed"]
    assert [(c.symbol, c.param, c.detail) for c in changes] == [("lib.dflt", "retries", "'retries' default 3 -> 5")]
    assert not changes[0].breaking


def test_non_literal_defaults_are_not_compared(tmp_path):
    from upkeep.apidiff import diff_api, extract_api
    old, new = tmp_path / "old", tmp_path / "new"
    (old / "lib").mkdir(parents=True); (new / "lib").mkdir(parents=True)
    (old / "lib" / "__init__.py").write_text("import os\n\ndef f(a, p=os.sep):\n    pass\n")
    (new / "lib" / "__init__.py").write_text("import os\n\ndef f(a, p=os.pathsep):\n    pass\n")
    assert diff_api(extract_api(old, "lib"), extract_api(new, "lib")) == []  # cannot know the values: stay quiet
