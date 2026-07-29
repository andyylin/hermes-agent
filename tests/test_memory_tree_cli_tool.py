import json
from pathlib import Path

from agent.memory_tree_lite import SourceRecord, build_markdown_pack


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


def test_memory_tree_status_json_reports_manual_context_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_pack(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "memory_tree:\n  enabled: false\n  mode: manual\n  packs:\n    - recent\n",
        encoding="utf-8",
    )

    from hermes_cli.memory_tree import memory_tree_status

    status = memory_tree_status(json_mode=True)
    payload = json.loads(status)

    assert payload["schema"] == "memory-tree-status-v1"
    assert payload["context"]["enabled"] is False
    assert payload["context"]["mode"] == "manual"
    assert payload["build"]["records_total"] == 1
    assert payload["packs"][0]["exists"] is True


def test_memory_tree_search_formats_provenance_matches(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_pack(tmp_path)

    from hermes_cli.memory_tree import memory_tree_search

    output = memory_tree_search("Mattermost", limit=2, chars=300)

    assert "Mattermost morning brief" in output
    assert "active-work / mattermost-brief" in output
    assert "deterministic packet context" in output


def test_memory_tree_tool_context_preview_is_callable_without_auto_injection(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_pack(tmp_path)

    from tools.memory_tree_tool import memory_tree_tool

    result = memory_tree_tool({"action": "context-preview", "query": "Mattermost", "limit": 1, "chars": 500})

    assert result["success"] is True
    assert result["auto_injected"] is False
    assert "Memory Tree context preview" in result["context"]
    assert "Mattermost morning brief" in result["context"]


def test_default_toolsets_expose_memory_tree_on_call():
    from toolsets import _HERMES_CORE_TOOLS, TOOLSETS

    assert "memory_tree" in _HERMES_CORE_TOOLS
    assert "memory_tree" in TOOLSETS["memory_tree"]["tools"]
