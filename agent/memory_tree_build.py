"""Repo-backed Memory Tree Lite build helpers.

The cron script and CLI use this module so Memory Tree generation is testable,
profile-aware, deterministic, and consistently redacted.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from hermes_constants import get_hermes_home
from agent.memory_tree_lite import SourceRecord, build_markdown_pack, collect_session_records, write_pack

try:  # pragma: no cover - zoneinfo exists on supported runtimes
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

DEFAULT_TZ = "Asia/Taipei"
DEFAULT_MAX_TEXT_CHARS = 4000
SECRET_KEY_RE = re.compile(r"(secret|token|password|passwd|api[_-]?key|authorization|auth|credential|cookie)", re.I)
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?im)\b([A-Z0-9_.-]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE[_-]?KEY)[A-Z0-9_.-]*)\b(\s*[:=]\s*)([^\s`'\"]+|'[^'\n]+'|\"[^\"\n]+\")"
)
SECRET_VALUE_RE = re.compile(
    r"(?i)(bearer\s+[a-z0-9._~+/=-]{12,}|xox[baprs]-[a-z0-9-]{12,}|sk-[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9_]{16,})"
)


@dataclass(frozen=True)
class BuildOptions:
    hermes_home: Path | None = None
    session_limit: int = 40
    cron_limit: int = 30
    ledger_limit: int = 80
    max_record_chars: int = DEFAULT_MAX_TEXT_CHARS
    include_tools: bool = False
    legacy_session_fallback: bool = False
    timezone_name: str = DEFAULT_TZ


def _home(options: BuildOptions) -> Path:
    return (options.hermes_home or get_hermes_home()).expanduser()


def _now_local(timezone_name: str = DEFAULT_TZ) -> datetime:
    if ZoneInfo is None:
        return datetime.now().astimezone()
    return datetime.now(ZoneInfo(timezone_name))


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
            tmp.write(text)
            tmp_name = tmp.name
        Path(tmp_name).replace(path)
    finally:
        if tmp_name:
            tmp_path = Path(tmp_name)
            if tmp_path.exists():
                tmp_path.unlink()


def redact_secrets(value: Any) -> Any:
    """Recursively redact obvious secret keys and token-like values."""

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if SECRET_KEY_RE.search(str(key)):
                out[str(key)] = "[REDACTED]"
            else:
                out[str(key)] = redact_secrets(item)
        return out
    if isinstance(value, list):
        return [redact_secrets(item) for item in value[:50]]
    if isinstance(value, str):
        redacted = SECRET_ASSIGNMENT_RE.sub("[REDACTED_SECRET_ASSIGNMENT]", value)
        return SECRET_VALUE_RE.sub("[REDACTED]", redacted)
    return value


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def compact_text(text: str, limit: int = DEFAULT_MAX_TEXT_CHARS) -> str:
    text = str(text).replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n\n...[truncated {len(text) - limit} chars]"


def _redact_record(record: SourceRecord, *, max_chars: int) -> SourceRecord:
    return SourceRecord(
        source_type=record.source_type,
        source_id=record.source_id,
        title=record.title,
        timestamp=record.timestamp,
        text=compact_text(str(redact_secrets(record.text)), max_chars),
        metadata={str(key): str(redact_secrets(value)) for key, value in record.metadata.items()},
    )


def collect_active_work_records(home: Path, *, limit: int) -> list[SourceRecord]:
    ledger = _load_json(home / "data" / "active-work" / "ledger.json", {})
    records = ledger.get("records", []) if isinstance(ledger, dict) else []
    if not isinstance(records, list):
        return []

    def score(item: dict[str, Any]) -> tuple[int, str]:
        status = str(item.get("status", ""))
        activeish = status in {"active", "scheduled", "scheduled_once", "enabled", "monitoring"}
        return (0 if activeish else 1, str(item.get("id", "")))

    out: list[SourceRecord] = []
    for item in sorted((r for r in records if isinstance(r, dict)), key=score)[: max(0, limit)]:
        source_id = str(item.get("id") or item.get("title") or "active-work-record")
        title = str(item.get("title") or source_id)
        body = {
            "id": source_id,
            "type": item.get("type"),
            "status": item.get("status"),
            "owner": item.get("owner"),
            "purpose": item.get("purpose"),
            "source_of_truth": item.get("source_of_truth"),
            "runtime": item.get("runtime"),
            "failure_behavior": item.get("failure_behavior"),
            "verification": item.get("verification"),
        }
        out.append(
            SourceRecord(
                source_type="active-work",
                source_id=source_id,
                title=title,
                timestamp=None,
                text=compact_text(stable_json(redact_secrets(body))),
                metadata={"ledger": "data/active-work/ledger.json", "status": str(item.get("status", ""))},
            )
        )
    return out


def iter_recent_cron_outputs(home: Path, limit: int) -> Iterable[Path]:
    root = home / "cron" / "output"
    if not root.exists():
        return []
    files: list[tuple[float, str, Path]] = []
    for path in root.glob("*/*.md"):
        try:
            if not path.is_file():
                continue
            files.append((path.stat().st_mtime, str(path), path))
        except FileNotFoundError:
            # Cron output can be pruned while the memory tree build is scanning.
            # A vanished record is not build-fatal; skip it and keep the pack fresh.
            continue
    files.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [path for _, _, path in files[: max(0, limit)]]


def collect_cron_records(home: Path, *, limit: int) -> list[SourceRecord]:
    out: list[SourceRecord] = []
    for path in iter_recent_cron_outputs(home, limit):
        try:
            stat = path.stat()
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except FileNotFoundError:
            # The output disappeared between discovery and read; skip the stale path.
            continue
        if not text or text.startswith("[SILENT]"):
            continue
        job_id = path.parent.name
        out.append(
            SourceRecord(
                source_type="cron-output",
                source_id=f"{job_id}/{path.name}",
                title=f"cron {job_id} {path.stem}",
                timestamp=stat.st_mtime,
                text=compact_text(str(redact_secrets(text))),
                metadata={"path": str(path.relative_to(home))},
            )
        )
    return out


def _parse_archive_frontmatter(lines: list[str]) -> tuple[dict[str, Any], int]:
    if not lines or lines[0].strip() != "---":
        return {}, 0
    end = next((idx for idx in range(1, len(lines)) if lines[idx].strip() == "---"), -1)
    if end < 0:
        return {}, 0
    values: dict[str, Any] = {}
    for line in lines[1:end]:
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        raw = raw.strip()
        try:
            values[key.strip()] = json.loads(raw)
        except json.JSONDecodeError:
            values[key.strip()] = raw.strip("\\\"'")
    return values, end + 1


def _bounded_archive_text(text: str, limit: int) -> str:
    text = text.strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    marker = "\n\n...[truncated]"
    return text[: max(0, limit - len(marker))].rstrip() + marker


def _archive_message_text(lines: list[str], start: int, *, max_chars: int) -> str:
    """Keep only user/assistant prose from an exported Markdown session."""
    parts: list[str] = []
    role: str | None = None
    message_lines: list[str] = []

    def flush() -> None:
        if role and message_lines:
            visible_lines = message_lines
            if role == "user":
                visible_lines = [
                    line
                    for line in message_lines
                    if not line.startswith(
                        "[IMPORTANT: You are running as a scheduled cron job."
                    )
                ]
            content = "\n".join(visible_lines).strip()
            if content:
                parts.append(f"{role.capitalize()}: {content}")

    for line in lines[start:]:
        if line.startswith("## Export verification"):
            break
        if line.startswith("### "):
            flush()
            label = line[4:].split(" — ", 1)[0].strip().lower()
            role = label if label in {"user", "assistant"} else None
            message_lines = []
            continue
        if line.startswith("## "):
            flush()
            role = None
            message_lines = []
            continue
        if role:
            message_lines.append(line)
    flush()
    conversation = "\n\n".join(parts)
    if conversation:
        conversation = "Conversation excerpt\n\n" + conversation
    return _bounded_archive_text(conversation, max_chars)


def _archive_path(manifest: Path, raw_path: Any) -> Path | None:
    """Resolve a manifest path only when it stays inside the export root."""
    if not isinstance(raw_path, str) or not raw_path:
        return None
    try:
        root = manifest.parent.resolve()
        path = Path(raw_path).expanduser()
        candidate = path.resolve() if path.is_absolute() else (root / path).resolve()
        candidate.relative_to(root)
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def collect_verified_archive_records(
    home: Path,
    *,
    limit: int = 40,
    max_record_chars: int = DEFAULT_MAX_TEXT_CHARS,
) -> list[SourceRecord]:
    """Collect one bounded record per SHA-verified Markdown archive session."""
    manifest = home / "session-exports" / "manifest.jsonl"
    if limit <= 0:
        return []
    try:
        lines = manifest.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []

    entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        session_id = str(entry.get("session_id") or "").strip()
        if not session_id or session_id in seen_ids:
            continue
        seen_ids.add(session_id)
        entries.append(entry)

    records: list[SourceRecord] = []
    for entry in entries:
        archive = _archive_path(manifest, entry.get("path"))
        expected_sha = str(entry.get("sha256") or "").lower()
        if archive is None or len(expected_sha) != 64:
            continue
        try:
            archive_bytes = archive.read_bytes()
            if hashlib.sha256(archive_bytes).hexdigest() != expected_sha:
                continue
            archive_lines = archive_bytes.decode("utf-8").splitlines()
        except (FileNotFoundError, OSError, UnicodeError):
            continue
        frontmatter, body_start = _parse_archive_frontmatter(archive_lines)
        session_id = str(entry.get("session_id") or frontmatter.get("session_id") or "")
        title = str(frontmatter.get("title") or session_id)
        text = _archive_message_text(archive_lines, body_start, max_chars=max_record_chars)
        timestamp = entry.get("exported_at")
        if not isinstance(timestamp, (int, float)):
            timestamp = None
        records.append(
            SourceRecord(
                source_type="session-archive",
                source_id=session_id,
                title=title,
                timestamp=timestamp,
                text=text,
                metadata={
                    "archive_path": str(archive),
                    "manifest": str(manifest),
                    "sha256": expected_sha,
                    "verified": "true",
                    "format": str(entry.get("format") or "md"),
                    "source": str(frontmatter.get("source") or ""),
                    "created_at": str(frontmatter.get("created_at") or ""),
                    "lineage_session_ids": json.dumps(
                        entry.get("lineage_session_ids") or [session_id],
                        ensure_ascii=False,
                    ),
                },
            )
        )
        if len(records) >= limit:
            break
    return records


def build_memory_tree_packs(options: BuildOptions | None = None) -> dict[str, Any]:
    """Build Memory Tree Lite packs and return the mutable state payload."""

    options = options or BuildOptions()
    home = _home(options)
    data_dir = home / "data" / "memory-tree-lite"
    state_path = data_dir / "state.json"
    session_records = collect_verified_archive_records(
        home, limit=options.session_limit, max_record_chars=options.max_record_chars
    )
    legacy_session_records: list[SourceRecord] = []
    if options.legacy_session_fallback and not session_records:
        legacy_session_records = collect_session_records(
            home / "sessions",
            limit_files=options.session_limit,
            include_tools=options.include_tools,
            max_record_chars=options.max_record_chars,
        )
    active_records = collect_active_work_records(home, limit=options.ledger_limit)
    cron_records = collect_cron_records(home, limit=options.cron_limit)
    records = [
        *(_redact_record(record, max_chars=options.max_record_chars) for record in session_records),
        *(_redact_record(record, max_chars=options.max_record_chars) for record in legacy_session_records),
        *(_redact_record(record, max_chars=options.max_record_chars) for record in active_records),
        *(_redact_record(record, max_chars=options.max_record_chars) for record in cron_records),
    ]

    recent_md = build_markdown_pack(
        records,
        title="Hermes Memory Tree Lite - Recent",
        max_record_chars=options.max_record_chars,
    )
    today = _now_local(options.timezone_name).date().isoformat()
    daily_md = build_markdown_pack(
        records,
        title=f"Hermes Memory Tree Lite - {today}",
        max_record_chars=options.max_record_chars,
    )
    outputs = {
        "recent": data_dir / "recent.md",
        "daily": data_dir / "daily" / f"{today}.md",
    }
    contents = {"recent": recent_md, "daily": daily_md}
    previous = _load_json(state_path, {})
    previous_hashes = previous.get("hashes", {}) if isinstance(previous, dict) else {}

    changed: list[str] = []
    hashes: dict[str, str] = {}
    for name, content in contents.items():
        digest = _sha256_text(content)
        hashes[name] = digest
        if previous_hashes.get(name) != digest or not outputs[name].exists():
            write_pack(outputs[name], content)
            changed.append(name)

    counts = {
        "records_total": len(records),
        "sessions": len(session_records),
        "session_archives": len(session_records),
        "legacy_sessions": len(legacy_session_records),
        "active_work": len(active_records),
        "cron_outputs": len(cron_records),
    }
    index_md = "\n".join(
        [
            "# Hermes Memory Tree Lite Index",
            "",
            f"records_total: {len(records)}",
            f"sessions: {len(session_records)}",
            f"session_archives: {len(session_records)}",
            f"legacy_sessions: {len(legacy_session_records)}",
            f"active_work: {len(active_records)}",
            f"cron_outputs: {len(cron_records)}",
            "",
            "## Packs",
            "",
            *[f"- {name}: `{path.relative_to(home)}`" for name, path in outputs.items()],
            "",
        ]
    )
    index_path = data_dir / "index.md"
    index_hash = _sha256_text(index_md)
    hashes["index"] = index_hash
    if previous_hashes.get("index") != index_hash or not index_path.exists():
        write_pack(index_path, index_md)
        changed.append("index")

    state = {
        "schema": "memory-tree-lite-state-v1",
        "updated_at": _now_local(options.timezone_name).isoformat(timespec="seconds"),
        "hashes": hashes,
        "outputs": {name: str(path) for name, path in {**outputs, "index": index_path}.items()},
        "counts": counts,
        "changed": changed,
    }
    _write_json_atomic(state_path, state)
    return state


def format_build_report(state: dict[str, Any]) -> str:
    changed = ", ".join(state.get("changed") or ["none"])
    counts = state.get("counts", {}) if isinstance(state.get("counts"), dict) else {}
    outputs = state.get("outputs", {}) if isinstance(state.get("outputs"), dict) else {}
    return (
        "Memory Tree Lite build complete\n"
        f"changed: {changed}\n"
        f"records: {counts.get('records_total', 0)} "
        f"(session archives {counts.get('session_archives', counts.get('sessions', 0))}, "
        f"legacy sessions {counts.get('legacy_sessions', 0)}, active-work {counts.get('active_work', 0)}, "
        f"cron {counts.get('cron_outputs', 0)})\n"
        f"recent: {outputs.get('recent', '')}\n"
        f"index: {outputs.get('index', '')}"
    )


__all__ = [
    "BuildOptions",
    "build_memory_tree_packs",
    "collect_active_work_records",
    "collect_cron_records",
    "collect_verified_archive_records",
    "compact_text",
    "format_build_report",
    "redact_secrets",
    "stable_json",
]
