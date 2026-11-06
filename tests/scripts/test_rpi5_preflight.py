"""The rpi5 pre-restart preflight: the launcher's runtime imports the served stack and every
home's Bitwarden secrets resolve, without printing a value.

rpi5 2026-10-19: a gateway restart hit `bws exited 1 ... 429 Too Many Requests` and came up
without its BWS-sourced secrets. A deploy must find that out while the old units still run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "docs" / "ops"))

import rpi5_deploy_engine as eng

TOKEN = "0.machine-token-never-printed"
SECRET = "line-secret-never-printed"


RATE_LIMITED = 'echo "Error: 0: [429 Too Many Requests]" >&2\nexit 1\n'


def _home(tmp_path: Path, name: str, *, bitwarden: bool = True, encrypted_cache: bool = False) -> Path:
    home = tmp_path / name
    home.mkdir()
    if bitwarden:
        cache = "    encrypted_cache:\n      enabled: true\n      max_stale_seconds: 604800\n"
        (home / "config.yaml").write_text(
            "secrets:\n  bitwarden:\n    enabled: true\n    project_id: proj-1\n"
            "    auto_install: false\n    cache_ttl_seconds: 0\n" + (cache if encrypted_cache else ""),
            encoding="utf-8")
        (home / ".env").write_text(f"BWS_ACCESS_TOKEN={TOKEN}\n", encoding="utf-8")
    return home


def _fake_bws(tmp_path: Path, script: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    bws = bin_dir / "bws"
    bws.write_text("#!/bin/sh\n" + script, encoding="utf-8")
    bws.chmod(0o755)
    return bin_dir


def _run(argv: list[str], tmp_path: Path, bin_dir: Path | None = None) -> tuple[int, dict, str]:
    env = {**os.environ, "HERMES_HOME": str(tmp_path / "launch-home")}
    if bin_dir is not None:
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    done = subprocess.run(argv, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
                          timeout=300, check=False)
    return done.returncode, json.loads(done.stdout.strip().splitlines()[-1]), done.stdout + done.stderr


def _preflight(tmp_path: Path, *homes: Path, bin_dir: Path | None = None) -> tuple[int, dict, str]:
    args = [arg for home in homes for arg in ("--home", str(home))]
    return _run([sys.executable, "-I", str(eng.PROBE), "--checkout", str(REPO_ROOT), "preflight", *args],
                tmp_path, bin_dir)


def test_an_interpreter_without_the_runtime_packages_fails_the_preflight(tmp_path):
    import venv

    bare = tmp_path / "bare-venv"
    venv.create(bare, with_pip=False)

    code, payload, _ = _run([str(bare / "bin" / "python"), "-I", str(eng.PROBE), "--checkout", str(REPO_ROOT),
                             "preflight"], tmp_path)

    assert code == 1 and payload["ok"] is False
    assert payload["failed"] == ["aiohttp:ModuleNotFoundError", "httpx2:ModuleNotFoundError",
                                 "mcp:ModuleNotFoundError"]


def test_a_bws_429_without_an_encrypted_cache_fails_the_preflight_without_printing_the_token(tmp_path):
    bin_dir = _fake_bws(tmp_path, RATE_LIMITED)
    home = _home(tmp_path, "default", encrypted_cache=True)

    code, payload, output = _preflight(tmp_path, home, bin_dir=bin_dir)

    assert code == 1 and payload["ok"] is False
    assert payload["failed"] == [f"secrets:{home}:rate_limited"]
    (entry,) = payload["secrets"]
    assert entry["secrets"] == 0 and "429" in entry["error"]
    assert entry["encrypted_cache_present"] is False
    assert TOKEN not in output


def test_a_bws_429_is_served_from_a_valid_encrypted_cache(tmp_path):
    listing = json.dumps([{"key": "LINE_CHANNEL_SECRET", "value": SECRET}])
    bin_dir = _fake_bws(tmp_path, f"cat <<'EOF'\n{listing}\nEOF\n")
    home = _home(tmp_path, "default", encrypted_cache=True)
    code, payload, _ = _preflight(tmp_path, home, bin_dir=bin_dir)
    assert code == 0 and payload["secrets"][0]["encrypted_cache_present"] is True, payload
    (bin_dir / "bws").write_text("#!/bin/sh\n" + RATE_LIMITED, encoding="utf-8")

    code, payload, output = _preflight(tmp_path, home, bin_dir=bin_dir)

    assert code == 0 and payload["ok"] is True and payload["failed"] == [], payload
    (entry,) = payload["secrets"]
    assert entry["secrets"] == 1 and entry["stale_cache"] is True
    assert SECRET not in output and TOKEN not in output


def test_resolved_secrets_are_counted_never_printed(tmp_path):
    listing = json.dumps([{"key": "LINE_CHANNEL_SECRET", "value": SECRET}])
    bin_dir = _fake_bws(tmp_path, f"cat <<'EOF'\n{listing}\nEOF\n")
    default, dad = _home(tmp_path, "default"), _home(tmp_path, "dad", bitwarden=False)

    code, payload, output = _preflight(tmp_path, default, dad, bin_dir=bin_dir)

    assert code == 0 and payload["ok"] is True, payload
    assert [(e["home"], e["bitwarden"], e.get("secrets")) for e in payload["secrets"]] == [
        (str(default), "enabled", 1), (str(dad), "disabled", None)]
    assert SECRET not in output and TOKEN not in output


def test_the_preflight_runs_through_a_real_launcher(tmp_path):
    from hermes_cli._launchers import mint_launcher

    bin_dir = tmp_path / "launcher-bin"
    bin_dir.mkdir()
    launcher = mint_launcher("hermes", REPO_ROOT, bin_dir, Path(sys.executable), None)
    assert launcher is not None
    home = _home(tmp_path, "default", bitwarden=False)

    code, payload, _ = _run([str(launcher), "--run-module", eng.PROBE_MODULE, "--checkout", str(REPO_ROOT),
                             "preflight", "--home", str(home)], tmp_path)

    assert code == 0 and payload["ok"] is True, payload
    assert payload["failed"] == []
