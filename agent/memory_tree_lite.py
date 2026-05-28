"""Local-first, source-aware memory pack helpers for Hermes.

Memory Tree Lite is deliberately small: it builds deterministic Markdown packs
from existing local Hermes state. It does not replace memory providers or inject
anything into prompts by itself.
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence


@dataclass(frozen=True)
class SourceRecord:
    """A provenance-bearing unit of local context."""

    source_type: str
    source_id: str
    title: str
    timestamp: float | int | None
    text: str
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MemorySearchResult:
    """A ranked Memory Tree Lite match with provenance."""

    score: int
    pack_path: Path
    source_type: str
    source_id: str
    title: str
    timestamp: float | int | None
    snippet: str
    metadata: dict[str, str] = field(default_factory=dict)


def normalize_text(text: str, max_chars: int = 4000) -> str:
    """Normalize record text and cap it with an explicit truncation marker."""

    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n").strip()
    if max_chars < 0:
        max_chars = 0
    if len(normalized) <= max_chars:
        return normalized
    omitted = len(normalized) - max_chars
    return f"{normalized[:max_chars].rstrip()}\n\n[truncated {omitted} chars]"


_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]*")


def _query_terms(query: str) -> list[str]:
    seen: set[str] = set()
    terms: list[str] = []
    for token in _TOKEN_RE.findall(query.lower()):
        if token not in seen:
            seen.add(token)
            terms.append(token)
    return terms


def _source_weight(source_type: str) -> int:
    weights = {"session": 3, "active-work": 2, "cron": 1}
    return weights.get(source_type, 0)


def _score_record(record: SourceRecord, terms: Sequence[str]) -> int:
    if not terms:
        return 0
    title = record.title.lower()
    metadata = "\n".join(f"{key}: {value}" for key, value in record.metadata.items()).lower()
    text = record.text.lower()
    score = 0
    for term in terms:
        if term in title:
            score += 8
        if term in record.source_id.lower():
            score += 5
        if term in metadata:
            score += 3
        text_hits = text.count(term)
        if text_hits:
            score += min(text_hits, 5) * 2
    return score


def _snippet(text: str, terms: Sequence[str], max_chars: int) -> str:
    text = str(text).replace("\r\n", "\n").replace("\r", "\n").strip()
    if max_chars < 0:
        max_chars = 0
    lower = text.lower()
    hit_positions = [lower.find(term) for term in terms if term and lower.find(term) >= 0]
    start = 0
    if hit_positions:
        start = max(min(hit_positions) - max_chars // 4, 0)
    excerpt = text[start : start + max_chars].strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if start + max_chars < len(text) else ""
    return f"{prefix}{excerpt}{suffix}"


def _looks_like_record_header(lines: Sequence[str], idx: int) -> bool:
    if idx >= len(lines) or not lines[idx].startswith("### "):
        return False
    cursor = idx + 1
    metadata_seen = 0
    while cursor < len(lines) and metadata_seen < 12:
        current = lines[cursor]
        if cursor != idx + 1 and (current.startswith("## ") or current.startswith("### ")):
            return False
        if not current.strip():
            cursor += 1
            continue
        if current.startswith("source_id: "):
            return True
        if ": " not in current:
            return False
        metadata_seen += 1
        cursor += 1
    return False


def _section_header_before_record(lines: Sequence[str], idx: int) -> bool:
    if idx >= len(lines) or not lines[idx].startswith("## "):
        return False
    cursor = idx + 1
    while cursor < len(lines):
        current = lines[cursor]
        if _looks_like_record_header(lines, cursor):
            return True
        if current.startswith("## "):
            return False
        if current.strip():
            return False
        cursor += 1
    return False


def _parse_markdown_pack(path: Path) -> list[SourceRecord]:
    """Parse deterministic Memory Tree Lite Markdown back into source records."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []

    records: list[SourceRecord] = []
    source_type = ""
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if _section_header_before_record(lines, idx):
            source_type = line[3:].strip()
            idx += 1
            continue
        if not _looks_like_record_header(lines, idx):
            idx += 1
            continue

        title = line[4:].strip()
        idx += 1
        metadata: dict[str, str] = {}
        body: list[str] = []
        in_body = False
        while idx < len(lines):
            current = lines[idx]
            if _section_header_before_record(lines, idx) or _looks_like_record_header(lines, idx):
                break
            if not in_body and current.strip() == "":
                idx += 1
                continue
            if not in_body and ": " in current:
                key, value = current.split(": ", 1)
                metadata[key.strip()] = value.strip()
                idx += 1
                continue
            in_body = True
            body.append(current)
            idx += 1

        source_id = metadata.pop("source_id", title)
        raw_timestamp = metadata.pop("timestamp", "")
        try:
            timestamp: float | int | None = float(raw_timestamp) if raw_timestamp else None
        except ValueError:
            timestamp = None
            metadata["timestamp"] = raw_timestamp
        records.append(
            SourceRecord(
                source_type=source_type,
                source_id=source_id,
                title=title,
                timestamp=timestamp,
                text="\n".join(body).strip(),
                metadata=metadata,
            )
        )
    return records


