"""A deploy stamp migrates the named profile and leaves every sibling config byte-for-byte."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "docs" / "ops"))

import rpi5_deploy_probe as probe
from hermes_cli import source_stamp


def _config(version: int, backend: str, marker: str) -> bytes:
    return (
        f"_config_version: {version}\n"
        f"browser:\n"
        f"  backend: {backend}\n"
        f"marker: {marker}\n"
    ).encode()


def test_deploy_migrates_only_the_targeted_profile_and_backs_it_up(tmp_path, monkeypatch):
    target = tmp_path / "default"
    sibling = tmp_path / "profiles" / "wife"
    target.mkdir()
    sibling.mkdir(parents=True)
    original = _config(51, "off", "default-profile")
    untouched = _config(51, "camofox", "sibling-untouched")
    (target / "config.yaml").write_bytes(original)
    (sibling / "config.yaml").write_bytes(untouched)
    monkeypatch.setenv("HERMES_HOME", str(target))

    result = probe.migrate_targeted_home(target)

    assert result["ok"] is True
    assert result["from"] == 51
    assert result["to"] >= 52
    assert (sibling / "config.yaml").read_bytes() == untouched
    written = (target / "config.yaml").read_text(encoding="utf-8")
    assert "_config_version: 51" not in written
    assert "backend:" not in written
    backups = list(target.glob("config.yaml.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original


def test_stamp_without_a_home_does_not_open_sibling_configs(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    sibling = tmp_path / "profiles" / "dad"
    sibling.mkdir(parents=True)
    untouched = _config(51, "browser-use", "dad")
    (sibling / "config.yaml").write_bytes(untouched)

    monkeypatch.setattr(source_stamp, "write_source_stamp", lambda root: {"commit": "c" * 40})

    code = probe.cmd_stamp(probe.build_parser().parse_args(
        ["--checkout", str(checkout), "stamp"]))

    assert code == 0
    assert (sibling / "config.yaml").read_bytes() == untouched
