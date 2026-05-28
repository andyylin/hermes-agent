"""Deterministic Memory Tree attention scanning.

This is the Hermes-native "subconscious" slice: cheap local scans surface stale
or failed assistant-owned work without waking an LLM. Empty output means no
attention needed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from hermes_constants import get_hermes_home

LEDGER_PATH = Path("data") / "active-work" / "ledger.json"
ATTENTION_STATUSES = {"active", "waiting", "blocked", "in_progress", "monitoring"}
DONE_STATUSES = {
    "completed",
    "completed_removed",
    "inactive",
    "inactive_tests",
    "archived_inactive",
    "stopped",
    "cancelled",
}
FAILED_MARKERS = {"fail", "failed", "failure", "error", "errored", "critical", "broken"}


@dataclass(frozen=True)
class AttentionItem:
    kind: str
    severity: str
    title: str
    source_type: str
    source_id: str
    source_path: Path
    age: str
    evidence: str
    next_action: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "title": self.title,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "source_path": str(self.source_path),
            "age": self.age,
            "evidence": self.evidence,
            "next_action": self.next_action,
        }


def _ledger_path() -> Path:
    return get_hermes_home() / LEDGER_PATH


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _record_time(record: dict[str, Any]) -> datetime | None:
    for key in ("updated_at", "last_checked_at", "created_at", "timestamp"):
        dt = _parse_dt(record.get(key))
        if dt:
            return dt
    runtime = record.get("runtime")
    if isinstance(runtime, dict):
        for key in ("last_run_at", "last_checked_at", "updated_at"):
            dt = _parse_dt(runtime.get(key))
            if dt:
                return dt
    verification = record.get("verification")
    if isinstance(verification, dict):
        for key in ("checked_at", "updated_at", "last_run_at"):
            dt = _parse_dt(verification.get(key))
            if dt:
                return dt
    return None


def _age(now: datetime, dt: datetime | None) -> tuple[float, str]:
    if not dt:
        return 10**9, "unknown"
    seconds = max((now - dt).total_seconds(), 0)
    days = seconds / 86400
    if days >= 1:
        return days, f"{int(days)}d"
    hours = seconds / 3600
    if hours >= 1:
        return days, f"{int(hours)}h"
    return days, f"{int(seconds // 60)}m"


def _stringify(value: Any) -> str:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return "" if value is None else str(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def _contains_failure_marker(*values: Any) -> bool:
    haystack = " ".join(_stringify(v).lower() for v in values)
    return any(marker in haystack for marker in FAILED_MARKERS)


def _status_values(*containers: Any) -> list[Any]:
    values: list[Any] = []
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ("status", "last_status", "state", "result", "outcome"):
            if key in container:
                values.append(container.get(key))
    return values


def _error_values(*containers: Any) -> list[Any]:
    values: list[Any] = []
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ("last_error", "error", "exception", "traceback"):
            if container.get(key):
                values.append(container.get(key))
    return values


def _short(text: str, limit: int = 280) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return text[:limit].rstrip() + f"…[{omitted} chars omitted]"


def _record_id(record: dict[str, Any], index: int) -> str:
    return str(record.get("id") or record.get("title") or f"ledger-record-{index}")


def _has_source_of_truth(record: dict[str, Any], runtime: dict[str, Any], verification: dict[str, Any]) -> bool:
    if str(record.get("source_of_truth") or "").strip():
        return True
    if str(record.get("source") or "").strip():
        return True
    if runtime.get("cron_job_id") or runtime.get("systemd_unit") or runtime.get("n8n_workflow_id"):
        return True
    if verification.get("source") or verification.get("command"):
        return True
    return False


def _scan_ledger_records(records: Iterable[dict[str, Any]], *, now: datetime, stale_days: int, source_path: Path) -> list[AttentionItem]:
    items: list[AttentionItem] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        record_id = _record_id(record, index)
        title = str(record.get("title") or record_id)
        status = str(record.get("status") or "").strip().lower()
        raw_runtime = record.get("runtime")
        raw_verification = record.get("verification")
        runtime: dict[str, Any] = raw_runtime if isinstance(raw_runtime, dict) else {}
        verification: dict[str, Any] = raw_verification if isinstance(raw_verification, dict) else {}

        failure_values = [status, *_status_values(runtime, verification), *_error_values(record, runtime, verification)]
        if _contains_failure_marker(*failure_values):
            evidence_parts = [f"status={status or 'unknown'}"]
            cron_job_id = runtime.get("cron_job_id") if isinstance(runtime, dict) else None
            if cron_job_id:
                evidence_parts.append(f"cron_job_id={cron_job_id}")
            for key in ("last_status", "last_error", "error", "message"):
                if isinstance(runtime, dict) and runtime.get(key):
                    evidence_parts.append(f"runtime.{key}={runtime[key]}")
                if isinstance(verification, dict) and verification.get(key):
                    evidence_parts.append(f"verification.{key}={verification[key]}")
            items.append(
                AttentionItem(
                    kind="failed_automation",
                    severity="failure",
                    title=title,
                    source_type="active-work",
                    source_id=record_id,
                    source_path=source_path,
                    age=_age(now, _record_time(record))[1],
                    evidence=_short("; ".join(evidence_parts)),
                    next_action="Inspect the automation logs/source of truth, repair the failure, then update the ledger verification snapshot.",
                )
            )
            continue

        if status in DONE_STATUSES:
            continue
        if status and status not in ATTENTION_STATUSES:
            continue
        record_dt = _record_time(record)
        if not _has_source_of_truth(record, runtime, verification) and record_dt is None:
            items.append(
                AttentionItem(
                    kind="missing_source_of_truth",
                    severity="attention",
                    title=title,
                    source_type="active-work",
                    source_id=record_id,
                    source_path=source_path,
                    age=_age(now, record_dt)[1],
                    evidence="active ledger record has no source_of_truth, runtime source ID, or verification command",
                    next_action="Add a concrete source_of_truth/runtime ID if this is alive, or complete/remove the stale ledger record.",
                )
            )
            continue
        days, age = _age(now, record_dt)
        if days >= stale_days:
            purpose = _short(str(record.get("purpose") or record.get("summary") or ""), 180)
            evidence = f"status={status or 'unknown'}; age={age}"
            if purpose:
                evidence += f"; purpose={purpose}"
            items.append(
                AttentionItem(
                    kind="stale_active_work",
                    severity="attention",
                    title=title,
                    source_type="active-work",
                    source_id=record_id,
                    source_path=source_path,
                    age=age,
                    evidence=evidence,
                    next_action="Review whether this is still active, complete/remove it, or refresh the ledger with the current blocker and next check.",
                )
            )
    return items


def scan_attention(*, now: datetime | None = None, stale_days: int = 7) -> list[AttentionItem]:
    """Return deterministic attention items from local Hermes state."""

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ledger_path = _ledger_path()
    ledger = _load_json(ledger_path)
    raw_records = ledger.get("records")
    records: list[dict[str, Any]] = raw_records if isinstance(raw_records, list) else []
    items = _scan_ledger_records(records, now=now, stale_days=stale_days, source_path=ledger_path)
    severity_rank = {"failure": 0, "attention": 1, "info": 2}
    return sorted(items, key=lambda item: (severity_rank.get(item.severity, 9), item.kind, item.source_id))


def format_attention_report(items: list[AttentionItem], *, max_chars: int = 4000) -> str:
    """Format a compact report. Empty string means silent no-op."""

    if not items:
        return ""
    lines = [f"Memory Tree attention: {len(items)} item(s)"]
    for idx, item in enumerate(items, start=1):
        lines.extend(
            [
                "",
                f"{idx}. [{item.severity}] {item.title}",
                f"   kind: {item.kind}",
                f"   source: {item.source_type} / {item.source_id}",
                f"   path: {item.source_path}",
                f"   age: {item.age}",
                f"   next: {item.next_action}",
                f"   why: {item.evidence}",
            ]
        )
    report = "\n".join(lines).rstrip()
    if len(report) <= max_chars:
        return report
    omitted = len(report) - max_chars
    marker = f"\n...[truncated {omitted} chars]"
    budget = max(max_chars - len(marker), 0)
    return report[:budget].rstrip() + marker


def _bounded_json(payload: dict[str, Any], *, max_chars: int) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if len(text) <= max_chars:
        return text
    payload = dict(payload)
    payload["truncated"] = True
    items = list(payload.get("items") or [])
    payload["items"] = items
    for item in items:
        if isinstance(item, dict):
            item["title"] = _short(str(item.get("title") or ""), 80)
            item["evidence"] = _short(str(item.get("evidence") or ""), 80)
            item["next_action"] = _short(str(item.get("next_action") or ""), 80)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if len(text) <= max_chars:
        return text
    compact_items = []
    for item in items:
        if isinstance(item, dict):
            compact_items.append(
                {
                    "kind": item.get("kind"),
                    "severity": item.get("severity"),
                    "source_id": item.get("source_id"),
                    "source_path": item.get("source_path"),
                    "age": item.get("age"),
                }
            )
    payload["items"] = compact_items
    while compact_items and len(json.dumps(payload, ensure_ascii=False, sort_keys=True)) > max_chars:
        compact_items.pop()
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if len(text) <= max_chars:
        return text
    payload["items"] = []
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if len(text) <= max_chars:
        return text
    return json.dumps({"schema": payload.get("schema"), "total_items": payload.get("total_items", 0), "truncated": True}, sort_keys=True)


def format_attention_json(items: list[AttentionItem], *, max_chars: int = 4000) -> str:
    """Format attention items as bounded machine-readable JSON."""

    payload: dict[str, Any] = {
        "schema": "memory-tree-attention-v1",
        "total_items": len(items),
        "truncated": False,
        "items": [item.to_dict() for item in items],
    }
    return _bounded_json(payload, max_chars=max_chars)


def scan_and_format_attention(*, stale_days: int = 7, max_chars: int = 4000) -> str:
    return format_attention_report(scan_attention(stale_days=stale_days), max_chars=max_chars)


__all__ = [
    "AttentionItem",
    "format_attention_json",
    "format_attention_report",
    "scan_and_format_attention",
    "scan_attention",
]
