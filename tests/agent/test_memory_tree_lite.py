from __future__ import annotations

import json
import os
from pathlib import Path


def test_build_pack_groups_records_by_source_and_includes_provenance():
    from agent.memory_tree_lite import SourceRecord, build_markdown_pack

    records = [
        SourceRecord(
            source_type="session",
            source_id="s1:2",
            title="Gateway fix",
            timestamp=2,
            text="Fixed TokenJuice smoke test.",
            metadata={"session_id": "s1"},
        ),
        SourceRecord(
            source_type="cron",
            source_id="c1:1",
            title="Digest",
            timestamp=1,
            text="No action needed.",
        ),
    ]

    md = build_markdown_pack(records, title="Recent Hermes Work")

    assert md.startswith("# Recent Hermes Work\n")
    assert "## cron" in md
    assert "## session" in md
    assert "source_id: c1:1" in md
    assert "source_id: s1:2" in md
    assert "session_id: s1" in md
    assert "Fixed TokenJuice smoke test." in md
    assert md.index("## cron") < md.index("## session")


def test_build_pack_is_deterministic_and_truncates_long_records():
    from agent.memory_tree_lite import SourceRecord, build_markdown_pack

    records = [
        SourceRecord("session", "b", "Later", 20, "b" * 32),
        SourceRecord("session", "a", "Earlier", 10, "a" * 5000),
    ]

    first = build_markdown_pack(records, title="Pack", max_record_chars=80)
    second = build_markdown_pack(list(reversed(records)), title="Pack", max_record_chars=80)

    assert first == second
    assert first.index("source_id: a") < first.index("source_id: b")
    assert "[truncated" in first
    assert "a" * 100 not in first


def test_collect_session_records_reads_recent_jsonl_and_skips_tools_by_default(tmp_path: Path):
    from agent.memory_tree_lite import collect_session_records

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    older = sessions / "older.jsonl"
    newer = sessions / "newer.jsonl"

    older.write_text(
        json.dumps({"role": "user", "content": "older user", "timestamp": 1, "session_id": "old"}) + "\n",
        encoding="utf-8",
    )
    newer.write_text(
        "not-json\n"
        + json.dumps({"role": "tool", "content": "tool payload", "timestamp": 2, "session_id": "new"})
        + "\n"
        + json.dumps({"role": "assistant", "content": "assistant answer", "timestamp": 3, "session_id": "new"})
        + "\n",
        encoding="utf-8",
    )
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))

    records = collect_session_records(sessions, limit_files=1)

    assert len(records) == 1
    assert records[0].text == "assistant answer"
    assert records[0].source_type == "session"
    assert records[0].source_id == "newer.jsonl:3"
    assert records[0].metadata["role"] == "assistant"
    assert records[0].metadata["malformed_lines"] == "1"


def test_collect_session_records_can_include_tools(tmp_path: Path):
    from agent.memory_tree_lite import collect_session_records

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "one.jsonl").write_text(
        json.dumps({"role": "tool", "content": "tool payload", "timestamp": 2, "session_id": "s"}) + "\n",
        encoding="utf-8",
    )

    records = collect_session_records(sessions, include_tools=True)

    assert len(records) == 1
    assert records[0].text == "tool payload"
    assert records[0].metadata["role"] == "tool"


def test_write_pack_creates_parent_dirs_and_replaces_atomically(tmp_path: Path):
    from agent.memory_tree_lite import write_pack

    target = tmp_path / "nested" / "pack.md"

    written = write_pack(target, "# one\n")
    assert written == target
    assert target.read_text(encoding="utf-8") == "# one\n"

    write_pack(target, "# two\n")
    assert target.read_text(encoding="utf-8") == "# two\n"
    assert not list(target.parent.glob("*.tmp"))
