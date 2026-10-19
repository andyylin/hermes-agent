"""Hermes-side probes for the rpi5 deploy listener.

``rpi5_deploy.py`` is stdlib-only and runs on the system Python so a broken
release cannot take the deployer down with it. Anything that has to answer
the way Hermes itself answers runs here instead, as a child process of the
committed PM interpreter with ``sys.path`` pointed at one checkout:

- ``idle``: each home's ``gateway_state.json`` through ``gateway.status``
  (PID-reuse guard, heartbeat staleness, ``active_agents``, which already sums
  chat turns, cron jobs, API runs and deferred workers).
- ``homes``: ``hermes_cli.profiles.list_profiles`` for ``$HERMES_HOME`` (default plus named).
- ``drain``: ``gateway.drain_control`` begin/clear-drain, the same marker the
  dashboard's drain-gated restart writes.
- ``stamp``: ``write_source_stamp``, what ``hermes update`` publishes after a pull.
- ``imports``: import the gateway's entry modules from a staged release.
- ``skills-sync``: ``tools.skills_sync.sync_skills`` for ``$HERMES_HOME``.
- ``venv-python``: the interpreter ``pm.environments`` committed for a checkout.
  This one runs on the system Python, before a PM interpreter is known.
- ``preflight``: run through the checkout's own launcher
  (``.hermes/bin/hermes --run-module docs.ops.rpi5_deploy_probe``), so it gets
  the interpreter and dependency environment a restarted gateway gets. Imports
  aiohttp, httpx2, mcp and the API server, LINE, Email and Telegram adapters,
  then resolves each ``--home``'s Bitwarden secrets the way gateway startup
  does (fresh cache, live ``bws``, stale encrypted cache on a transport
  failure). Reports counts and error kinds, never values.

Output is one JSON object on stdout. Nothing here prints a credential.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

# The modules a gateway process imports before it serves a turn.
RELEASE_IMPORTS = ("hermes_cli.main", "gateway.run", "run_agent", "model_tools", "cron.scheduler")
PREFLIGHT_IMPORTS = (
    "aiohttp", "httpx2", "mcp", "gateway.platforms.api_server", "plugins.platforms.line.adapter",
    "plugins.platforms.email.adapter", "plugins.platforms.telegram.adapter",
)


def _emit(payload: dict) -> int:
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload.get("ok", True) else 1


def cmd_idle(args: argparse.Namespace) -> int:
    from gateway.status import (
        parse_active_agents, read_runtime_status, runtime_status_is_stale, runtime_status_pid_is_live,
    )

    homes = []
    for raw in args.home:
        home = Path(raw)
        record = read_runtime_status(home / "gateway_state.json")
        live = runtime_status_pid_is_live(record)
        record = record or {}
        homes.append({
            "home": str(home),
            "live": live,
            "state": record.get("gateway_state"),
            "active_agents": parse_active_agents(record.get("active_agents")),
            "stale": runtime_status_is_stale(record),
            "code_sha": record.get("code_sha"),
        })
    return _emit({"ok": True, "homes": homes})


def cmd_homes(args: argparse.Namespace) -> int:
    from hermes_cli.profiles import list_profiles

    return _emit({"ok": True, "homes": [str(p.path) for p in list_profiles(lazy_skill_count=True)]})


def cmd_drain(args: argparse.Namespace) -> int:
    from gateway.drain_control import clear_drain_request, write_drain_request

    for raw in args.home:
        if args.state == "on":
            write_drain_request(principal="rpi5-deploy", suppress_notification=True, home=Path(raw))
        else:
            clear_drain_request(home=Path(raw))
    return _emit({"ok": True})


def cmd_stamp(args: argparse.Namespace) -> int:
    from hermes_cli.source_stamp import write_source_stamp

    stamp = write_source_stamp(Path(args.checkout))
    commit = (stamp or {}).get("commit")
    return _emit({"ok": bool(commit), "commit": commit})


def cmd_imports(args: argparse.Namespace) -> int:
    failed = []
    for raw in args.compile:
        try:
            compile(Path(raw).read_bytes(), raw, "exec", dont_inherit=True)
        except (SyntaxError, ValueError, OSError) as exc:
            failed.append(f"{raw}:{type(exc).__name__}")
    for name in RELEASE_IMPORTS:
        try:
            importlib.import_module(name)
        except Exception as exc:  # health: allow BLE001 -- any import failure rejects the release
            failed.append(f"{name}:{type(exc).__name__}")
            continue
        module_file = getattr(sys.modules[name], "__file__", "") or ""
        if not Path(module_file).resolve().is_relative_to(Path(args.checkout).resolve()):
            failed.append(f"{name}:outside-release")
    return _emit({"ok": not failed, "failed": failed})


def cmd_skills_sync(args: argparse.Namespace) -> int:
    from tools.skills_sync import sync_skills

    sync_skills(quiet=True)
    return _emit({"ok": True})


def cmd_venv_python(args: argparse.Namespace) -> int:
    from pm.environments import committed_venv, venv_python

    venv = committed_venv(Path(args.checkout))
    python = venv_python(venv) if venv is not None else None
    ok = python is not None and python.is_file()
    return _emit({"ok": ok, "python": str(python) if ok else None})


def _preflight_import_failures() -> list[str]:
    failed = []
    saved = os.environ.get("HERMES_HOME")
    # Adapter imports must not write into a live profile home.
    with tempfile.TemporaryDirectory(prefix="rpi5-preflight-home.") as scratch:
        os.environ["HERMES_HOME"] = scratch
        try:
            for name in PREFLIGHT_IMPORTS:
                try:
                    importlib.import_module(name)
                except Exception as exc:  # health: allow BLE001 -- any import failure blocks the restart
                    failed.append(f"{name}:{type(exc).__name__}")
        finally:
            if saved is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = saved
    return failed


def _bitwarden_status(home: Path) -> dict:
    """One home's Bitwarden resolution through ``apply_all``, the call gateway startup makes."""
    from agent.secret_scope import load_env_file
    from agent.secret_sources.bitwarden import _encrypted_disk_cache_path
    from agent.secret_sources.registry import apply_all
    from hermes_cli.env_loader import _load_secrets_config

    section = (_load_secrets_config(home) or {}).get("bitwarden")
    if not isinstance(section, dict) or section.get("enabled") is not True:
        return {"home": str(home), "ok": True, "bitwarden": "disabled"}
    environ = {**os.environ, **load_env_file(home / ".env"), "HERMES_HOME": str(home)}
    reports = apply_all({"bitwarden": section}, home, environ=environ).sources
    if not reports:
        return {"home": str(home), "ok": False, "bitwarden": "enabled", "kind": "not_registered"}
    result = reports[0].result
    return {
        "home": str(home), "ok": result.ok and bool(result.secrets), "bitwarden": "enabled",
        "secrets": len(result.secrets), "kind": result.error_kind.value if result.error_kind else None,
        "error": (result.error or "")[:200] or None,
        "stale_cache": any("falling back" in warning for warning in result.warnings),
        "encrypted_cache_present": _encrypted_disk_cache_path(home).is_file(),
    }


