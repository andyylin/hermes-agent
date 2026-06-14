from __future__ import annotations

import json
from pathlib import Path


def _write_ledger(home: Path, records: list[dict]) -> Path:
    path = home / "data" / "active-work" / "ledger.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema": "hermes-active-work-ledger-v1",
                "updated_at": "2026-05-19T11:00:00+08:00",
                "records": records,
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_cron_jobs(path: Path, jobs: list[dict]) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"jobs": jobs}), encoding="utf-8")


def test_reconcile_verifies_active_cron_job_from_source_of_truth(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cron_path = tmp_path / "cron" / "jobs.json"
    _write_cron_jobs(
        cron_path,
        [
            {
                "id": "job123",
                "name": "nightly-memory-tree-lite-build",
                "enabled": True,
                "last_run_at": "2026-05-19T02:30:29+08:00",
                "last_status": "ok",
                "next_run_at": "2026-05-20T02:30:00+08:00",
            }
        ],
    )
    _write_ledger(
        tmp_path,
        [
            {
                "id": "nightly-memory-tree-lite-build",
                "status": "active",
                "title": "Nightly Memory Tree Lite build",
                "source_of_truth": {
                    "cron_jobs_file": str(cron_path),
                    "cron_job_id": "job123",
                },
            }
        ],
    )

    from agent.memory_tree_reconcile import reconcile_active_work

    report = reconcile_active_work()

    assert report.summary["active_verified"] == 1
    assert report.summary["dead_reference"] == 0
    assert report.items[0].status == "active_verified"
    assert report.items[0].source_id == "nightly-memory-tree-lite-build"
    assert report.items[0].evidence == "cron job job123 enabled=True last_status=ok"


def test_reconcile_verifies_hermes_prefixed_cron_source_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cron_path = tmp_path / "cron" / "jobs.json"
    _write_cron_jobs(
        cron_path,
        [
            {
                "id": "job123",
                "name": "joi-morning-briefing-hermes-owner",
                "enabled": True,
                "last_run_at": "2026-05-19T07:15:00+08:00",
                "last_status": "ok",
                "next_run_at": "2026-05-20T07:15:00+08:00",
            }
        ],
    )
    _write_ledger(
        tmp_path,
        [
            {
                "id": "joi-morning-briefing-hermes-owner",
                "status": "active",
                "title": "Joi/OpenClaw morning briefing delivery",
                "source_of_truth": {
                    "hermes_cron_jobs_file": str(cron_path),
                    "hermes_cron_job_id": "job123",
                },
            }
        ],
    )

    from agent.memory_tree_reconcile import reconcile_active_work

    report = reconcile_active_work()

    assert report.summary["active_verified"] == 1
    assert report.summary["verification_unavailable"] == 0
    assert report.items[0].status == "active_verified"


def test_reconcile_reports_missing_cron_job_as_dead_reference(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cron_path = tmp_path / "profiles" / "wife" / "cron" / "jobs.json"
    _write_cron_jobs(cron_path, [{"id": "alive", "enabled": True, "last_status": "ok"}])
    _write_ledger(
        tmp_path,
        [
            {
                "id": "wife-old-job",
                "status": "active",
                "source_of_truth": {
                    "cron_jobs_file": str(cron_path),
                    "cron_job_id": "missing",
                    "profile": "wife",
                },
            }
        ],
    )

    from agent.memory_tree_reconcile import reconcile_active_work

    report = reconcile_active_work()

    assert report.summary["dead_reference"] == 1
    item = report.items[0]
    assert item.status == "dead_reference"
    assert item.source_id == "wife-old-job"
    assert item.source_path == str(cron_path)
    assert "missing" in item.evidence


def test_reconcile_reports_active_record_without_source(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_ledger(tmp_path, [{"id": "undocumented", "status": "active"}])

    from agent.memory_tree_reconcile import reconcile_active_work

    report = reconcile_active_work()

    assert report.summary["missing_source"] == 1
    assert report.items[0].status == "missing_source"
    assert report.items[0].source_id == "undocumented"


def test_reconcile_json_is_bounded_and_source_rich(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_ledger(
        tmp_path,
        [
            {
                "id": f"record-{i}",
                "status": "active",
                "title": "x" * 200,
            }
            for i in range(20)
        ],
    )

    from agent.memory_tree_reconcile import format_reconcile_json, reconcile_active_work

    text = format_reconcile_json(reconcile_active_work(), max_chars=700)
    payload = json.loads(text)

    assert payload["schema"] == "memory-tree-reconcile-v1"
    assert payload["total_items"] == 20
    assert payload["truncated"] is True
    assert payload["items"]
    assert set(payload["items"][0]) >= {"source_id", "status", "severity", "source_path"}
    assert len(text) <= 700


def test_reconcile_does_not_mutate_ledger_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ledger = _write_ledger(tmp_path, [{"id": "undocumented", "status": "active"}])
    before = ledger.read_text(encoding="utf-8")

    from agent.memory_tree_reconcile import reconcile_active_work

    reconcile_active_work()

    assert ledger.read_text(encoding="utf-8") == before


def test_reconcile_verifies_n8n_workflow_from_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import sqlite3

    db = tmp_path / ".n8n" / "database.sqlite"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as con:
        con.execute("create table workflow_entity (id text primary key, name text, active integer, updatedAt text)")
        con.execute(
            "insert into workflow_entity values (?, ?, ?, ?)",
            ("workflow-1", "Workflow One", 1, "2026-05-19 10:00:00"),
        )
    _write_ledger(
        tmp_path,
        [
            {
                "id": "n8n-work",
                "status": "active",
                "runtime": {"n8n_workflow_id": "workflow-1"},
            }
        ],
    )

    from agent.memory_tree_reconcile import reconcile_active_work

    report = reconcile_active_work()

    assert report.summary["active_verified"] == 1
    assert report.items[0].status == "active_verified"
    assert report.items[0].source_path == str(db)
    assert "workflow-1 active=True" in report.items[0].evidence


def test_reconcile_verifies_multiple_cron_jobs_from_source_list(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cron_path = tmp_path / "profiles" / "wife" / "cron" / "jobs.json"
    _write_cron_jobs(
        cron_path,
        [
            {"id": "a", "enabled": True, "last_status": "ok"},
            {"id": "b", "enabled": True, "last_status": "ok"},
        ],
    )
    _write_ledger(
        tmp_path,
        [
            {
                "id": "multi-cron",
                "status": "active",
                "source_of_truth": {
                    "cron_jobs_file": str(cron_path),
                    "current_cron_job_ids": ["a", "b"],
                },
            }
        ],
    )

    from agent.memory_tree_reconcile import reconcile_active_work

    report = reconcile_active_work()

    assert report.summary["active_verified"] == 1
    assert report.items[0].status == "active_verified"
    assert "cron jobs verified=2" in report.items[0].evidence


def test_reconcile_accepts_recent_manual_verification_for_remote_systemd(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_ledger(
        tmp_path,
        [
            {
                "id": "remote-systemd",
                "status": "active",
                "source_of_truth": {
                    "host": "nemo",
                    "systemd_user_timer": "thing.timer",
                },
                "verification": {"status": "active", "checked_at": "2026-05-19T11:00:00+08:00"},
            }
        ],
    )

    from agent.memory_tree_reconcile import reconcile_active_work

    report = reconcile_active_work()

    assert report.summary["active_verified"] == 1
    assert report.items[0].status == "active_verified"
    assert "manual verification status=active" in report.items[0].evidence


def test_reconcile_verifies_gateway_config_key_across_profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for rel in ["config.yaml", "profiles/dad/config.yaml", "profiles/wife/config.yaml"]:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("gateway:\n  utility_routing:\n    enabled: true\n", encoding="utf-8")
    _write_ledger(
        tmp_path,
        [
            {
                "id": "utility-routing",
                "status": "active",
                "runtime": {
                    "gateway_config_key": "gateway.utility_routing",
                    "profiles_enabled": ["default", "dad", "wife"],
                },
            }
        ],
    )

    from agent.memory_tree_reconcile import reconcile_active_work

    report = reconcile_active_work()

    assert report.summary["active_verified"] == 1
    assert report.items[0].status == "active_verified"
    assert "profiles verified=3" in report.items[0].evidence
