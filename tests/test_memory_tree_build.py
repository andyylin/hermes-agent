import hashlib
import json
from pathlib import Path

from agent.memory_tree_build import (
    BuildOptions,
    _archive_message_text,
    build_memory_tree_packs,
    collect_cron_records,
    collect_verified_archive_records,
    iter_recent_cron_outputs,
)
from agent.memory_tree_lite import SourceRecord, _query_terms, build_markdown_pack, search_memory_packs


def test_iter_recent_cron_outputs_skips_file_removed_during_scan(tmp_path, monkeypatch):
    home = tmp_path
    output = home / "cron" / "output" / "job1"
    output.mkdir(parents=True)
    vanished = output / "gone.md"
    vanished.write_text("gone", encoding="utf-8")
    kept = output / "kept.md"
    kept.write_text("kept", encoding="utf-8")

    original_stat = Path.stat
    stat_calls = {vanished: 0}

    def flaky_stat(self, *args, **kwargs):
        if self == vanished:
            stat_calls[vanished] += 1
            if stat_calls[vanished] >= 2:
                raise FileNotFoundError(str(self))
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)

    assert iter_recent_cron_outputs(home, 10) == [kept]


def test_collect_cron_records_skips_file_removed_after_discovery(tmp_path, monkeypatch):
    home = tmp_path
    output = home / "cron" / "output" / "job1"
    output.mkdir(parents=True)
    vanished = output / "gone.md"
    vanished.write_text("gone", encoding="utf-8")

    monkeypatch.setattr("agent.memory_tree_build.iter_recent_cron_outputs", lambda home, limit: [vanished])
    vanished.unlink()

    assert collect_cron_records(home, limit=10) == []


def test_archive_message_text_strips_cron_delivery_wrapper_but_keeps_task():
    lines = [
        "## Messages",
        "### User",
        "[IMPORTANT: You are running as a scheduled cron job. DELIVERY: automatic.]",
        "[DISCORD FORMAT: This cron output is delivered to Discord.]",
        "Prepare the morning brief.",
        "### Assistant",
        "Morning brief completed.",
        "## Export verification",
    ]

    text = _archive_message_text(lines, 0, max_chars=500)

    assert "scheduled cron job" not in text
    assert "DISCORD FORMAT" not in text
    assert "Prepare the morning brief" in text
    assert "Morning brief completed" in text


def test_collect_verified_archive_records_uses_manifest_sha_and_one_bounded_record(tmp_path):
    archive_dir = tmp_path / "session-exports"
    archive_dir.mkdir()
    archive = archive_dir / "s1-archive.md"
    archive.write_text(
        """---
session_id: \"s1\"
title: \"Archived Session\"
source: \"cli\"
message_count: 5
---
# Archived Session

## Messages

### User — 2026-08-17T01:00:00Z
Please investigate the archive index.

### Assistant — 2026-08-17T01:00:01Z
The archive index is verified.

### Tool — terminal
raw tool output must not be indexed

## Tool calls

```json
{\"command\": \"cat secret.txt\"}
```

## Export verification

- SHA256 of exported body: `not-indexed`
""",
        encoding="utf-8",
    )
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = archive_dir / "manifest.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "session_id": "s1",
                "path": str(archive),
                "format": "md",
                "message_count": 5,
                "sha256": digest,
                "exported_at": 123.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    records = collect_verified_archive_records(tmp_path, limit=1, max_record_chars=120)

    assert len(records) == 1
    record = records[0]
    assert record.source_type == "session-archive"
    assert record.source_id == "s1"
    assert "Please investigate" in record.text
    assert "archive index is verified" in record.text
    assert "raw tool output" not in record.text
    assert "Tool calls" not in record.text
    assert "cat secret.txt" not in record.text
    assert record.metadata["sha256"] == digest
    assert record.metadata["verified"] == "true"
    assert record.metadata["source"] == "cli"
    assert len(record.text) <= 120


