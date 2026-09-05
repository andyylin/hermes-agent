"""Plugin registration smoke tests."""

from __future__ import annotations

from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
from hermes_plugins.memory_tree import register


def test_register_adds_tool_and_cli():
    mgr = PluginManager()
    manifest = PluginManifest(name="memory-tree", key="memory-tree")
    register(PluginContext(manifest, mgr))

    assert "memory_tree" in mgr._plugin_tool_names
    assert "memory-tree" in mgr._cli_commands
    entry = mgr._cli_commands["memory-tree"]
    assert callable(entry["setup_fn"])
    assert callable(entry["handler_fn"])
