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
        return SECRET_VALUE_RE.sub("[REDACTED]", value)
    return value


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def compact_text(text: str, limit: int = DEFAULT_MAX_TEXT_CHARS) -> str:
    text = str(text).replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n\n...[truncated {len(text) - limit} chars]"


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
    files = [p for p in root.glob("*/*.md") if p.is_file()]
    files.sort(key=lambda p: (p.stat().st_mtime, str(p)), reverse=True)
    return files[: max(0, limit)]


def collect_cron_records(home: Path, *, limit: int) -> list[SourceRecord]:
    out: list[SourceRecord] = []
    for path in iter_recent_cron_outputs(home, limit):
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if not text or text.startswith("[SILENT]"):
            continue
        job_id = path.parent.name
        out.append(
            SourceRecord(
                source_type="cron-output",
                source_id=f"{job_id}/{path.name}",
                title=f"cron {job_id} {path.stem}",
                timestamp=path.stat().st_mtime,
                text=compact_text(str(redact_secrets(text))),
                metadata={"path": str(path.relative_to(home))},
            )
        )
    return out


def build_memory_tree_packs(options: BuildOptions | None = None) -> dict[str, Any]:
    """Build Memory Tree Lite packs and return the mutable state payload."""

    options = options or BuildOptions()
    home = _home(options)
    data_dir = home / "data" / "memory-tree-lite"
    state_path = data_dir / "state.json"
    session_records = collect_session_records(
        home / "sessions",
        limit_files=options.session_limit,
        include_tools=options.include_tools,
        max_record_chars=options.max_record_chars,
    )
    active_records = collect_active_work_records(home, limit=options.ledger_limit)
    cron_records = collect_cron_records(home, limit=options.cron_limit)
    records = [*session_records, *active_records, *cron_records]

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
        "active_work": len(active_records),
        "cron_outputs": len(cron_records),
    }
    index_md = "\n".join(
        [
            "# Hermes Memory Tree Lite Index",
            "",
            f"records_total: {len(records)}",
            f"sessions: {len(session_records)}",
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
        f"(sessions {counts.get('sessions', 0)}, active-work {counts.get('active_work', 0)}, "
        f"cron {counts.get('cron_outputs', 0)})\n"
        f"recent: {outputs.get('recent', '')}\n"
        f"index: {outputs.get('index', '')}"
    )


__all__ = [
    "BuildOptions",
    "build_memory_tree_packs",
    "collect_active_work_records",
    "collect_cron_records",
    "compact_text",
    "format_build_report",
    "redact_secrets",
    "stable_json",
]
