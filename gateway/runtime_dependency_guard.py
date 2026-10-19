"""Startup guard for a gateway whose install is pinned to an operator-managed runtime venv.

A pinned venv (``pm.pinned_venv``) is provisioned at deploy time by ``hermes pm install``;
boot never repairs it. So a gateway that comes up on another interpreter, or on a venv that
cannot import what its enabled platforms need, is a deploy fault: refuse to start (exit 78)
with one log line naming the interpreter and every missing piece, instead of skipping the
platform and leaving the rest half-served.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

from pm.environments import pinned_venv, running_from
from pm.pinned_venv import RUNTIME_IMPORTS


def _builtin_probe(platform: Any) -> Callable[[], bool] | bool | None:
    from gateway.run import _BUILTIN_ADAPTERS, _builtin_adapter_import

    spec = _BUILTIN_ADAPTERS.get(platform)
    if spec is None:
        return None
    module, adapter_name, requirement, _warning = spec
    try:
        return _builtin_adapter_import(module, adapter_name, requirement)[1]
    except ImportError:
        return False


def _requirements_met(platform: Any) -> bool:
    """The platform's own non-installing requirement probe (never ``ensure_deps_fn``)."""
    from gateway.platform_registry import platform_registry

    entry = platform_registry.get(platform.value)
    probe = entry.check_fn if entry is not None else _builtin_probe(platform)
    if probe is None:
        return True
    return bool(probe() if callable(probe) else probe)


def pinned_runtime_failures(platforms: Mapping[Any, Any],
                            served: Callable[[Any, Any], bool] = lambda _p, _c: True) -> list[str]:
    """What keeps this pinned gateway from serving its enabled platforms; empty when unpinned."""
    pinned = pinned_venv(Path(__file__).resolve().parents[1])
    if pinned is None:
        return []
    failures = []
    if not running_from(pinned):
        failures.append(f"interpreter {sys.executable} (prefix {sys.prefix}) is not the pinned venv {pinned}")
    for name in RUNTIME_IMPORTS:
        try:
            importlib.import_module(name)
        except ImportError as exc:
            failures.append(f"cannot import {name}: {exc}")
    from hermes_cli.plugins import discover_plugins

    discover_plugins()  # Startup runs this guard before discovery; LINE and Email are plugin platforms.
    for platform, config in platforms.items():
        if getattr(config, "enabled", False) and served(platform, config) and not _requirements_met(platform):
            failures.append(f"platform {platform.value}: requirements not met")
    return failures
