"""A pinned gateway that cannot import what it serves refuses to start (exit 78), with the reason.

rpi5 regression: the gateway came up on bare system Python, LINE and Email failed their
requirement checks, the API server never bound, and only Telegram kept working.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from gateway import runtime_dependency_guard as guard
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.restart import GATEWAY_FATAL_CONFIG_EXIT_CODE
from pm.pinned_venv import write_pin

CHECKOUT = Path(guard.__file__).resolve().parents[1]


def _pin(tmp_path: Path) -> Path:
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("version = 3.14.7\n")
    (venv / "bin" / "python").write_text("")
    write_pin(CHECKOUT, venv)
    return venv


@pytest.fixture
def importable_runtime(monkeypatch):
    monkeypatch.setattr(guard, "RUNTIME_IMPORTS", ("json",))


def test_an_unpinned_gateway_keeps_its_own_contract():
    line = {Platform("line"): PlatformConfig(enabled=True)}
    assert guard.pinned_runtime_failures(line) == []


def test_another_interpreter_is_named(tmp_path, importable_runtime):
    venv = _pin(tmp_path)
    (failure,) = guard.pinned_runtime_failures({})
    assert sys.executable in failure and str(venv) in failure


def test_missing_runtime_modules_are_named(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "prefix", str(_pin(tmp_path)))
    monkeypatch.setattr(guard, "RUNTIME_IMPORTS", ("json", "hermes_no_such_sdk"))
    (failure,) = guard.pinned_runtime_failures({})
    assert "hermes_no_such_sdk" in failure


def test_an_enabled_platform_with_unmet_requirements_is_named(tmp_path, monkeypatch, importable_runtime):
    monkeypatch.setattr(sys, "prefix", str(_pin(tmp_path)))
    for name in ("LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET"):
        monkeypatch.delenv(name, raising=False)
    platforms = {Platform("line"): PlatformConfig(enabled=True), Platform.EMAIL: PlatformConfig(enabled=False)}

    assert guard.pinned_runtime_failures(platforms) == ["platform line: requirements not met"]
    assert guard.pinned_runtime_failures(platforms, served=lambda _p, _c: False) == []


@pytest.mark.asyncio
async def test_the_gateway_exits_78_before_serving_anything(tmp_path, importable_runtime):
    from gateway.run import GatewayRunner

    _pin(tmp_path)
    runner = GatewayRunner(GatewayConfig(platforms={}, sessions_dir=tmp_path / "sessions"))

    assert await runner.start() is True

    assert runner.exit_code == GATEWAY_FATAL_CONFIG_EXIT_CODE
    assert "pinned runtime venv" in (runner.exit_reason or "")
    assert not runner.adapters
