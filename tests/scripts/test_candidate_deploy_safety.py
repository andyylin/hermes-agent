"""Deployment-safety contracts for an exceptional-lane candidate.

The duplicate-def guard is the regression for the merged cron subject helper,
which was defined twice in the v0.23.0 squash. Citation checks fail when
PATCH-SCORE and the ledger disagree about the freeze, and they flag a newer
score target instead of advancing that freeze.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "docs" / "ops"))

import candidate_deploy_safety as safety
import rpi5_deploy_engine as eng


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "init.defaultBranch=main", *args],
        cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def test_checkout_score_cites_the_ledger_freeze():
    ledger = (REPO / "ANDY_RUNTIME_PATCHES.md").read_text(encoding="utf-8")
    score = (REPO / "PATCH-SCORE.md").read_text(encoding="utf-8")
    assert safety.citation_ok(safety.parse_ledger_freeze(ledger), safety.parse_score(score))


def test_a_score_that_misquotes_the_freeze_fails_citation():
    freeze = safety.Freeze("a" * 40, "v0.22.5")
    score = safety.Score("v0.23.0", "b" * 12, "v0.23.0", "c" * 12, "v0.22.5")
    assert safety.citation_ok(freeze, score) is False


def test_a_newer_score_target_is_flagged_without_failing_the_citation():
    freeze = safety.Freeze("a" * 40, "v0.22.5")
    score = safety.Score("v0.23.0", "b" * 12, "v0.23.0", "a" * 12, "v0.22.5")
    assert safety.citation_ok(freeze, score) is True
    finding = safety._divergence(freeze, "b" * 40)
    assert finding["status"] == "flag"
    assert finding["check"] == "freeze-versus-score"


def test_duplicate_function_defs_are_reported():
    source = "def keep():\n    return 1\n\ndef keep():\n    return 2\n"
    assert safety.duplicate_names(source) == ["keep@[1, 4]"]


def test_scored_python_files_define_each_function_once():
    score = (REPO / "PATCH-SCORE.md").read_text(encoding="utf-8")
    names = [line for line in safety.git(
        REPO, "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines() if line]
    assert safety._missing_scored_py(score, names) == []
    for path in safety.scored_paths(score, names):
        if path.endswith(".py"):
            assert safety.duplicate_names((REPO / path).read_text(encoding="utf-8")) == []


def test_frozen_tag_core_deps_carry_the_ledger_marker():
    ledger = (REPO / "ANDY_RUNTIME_PATCHES.md").read_text(encoding="utf-8")
    freeze = safety.parse_ledger_freeze(ledger)
    pyproject = safety.git(REPO, "show", f"{freeze.sha}:pyproject.toml").stdout
    assert safety.MARKER in ledger
    assert safety.marker_gaps(pyproject) == []


def test_a_core_dep_without_the_marker_is_a_gap():
    text = '[project]\ndependencies = ["plain==1.0.0", "marked==1.0.0; python_version >= \'3.14\'"]\n'
    assert safety.marker_gaps(text) == ["plain==1.0.0"]


def test_python_below_3_14_cannot_satisfy_core_markers():
    assert safety.interpreter_satisfies_core_markers((3, 12, 3)) is False
    assert safety.interpreter_satisfies_core_markers((3, 14, 0)) is True


def test_dependency_manifests_stay_manual_and_match_pm():
    paths = ["pyproject.toml", "uv.lock", "pm/lock.json", "gateway/run.py"]
    finding = safety.manual_gate(paths)
    assert finding["status"] == "pass"
    assert finding["detail"]["kind"] == "manual"
    assert set(eng.DEPENDENCY_INPUTS) == set(safety.ACTIVATION_INPUTS)


def test_ledger_to_head_dependency_delta_stays_manual():
    freeze = safety.parse_ledger_freeze((REPO / "ANDY_RUNTIME_PATCHES.md").read_text(encoding="utf-8"))
    paths = [line for line in safety.git(
        REPO, "diff", "--name-only", "--no-renames", freeze.sha, "HEAD").stdout.splitlines() if line]
    finding = safety.manual_gate(paths)
    assert finding["status"] == "pass"
    if any(path in eng.DEPENDENCY_INPUTS for path in paths):
        assert finding["detail"]["kind"] == "manual"


def test_prune_keeps_the_release_being_rolled_back_to(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(checkout, "init", "-q")
    shas = []
    for index in range(3):
        (checkout / "f").write_text(f"{index}\n", encoding="utf-8")
        _git(checkout, "add", "-A")
        _git(checkout, "commit", "-q", "-m", str(index))
        shas.append(_git(checkout, "rev-parse", "HEAD"))
    home = tmp_path / "home"
    home.mkdir()
    config = eng.DeployConfig(checkout=checkout, home=home)
    releases = config.releases_dir
    releases.mkdir(parents=True)
    for sha in shas:
        _git(checkout, "worktree", "add", "--detach", str(releases / sha), sha)
    deployer = eng.Deployer(config, eng.Host(config), eng.StateStore(config.deploy_dir))
    current, extra, target = shas
    deployer._prune_releases(keep={current, target})
    assert (releases / current).is_dir()
    assert (releases / target).is_dir()
    assert not (releases / extra).exists()


def test_schema_version_reads_the_default_config_stamp():
    source = "DEFAULT_CONFIG = {'_config_version': 51, 'model': {}}\n"
    assert safety.schema_version(source) == 51


def test_squash_candidate_flags_score_ancestry_without_dropping_the_finding():
    """A one-parent squash is not a descendant of the scored tag; that is a flag."""
    freeze = safety.parse_ledger_freeze(
        (REPO / "ANDY_RUNTIME_PATCHES.md").read_text(encoding="utf-8"))
    score_sha = safety._commit_for_prefix(REPO, "2bd378ed", "v0.23.0")
    finding = safety._ancestry_finding(
        REPO, "ab2bbde6afc428a85e74a8b39a76754fcac3745b", freeze.sha, score_sha)
    assert finding["status"] == "flag"
    assert finding["check"] == "ancestry"


def test_step_summary_records_flags(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    safety._print_summary({
        "sha": "abc123",
        "findings": [{
            "area": "ledger",
            "check": "freeze-versus-score",
            "status": "flag",
            "summary": "left for a human",
        }],
    })
    text = summary.read_text(encoding="utf-8")
    assert "abc123" in text
    assert "flag" in text
    assert "manual deploy gate" in text


def test_squash_candidate_duplicates_the_cron_subject_helper():
    """The guard is red on the candidate squash and clean after the fix."""
    blob = safety.git(
        REPO, "show", "ab2bbde6afc428a85e74a8b39a76754fcac3745b:cron/scheduler_delivery.py").stdout
    assert any(item.startswith("_format_cron_email_subject@") for item in safety.duplicate_names(blob))
    current = (REPO / "cron" / "scheduler_delivery.py").read_text(encoding="utf-8")
    assert safety.duplicate_names(current) == []
