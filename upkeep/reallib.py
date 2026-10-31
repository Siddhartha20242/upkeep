"""Real-library benchmark: ACTUAL releases of real libraries, small downstream apps authored by the project author.

What is real: library versions, their source (for the API diff), their release notes (read from the
release archive), and how they fail. What is authored: the downstream app, its tests, hidden tests and
reference solution. Each case is validated in Docker against the real libraries before any model run:
  1. original app + OLD library  -> visible and hidden tests pass
  2. original app + NEW library  -> visible tests fail
  3. reference solution + NEW    -> visible and hidden tests pass
"""

from __future__ import annotations

import io
import json
import re
import shutil
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent import Agent
from .charact import BehaviorChecker
from .apidiff import diff_api, extract_api  # noqa: F401  (diff used by radar)
from .evalcases import _w
from .evalharness import Outcome
from .patchlint import import_roots_from
from .pypi import changelog_section, check_sha256
from .radar import run_radar
from .sandbox import prepare_deps, run_python, run_tests

BUILT_FROM_SOURCE: list[tuple] = []  # dependency sets that needed a source build (reported, never silent)
IMAGE = "python:3.9-slim"  # old releases (PyYAML 5.4.1) still ship wheels for 3.9


@dataclass
class RealCase:
    name: str
    dist: str            # PyPI distribution name
    module: str          # import package name
    old_ver: str
    new_ver: str
    old_pins: list[str]
    new_pins: list[str]
    kind: str
    repo: dict[str, str]
    hidden: dict[str, str]
    solution: dict[str, str]
    notes: str = ""      # what is subtle about this case
    extra: dict = field(default_factory=dict)


