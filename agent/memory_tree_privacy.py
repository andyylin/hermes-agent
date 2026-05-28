"""Deterministic privacy/leak scanner for Memory Tree Lite packs.

This scans generated Memory Tree pack outputs for credential-shaped lines without
printing full secrets back into reports. It is intentionally boring: local files,
bounded findings, redacted snippets, and silence when clean.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

STATE_RELATIVE_PATH = Path("data") / "memory-tree-lite" / "state.json"

_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Z0-9_\-.]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE[_-]?KEY)[A-Z0-9_\-.]*)\b\s*[:=]\s*([^\s`'\"]{8,}|['\"][^'\"]{8,}['\"])",
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/=-]{16,})")
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_LONG_CREDENTIAL_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")


@dataclass(frozen=True)
class PrivacyFinding:
    pack: str
    path: str
    line: int
    kind: str
    severity: str
    snippet: str


@dataclass(frozen=True)
class PrivacyReport:
    findings: list[PrivacyFinding]
    summary: dict[str, int]


def _state_path(home: Path | None = None) -> Path:
    return (home or get_hermes_home()) / STATE_RELATIVE_PATH


def _load_pack_paths(state_path: Path) -> dict[str, Path]:
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    outputs = data.get("outputs") if isinstance(data, dict) else None
    if not isinstance(outputs, dict):
        return {}
    paths: dict[str, Path] = {}
    for name, value in outputs.items():
        if isinstance(value, str):
            paths[str(name)] = Path(value)
    return paths


def _truncate(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    text = " ".join(text.strip().split())
    if len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return "…"[:max_chars]
    return text[: max_chars - 1].rstrip() + "…"


def _redact_line(line: str) -> str:
    redacted = _SECRET_ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}=<redacted>", line)
    redacted = _BEARER_RE.sub("Bearer <redacted>", redacted)
    redacted = _LONG_CREDENTIAL_RE.sub("<redacted>", redacted)
    if _PRIVATE_KEY_RE.search(redacted):
        redacted = _PRIVATE_KEY_RE.sub("-----BEGIN <redacted> PRIVATE KEY-----", redacted)
    return redacted


def _finding_kind(line: str) -> str | None:
    if _PRIVATE_KEY_RE.search(line):
        return "private_key"
    if _SECRET_ASSIGNMENT_RE.search(line):
        return "secret_assignment"
    if _BEARER_RE.search(line):
        return "bearer_token"
    if _LONG_CREDENTIAL_RE.search(line):
        return "credential_like_value"
    return None


def _scan_pack(pack: str, path: Path, max_snippet_chars: int) -> list[PrivacyFinding]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    findings: list[PrivacyFinding] = []
    for line_no, line in enumerate(lines, start=1):
        kind = _finding_kind(line)
        if not kind:
            continue
        findings.append(
            PrivacyFinding(
                pack=pack,
                path=str(path),
                line=line_no,
                kind=kind,
                severity="problem",
                snippet=_truncate(_redact_line(line), max_snippet_chars),
            )
        )
    return findings


def scan_memory_tree_privacy(
    *,
    state_path: Path | None = None,
    max_snippet_chars: int = 160,
) -> PrivacyReport:
    """Scan generated Memory Tree pack outputs for credential-shaped leaks."""
    state = state_path or _state_path()
    findings: list[PrivacyFinding] = []
    for pack, path in sorted(_load_pack_paths(state).items()):
        findings.extend(_scan_pack(pack, path, max_snippet_chars))
    summary = {"findings": len(findings), "packs_scanned": len(_load_pack_paths(state))}
    return PrivacyReport(findings=findings, summary=summary)


def _finding_to_json(finding: PrivacyFinding) -> dict[str, Any]:
    return asdict(finding)


def format_privacy_json(report: PrivacyReport, *, max_chars: int = 4000) -> str:
    payload: dict[str, Any] = {
        "schema": "memory-tree-privacy-v1",
        "summary": report.summary,
        "total_findings": len(report.findings),
        "truncated": False,
        "findings": [],
    }
    for finding in report.findings:
        candidate = dict(payload)
        candidate["findings"] = payload["findings"] + [_finding_to_json(finding)]
        text = json.dumps(candidate, ensure_ascii=False, sort_keys=True)
        if len(text) > max_chars:
            payload["truncated"] = True
            break
        payload = candidate
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if len(text) <= max_chars:
        return text
    # Pathological tiny bound: return a valid minimal JSON object.
    minimal = {
        "schema": "memory-tree-privacy-v1",
        "summary": report.summary,
        "total_findings": len(report.findings),
        "truncated": True,
        "findings": [],
    }
    return json.dumps(minimal, ensure_ascii=False, sort_keys=True)[:max_chars]


def format_privacy_text(report: PrivacyReport, *, max_chars: int = 4000) -> str:
    if not report.findings:
        return ""
    lines = [f"Memory Tree privacy scan found {len(report.findings)} finding(s):"]
    for finding in report.findings:
        lines.append(
            f"- {finding.severity}: {finding.kind} in {finding.pack} line {finding.line} ({finding.path}) — {finding.snippet}"
        )
        text = "\n".join(lines)
        if len(text) > max_chars:
            return _truncate(text, max_chars)
    return "\n".join(lines)
