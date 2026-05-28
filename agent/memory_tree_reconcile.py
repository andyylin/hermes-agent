"""Deterministic source-of-truth reconciliation for Memory Tree attention.

This module checks whether assistant-owned active-work ledger records still point
at concrete, live runtime state. It is intentionally read-only by default: it
reports dead or undocumented references instead of mutating the ledger.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from hermes_constants import get_hermes_home

LEDGER_PATH = Path("data") / "active-work" / "ledger.json"
ACTIVE_STATUSES = {"active", "waiting", "blocked", "in_progress", "monitoring"}
RECONCILE_SCHEMA = "memory-tree-reconcile-v1"


@dataclass(frozen=True)
class ReconcileItem:
    status: str
    source_id: str
    title: str
    severity: str
    source_path: str
    evidence: str
    next_action: str


@dataclass(frozen=True)
class ReconcileReport:
    items: list[ReconcileItem]
    summary: dict[str, int]


def _ledger_path(home: Path | None = None) -> Path:
    return (home or get_hermes_home()) / LEDGER_PATH


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_ledger_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = _load_json(path)
    records = data.get("records", []) if isinstance(data, dict) else []
    return [record for record in records if isinstance(record, dict)]


def _active_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if str(record.get("status", "")).strip().lower() in ACTIVE_STATUSES
    ]


def _source_dict(record: dict[str, Any]) -> dict[str, Any]:
    source = record.get("source_of_truth")
    if isinstance(source, dict):
        merged = dict(source)
    else:
        merged = {}
    runtime = record.get("runtime")
    if isinstance(runtime, dict):
        # Some older ledger entries put the concrete handle in runtime only.
        keys = {
            "cron_job_id",
            "hermes_cron_job_id",
            "cron_jobs_file",
            "hermes_cron_jobs_file",
            "current_cron_job_ids",
            "cron_jobs",
            "profile",
            "script",
            "state_file",
            "n8n_workflow_id",
            "n8n_workflow_name",
            "health_endpoint",
            "service_dependencies",
            "gateway_config_key",
            "profiles_enabled",
            "systemd_user_timer",
            "systemd_user_service",
        }
        for key in keys:
            if key in runtime and key not in merged:
                merged[key] = runtime[key]
    if "cron_job_id" not in merged and merged.get("hermes_cron_job_id"):
        merged["cron_job_id"] = merged["hermes_cron_job_id"]
    if "cron_jobs_file" not in merged and merged.get("hermes_cron_jobs_file"):
        merged["cron_jobs_file"] = merged["hermes_cron_jobs_file"]
    return merged


def _default_cron_path(profile: str | None = None, home: Path | None = None) -> Path:
    root = home or get_hermes_home()
    if profile and profile not in {"default", "main"}:
        return root / "profiles" / profile / "cron" / "jobs.json"
    return root / "cron" / "jobs.json"


def _cron_jobs_from_file(path: Path) -> list[dict[str, Any]]:
    data = _load_json(path)
    if isinstance(data, dict):
        jobs = data.get("jobs", [])
    elif isinstance(data, list):
        jobs = data
    else:
        jobs = []
    return [job for job in jobs if isinstance(job, dict)]


def _find_cron_job(path: Path, job_id: str) -> dict[str, Any] | None:
    for job in _cron_jobs_from_file(path):
        if str(job.get("id", "")) == job_id:
            return job
    return None


def _record_id(record: dict[str, Any]) -> str:
    return str(record.get("id") or record.get("record_id") or record.get("title") or "unknown")


def _record_title(record: dict[str, Any]) -> str:
    return str(record.get("title") or _record_id(record))


def _missing_source(record: dict[str, Any], ledger_path: Path) -> ReconcileItem:
    return ReconcileItem(
        status="missing_source",
        source_id=_record_id(record),
        title=_record_title(record),
        severity="attention",
        source_path=str(ledger_path),
        evidence="active ledger record has no concrete source_of_truth or runtime handle",
        next_action="Add a source_of_truth/runtime handle, mark it complete, or remove the stale record.",
    )


def _verify_cron_record(record: dict[str, Any], source: dict[str, Any], home: Path) -> ReconcileItem:
    job_id = str(source.get("cron_job_id") or "")
    cron_path_raw = source.get("cron_jobs_file")
    cron_path = Path(cron_path_raw) if cron_path_raw else _default_cron_path(source.get("profile"), home)
    if not cron_path.is_absolute():
        cron_path = home / cron_path

    if not cron_path.exists():
        return ReconcileItem(
            status="verification_unavailable",
            source_id=_record_id(record),
            title=_record_title(record),
            severity="attention",
            source_path=str(cron_path),
            evidence=f"cron jobs file missing for cron_job_id={job_id}",
            next_action="Verify the profile/home path or update the ledger source_of_truth.",
        )

    try:
        job = _find_cron_job(cron_path, job_id)
    except (OSError, json.JSONDecodeError) as exc:
        return ReconcileItem(
            status="verification_unavailable",
            source_id=_record_id(record),
            title=_record_title(record),
            severity="attention",
            source_path=str(cron_path),
            evidence=f"could not read cron jobs file: {exc}",
            next_action="Fix the cron jobs file or update the ledger source_of_truth.",
        )

    if job is None:
        return ReconcileItem(
            status="dead_reference",
            source_id=_record_id(record),
            title=_record_title(record),
            severity="problem",
            source_path=str(cron_path),
            evidence=f"cron job {job_id} not found",
            next_action="Retire/remove the ledger record or update it to the current cron job id.",
        )

    last_status = str(job.get("last_status") or "unknown")
    enabled = bool(job.get("enabled", True))
    status = "active_verified" if enabled and last_status != "error" else "runtime_attention"
    severity = "ok" if status == "active_verified" else "attention"
    return ReconcileItem(
        status=status,
        source_id=_record_id(record),
        title=_record_title(record),
        severity=severity,
        source_path=str(cron_path),
        evidence=f"cron job {job_id} enabled={enabled} last_status={last_status}",
        next_action="No action." if status == "active_verified" else "Inspect the cron job and update the ledger verification.",
    )


def _cron_job_ids(source: dict[str, Any]) -> list[str]:
    if source.get("cron_job_id"):
        return [str(source["cron_job_id"])]
    ids = source.get("current_cron_job_ids")
    if isinstance(ids, list):
        return [str(item) for item in ids if item]
    jobs = source.get("cron_jobs")
    if isinstance(jobs, list):
        return [str(job.get("cron_job_id") or job.get("id")) for job in jobs if isinstance(job, dict) and (job.get("cron_job_id") or job.get("id"))]
    return []


def _verify_cron_jobs_record(record: dict[str, Any], source: dict[str, Any], home: Path) -> ReconcileItem:
    ids = _cron_job_ids(source)
    if len(ids) == 1:
        source = dict(source)
        source["cron_job_id"] = ids[0]
        return _verify_cron_record(record, source, home)
    cron_path_raw = source.get("cron_jobs_file")
    cron_path = Path(cron_path_raw) if cron_path_raw else _default_cron_path(source.get("profile"), home)
    if not cron_path.is_absolute():
        cron_path = home / cron_path
    if not ids:
        return _missing_source(record, _ledger_path(home))
    if not cron_path.exists():
        return ReconcileItem(
            status="verification_unavailable",
            source_id=_record_id(record),
            title=_record_title(record),
            severity="attention",
            source_path=str(cron_path),
            evidence="cron jobs file missing for multiple cron job ids",
            next_action="Verify the profile/home path or update the ledger source_of_truth.",
        )
    try:
        jobs = {str(job.get("id")): job for job in _cron_jobs_from_file(cron_path)}
    except (OSError, json.JSONDecodeError) as exc:
        return ReconcileItem(
            status="verification_unavailable",
            source_id=_record_id(record),
            title=_record_title(record),
            severity="attention",
            source_path=str(cron_path),
            evidence=f"could not read cron jobs file: {exc}",
            next_action="Fix the cron jobs file or update the ledger source_of_truth.",
        )
    missing = [job_id for job_id in ids if job_id not in jobs]
    if missing:
        return ReconcileItem(
            status="dead_reference",
            source_id=_record_id(record),
            title=_record_title(record),
            severity="problem",
            source_path=str(cron_path),
            evidence=f"cron jobs missing={','.join(missing)}",
            next_action="Retire/remove missing job ids or update the ledger to current cron ids.",
        )
    bad = [job_id for job_id in ids if not bool(jobs[job_id].get("enabled", True)) or str(jobs[job_id].get("last_status") or "unknown") == "error"]
    status = "active_verified" if not bad else "runtime_attention"
    return ReconcileItem(
        status=status,
        source_id=_record_id(record),
        title=_record_title(record),
        severity="ok" if status == "active_verified" else "attention",
        source_path=str(cron_path),
        evidence=f"cron jobs verified={len(ids)}" if not bad else f"cron jobs need attention={','.join(bad)}",
        next_action="No action." if status == "active_verified" else "Inspect the cron jobs and update ledger verification.",
    )


def _n8n_db_path(home: Path) -> Path:
    # Tests isolate n8n under HERMES_HOME/.n8n; Andy's live runtime uses
    # /home/pi/.n8n/database.sqlite while HERMES_HOME is /home/pi/.hermes.
    hermes_local = home / ".n8n" / "database.sqlite"
    if hermes_local.exists():
        return hermes_local
    return home.parent / ".n8n" / "database.sqlite"


def _verify_n8n_record(record: dict[str, Any], source: dict[str, Any], home: Path) -> ReconcileItem:
    workflow_id = str(source.get("n8n_workflow_id") or "")
    db_path = Path(str(source.get("n8n_db_path") or _n8n_db_path(home)))
    if not db_path.exists():
        return ReconcileItem(
            status="verification_unavailable",
            source_id=_record_id(record),
            title=_record_title(record),
            severity="attention",
            source_path=str(db_path),
            evidence=f"n8n database missing for workflow_id={workflow_id}",
            next_action="Verify n8n DB path or update the ledger source_of_truth.",
        )
    try:
        with sqlite3.connect(db_path) as con:
            row = con.execute(
                "select id, name, active, updatedAt from workflow_entity where id=?",
                (workflow_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        return ReconcileItem(
            status="verification_unavailable",
            source_id=_record_id(record),
            title=_record_title(record),
            severity="attention",
            source_path=str(db_path),
            evidence=f"could not query n8n workflow_entity: {exc}",
            next_action="Verify n8n DB schema/path or update the ledger source_of_truth.",
        )
    if row is None:
        return ReconcileItem(
            status="dead_reference",
            source_id=_record_id(record),
            title=_record_title(record),
            severity="problem",
            source_path=str(db_path),
            evidence=f"n8n workflow {workflow_id} not found",
            next_action="Retire/remove the ledger record or update it to the current workflow id.",
        )
    active = bool(row[2])
    status = "active_verified" if active else "runtime_attention"
    return ReconcileItem(
        status=status,
        source_id=_record_id(record),
        title=_record_title(record),
        severity="ok" if active else "attention",
        source_path=str(db_path),
        evidence=f"n8n workflow {workflow_id} active={active}",
        next_action="No action." if active else "Inspect/activate the n8n workflow or update the ledger.",
    )


def _verification_status(record: dict[str, Any]) -> str | None:
    verification = record.get("verification")
    if isinstance(verification, dict):
        status = verification.get("status") or verification.get("last_status")
        if status:
            return str(status)
    return None


def _verify_manual_record(record: dict[str, Any], ledger_path: Path) -> ReconcileItem | None:
    status = (_verification_status(record) or "").strip().lower()
    if status in {"active", "ok", "active_partial"}:
        return ReconcileItem(
            status="active_verified",
            source_id=_record_id(record),
            title=_record_title(record),
            severity="ok",
            source_path=str(ledger_path),
            evidence=f"manual verification status={status}",
            next_action="No action.",
        )
    return None


def _get_nested(data: dict[str, Any], dotted_key: str) -> Any:
    node: Any = data
    for part in dotted_key.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _config_path_for_profile(home: Path, profile: str) -> Path:
    if profile in {"default", "main"}:
        return home / "config.yaml"
    return home / "profiles" / profile / "config.yaml"


def _verify_gateway_config_record(record: dict[str, Any], source: dict[str, Any], home: Path) -> ReconcileItem:
    key = str(source.get("gateway_config_key") or "")
    profiles = source.get("profiles_enabled") or ["default"]
    if not isinstance(profiles, list):
        profiles = ["default"]
    missing: list[str] = []
    disabled: list[str] = []
    verified = 0
    for profile in [str(p) for p in profiles]:
        path = _config_path_for_profile(home, profile)
        if not path.exists():
            missing.append(profile)
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            missing.append(profile)
            continue
        value = _get_nested(data, key)
        if isinstance(value, dict) and value.get("enabled") is True:
            verified += 1
        elif value is True:
            verified += 1
        else:
            disabled.append(profile)
    if missing or disabled:
        return ReconcileItem(
            status="runtime_attention",
            source_id=_record_id(record),
            title=_record_title(record),
            severity="attention",
            source_path=str(home),
            evidence=f"gateway config key {key} missing={','.join(missing) or 'none'} disabled={','.join(disabled) or 'none'}",
            next_action="Update the gateway config/profile list or mark the ledger record inactive.",
        )
    return ReconcileItem(
        status="active_verified",
        source_id=_record_id(record),
        title=_record_title(record),
        severity="ok",
        source_path=str(home),
        evidence=f"gateway config key {key} profiles verified={verified}",
        next_action="No action.",
    )


def _verify_record(record: dict[str, Any], ledger_path: Path, home: Path) -> ReconcileItem:
    source = _source_dict(record)
    if not source:
        return _missing_source(record, ledger_path)
    if _cron_job_ids(source):
        return _verify_cron_jobs_record(record, source, home)
    if source.get("n8n_workflow_id"):
        return _verify_n8n_record(record, source, home)
    if source.get("gateway_config_key"):
        return _verify_gateway_config_record(record, source, home)
    manual = _verify_manual_record(record, ledger_path)
    if manual is not None:
        return manual
    # Deterministic but conservative: unknown source types are not failures.
    return ReconcileItem(
        status="verification_unavailable",
        source_id=_record_id(record),
        title=_record_title(record),
        severity="attention",
        source_path=str(ledger_path),
        evidence="source_of_truth exists but no supported verifier is available",
        next_action="Add a supported verifier or refresh this record manually.",
    )


def reconcile_active_work(home: Path | None = None) -> ReconcileReport:
    root = home or get_hermes_home()
    ledger_path = _ledger_path(root)
    records = _active_records(_load_ledger_records(ledger_path))
    items = [_verify_record(record, ledger_path, root) for record in records]
    summary = {
        "active_verified": 0,
        "missing_source": 0,
        "dead_reference": 0,
        "runtime_attention": 0,
        "verification_unavailable": 0,
    }
    for item in items:
        summary[item.status] = summary.get(item.status, 0) + 1
    return ReconcileReport(items=items, summary=summary)


def _truncate(value: str, max_len: int) -> str:
    if len(value) <= max_len:
        return value
    if max_len <= 1:
        return "…"[:max_len]
    return value[: max_len - 1].rstrip() + "…"


def _item_to_json(item: ReconcileItem, max_field_chars: int = 160) -> dict[str, str]:
    data = asdict(item)
    for key in ("title", "evidence", "next_action", "source_path"):
        data[key] = _truncate(str(data[key]), max_field_chars)
    return data


def format_reconcile_json(report: ReconcileReport, max_chars: int = 4000) -> str:
    # Keep machine output useful even when tightly bounded: shorter per-field
    # snippets are better than dropping every item.
    field_chars = 160 if max_chars >= 1200 else 72
    compact_items = [_item_to_json(item, max_field_chars=field_chars) for item in report.items]
    payload: dict[str, Any] = {
        "schema": RECONCILE_SCHEMA,
        "summary": report.summary,
        "total_items": len(report.items),
        "truncated": False,
        "items": compact_items,
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    while len(text) > max_chars and compact_items:
        compact_items.pop()
        payload["truncated"] = True
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if len(text) <= max_chars:
        return text
    payload = {
        "schema": RECONCILE_SCHEMA,
        "summary": report.summary,
        "total_items": len(report.items),
        "truncated": True,
        "items": [],
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return _truncate(text, max_chars)


def format_reconcile_text(report: ReconcileReport, max_chars: int = 4000) -> str:
    actionable = [item for item in report.items if item.status != "active_verified"]
    if not actionable:
        return ""
    lines = ["Memory Tree reconcile attention", ""]
    for key, value in report.summary.items():
        if value:
            lines.append(f"{key}: {value}")
    lines.append("")
    for item in actionable:
        lines.extend(
            [
                f"- {item.title} [{item.status}]",
                f"  source_id: {item.source_id}",
                f"  source_path: {item.source_path}",
                f"  evidence: {item.evidence}",
                f"  next: {item.next_action}",
            ]
        )
    text = "\n".join(lines).rstrip() + "\n"
    return _truncate(text, max_chars)
