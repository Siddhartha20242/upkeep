"""The boundaries that matter if the model, a library archive or a changelog is hostile."""

import difflib
import hashlib
import io
import json
import random
import tarfile
import zipfile
from pathlib import Path

import pytest

from upkeep import pypi
from upkeep.guard import allowed_ranges, check_diff
from upkeep.sandbox import SandboxRefused, docker_command, prepare_deps_command, run_python, run_tests
from upkeep.scan import CallSite


# ---------------------------------------------------------------- sandbox command line
def flag_values(argv, flag):
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag and i + 1 < len(argv)]


def test_test_run_container_has_no_network_no_capabilities_and_read_only_dependencies(tmp_path):
    deps, extra = tmp_path / "deps", tmp_path / "extra"
    deps.mkdir(), extra.mkdir()
    argv = docker_command(tmp_path, ["python", "-m", "pytest"], deps_dir=deps, extra_paths=[extra])
    assert flag_values(argv, "--network") == ["none"]
    assert flag_values(argv, "--cap-drop") == ["ALL"]
    assert "no-new-privileges" in flag_values(argv, "--security-opt")
    assert flag_values(argv, "--pids-limit") and flag_values(argv, "--memory") and flag_values(argv, "--cpus")
    mounts = flag_values(argv, "-v")
    assert any(m.endswith("/deps:ro") for m in mounts) and any(m.endswith("/extra/0:ro") for m in mounts)
    assert not any("docker.sock" in a or a in ("--privileged", "--env-file", "--pid=host", "--net=host") for a in argv)
    assert all(v.startswith(("HOME=", "PYTHONPATH=")) for v in flag_values(argv, "-e"))  # no host environment leaks in
    assert flag_values(argv, "--user") and not flag_values(argv, "--user")[0].startswith("0:")  # never root


def test_dependency_install_may_use_the_network_but_still_drops_capabilities(tmp_path):
    argv = prepare_deps_command(tmp_path, ["PyYAML==5.4.1"])
    assert "--network" not in argv or flag_values(argv, "--network") != ["none"]
    assert flag_values(argv, "--cap-drop") == ["ALL"] and "no-new-privileges" in flag_values(argv, "--security-opt")


def test_unsandboxed_backend_is_refused_unless_explicitly_allowed(tmp_path):
    with pytest.raises(SandboxRefused):
        run_tests(tmp_path, backend="local")
    with pytest.raises(SandboxRefused):
        run_python(tmp_path, "print(1)", backend="local")


# ---------------------------------------------------------------- hostile archives
def serve(monkeypatch, tmp_path, blob, name="evil-1.0.tar.gz"):
    def read(url, timeout=45, tries=4):
        if url.endswith("/json"):
            return json.dumps({"urls": [{"packagetype": "sdist", "url": f"https://f/{name}", "size": len(blob),
                                         "digests": {"sha256": hashlib.sha256(blob).hexdigest()}}]}).encode()
        return blob
    monkeypatch.setattr(pypi, "_read", read)
    return tmp_path / "cache"


def tar_with(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for info, data in members:
            t.addfile(info, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


def file_info(name, data=b"x"):
    i = tarfile.TarInfo(name)
    i.size = len(data)
    return i, data


def test_zip_slip_is_rejected_and_nothing_is_written_outside_the_cache(monkeypatch, tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../escaped.py", "x = 1\n")
    cache = serve(monkeypatch, tmp_path, buf.getvalue(), "evil-1.0.zip")
    with pytest.raises(pypi.PyPIError, match="unsafe path"):
        pypi.fetch_sdist("evil", "1.0", cache=cache)
    assert not (tmp_path / "escaped.py").exists() and not (cache.parent / "escaped.py").exists()


@pytest.mark.parametrize("member", ["../escaped.py", "/tmp/upkeep_abs_escape.py"])
def test_tar_path_traversal_and_absolute_paths_never_escape_the_cache(monkeypatch, tmp_path, member):
    cache = serve(monkeypatch, tmp_path, tar_with([file_info(member)]))
    try:
        root = pypi.fetch_sdist("evil", "1.0", cache=cache)
    except Exception:  # rejecting the archive is fine; extracting outside the cache is not
        root = None
    assert not (tmp_path / "escaped.py").exists() and not Path("/tmp/upkeep_abs_escape.py").exists()
    if root is not None:
        assert Path(root).resolve().is_relative_to(cache.resolve())


def test_tar_symlink_pointing_outside_is_not_followed_out_of_the_cache(monkeypatch, tmp_path):
    link = tarfile.TarInfo("pkg-1.0/link")
    link.type, link.linkname = tarfile.SYMTYPE, "../../../../etc"
    cache = serve(monkeypatch, tmp_path, tar_with([(link, None), file_info("pkg-1.0/ok.py", b"y = 1\n")]))
    try:
        root = pypi.fetch_sdist("evil", "1.0", cache=cache)
    except Exception:
        return
    for p in Path(root).rglob("*"):
        assert p.resolve().is_relative_to(cache.resolve()) or not p.is_symlink(), p


def test_oversized_archive_is_refused_before_download(monkeypatch, tmp_path):
    blob = tar_with([file_info("pkg-1.0/a.py")])
    cache = serve(monkeypatch, tmp_path, blob)
    with pytest.raises(pypi.PyPIError, match="limit"):
        pypi.fetch_sdist("evil", "1.0", cache=cache, max_bytes=10)


# ---------------------------------------------------------------- scope guard
def make_diff(old: list[str], new: list[str], path="app.py") -> str:
    return "".join(difflib.unified_diff([l + "\n" for l in old], [l + "\n" for l in new], f"a/{path}", f"b/{path}", n=0))


def test_random_single_line_edits_are_accepted_exactly_when_they_hit_an_affected_line():
    rng = random.Random(5)
    old = [f"line{i} = {i}" for i in range(1, 41)]
    sites = [CallSite("app.py", 10, 12, "lib.f", "call"), CallSite("app.py", 25, 25, "lib.g", "call")]
    allowed = allowed_ranges(sites)
    for _ in range(300):
        i = rng.randrange(len(old))
        new = old[:i] + [old[i] + "  # changed"] + old[i + 1:]
        inside = (10 <= i + 1 <= 12) or (i + 1 == 25)
        assert check_diff(make_diff(old, new), allowed).ok == inside, i + 1


def test_edits_to_other_files_or_dev_null_or_empty_diffs_are_rejected():
    allowed = allowed_ranges([CallSite("app.py", 3, 3, "lib.f", "call")])
    old, new = ["a", "b", "c", "d"], ["a", "b", "C", "d"]
    assert check_diff(make_diff(old, new), allowed).ok
    assert not check_diff(make_diff(old, new, "other.py"), allowed).ok
    assert not check_diff(make_diff(old, new, "../app.py"), allowed).ok
    assert not check_diff("--- a/app.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-a\n", allowed).ok
    assert not check_diff("", allowed).ok
