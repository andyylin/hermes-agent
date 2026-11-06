"""Committed-generation interpreter hygiene (#122183 Linux analogue)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pm.environments import (
    CommittedInterpreterMismatch,
    activate_dependencies,
    committed_launch_python,
    install_state_dir,
    launch_python_needs_reexec,
    site_packages,
    venv_python,
)


def _write_generation(root: Path, *, major: int, minor: int) -> Path:
    state = install_state_dir(root)
    generation = state / "environments" / f"py{major}{minor}" / "venv"
    site = generation / f"lib/python{major}.{minor}/site-packages"
    site.mkdir(parents=True)
    (generation / "pyvenv.cfg").write_text(
        f"home = test\nversion_info = {major}.{minor}.0\n", encoding="utf-8"
    )
    venv_python(generation).parent.mkdir(parents=True, exist_ok=True)
    venv_python(generation).write_text("", encoding="utf-8")
    (site / "probe.py").write_text("VALUE = 'ok'\n", encoding="utf-8")
    (state / "facts.json").write_text(
        json.dumps({"schema": 1, "packages": {"venv": {"environment": str(generation)}}}),
        encoding="utf-8",
    )
    return generation


def test_activate_dependencies_refuses_a_foreign_abi(tmp_path, monkeypatch):
    from hermes_cli import _launchers

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    foreign = (3, 11) if sys.version_info[:2] != (3, 11) else (3, 12)
    generation = _write_generation(repo, major=foreign[0], minor=foreign[1])
    store_python = venv_python(generation)
    monkeypatch.setattr(_launchers, "resolve_store_python", lambda _root: store_python)

    with pytest.raises(CommittedInterpreterMismatch, match="requires Python"):
        activate_dependencies(repo)

    assert committed_launch_python(repo) == store_python


def test_launch_python_needs_reexec_when_abi_differs(tmp_path, monkeypatch):
    from hermes_cli import _launchers

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    foreign = (3, 11) if sys.version_info[:2] != (3, 11) else (3, 12)
    generation = _write_generation(repo, major=foreign[0], minor=foreign[1])
    store_python = venv_python(generation)
    monkeypatch.setattr(_launchers, "resolve_store_python", lambda _root: store_python)

    target = launch_python_needs_reexec(repo)
    assert target == store_python


def test_boot_reexecs_onto_the_committed_generation_in_a_fresh_process(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    generation = _write_generation(repo, major=sys.version_info.major, minor=sys.version_info.minor)
    other = tmp_path / "other"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(other)], check=True, timeout=60)
    code = (
        "import sys; from pathlib import Path; "
        "sys.path.insert(0, sys.argv[1]); "
        "from pm.environments import activate_dependencies, site_packages, install_state_dir; "
        "root = Path(sys.argv[2]); activate_dependencies(root); "
        "selected = site_packages(install_state_dir(root) / 'environments' / "
        f"'py{sys.version_info.major}{sys.version_info.minor}' / 'venv'); "
        "import probe; print(probe.VALUE, any(Path(p).resolve() == selected.resolve() "
        "for p in sys.path if p))"
    )
    result = subprocess.run(
        [str(venv_python(other)), "-I", "-c", code, str(Path(__file__).resolve().parents[2]), str(repo)],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok True"
