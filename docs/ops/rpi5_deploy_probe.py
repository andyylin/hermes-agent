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

Output is one JSON object on stdout. Nothing here prints a credential.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

# The modules a gateway process imports before it serves a turn.
RELEASE_IMPORTS = ("hermes_cli.main", "gateway.run", "run_agent", "model_tools", "cron.scheduler")


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


COMMANDS = {
    "idle": cmd_idle,
    "homes": cmd_homes,
    "drain": cmd_drain,
    "stamp": cmd_stamp,
    "imports": cmd_imports,
    "skills-sync": cmd_skills_sync,
    "venv-python": cmd_venv_python,
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
    for name in ("homes", "stamp", "skills-sync", "venv-python"):
        sub.add_parser(name)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sys.path.insert(0, str(Path(args.checkout).resolve()))
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
