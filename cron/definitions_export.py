"""Deterministic export of Hermes cron job definitions.

The live cron database mixes durable job intent with volatile scheduler state.
This module renders a stable backup artifact that strips runtime fields such as
last/next run timestamps while preserving the actual job definitions.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from utils import atomic_replace

_VOLATILE_JOB_FIELDS = frozenset(
    {
        "last_run_at",
        "next_run_at",
        "last_status",
        "last_error",
        "last_delivery_error",
        "paused_at",
    }
)

_VOLATILE_REPEAT_FIELDS = frozenset({"completed"})


def definitions_path_for_jobs_file(jobs_file: Path | str) -> Path:
    """Return the deterministic definitions-export path for a jobs.json file."""

    path = Path(jobs_file)
    return path.with_name(f"{path.stem}.definitions{path.suffix}")


def normalize_job_definition(job: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *job* without volatile scheduler bookkeeping."""

    cleaned = {
        key: copy.deepcopy(value)
        for key, value in job.items()
        if key not in _VOLATILE_JOB_FIELDS
    }

    repeat = cleaned.get("repeat")
    if isinstance(repeat, dict):
        repeat_cleaned = {
            key: copy.deepcopy(value)
            for key, value in repeat.items()
            if key not in _VOLATILE_REPEAT_FIELDS
        }
        cleaned["repeat"] = repeat_cleaned

    return cleaned


def render_cron_definitions(jobs: Iterable[dict[str, Any]]) -> str:
    """Render cron jobs as deterministic JSON suitable for git backup."""

    definitions = [normalize_job_definition(job) for job in jobs]
    return json.dumps(
        {"jobs": definitions},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def write_cron_definitions(jobs_file: Path | str | None = None) -> Path:
    """Render live cron definitions and atomically write jobs.definitions.json."""

    from cron.jobs import JOBS_FILE, load_jobs

    source = Path(jobs_file) if jobs_file is not None else JOBS_FILE
    destination = definitions_path_for_jobs_file(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_cron_definitions(load_jobs())

    fd, tmp_path = tempfile.mkstemp(
        dir=str(destination.parent),
        suffix=".tmp",
        prefix=f".{destination.name}.",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace(tmp_path, destination)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    try:
        os.chmod(destination, 0o600)
    except (OSError, NotImplementedError):
        pass

    return destination