def search_memory_packs(
    pack_paths: Sequence[Path | str],
    query: str,
    *,
    limit: int = 5,
    max_snippet_chars: int = 500,
) -> list[MemorySearchResult]:
    """Search generated Memory Tree Lite packs with deterministic lexical ranking."""

    terms = _query_terms(query)
    if not terms or limit <= 0:
        return []

    results: list[MemorySearchResult] = []
    for raw_path in pack_paths:
        path = Path(raw_path).expanduser()
        for record in _parse_markdown_pack(path):
            score = _score_record(record, terms)
            if score <= 0:
                continue
            results.append(
                MemorySearchResult(
                    score=score,
                    pack_path=path,
                    source_type=record.source_type,
                    source_id=record.source_id,
                    title=record.title,
                    timestamp=record.timestamp,
                    snippet=_snippet(record.text, terms, max_snippet_chars),
                    metadata=record.metadata,
                )
            )

    results.sort(
        key=lambda result: (
            -result.score,
            -float(result.timestamp or 0),
            -_source_weight(result.source_type),
            result.source_type,
            result.source_id,
            str(result.pack_path),
        )
    )
    return results[:limit]


def _record_sort_key(record: SourceRecord) -> tuple[str, float, str]:
    timestamp = float(record.timestamp or 0)
    return (record.source_type, timestamp, record.source_id)


def build_markdown_pack(
    records: Sequence[SourceRecord],
    *,
    title: str,
    max_record_chars: int = 4000,
) -> str:
    """Build a deterministic Markdown pack grouped by source type."""

    lines: list[str] = [f"# {title}", ""]
    current_source: str | None = None
    for record in sorted(records, key=_record_sort_key):
        if record.source_type != current_source:
            current_source = record.source_type
            lines.extend([f"## {current_source}", ""])

        lines.extend(
            [
                f"### {record.title or record.source_id}",
                "",
                f"source_id: {record.source_id}",
                f"timestamp: {record.timestamp if record.timestamp is not None else ''}",
            ]
        )
        for key in sorted(record.metadata):
            lines.append(f"{key}: {record.metadata[key]}")
        lines.extend(["", normalize_text(record.text, max_chars=max_record_chars), ""])

    return "\n".join(lines).rstrip() + "\n"


def _message_text(payload: dict[str, Any]) -> str:
    content = payload.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                value = item.get("text") or item.get("content")
                if isinstance(value, str):
                    parts.append(value)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(content)


def collect_session_records(
    sessions_dir: Path,
    *,
    limit_files: int = 20,
    include_tools: bool = False,
    max_record_chars: int = 4000,
) -> list[SourceRecord]:
    """Collect source records from recent Hermes JSONL session files."""

    if not sessions_dir.exists():
        return []

    files = sorted(
        (p for p in sessions_dir.glob("*.jsonl") if p.is_file()),
        key=lambda p: (p.stat().st_mtime, p.name),
        reverse=True,
    )[: max(0, limit_files)]

    records: list[SourceRecord] = []
    for path in files:
        malformed = 0
        accepted: list[SourceRecord] = []
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, 1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError:
                    malformed += 1
                    continue
                if not isinstance(payload, dict):
                    malformed += 1
                    continue

                role = str(payload.get("role") or "")
                if role == "tool" and not include_tools:
                    continue
                if role not in {"user", "assistant", "tool", "system"}:
                    continue

                text = normalize_text(_message_text(payload), max_chars=max_record_chars)
                if not text:
                    continue
                metadata = {
                    "role": role,
                    "file": path.name,
                }
                session_id = payload.get("session_id") or payload.get("session")
                if session_id is not None:
                    metadata["session_id"] = str(session_id)
                timestamp = payload.get("timestamp") or payload.get("created_at")
                title = str(payload.get("title") or f"{role} message")
                accepted.append(
                    SourceRecord(
                        source_type="session",
                        source_id=f"{path.name}:{line_number}",
                        title=title,
                        timestamp=timestamp if isinstance(timestamp, (int, float)) else None,
                        text=text,
                        metadata=metadata,
                    )
                )

        if malformed:
            for idx, record in enumerate(accepted):
                md = dict(record.metadata)
                md["malformed_lines"] = str(malformed)
                accepted[idx] = SourceRecord(
                    record.source_type,
                    record.source_id,
                    record.title,
                    record.timestamp,
                    record.text,
                    md,
                )
        records.extend(accepted)

    return records


def write_pack(path: Path, content: str) -> Path:
    """Write a Markdown pack through an atomic same-directory replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_name = handle.name
            handle.write(content)
        Path(tmp_name).replace(path)
    finally:
        if tmp_name:
            tmp = Path(tmp_name)
            if tmp.exists():
                tmp.unlink()
    return path
