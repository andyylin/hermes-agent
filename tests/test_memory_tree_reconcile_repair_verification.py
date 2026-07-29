import json
from pathlib import Path

from agent.memory_tree_reconcile import format_reconcile_text, reconcile_active_work


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_cron_error_after_post_failure_verification_is_active(tmp_path: Path) -> None:
    cron_path = tmp_path / "profiles" / "wife" / "cron" / "jobs.json"
    _write_json(
        cron_path,
        {
            "jobs": [
                {
                    "id": "job-a",
                    "enabled": True,
                    "last_status": "error",
                    "last_run_at": "2026-07-02T01:36:22+08:00",
                    "last_repair_verification_status": "ok",
                    "last_repair_verification_at": "2026-07-02T07:38:40+08:00",
                }
            ]
        },
    )
    _write_json(
        tmp_path / "data" / "active-work" / "ledger.json",
        {
            "records": [
                {
                    "id": "wife-job",
                    "title": "Wife job",
                    "status": "active",
                    "source_of_truth": {
                        "cron_jobs_file": str(cron_path),
                        "cron_job_id": "job-a",
                    },
                }
            ]
        },
    )

    report = reconcile_active_work(tmp_path)

    assert report.summary["active_verified"] == 1
    assert report.summary["runtime_attention"] == 0
    assert format_reconcile_text(report) == ""
    assert "post_failure_verification=ok" in report.items[0].evidence


def test_cron_error_with_stale_repair_verification_still_needs_attention(tmp_path: Path) -> None:
    cron_path = tmp_path / "profiles" / "wife" / "cron" / "jobs.json"
    _write_json(
        cron_path,
        {
            "jobs": [
                {
                    "id": "job-a",
                    "enabled": True,
                    "last_status": "error",
                    "last_run_at": "2026-07-02T07:00:00+08:00",
                    "last_repair_verification_status": "ok",
                    "last_repair_verification_at": "2026-07-02T06:59:59+08:00",
                }
            ]
        },
    )
    _write_json(
        tmp_path / "data" / "active-work" / "ledger.json",
        {
            "records": [
                {
                    "id": "wife-job",
                    "title": "Wife job",
                    "status": "active",
                    "source_of_truth": {
                        "cron_jobs_file": str(cron_path),
                        "cron_job_id": "job-a",
                    },
                }
            ]
        },
    )

    report = reconcile_active_work(tmp_path)

    assert report.summary["runtime_attention"] == 1
    text = format_reconcile_text(report)
    assert "Memory Tree reconcile attention" in text
    assert "wife-job" in text


def test_multi_cron_record_accepts_post_failure_verification(tmp_path: Path) -> None:
    cron_path = tmp_path / "profiles" / "wife" / "cron" / "jobs.json"
    _write_json(
        cron_path,
        {
            "jobs": [
                {
                    "id": "job-a",
                    "enabled": True,
                    "last_status": "error",
                    "last_run_at": "2026-07-02T01:36:22+08:00",
                    "last_repair_verification_status": "ok",
                    "last_repair_verification_at": "2026-07-02T07:38:40+08:00",
                },
                {
                    "id": "job-b",
                    "enabled": True,
                    "last_status": "ok",
                },
            ]
        },
    )
    _write_json(
        tmp_path / "data" / "active-work" / "ledger.json",
        {
            "records": [
                {
                    "id": "wife-jobs",
                    "title": "Wife jobs",
                    "status": "active",
                    "source_of_truth": {
                        "cron_jobs_file": str(cron_path),
                        "current_cron_job_ids": ["job-a", "job-b"],
                    },
                }
            ]
        },
    )

    report = reconcile_active_work(tmp_path)

    assert report.summary["active_verified"] == 1
    assert report.summary["runtime_attention"] == 0
    assert report.items[0].evidence == "cron jobs verified by post-failure repair diagnostics"
