"""Shared helpers for Memory Tree plugin tests."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = REPO_ROOT / "plugin-export" / "memory-tree"
MODULE_NAME = "hermes_plugins.memory_tree"


def load_plugin_package():
    """Import the export package the same way PluginManager does."""
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns

    stale_prefix = f"{MODULE_NAME}."
    for name in [n for n in list(sys.modules) if n == MODULE_NAME or n.startswith(stale_prefix)]:
        del sys.modules[name]

    spec = importlib.util.spec_from_file_location(
        MODULE_NAME,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = MODULE_NAME
    module.__path__ = [str(PLUGIN_DIR)]
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


load_plugin_package()
