from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from agent.memory_tree_attention import _scan_ledger_records


def _items(records):
    return _scan_ledger_records(
        records,
        now=datetime(2026, 6, 4, tzinfo=timezone.utc),
        stale_days=7,
        source_path=Path("ledger.json"),
    )


def test_ok_status_with_historical_error_word_is_stale_not_failure():
    records = [
        {
            "id": "backup-monitor",
            "title": "Backup monitor",
            "status": "active",
            "updated_at": "2026-05-28T00:00:00+00:00",
            "runtime": {"cron_job_id": "abc123", "last_status": "ok"},
            "verification": {
                "status": "ok_with_personal_rclone_errors",
                "checked_at": "2026-05-28T00:00:00+00:00",
                "evidence": "Old note mentions prior errors but current run is clean.",
            },
        }
    ]

    items = _items(records)

    assert len(items) == 1
    assert items[0].kind == "stale_active_work"
    assert items[0].severity == "attention"


def test_current_runtime_error_is_still_failure():
    records = [
        {
            "id": "broken-job",
            "title": "Broken job",
            "status": "active",
            "runtime": {"cron_job_id": "abc123", "last_status": "error"},
            "verification": {"status": "ok", "checked_at": "2026-06-04T00:00:00+00:00"},
        }
    ]

    items = _items(records)

    assert len(items) == 1
    assert items[0].kind == "failed_automation"
    assert items[0].severity == "failure"


def test_stale_active_work_can_be_suppressed_for_quiet_monitoring():
    records = [
        {
            "id": "stale-but-not-broken",
            "title": "Stale but not broken",
            "status": "active",
            "updated_at": "2026-05-01T00:00:00+00:00",
            "runtime": {"cron_job_id": "abc123", "last_status": "ok"},
            "verification": {"status": "ok", "checked_at": "2026-05-01T00:00:00+00:00"},
        }
    ]

    assert _scan_ledger_records(
        records,
        now=datetime(2026, 6, 4, tzinfo=timezone.utc),
        stale_days=7,
        source_path=Path("ledger.json"),
        include_stale=False,
    ) == []


def test_recent_verification_with_source_handle_is_not_attention():
    records = [
        {
            "id": "healthy-job",
            "title": "Healthy job",
            "status": "active",
            "updated_at": "2026-05-01T00:00:00+00:00",
            "runtime": {"cron_job_id": "abc123", "last_status": "ok"},
            "verification": {"status": "ok", "checked_at": "2026-06-03T00:00:00+00:00"},
        }
    ]

    assert _items(records) == []
