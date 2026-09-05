import json
from pathlib import Path

from hermes_plugins.memory_tree.memory_tree_lite import SourceRecord, build_markdown_pack


def _write_pack(home: Path) -> Path:
    pack = home / "data" / "memory-tree-lite" / "recent.md"
    pack.parent.mkdir(parents=True, exist_ok=True)
    pack.write_text(
        build_markdown_pack(
            [
                SourceRecord(
                    source_type="active-work",
                    source_id="mattermost-brief",
                    title="Mattermost morning brief",
                    timestamp=123.0,
                    text="The Mattermost brief uses deterministic packet context and stays silent on no-op.",
                    metadata={"path": "data/active-work/ledger.json"},
                )
            ],
            title="Hermes Memory Tree Lite - Recent",
        ),
        encoding="utf-8",
    )
    (home / "data" / "memory-tree-lite" / "state.json").write_text(
        json.dumps(
            {
                "schema": "memory-tree-lite-state-v1",
                "updated_at": "2026-06-01T12:00:00+08:00",
                "counts": {"records_total": 1, "sessions": 0, "active_work": 1, "cron_outputs": 0},
                "outputs": {"recent": str(pack)},
            }
        ),
        encoding="utf-8",
    )
    return pack


def _enable_memory_tree_config(home: Path) -> None:
    (home / "config.yaml").write_text(
        "memory_tree:\n  enabled: true\n  mode: manual\n  packs:\n    - recent\n",
        encoding="utf-8",
    )


def test_memory_tree_status_json_reports_manual_context_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_pack(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "memory_tree:\n  enabled: false\n  mode: manual\n  packs:\n    - recent\n",
        encoding="utf-8",
    )

    from hermes_plugins.memory_tree.cli import memory_tree_status

    status = memory_tree_status(json_mode=True)
    payload = json.loads(status)

    assert payload["schema"] == "memory-tree-status-v1"
    assert payload["context"]["enabled"] is False
    assert payload["context"]["mode"] == "manual"
    assert payload["build"]["records_total"] == 1
    assert payload["build"]["session_archives"] == 0
    assert payload["packs"][0]["exists"] is True


def test_memory_tree_status_treats_pre_archive_session_count_as_legacy(tmp_path):
    data_dir = tmp_path / "data" / "memory-tree-lite"
    data_dir.mkdir(parents=True)
    (data_dir / "state.json").write_text(
        json.dumps({"schema": "memory-tree-lite-state-v1", "counts": {"records_total": 7, "sessions": 7, "active_work": 0, "cron_outputs": 0}, "outputs": {}}),
        encoding="utf-8",
    )
    from hermes_plugins.memory_tree.cli import memory_tree_status

    payload = json.loads(memory_tree_status(json_mode=True, home=tmp_path))
    text = memory_tree_status(home=tmp_path)
    assert payload["build"]["session_archives"] == 0
    assert payload["build"]["legacy_sessions"] == 7
    assert "session_archives=0" in text
    assert "legacy_sessions=7" in text


def test_memory_tree_search_formats_provenance_matches(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_pack(tmp_path)

    from hermes_plugins.memory_tree.cli import memory_tree_search

    output = memory_tree_search("Mattermost", limit=2, chars=300)

    assert "Mattermost morning brief" in output
    assert "active-work / mattermost-brief" in output
    assert "deterministic packet context" in output


def test_memory_tree_tool_context_preview_is_callable_without_auto_injection(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_pack(tmp_path)
    _enable_memory_tree_config(tmp_path)

    from hermes_plugins.memory_tree.tool import memory_tree_tool

    result = json.loads(memory_tree_tool({"action": "context-preview", "query": "Mattermost", "limit": 1, "chars": 500}))

    assert result["success"] is True
    assert result["auto_injected"] is False
    assert "Memory Tree context preview" in result["context"]
    assert "Mattermost morning brief" in result["context"]


def test_memory_tree_registry_dispatch_returns_supported_string(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_pack(tmp_path)
    _enable_memory_tree_config(tmp_path)

    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    from hermes_plugins.memory_tree import register
    from tools.registry import registry

    mgr = PluginManager()
    manifest = PluginManifest(name="memory-tree", key="memory-tree", path=str(tmp_path))
    register(PluginContext(manifest, mgr))

    raw = registry.dispatch(
        "memory_tree",
        {"action": "search", "query": "Mattermost", "limit": 1, "chars": 300},
    )
    assert isinstance(raw, str)
    result = json.loads(raw)
    assert result["success"] is True
    assert result["auto_injected"] is False
    assert result["result"]["results"][0]["source_id"] == "mattermost-brief"


def test_memory_tree_cli_parser_registers_archive_build_command():
    import argparse

    from hermes_plugins.memory_tree.cli import register_cli

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    plugin_parser = subparsers.add_parser("memory-tree")
    register_cli(plugin_parser)

    args = parser.parse_args(["memory-tree", "build", "--legacy-session-fallback"])

    assert args.command == "memory-tree"
    assert args.memory_tree_command == "build"
    assert args.legacy_session_fallback is True
