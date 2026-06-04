"""Deterministic cron definition export.

Hermes' live ``cron/jobs.json`` intentionally mixes durable job definitions with
scheduler runtime state (last/next run timestamps, counters, delivery errors).
This module writes a stable backup projection that keeps only the recoverable job
configuration so git backups do not churn on every scheduler tick.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

DEFINITIONS_FILENAME = "jobs.definitions.json"
EXPORT_VERSION = 1

# Keep field order explicit so exports are reviewable and stable.
_DEFINITION_FIELDS = (
    "id",
    "name",
    "enabled",
    "state",
    "paused_reason",
    "prompt",
    "skills",
    "skill",
    "model",
    "provider",
    "base_url",
    "script",
    "no_agent",
    "context_from",
    "schedule",
    "schedule_display",
    "repeat",
    "deliver",
    "origin",
    "enabled_toolsets",
    "workdir",
    "profile",
    "created_at",
)

_VOLATILE_JOB_FIELDS = {
    "next_run_at",
    "last_run_at",
    "last_status",
    "last_error",
    "last_delivery_error",
    "paused_at",
}


def definitions_path_for_jobs_file(jobs_file: Path) -> Path:
    """Return the default deterministic export path next to ``jobs_file``."""

    return Path(jobs_file).with_name(DEFINITIONS_FILENAME)


def _normalize_repeat(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return {"times": value.get("times")}
    return {"times": value}


def normalize_job_definition(job: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the deterministic, non-runtime definition for one cron job."""

    source = dict(job)
    definition: Dict[str, Any] = {}

    for field in _DEFINITION_FIELDS:
        if field not in source:
            continue
        value = source[field]

        if field == "repeat":
            value = _normalize_repeat(value)
        elif field == "state":
            # ``scheduled`` and ``error`` are scheduler/runtime states. Preserve
            # paused because it is a user-facing durable lifecycle choice.
            if value != "paused":
                continue
        elif field == "paused_reason":
            if not value:
                continue
        else:
            value = copy.deepcopy(value)

        definition[field] = value

    # Future-proofing: never allow known volatile fields to leak even if the
    # ordered allow-list changes later.
    for field in _VOLATILE_JOB_FIELDS:
        definition.pop(field, None)

    return definition


def build_cron_definitions(jobs: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Build the stable export document for a collection of cron jobs."""

    normalized: List[Dict[str, Any]] = [normalize_job_definition(job) for job in jobs]
    normalized.sort(key=lambda item: (str(item.get("id") or ""), str(item.get("name") or "")))
    return {
        "version": EXPORT_VERSION,
        "source": "cron/jobs.json",
        "jobs": normalized,
    }


def render_cron_definitions(jobs: Iterable[Mapping[str, Any]]) -> str:
    """Render the deterministic export JSON."""

    return json.dumps(
        build_cron_definitions(jobs),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def export_cron_definitions(
    jobs: Iterable[Mapping[str, Any]],
    output_path: Path,
    *,
    write_if_changed: bool = True,
) -> bool:
    """Write deterministic cron definitions.

    Returns ``True`` when the file was written and ``False`` when the existing
    file already matched and ``write_if_changed`` avoided a no-op rewrite.
    """

    path = Path(output_path)
    rendered = render_cron_definitions(jobs)
    if write_if_changed and path.exists() and path.read_text(encoding="utf-8") == rendered:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")
    return True