def cmd_preflight(args: argparse.Namespace) -> int:
    failed = _preflight_import_failures()
    secrets = [_bitwarden_status(Path(raw)) for raw in args.home]
    failed += [f"secrets:{entry['home']}:{entry.get('kind') or 'empty'}" for entry in secrets if not entry["ok"]]
    return _emit({"ok": not failed, "failed": failed, "python": sys.executable, "secrets": secrets})


COMMANDS = {
    "idle": cmd_idle,
    "homes": cmd_homes,
    "drain": cmd_drain,
    "stamp": cmd_stamp,
    "imports": cmd_imports,
    "skills-sync": cmd_skills_sync,
    "venv-python": cmd_venv_python,
    "preflight": cmd_preflight,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--checkout", required=True, help="Hermes tree to import from")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("idle").add_argument("--home", action="append", default=[])
    drain = sub.add_parser("drain")
    drain.add_argument("state", choices=("on", "off"))
    drain.add_argument("--home", action="append", default=[])
    sub.add_parser("imports").add_argument("--compile", action="append", default=[])
    sub.add_parser("preflight").add_argument("--home", action="append", default=[])
    for name in ("homes", "stamp", "skills-sync", "venv-python"):
        sub.add_parser(name)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sys.path.insert(0, str(Path(args.checkout).resolve()))
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
