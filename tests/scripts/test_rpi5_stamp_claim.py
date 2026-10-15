"""Claim an external rpi5 checkout as PM-managed, and roll that claim back.

Fixtures use a marker string in the stamp. They must not contain a credential,
and the helper's stdout must not print the stamp body.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "docs" / "ops"))

import rpi5_migration as mig

MARKER = "external-venv"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
        env={
            "HOME": str(repo.parent),
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_NOSYSTEM": "1",
        },
    )
    return result.stdout.strip()


def _checkout(tmp_path: Path, *, commit: bool = True) -> Path:
    repo = tmp_path / "hermes-agent"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Hermes Test")
    _git(repo, "config", "user.email", "hermes@example.invalid")
    (repo / "pyproject.toml").write_text("[project]\nname = 'hermes'\n", encoding="utf-8")
    if not commit:
        return repo
    (repo / "tracked").write_text("release\n", encoding="utf-8")
    _git(repo, "add", "pyproject.toml", "tracked")
    _git(repo, "commit", "-qm", "release")
    return repo


def _write_stamp(repo: Path, mechanism: str) -> bytes:
    body = {
        "schemaVersion": 2,
        "updateMechanism": mechanism,
        "source": "local",
        "distribution": None,
        "commit": "b" * 40,
        "marker": MARKER,
    }
    payload = (json.dumps(body, indent=2) + "\n").encode()
    (repo / mig.STAMP_NAME).write_bytes(payload)
    return payload


def _allow_store_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "hermes_cli._launchers.resolve_store_python",
        lambda _root, publication=False: Path("/managed/python"),
    )


def _spy_publish(monkeypatch: pytest.MonkeyPatch, *, create_launcher: bool = True, boom: bool = False) -> dict:
    seen: dict = {}

    def publish(project_root, *, create=True):
        if boom:
            raise RuntimeError("publish boom")
        stamp = json.loads((Path(project_root) / mig.STAMP_NAME).read_text(encoding="utf-8"))
        seen["mechanism"] = stamp["updateMechanism"]
        seen["create"] = create
        if not create_launcher:
            return
        launcher = Path(project_root) / ".hermes" / "bin" / "hermes"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        launcher.chmod(0o755)

    monkeypatch.setattr("hermes_cli.venv_sync.publish_launchers", publish)
    return seen


def test_external_stamp_becomes_the_managed_stamp_and_publishes_the_launcher(tmp_path, monkeypatch):
    repo = _checkout(tmp_path)
    original = _write_stamp(repo, "external")
    _allow_store_python(monkeypatch)
    seen = _spy_publish(monkeypatch)

    message = mig.claim_external_checkout(repo)

    stamp = json.loads((repo / mig.STAMP_NAME).read_text(encoding="utf-8"))
    assert stamp["updateMechanism"] == "self"
    assert stamp["source"] == "git"
    assert stamp["schemaVersion"] == 2
    assert stamp["payload"] == "bootstrap"
    assert stamp["distribution"] is None
    assert stamp["commit"] == _git(repo, "rev-parse", "HEAD")
    assert "adoptedAt" not in stamp
    assert MARKER not in json.dumps(stamp)
    assert (repo / mig.STAMP_BACKUP_NAME).read_bytes() == original
    assert seen == {"mechanism": "self", "create": False}
    launcher = repo / ".hermes" / "bin" / "hermes"
    assert launcher.is_file() and os.access(launcher, os.X_OK)
    assert str(launcher) in message
    assert MARKER not in message

    again = (repo / mig.STAMP_NAME).read_bytes()
    second = mig.claim_external_checkout(repo)
    assert second.startswith("already managed:")
    assert (repo / mig.STAMP_NAME).read_bytes() == again


@pytest.mark.parametrize("mechanism", ["self", "electron-updater"])
def test_claim_leaves_a_non_external_stamp_unchanged(tmp_path, monkeypatch, mechanism):
    repo = _checkout(tmp_path)
    original = _write_stamp(repo, mechanism)
    _allow_store_python(monkeypatch)
    seen = _spy_publish(monkeypatch)

    message = mig.claim_external_checkout(repo)

    assert message == f"not claimed: updateMechanism is {mechanism}"
    assert (repo / mig.STAMP_NAME).read_bytes() == original
    assert not (repo / mig.STAMP_BACKUP_NAME).exists()
    assert seen == {}


def test_claim_refuses_without_a_store_interpreter(tmp_path, monkeypatch):
    repo = _checkout(tmp_path)
    original = _write_stamp(repo, "external")
    monkeypatch.setattr("hermes_cli._launchers.resolve_store_python", lambda *_a, **_k: None)
    seen = _spy_publish(monkeypatch)

    with pytest.raises(mig.ClaimError, match="managed interpreter is not installed"):
        mig.claim_external_checkout(repo)

    assert (repo / mig.STAMP_NAME).read_bytes() == original
    assert not (repo / mig.STAMP_BACKUP_NAME).exists()
    assert seen == {}


def test_claim_refuses_a_tree_that_is_not_a_git_checkout(tmp_path, monkeypatch):
    repo = tmp_path / "sealed"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname = 'hermes'\n", encoding="utf-8")
    original = _write_stamp(repo, "external")
    _allow_store_python(monkeypatch)

    with pytest.raises(mig.ClaimError, match="git checkout"):
        mig.claim_external_checkout(repo)

    assert (repo / mig.STAMP_NAME).read_bytes() == original
    assert not (repo / mig.STAMP_BACKUP_NAME).exists()


def test_failed_publish_restores_the_external_stamp(tmp_path, monkeypatch):
    repo = _checkout(tmp_path)
    original = _write_stamp(repo, "external")
    _allow_store_python(monkeypatch)
    _spy_publish(monkeypatch, create_launcher=False)

    with pytest.raises(mig.ClaimError, match="official launcher missing"):
        mig.claim_external_checkout(repo)

    assert (repo / mig.STAMP_NAME).read_bytes() == original
    assert not (repo / mig.STAMP_BACKUP_NAME).exists()
    assert not (repo / ".hermes" / "bin" / "hermes").exists()
    assert not (repo / mig.CLAIM_RECORD_NAME).exists()


def test_publish_error_restores_the_external_stamp(tmp_path, monkeypatch):
    repo = _checkout(tmp_path)
    original = _write_stamp(repo, "external")
    _allow_store_python(monkeypatch)
    _spy_publish(monkeypatch, boom=True)

    with pytest.raises(mig.ClaimError, match="launcher publish failed"):
        mig.claim_external_checkout(repo)

    assert (repo / mig.STAMP_NAME).read_bytes() == original
    assert MARKER in (repo / mig.STAMP_NAME).read_text(encoding="utf-8")


def test_unidentifiable_git_tree_restores_the_external_stamp(tmp_path, monkeypatch):
    repo = _checkout(tmp_path, commit=False)
    original = _write_stamp(repo, "external")
    _allow_store_python(monkeypatch)
    _spy_publish(monkeypatch)

    with pytest.raises(mig.ClaimError, match="could not write a managed install stamp"):
        mig.claim_external_checkout(repo)

    assert (repo / mig.STAMP_NAME).read_bytes() == original


def test_rollback_restores_the_external_stamp_and_removes_the_new_launcher(tmp_path, monkeypatch):
    repo = _checkout(tmp_path)
    original = _write_stamp(repo, "external")
    receipt = {"schemaVersion": 1, "pinnedCommit": "a" * 40, "pinnedBranch": "main"}
    (repo / mig.RECEIPT_NAME).write_text(json.dumps(receipt) + "\n", encoding="utf-8")
    _allow_store_python(monkeypatch)
    _spy_publish(monkeypatch)
    mig.claim_external_checkout(repo)
    claimed_receipt = json.loads((repo / mig.RECEIPT_NAME).read_text(encoding="utf-8"))
    assert claimed_receipt["pinnedCommit"] == _git(repo, "rev-parse", "HEAD")

    message = mig.restore_external_stamp(repo)

    assert (repo / mig.STAMP_NAME).read_bytes() == original
    assert json.loads((repo / mig.RECEIPT_NAME).read_text(encoding="utf-8"))["pinnedCommit"] == "a" * 40
    assert not (repo / ".hermes" / "bin" / "hermes").exists()
    assert not (repo / mig.STAMP_BACKUP_NAME).exists()
    assert not (repo / mig.RECEIPT_BACKUP_NAME).exists()
    assert not (repo / mig.CLAIM_RECORD_NAME).exists()
    assert "restored install-stamp.json" in message
    assert "removed .hermes/bin/hermes" in message
    assert MARKER not in message

    again = mig.restore_external_stamp(repo)
    assert again == "no external stamp backup; stamp unchanged"
    assert (repo / mig.STAMP_NAME).read_bytes() == original


def test_rollback_without_a_claim_leaves_an_existing_launcher(tmp_path):
    repo = _checkout(tmp_path)
    _write_stamp(repo, "self")
    launcher = repo / ".hermes" / "bin" / "hermes"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o755)

    message = mig.restore_external_stamp(repo)

    assert message == "no external stamp backup; stamp unchanged"
    assert launcher.is_file()


def test_rollback_keeps_a_launcher_that_existed_before_the_claim(tmp_path, monkeypatch):
    repo = _checkout(tmp_path)
    original = _write_stamp(repo, "external")
    launcher = repo / ".hermes" / "bin" / "hermes"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\npreexisting\n", encoding="utf-8")
    launcher.chmod(0o755)
    _allow_store_python(monkeypatch)
    _spy_publish(monkeypatch)

    mig.claim_external_checkout(repo)
    mig.restore_external_stamp(repo)

    assert (repo / mig.STAMP_NAME).read_bytes() == original
    assert launcher.is_file()


def test_claim_refuses_to_overwrite_a_different_stamp_backup(tmp_path, monkeypatch):
    repo = _checkout(tmp_path)
    original = _write_stamp(repo, "external")
    (repo / mig.STAMP_BACKUP_NAME).write_text('{"updateMechanism": "external"}\n', encoding="utf-8")
    _allow_store_python(monkeypatch)
    _spy_publish(monkeypatch)

    with pytest.raises(mig.ClaimError, match="refusing to overwrite"):
        mig.claim_external_checkout(repo)

    assert (repo / mig.STAMP_NAME).read_bytes() == original


def test_claim_cli_prints_the_launcher_path_and_not_the_stamp(tmp_path, monkeypatch, capsys):
    repo = _checkout(tmp_path)
    _write_stamp(repo, "external")
    _allow_store_python(monkeypatch)
    _spy_publish(monkeypatch)

    code = mig.main(["claim-managed", "--checkout", str(repo)])
    out = capsys.readouterr().out

    assert code == 0
    assert str(repo / ".hermes" / "bin" / "hermes") in out
    assert MARKER not in out

    code = mig.main(["restore-external-stamp", "--checkout", str(repo)])
    out = capsys.readouterr().out
    assert code == 0
    assert "restored install-stamp.json" in out
    assert MARKER not in out


def test_claim_cli_exits_4_when_the_store_interpreter_is_absent(tmp_path, monkeypatch, capsys):
    repo = _checkout(tmp_path)
    _write_stamp(repo, "external")
    monkeypatch.setattr("hermes_cli._launchers.resolve_store_python", lambda *_a, **_k: None)

    code = mig.main(["claim-managed", "--checkout", str(repo)])
    captured = capsys.readouterr()

    assert code == 4
    assert "managed interpreter is not installed" in captured.err
    assert MARKER not in captured.out
    assert MARKER not in captured.err
