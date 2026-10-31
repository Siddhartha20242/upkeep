from upkeep import audit as audit_mod
from upkeep.notes import mentions, used_names

NOTES = """2.0.0
- `safe_load` no longer accepts a bytes stream with a BOM.
- The default timeout of `fetch` is now 10 seconds instead of none.
- unrelated change to `other_thing` and the docs
- fetch
"""


def test_names_come_from_imports_and_attribute_calls(tmp_path):
    (tmp_path / "a.py").write_text("import lib as L\nfrom lib import safe_load, fetch as get\nL.timeout_default()\nos.path.join()\n")
    (tmp_path / "b.py").write_text("import other\nother.safe_dump()\n")
    assert used_names(tmp_path, "lib") == {"safe_load", "fetch", "timeout_default"}


def test_short_and_dunder_names_are_ignored(tmp_path):
    (tmp_path / "a.py").write_text("import lib\nlib.dump()\nlib.__version__\nlib.ab()\n")
    assert used_names(tmp_path, "lib") == {"dump"}


def test_note_lines_naming_used_symbols_are_listed_and_headings_are_not():
    got = mentions(NOTES, {"safe_load", "fetch"})
    assert [g[0] for g in got] == ["safe_load", "fetch"] and "BOM" in got[0][1] and "10 seconds" in got[1][1]
    assert not any(line.strip() == "fetch" for _, line in got)   # the bare "- fetch" heading line adds nothing


def test_nothing_used_means_nothing_listed_and_matches_are_capped():
    assert mentions(NOTES, set()) == []
    assert len(mentions("\n".join(f"- `work` changed in thing {i} somewhere" for i in range(30)), {"work"})) == 8


def test_a_notes_failure_never_breaks_the_audit(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("lib==1.0\n")
    (tmp_path / "app.py").write_text("import lib\nlib.thing()\n")
    from types import SimpleNamespace
    rep = SimpleNamespace(risk="green", changes=[], affected=[])
    monkeypatch.setattr(audit_mod, "latest_version", lambda d: "2.0")
    monkeypatch.setattr(audit_mod, "sources_for", lambda *a, **k: (tmp_path, tmp_path, "lib"))
    monkeypatch.setattr(audit_mod, "run_radar", lambda *a, **k: rep)
    monkeypatch.setattr(audit_mod, "fetch_sdist", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = audit_mod.audit(tmp_path)[0]
    assert r.status == "checked" and r.notes == [] and r.risk == "green"


def test_notes_cover_every_release_between_the_two_versions(tmp_path):
    from upkeep.pypi import changelog_between
    (tmp_path / "CHANGES.rst").write_text("3.0.0\n- c\n- `load` is gone\n2.0.0\n- b\n1.0.0\n- a\n")
    got = changelog_between(tmp_path, "1.0.0", "3.0.0")
    assert "load" in got and "- b" in got and "- a" not in got      # 3.0.0 and 2.0.0, stopping at the pinned 1.0.0


def test_without_a_heading_for_the_old_version_only_the_newest_section_is_used(tmp_path):
    from upkeep.pypi import changelog_between
    (tmp_path / "CHANGES.rst").write_text("3.0.0\n" + "\n".join(f"- line {i}" for i in range(200)))
    assert changelog_between(tmp_path, "1.0.0", "3.0.0").count("\n") == 69


def test_a_dotted_mention_like_module_dot_name_matches():
    got = mentions("* always require `Loader` arg to `yaml.load()` in this release", {"load"})
    assert got and got[0][0] == "load"
