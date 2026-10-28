"""Run a repository's tests in isolation, optionally collecting line coverage.

Repository code is untrusted. Two backends:

* ``docker`` (default): no network, no host environment, dropped capabilities,
  memory/CPU/pid limits. Dependencies must be installed in the image or in a
  prepared directory (``prepare_deps_command`` does that in a separate,
  networked step).
* ``local``: runs on the host. Refused unless ``allow_unsandboxed=True``. It
  still uses a copy of the repo and a stripped environment (no secrets), but it
  is NOT isolated from the filesystem or network. Development use only.

The docker command construction is unit-tested; actually launching containers
needs a running Docker daemon.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .scan import CallSite

MAX_OUTPUT = 20_000
COPY_IGNORE = shutil.ignore_patterns(".git", ".venv", "venv", "__pycache__", "node_modules", ".tox")


class SandboxRefused(RuntimeError):
    pass


@dataclass
class TestResult:
    __test__ = False  # not a pytest class

    ok: bool
    returncode: int
    output: str
    backend: str
    executed_lines: dict[str, set[int]] | None = None


def docker_command(
    workdir: Path,
    cmd: list[str],
    image: str = "python:3.12-slim",
    extra_paths: list[Path] | None = None,
    memory: str = "1g",
    cpus: str = "1",
    pids: int = 256,
    network: bool = False,
    deps_dir: Path | None = None,
) -> list[str]:
    argv = ["docker", "run", "--rm"]
    if not network:
        argv += ["--network", "none"]
    argv += [
        "--memory", memory,
        "--cpus", cpus,
        "--pids-limit", str(pids),
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "-v", f"{workdir}:/work",
        "-w", "/work",
        "-e", "HOME=/tmp",
    ]
    py_paths = []
    for i, p in enumerate(extra_paths or []):
        argv += ["-v", f"{Path(p).resolve()}:/extra/{i}:ro"]
        py_paths.append(f"/extra/{i}")
    if deps_dir is not None:
        argv += ["-v", f"{Path(deps_dir).resolve()}:/deps:ro"]
        py_paths.append("/deps")
    if py_paths:
        argv += ["-e", "PYTHONPATH=" + ":".join(py_paths)]
    return argv + [image, *cmd]


def prepare_deps_command(deps_dir: Path, requirements: list[str], image: str = "python:3.12-slim") -> list[str]:
    """Networked, separate step: install dependencies into ``deps_dir`` inside a container."""
    return docker_command(
        deps_dir,
        ["pip", "install", "--target", "/work", *requirements],
        image=image,
        network=True,
    )


def _truncate(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT] + "\n...[truncated]"


def _read_coverage(workdir: Path) -> dict[str, set[int]] | None:
    f = workdir / "coverage.json"
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text())
    except (OSError, ValueError):
        return None
    return {name: set(info.get("executed_lines", [])) for name, info in data.get("files", {}).items()}


def run_tests(
    repo: Path,
    backend: str = "docker",
    image: str = "python:3.12-slim",
    extra_paths: list[Path] | None = None,
    with_coverage: bool = True,
    timeout: int = 300,
    allow_unsandboxed: bool = False,
    python: str | None = None,
    deps_dir: Path | None = None,
    pytest_args: list[str] | None = None,
    memory: str = "1g",
) -> TestResult:
    if backend == "local" and not allow_unsandboxed:
        raise SandboxRefused("local backend runs untrusted code on the host; pass allow_unsandboxed=True")
    if backend not in ("docker", "local"):
        raise ValueError(f"unknown backend: {backend}")

    tmp = Path(tempfile.mkdtemp(prefix="upkeep-run-"))
    try:
        work = tmp / "work"
        shutil.copytree(repo, work, symlinks=True, ignore=COPY_IGNORE)
        if backend == "docker":
            return _run_docker(work, image, extra_paths, with_coverage, timeout, deps_dir, pytest_args, memory)
        paths = list(extra_paths or []) + ([deps_dir] if deps_dir else [])
        return _run_local(work, paths, with_coverage, timeout, python or sys.executable, pytest_args)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_docker(work: Path, image: str, extra_paths, with_coverage: bool, timeout: int,
                deps_dir: Path | None = None, pytest_args: list[str] | None = None,
                memory: str = "1g") -> TestResult:
    pt = shlex.join(pytest_args or ["-q", "-p", "no:cacheprovider"])
    if with_coverage:
        script = (
            f"python -m coverage run -m pytest {pt}; rc=$?; "
            "python -m coverage json -q -o coverage.json; exit $rc"
        )
    else:
        script = f"python -m pytest {pt}"
    argv = docker_command(work, ["sh", "-c", script], image=image, extra_paths=extra_paths,
                          deps_dir=deps_dir, memory=memory)
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return TestResult(False, -1, "timed out", "docker")
    except FileNotFoundError:
        return TestResult(False, -1, "docker not installed", "docker")
    return TestResult(
        p.returncode == 0,
        p.returncode,
        _truncate(p.stdout + p.stderr),
        "docker",
        _read_coverage(work) if with_coverage else None,
    )


def _run_local(work: Path, extra_paths, with_coverage: bool, timeout: int, py: str,
               pytest_args: list[str] | None = None) -> TestResult:
    pt = list(pytest_args or ["-q", "-p", "no:cacheprovider"])
    # Stripped environment: nothing from the caller's shell (API keys, tokens) is passed on.
    env = {"PATH": "/usr/bin:/bin", "HOME": str(work), "PYTHONDONTWRITEBYTECODE": "1"}
    paths = [str(Path(p).resolve()) for p in (extra_paths or [])]
    if paths:
        env["PYTHONPATH"] = os.pathsep.join(paths)
    try:
        if with_coverage:
            p = subprocess.run(
                [py, "-m", "coverage", "run", "-m", "pytest", *pt],
                cwd=work, env=env, capture_output=True, text=True, timeout=timeout,
            )
            subprocess.run(
                [py, "-m", "coverage", "json", "-q", "-o", "coverage.json"],
                cwd=work, env=env, capture_output=True, text=True, timeout=60,
            )
        else:
            p = subprocess.run(
                [py, "-m", "pytest", *pt],
                cwd=work, env=env, capture_output=True, text=True, timeout=timeout,
            )
    except subprocess.TimeoutExpired:
        return TestResult(False, -1, "timed out", "local")
    return TestResult(
        p.returncode == 0,
        p.returncode,
        _truncate(p.stdout + p.stderr),
        "local",
        _read_coverage(work) if with_coverage else None,
    )


def site_is_covered(site: CallSite, executed: dict[str, set[int]] | None) -> bool | None:
    """True/False if coverage data exists for the file, None if unknown."""
    if executed is None:
        return None
    lines = executed.get(site.file)
    if lines is None:
        return False
    return any(site.line <= n <= site.end_line for n in lines)


def prepare_deps(
    repo: Path,
    deps_dir: Path,
    packages: list[str],
    requirements_file: str | None = None,
    image: str = "python:3.12-slim",
    timeout: int = 900,
    pip_args: list[str] | None = None,
    wheels_only: bool = False,
) -> TestResult:
    """Install dependencies into ``deps_dir`` inside a container WITH network.

    This is the only networked step. Test execution afterwards uses
    ``network none`` and mounts ``deps_dir`` read-only.
    """
    deps_dir = Path(deps_dir)
    deps_dir.mkdir(parents=True, exist_ok=True)
    # --only-binary=:all: never runs a package's setup.py: wheels are unpacked, not built, which removes most of the
    # supply-chain exposure of this (networked) step.
    only = ["--only-binary=:all:"] if wheels_only else []
    cmd = ["pip", "install", "--quiet", "--no-input", "--target", "/work", *only, *(pip_args or []), *packages]
    if requirements_file:
        cmd += ["-r", f"/extra/0/{requirements_file}"]
    argv = docker_command(deps_dir, cmd, image=image, extra_paths=[repo], network=True, memory="2g")
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return TestResult(False, -1, "pip timed out", "docker")
    return TestResult(p.returncode == 0, p.returncode, _truncate(p.stdout + p.stderr), "docker")


def run_python(
    repo: Path,
    script: str,
    backend: str = "docker",
    extra_paths: list[Path] | None = None,
    deps_dir: Path | None = None,
    image: str = "python:3.12-slim",
    timeout: int = 120,
    allow_unsandboxed: bool = False,
    python: str | None = None,
) -> tuple[bool, str]:
    """Run ``python -c script`` with the repo as the working directory, in the sandbox.

    Used for the behavior check: the same probe script runs once in the "old world"
    (original code + old library) and once in the "new world" (patched code + new library).
    """
    if backend == "local" and not allow_unsandboxed:
        raise SandboxRefused("local backend runs untrusted code on the host; pass allow_unsandboxed=True")
    tmp = Path(tempfile.mkdtemp(prefix="upkeep-py-"))
    try:
        work = tmp / "work"
        shutil.copytree(repo, work, symlinks=True, ignore=COPY_IGNORE)
        if backend == "docker":
            argv = docker_command(work, ["python", "-c", script], image=image, extra_paths=extra_paths, deps_dir=deps_dir)
            env = None
        else:
            paths = [str(Path(p).resolve()) for p in (list(extra_paths or []) + ([deps_dir] if deps_dir else []))]
            env = {"PATH": "/usr/bin:/bin", "HOME": str(work), "PYTHONDONTWRITEBYTECODE": "1"}
            if paths:
                env["PYTHONPATH"] = os.pathsep.join(paths)
            argv = [python or sys.executable, "-c", script]
        try:
            p = subprocess.run(argv, cwd=None if backend == "docker" else work, env=env,
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "timed out"
        except FileNotFoundError:
            return False, "docker not installed"
        return p.returncode == 0, _truncate(p.stdout + p.stderr)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