CASES: list[RealCase] = [
    RealCase(
        "pyyaml_load", "PyYAML", "yaml", "5.4.1", "6.0.2", ["PyYAML==5.4.1"], ["PyYAML==6.0.2"],
        "Loader argument became required",
        {"app.py": "import yaml\n\n\ndef load_config(text):\n    return yaml.load(text)\n\n\ndef load_docs(text):\n    return list(yaml.load_all(text))\n",
         "test_app.py": "import app\n\n\ndef test_config():\n    assert app.load_config('a: 1\\nb: [2, 3]\\n') == {'a': 1, 'b': [2, 3]}\n"},
        {"test_hidden.py": "import app\n\n\ndef test_docs():\n    assert app.load_docs('---\\na: 1\\n---\\nb: 2\\n') == [{'a': 1}, {'b': 2}]\n"},
        {"app.py": "import yaml\n\n\ndef load_config(text):\n    return yaml.safe_load(text)\n\n\ndef load_docs(text):\n    return list(yaml.safe_load_all(text))\n"},
        "load_docs() has no visible test: only the hidden test notices a wrong or missing fix there.",
    ),
    RealCase(
        "jinja2_removed_names", "Jinja2", "jinja2", "3.0.3", "3.1.4", ["Jinja2==3.0.3", "MarkupSafe==2.1.5"],
        ["Jinja2==3.1.4", "MarkupSafe==2.1.5"], "names removed from the package namespace",
        {"app.py": "from jinja2 import Environment, Markup, contextfilter, escape\n\nenv = Environment()\n\n\n@contextfilter\ndef shout(ctx, value):\n    return str(value).upper()\n\n\nenv.filters['shout'] = shout\n\n\ndef render(template, **kw):\n    return env.from_string(template).render(**kw)\n\n\ndef safe(s):\n    return Markup(s)\n\n\ndef esc(s):\n    return str(escape(s))\n",
         "test_app.py": "import app\n\n\ndef test_filter():\n    assert app.render('{{ x|shout }}', x='a') == 'A'\n\n\ndef test_escape():\n    assert app.esc('<b>') == '&lt;b&gt;'\n"},
        {"test_hidden.py": "import app\n\n\ndef test_markup_not_escaped():\n    assert app.render('{{ s }}', s=app.safe('<i>')) == '<i>'\n"},
        {"app.py": "from jinja2 import Environment, pass_context\nfrom markupsafe import Markup, escape\n\nenv = Environment()\n\n\n@pass_context\ndef shout(ctx, value):\n    return str(value).upper()\n\n\nenv.filters['shout'] = shout\n\n\ndef render(template, **kw):\n    return env.from_string(template).render(**kw)\n\n\ndef safe(s):\n    return Markup(s)\n\n\ndef esc(s):\n    return str(escape(s))\n"},
        "The fix needs two different replacements (markupsafe import and pass_context) at an import line and a decorator.",
        extra={"changelog_ver": "3.1.0"},  # the removals were announced in 3.1.0, not in the 3.1.4 section
    ),
    RealCase(
        "werkzeug_urls", "Werkzeug", "werkzeug", "2.3.8", "3.0.3", ["Werkzeug==2.3.8", "MarkupSafe==2.1.5"],
        ["Werkzeug==3.0.3", "MarkupSafe==2.1.5"], "helpers removed; stdlib replacement has different defaults",
        {"app.py": "from werkzeug.urls import url_encode, url_quote\n\n\ndef quote(s):\n    return url_quote(s)\n\n\ndef encode(d):\n    return url_encode(d)\n",
         "test_app.py": "import app\n\n\ndef test_quote_simple():\n    assert app.quote('a b') == 'a%20b'\n\n\ndef test_encode():\n    assert app.encode({'a': 1, 'b': 2}) == 'a=1&b=2'\n"},
        {"test_hidden.py": "import app\n\n\ndef test_quote_keeps_slash_and_colon():\n    assert app.quote('a b/c:d') == 'a%20b/c:d'\n"},
        {"app.py": "from urllib.parse import quote, urlencode\n\n\ndef quote_(s):\n    return quote(s, safe='/:')\n\n\ndef encode(d):\n    return urlencode(d)\n"},
        "werkzeug's url_quote kept '/' and ':' unescaped by default; urllib.parse.quote escapes ':'. A naive swap is green on visible tests and wrong on the hidden one.",
    ),
]
BY_NAME = {c.name: c for c in CASES}
# the third solution above must keep the app's public names; fix the helper name mismatch explicitly
CASES[2].solution["app.py"] = ("from urllib.parse import quote as _quote, urlencode\n\n\n"
                               "def quote(s):\n    return _quote(s, safe='/:')\n\n\ndef encode(d):\n    return urlencode(d)\n")


# ---- real release archives -------------------------------------------------

def _urlopen_read(url: str, timeout: int = 45, tries: int = 6) -> bytes:
    last: Exception | None = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 - https PyPI URLs only
                return r.read()
        except (TimeoutError, OSError) as e:
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"download failed after {tries} tries: {url}: {last}")


def _download_sdist(dist: str, ver: str, dest: Path) -> Path:
    root = dest / f"{dist}-{ver}"
    if root.exists() and any(root.rglob("*.py")):  # a cache of empty folders is NOT a cache (it has happened)
        return root
    shutil.rmtree(root, ignore_errors=True)
    data = json.loads(_urlopen_read(f"https://pypi.org/pypi/{dist}/{ver}/json"))
    entry = next(u for u in data["urls"] if u["packagetype"] == "sdist")
    url = entry["url"]
    blob = _urlopen_read(url)  # download fully BEFORE creating the folder, so a failure leaves nothing behind
    check_sha256(blob, entry, f"{dist}=={ver}")
    tmp = dest / f".tmp-{dist}-{ver}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    if url.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            for n in z.namelist():
                if n.startswith("/") or ".." in Path(n).parts:
                    raise RuntimeError("unsafe archive path")
            z.extractall(tmp)
    else:
        with tarfile.open(fileobj=io.BytesIO(blob)) as t:
            t.extractall(tmp, filter="data")
    shutil.rmtree(root, ignore_errors=True)
    tmp.rename(root)
    return root