def test_collect_verified_archive_records_rejects_non_markdown_manifest_format(tmp_path):
    archive_dir = tmp_path / "session-exports"
    archive_dir.mkdir()
    archive = archive_dir / "s1.jsonl"
    archive.write_text('{"role":"user","content":"raw"}\n', encoding="utf-8")
    (archive_dir / "manifest.jsonl").write_text(
        json.dumps({"session_id": "s1", "path": str(archive), "format": "jsonl", "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "exported_at": 123.0}) + "\n",
        encoding="utf-8",
    )
    assert collect_verified_archive_records(tmp_path, limit=10) == []


def test_collect_verified_archive_records_rejects_markdown_format_with_wrong_extension(tmp_path):
    archive_dir = tmp_path / "session-exports"
    archive_dir.mkdir()
    archive = archive_dir / "s1.jsonl"
    archive.write_text("---\nsession_id: s1\n---\n## Messages\n### User\nraw\n", encoding="utf-8")
    (archive_dir / "manifest.jsonl").write_text(
        json.dumps({"session_id": "s1", "path": str(archive), "format": "md", "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "exported_at": 123.0}) + "\n",
        encoding="utf-8",
    )
    assert collect_verified_archive_records(tmp_path, limit=10) == []


def test_collect_verified_archive_records_rejects_manifest_path_outside_export_root(tmp_path):
    archive_dir = tmp_path / "session-exports"
    archive_dir.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text(
        "---\nsession_id: \"outside\"\ntitle: \"Outside\"\n---\n"
        "## Messages\n### User\nshould never be indexed\n",
        encoding="utf-8",
    )
    (archive_dir / "manifest.jsonl").write_text(
        json.dumps(
            {
                "session_id": "outside",
                "path": str(outside),
                "format": "md",
                "sha256": hashlib.sha256(outside.read_bytes()).hexdigest(),
                "exported_at": 123.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert collect_verified_archive_records(tmp_path, limit=10) == []


def test_archived_conversation_labels_remain_searchable_body_not_metadata(tmp_path):
    archive_dir = tmp_path / "session-exports"
    archive_dir.mkdir()
    archive = archive_dir / "s1.md"
    archive.write_text(
        "---\nsession_id: \"s1\"\ntitle: \"Searchable\"\nsource: \"cron\"\n---\n"
        "## Messages\n### User\nfind this request\n### Assistant\nfound this result\n"
        "## Export verification\n",
        encoding="utf-8",
    )
    (archive_dir / "manifest.jsonl").write_text(
        json.dumps(
            {
                "session_id": "s1",
                "path": str(archive),
                "format": "md",
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "exported_at": 123.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    build_memory_tree_packs(
        BuildOptions(hermes_home=tmp_path, session_limit=10, cron_limit=0, ledger_limit=0)
    )

    results = search_memory_packs(
        [tmp_path / "data" / "memory-tree-lite" / "recent.md"],
        "find request",
        limit=1,
        max_snippet_chars=300,
    )

    assert len(results) == 1
    assert "find this request" in results[0].snippet
    assert "User" not in results[0].metadata
    assert "Assistant" not in results[0].metadata


def test_build_memory_tree_does_not_use_legacy_jsonl_without_explicit_fallback(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "stale.jsonl").write_text(
        json.dumps({"role": "user", "content": "stale raw session"}) + "\n",
        encoding="utf-8",
    )

    state = build_memory_tree_packs(
        BuildOptions(hermes_home=tmp_path, session_limit=10, cron_limit=0, ledger_limit=0)
    )

    assert state["counts"]["sessions"] == 0
    assert state["counts"]["legacy_sessions"] == 0
    assert "stale raw session" not in (tmp_path / "data" / "memory-tree-lite" / "recent.md").read_text(
        encoding="utf-8"
    )


def test_build_memory_tree_uses_legacy_jsonl_only_when_requested(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "legacy.jsonl").write_text(
        json.dumps({"role": "user", "content": "explicit legacy fallback"}) + "\n",
        encoding="utf-8",
    )

    state = build_memory_tree_packs(
        BuildOptions(
            hermes_home=tmp_path,
            session_limit=10,
            cron_limit=0,
            ledger_limit=0,
            legacy_session_fallback=True,
        )
    )

    assert state["counts"]["sessions"] == 0
    assert state["counts"]["legacy_sessions"] == 1
    assert "explicit legacy fallback" in (tmp_path / "data" / "memory-tree-lite" / "recent.md").read_text(
        encoding="utf-8"
    )


def test_query_terms_keep_ascii_and_emit_cjk_runs():
    assert _query_terms("LRG2608220276") == ["lrg2608220276"]
    terms = _query_terms("智匯桃園 蔡雅蕙 NTUT")
    assert "ntut" in terms
    assert "智匯桃園" in terms
    assert "蔡雅蕙" in terms


def test_memory_tree_search_matches_cjk_and_mixed_queries(tmp_path):
    pack = tmp_path / "recent.md"
    pack.write_text(
        build_markdown_pack(
            [
                SourceRecord(
                    source_type="grok-bot-work-log",
                    source_id="fyi-0824",
                    title="Joi Watch FYI",
                    timestamp=1.0,
                    text="NTUT 蔡雅蕙 invited via Prodesign to 智匯桃園 AI領航 成果發表會.",
                    metadata={"work_log": "data/grok-bot/work-log.md"},
                )
            ],
            title="Hermes Memory Tree Lite - Recent",
        ),
        encoding="utf-8",
    )
    cjk = search_memory_packs([pack], "智匯桃園", limit=3, max_snippet_chars=200)
    assert cjk
    assert "智匯桃園" in cjk[0].snippet
    mixed = search_memory_packs([pack], "NTUT 蔡雅蕙", limit=3, max_snippet_chars=200)
    assert mixed
    assert mixed[0].source_id == "fyi-0824"
    ascii_only = search_memory_packs([pack], "NTUT", limit=1)
    assert ascii_only and ascii_only[0].source_id == "fyi-0824"
