"""The rpi5 pre-restart check: the gateway interpreter must be the pinned venv and import its stack."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "docs" / "ops"))

import rpi5_deploy_engine as eng

from pm.pinned_venv import write_pin


def _probe(home: Path, *args: str) -> tuple[int, dict]:
    done = subprocess.run(
        [sys.executable, "-I", str(eng.PROBE), "--checkout", str(REPO_ROOT), *args],
        capture_output=True, text=True, check=False,
        env={**os.environ, "HERMES_HOME": str(home)}, stdin=subprocess.DEVNULL, timeout=300)
    return done.returncode, json.loads(done.stdout.strip().splitlines()[-1])


def _foreign_venv(tmp_path: Path) -> Path:
    venv = tmp_path / "pinned-venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("version = 3.14.7\n")
    (venv / "bin" / "python").write_text("")
    return venv


def test_preflight_refuses_an_interpreter_that_is_not_the_pinned_venv(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    venv = _foreign_venv(tmp_path)
    write_pin(REPO_ROOT, venv)

    code, payload = _probe(home, "preflight")

    assert code == 1 and payload["ok"] is False
    assert payload["pinned"] == str(venv)
    assert any(item.startswith("interpreter:") for item in payload["failed"])


def test_venv_python_answers_the_pinned_interpreter(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    venv = _foreign_venv(tmp_path)
    write_pin(REPO_ROOT, venv)

    assert _probe(home, "venv-python")[1] == {"ok": True, "python": str(venv / "bin" / "python")}