def _locate(root: Path, package: str) -> Path | None:
    best = None
    for init in root.rglob("__init__.py"):
        if init.parent.name == package and not any(p in ("tests", "test", "docs") for p in init.parts):
            depth = len(init.parent.relative_to(root).parts)
            if best is None or depth < best[0]:
                best = (depth, init.parent.parent)
    return best[1] if best else None


@dataclass
class Prepared:
    old_src: Path
    new_src: Path
    changelog: str | None
    old_deps: Path
    new_deps: Path


def _deps(pins: list[str], cache: Path) -> Path:
    key = re.sub(r"[^a-zA-Z0-9]+", "_", "_".join(sorted(pins)))
    d = cache / f"deps-cov-{IMAGE.replace(':', '_')}-{key}"
    if not d.exists():
        dummy = Path(tempfile.mkdtemp())
        res = prepare_deps(dummy, d, ["pytest", "coverage", *pins], image=IMAGE, wheels_only=True)
        if not res.ok:  # no wheel exists: fall back to building from source, and SAY so
            BUILT_FROM_SOURCE.append(tuple(pins))
            print(f"warning: no wheels for {pins}; building from source inside the container", flush=True)
            res = prepare_deps(dummy, d, ["pytest", "coverage", *pins], image=IMAGE, wheels_only=False)
        shutil.rmtree(dummy, ignore_errors=True)
        if not res.ok:
            shutil.rmtree(d, ignore_errors=True)
            raise RuntimeError(f"dependency install failed for {pins}: {res.output[-600:]}")
    return d


def prepare(case: RealCase, cache: Path) -> Prepared:
    cache.mkdir(parents=True, exist_ok=True)
    old_root = _download_sdist(case.dist, case.old_ver, cache)
    new_root = _download_sdist(case.dist, case.new_ver, cache)
    old_src, new_src = _locate(old_root, case.module), _locate(new_root, case.module)
    if not old_src or not new_src:
        raise RuntimeError(f"package dir {case.module!r} not found in sdist")
    cl_ver = case.extra.get("changelog_ver", case.new_ver)
    changelog = changelog_section(new_root, cl_ver)
    if changelog is None and cl_ver != case.new_ver:  # the newer archive may not ship the notes
        changelog = changelog_section(_download_sdist(case.dist, cl_ver, cache), cl_ver)
    return Prepared(old_src, new_src, changelog, _deps(case.old_pins, cache), _deps(case.new_pins, cache))


def _run(repo: Path, deps: Path, extra: dict[str, str] | None = None):
    work = Path(tempfile.mkdtemp()) / "repo"
    shutil.copytree(repo, work, symlinks=True)
    if extra:
        _w(work, extra)
    r = run_tests(work, backend="docker", image=IMAGE, deps_dir=deps, with_coverage=False,
                  pytest_args=["-q", "-p", "no:cacheprovider", "--tb=short"], memory="1g", timeout=300)
    shutil.rmtree(work.parent, ignore_errors=True)
    return r


def selfcheck(case: RealCase, prep: Prepared) -> dict:
    repo = Path(tempfile.mkdtemp()) / "repo"
    _w(repo, case.repo)
    sol = Path(tempfile.mkdtemp()) / "repo"
    shutil.copytree(repo, sol)
    _w(sol, case.solution)
    checks = {
        "old_lib_passes_visible_and_hidden": _run(repo, prep.old_deps, case.hidden).ok,
        "new_lib_fails_visible": not _run(repo, prep.new_deps).ok,
        "solution_passes_visible_and_hidden_on_new": _run(sol, prep.new_deps, case.hidden).ok,
    }
    report = run_radar(repo, prep.old_src, prep.new_src, case.module)
    checks["radar_detects_break"] = bool(report.affected)
    checks["changelog_found"] = prep.changelog is not None
    shutil.rmtree(repo.parent, ignore_errors=True)
    shutil.rmtree(sol.parent, ignore_errors=True)
    return {"case": case.name, "sound": all(v for k, v in checks.items() if k != "changelog_found"), **checks}


