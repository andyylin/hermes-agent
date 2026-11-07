"""Fork PRs must resolve runners GitHub Hosted will actually start.

NousResearch runners (``blacksmith-*`` and the 32-core larger labels) are not
registered on this fork. A bare label queues with ``runner_id`` 0 and no
steps. A job-level ``if`` that reads ``matrix`` is rejected before any job
exists (run 42258444666). These checks evaluate the workflow files.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.ci import _gha_expr as gha

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOWS = _REPO / ".github" / "workflows"
_FREE = frozenset({
    "ubuntu-latest",
    "ubuntu-24.04",
    "ubuntu-22.04",
    "windows-latest",
    "macos-latest",
    "macos-15",
    "macos-14",
    "macos-13",
})
_MATRIX_IF = re.compile(r"(^|[^.\w-])matrix\.")


def _yaml(path: Path) -> dict:
    yaml = pytest.importorskip("hermes_yaml")
    return yaml.safe_load(path.read_text(encoding="utf-8-sig"))


def _on(doc: dict) -> dict:
    raw = doc.get("on") or {}
    return raw if isinstance(raw, dict) else {}


def _pull_request_unfiltered(doc: dict) -> bool:
    pull = _on(doc).get("pull_request")
    if pull is None and "pull_request" not in _on(doc):
        return False
    if not isinstance(pull, dict):
        return True
    return "paths" not in pull and "paths-ignore" not in pull


def _called_by_ci() -> set[str]:
    names = set()
    for job in _yaml(_WORKFLOWS / "ci.yaml")["jobs"].values():
        uses = job.get("uses")
        if isinstance(uses, str) and uses.startswith("./.github/workflows/"):
            names.add(uses.rsplit("/", 1)[-1])
    return names


def _reachable() -> list[Path]:
    called = _called_by_ci()
    found = []
    for path in sorted(_WORKFLOWS.glob("*.yml")) + sorted(_WORKFLOWS.glob("*.yaml")):
        doc = _yaml(path)
        if path.name in called or path.name == "ci.yaml" or _pull_request_unfiltered(doc):
            found.append(path)
        elif path.name == "ci-review-comment.yml":
            # workflow_run on CI, read from the default branch once this lands.
            found.append(path)
    return found


def _runner_labels(job: dict) -> list[str]:
    matrix = (job.get("strategy") or {}).get("matrix") or {}
    include = matrix.get("include") or []
    labels = []
    for row in include:
        if not isinstance(row, dict):
            continue
        if isinstance(row.get("runner"), str):
            labels.append(row["runner"])
        target = row.get("target")
        if isinstance(target, dict) and isinstance(target.get("runner"), str):
            labels.append(target["runner"])
    return labels or ["blacksmith-4vcpu-ubuntu-2404"]


def _context(owner: str, label: str) -> dict:
    return {
        "github": {"repository_owner": owner, "event_name": "pull_request"},
        "matrix": {"runner": label, "target": {"runner": label}},
        "inputs": {"runner": label, "bundle-arch": "amd64", "arch": "amd64"},
    }


def test_job_level_if_does_not_read_matrix():
    """GitHub rejects the workflow file when a job ``if`` names ``matrix``."""
    offenders = []
    for path in sorted(_WORKFLOWS.glob("*.yml")) + sorted(_WORKFLOWS.glob("*.yaml")):
        for name, job in (_yaml(path).get("jobs") or {}).items():
            cond = job.get("if")
            if isinstance(cond, str) and _MATRIX_IF.search(cond):
                offenders.append(f"{path.name}:{name}")
    assert offenders == []


def test_fork_pr_runners_are_free_hosted_labels():
    """This fork keeps NousResearch's labels and resolves its own to free runners."""
    paid_on_fork = []
    rewritten_upstream = []
    for path in _reachable():
        for name, job in _yaml(path)["jobs"].items():
            runs_on = job.get("runs-on")
            if not isinstance(runs_on, str):
                continue
            uses_matrix = "matrix." in runs_on or "inputs.runner" in runs_on
            for label in _runner_labels(job):
                fork = gha.render(runs_on, _context("andyylin", label))
                upstream = gha.render(runs_on, _context("NousResearch", label))
                if fork not in _FREE:
                    paid_on_fork.append(f"{path.name}:{name} {label!r} -> {fork!r}")
                if uses_matrix and (
                    label.startswith("blacksmith-") or "windows-latest-32" in label
                ) and upstream != label:
                    rewritten_upstream.append(f"{path.name}:{name} {label!r} -> {upstream!r}")
                if not uses_matrix and "blacksmith-" in runs_on and not str(upstream).startswith("blacksmith-"):
                    rewritten_upstream.append(f"{path.name}:{name} -> {upstream!r}")
    assert paid_on_fork == []
    assert rewritten_upstream == []
