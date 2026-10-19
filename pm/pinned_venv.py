"""An operator-pinned runtime venv: the one interpreter and package set a checkout runs on.

A host whose services ``ExecStart`` a venv interpreter (rpi5's KEEP 6 drop-ins) records that
venv here. While the record exists the launcher never re-execs onto PM's store Python,
activation never swaps the venv's site-packages for a PM generation, and ``hermes pm
install`` / ``hermes update`` sync ``uv.lock`` into that venv instead of committing a new
generation. Without the record every path keeps PM's own contract.

The launcher reads the record through ``pm.environments`` (``pinned_venv``), which is in
its boot closure; this module writes it and syncs the venv.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

from pm.environments import install_state_dir, pin_record_path, pinned_venv, venv_python

# What the gateway's servers import from [all] (API server, LINE and SMS webhooks, MCP).
# A pinned venv that cannot import these is not the environment `hermes pm install` built.
RUNTIME_IMPORTS = ("aiohttp", "httpx2", "mcp")

_IMPORT_PROBE = (
    "import importlib, json, sys\n"
    "failed = []\n"
    "for name in sys.argv[1:]:\n"
    "    try:\n"
    "        importlib.import_module(name)\n"
    "    except Exception as exc:\n"
    "        failed.append(f'{name}: {type(exc).__name__}: {exc}')\n"
    "print(json.dumps(failed))\n"
)


def _sync_record_path(project_root: Path) -> Path:
    return install_state_dir(project_root) / "pinned-venv-sync.json"


def write_pin(project_root: Path, venv: Path) -> Path:
    venv = Path(venv).absolute()
    if not (venv / "pyvenv.cfg").is_file() or not venv_python(venv).is_file():
        raise RuntimeError(f"not a venv with an interpreter: {venv}")
    path = pin_record_path(project_root)
    _atomic_write_json(path, {"venv": str(venv)})
    return path


def remove_pin(project_root: Path) -> None:
    pin_record_path(project_root).unlink(missing_ok=True)
    _sync_record_path(project_root).unlink(missing_ok=True)


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _lock_digest(project_root: Path) -> str:
    return hashlib.sha256((Path(project_root) / "uv.lock").read_bytes()).hexdigest()


def pinned_venv_current(project_root: Path, extras: Iterable[str] | None = None) -> bool:
    """True when the last pinned sync installed this ``uv.lock`` with at least *extras*."""
    try:
        record = json.loads(_sync_record_path(project_root).read_text(encoding="utf-8-sig"))
        synced = set(record["extras"])
        return record["lock"] == _lock_digest(project_root) and set(extras or ()) <= synced
    except (OSError, ValueError, KeyError, TypeError):
        return False


def import_failures(python: Path, modules: Iterable[str] = RUNTIME_IMPORTS) -> list[str]:
    """``module: error`` for each of *modules* that *python* cannot import, run isolated."""
    done = subprocess.run([str(python), "-I", "-c", _IMPORT_PROBE, *modules], capture_output=True,
                          text=True, timeout=180, stdin=subprocess.DEVNULL, check=False)
    try:
        failed = json.loads(done.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return [f"{python}: import probe exited {done.returncode}: {done.stderr.strip()[-400:]}"]
    return [str(item) for item in failed]


def sync_pinned_venv(project_root: Path, venv: Path, extras: Iterable[str] | None, *,
                     explicit: bool) -> list[str]:
    """Install ``uv.lock``'s pins for [all], *extras* and every extra *venv* already carries.

    Additive (``uv pip install``): nothing the venv holds is removed, and the venv is never
    recreated, so a plugin or hand-installed package survives. Returns the synced extras.
    """
    from pm.environment import managed_environment
    from pm.extras import carried_extras
    from pm.package import InstallError

    root = Path(project_root)
    selected = sorted({"all", *(extras or ()), *carried_extras(venv)})
    python = venv_python(venv)
    environment = managed_environment(venv, python=python, explicit=explicit, output=sys.stderr)
    with tempfile.TemporaryDirectory(prefix="pm-pinned-venv-") as temporary:
        requirements = Path(temporary) / "requirements.txt"
        environment.export_requirements(root, requirements, extras=selected)
        lines = requirements.read_text(encoding="utf-8").splitlines()
        environment.install_requirements([line for line in lines if line.strip() and not line.lstrip().startswith("#")])
    environment.check()
    failed = import_failures(python)
    if failed:
        raise InstallError("venv", f"pinned runtime venv {venv} cannot import: {'; '.join(failed)}",
                           f"repair {venv} and rerun `hermes pm install`")
    _atomic_write_json(_sync_record_path(root), {"lock": _lock_digest(root), "extras": selected})
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pm.pinned_venv",
                                     description="Pin, show or remove this checkout's runtime venv.")
    parser.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parents[1])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("pin").add_argument("venv", type=Path)
    sub.add_parser("show")
    sub.add_parser("unpin")
    options = parser.parse_args(argv)
    root = options.checkout.resolve()
    if options.command == "pin":
        print(f"pinned {options.venv} in {write_pin(root, options.venv)}")
    elif options.command == "unpin":
        remove_pin(root)
        print("unpinned")
    else:
        venv = pinned_venv(root)
        print(json.dumps({"venv": str(venv) if venv else None,
                          "current": bool(venv) and pinned_venv_current(root)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