def run_agent_case(case: RealCase, prep: Prepared, client, use_changelog: bool = True, behavior_check: bool = False,
                   focus_notes: bool = True, notes_mode: str = "tool", max_turns: int = 14,
                   prober_client=None, n_random: int = 0, library_source: bool = False,
                   explore: bool = False, escalate_client=None, escalate_turns: int = 40) -> Outcome:
    root = Path(tempfile.mkdtemp(prefix=f"real-{case.name}-"))
    try:
        repo = root / "repo"
        _w(repo, case.repo)
        report = run_radar(repo, prep.old_src, prep.new_src, case.module)
        base: dict[str, Any] = dict(name=case.name, kind=case.kind, expect="fix", config=("real-behavior" if behavior_check else "real-full") if use_changelog else "real-nochangelog",
                    radar_risk=report.risk)
        if not report.affected:
            return Outcome(**base, outcome="missed", status="not run", reason="radar found nothing")
        runner = lambda w: run_tests(  # noqa: E731
            w, backend="docker", image=IMAGE, deps_dir=prep.new_deps, with_coverage=False,
            pytest_args=["-q", "-p", "no:cacheprovider", "--tb=short"], memory="1g", timeout=300)
        checker = None
        if behavior_check:
            sites = [s for _, ss in report.affected for s in ss]
            world = lambda deps: (lambda d, script: run_python(d, script, backend="docker", image=IMAGE, deps_dir=deps))  # noqa: E731
            checker = BehaviorChecker(prober_client or client, repo, sites, world(prep.old_deps), world(prep.new_deps),
                                     n_random=n_random)
        def make_explorer():
            scratch = Path(tempfile.mkdtemp(prefix="upkeep-explore-"))

            def explorer(code: str) -> tuple[str, str]:
                o = run_python(scratch, code, backend="docker", image=IMAGE, deps_dir=prep.old_deps, timeout=60)[1]
                n = run_python(scratch, code, backend="docker", image=IMAGE, deps_dir=prep.new_deps, timeout=60)[1]
                return o, n
            return explorer

        def build(cl, turns: int, with_explorer: bool):
            return Agent(cl, repo, report, runner, changelog=prep.changelog if use_changelog else None,
                         explorer=make_explorer() if with_explorer else None, import_roots=import_roots_from(prep.new_deps, prep.new_src),
                         behavior_checker=checker, focus_notes=focus_notes, notes_mode=notes_mode, max_turns=turns,
                         library_roots={"old": prep.old_src, "new": prep.new_src} if library_source else None)
        agent = build(client, max_turns, explore)
        t0 = time.monotonic()
        try:
            res = agent.run()
            if res.status != "verified" and escalate_client is not None:  # the verifier declined: the stronger model tries, same verifier
                first_calls, first_tokens = res.model_calls, res.total_tokens
                agent.cleanup()
                agent = build(escalate_client, escalate_turns, True)
                res = agent.run()
                res.model_calls += first_calls
                res.total_tokens += first_tokens
        except Exception as e:  # one broken case must not lose the whole benchmark run
            return Outcome(**base, outcome="error", status="error", reason=f"{type(e).__name__}: {str(e)[:150]}")
        dt = time.monotonic() - t0
        hidden_ok = None
        if res.status == "verified":
            check = root / "check"
            shutil.copytree(agent.work, check, symlinks=True)
            _w(check, case.hidden)
            hidden_ok = runner(check).ok
        outcome = "declined_failure" if res.status == "declined" else ("success" if hidden_ok else "wrong_but_green")
        out = Outcome(**base, outcome=outcome, status=res.status, reason=res.reason, model_calls=res.model_calls,
                      tokens=res.total_tokens, seconds=round(dt, 1), guard_rejections=res.guard_rejections,
                      injection_flags=res.injection_flags, hidden_ok=hidden_ok,
                      tools=[s["tool"] for s in res.steps], diff=res.diff[:1500])
        agent.cleanup()
        return out
    finally:
        shutil.rmtree(root, ignore_errors=True)
