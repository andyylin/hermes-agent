"""A pinned runtime venv is the one environment a checkout's services run on and PM installs into.

rpi5 regression: `hermes pm install` committed a PM generation for a `self`-stamped checkout,
and the next gateway restart re-exec'd onto PM's bare store Python (`-I`) instead of the venv
the systemd drop-ins start, so aiohttp and the LINE/Email adapters were missing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import pm
from pm import install, paths
from pm.package import InstallError
from pm.environments import pin_record_path, pinned_venv, running_from
from pm.pinned_venv import import_failures, pinned_venv_current, remove_pin, sync_pinned_venv, write_pin


def _venv(tmp_path: Path, name: str = "venv") -> Path:
    venv = tmp_path / name
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.14.7\n")
    python = venv / "bin" / "python"
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)
    return venv


def _checkout(tmp_path: Path) -> Path:
    root = tmp_path / "hermes-agent"
    (root / ".git").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    (root / "uv.lock").write_text("lock-v1\n")
    (root / "install-stamp.json").write_text(json.dumps({"updateMechanism": "self"}))
    return root


class _FakeEnvironment:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    def export_requirements(self, root, target, *, extras):
        self.calls.append(("export", tuple(extras)))
        Path(target).write_text("# generated\naiohttp==3.12.0\n\nhttpx2==2.12.0\n")

    def install_requirements(self, requirements):
        self.calls.append(("install", tuple(requirements)))

    def check(self):
        self.calls.append(("check",))


@pytest.fixture
def fake_environment(monkeypatch):
    calls: list = []
    import pm.environment

    def managed(venv, *, python, explicit, output):
        calls.append(("managed", Path(venv), Path(python), explicit))
        return _FakeEnvironment(calls)

    monkeypatch.setattr(pm.environment, "managed_environment", managed)
    monkeypatch.setattr("pm.pinned_venv.import_failures", lambda python, modules=(): [])
    return calls


def test_nothing_is_pinned_without_a_record(tmp_path):
    assert pinned_venv(_checkout(tmp_path)) is None


def test_a_pin_round_trips_and_unpins(tmp_path):
    root, venv = _checkout(tmp_path), _venv(tmp_path)
    write_pin(root, venv)
    assert pinned_venv(root) == venv
    remove_pin(root)
    assert pinned_venv(root) is None


@pytest.mark.parametrize("record", ["{not json", json.dumps({"venv": "relative/venv"}),
                                    json.dumps({"venv": "/nonexistent/hermes-venv"}), json.dumps(["x"])])
def test_a_broken_record_raises_instead_of_falling_back_to_the_store_python(tmp_path, record):
    root = _checkout(tmp_path)
    path = pin_record_path(root)
    path.parent.mkdir(parents=True)
    path.write_text(record)
    with pytest.raises(RuntimeError):
        pinned_venv(root)


def test_only_a_venv_with_an_interpreter_can_be_pinned(tmp_path):
    with pytest.raises(RuntimeError):
        write_pin(_checkout(tmp_path), tmp_path / "not-a-venv")


def test_running_from_compares_the_interpreter_prefix(tmp_path, monkeypatch):
    venv = _venv(tmp_path)
    monkeypatch.setattr(sys, "prefix", str(venv))
    assert running_from(venv)
    assert not running_from(_venv(tmp_path, "other"))


def test_import_failures_names_each_missing_module():
    failed = import_failures(Path(sys.executable), ["json", "hermes_no_such_module"])
    assert len(failed) == 1 and failed[0].startswith("hermes_no_such_module:")


def test_launch_runs_on_the_pinned_venv_never_the_store_python(tmp_path, monkeypatch):
    from hermes_cli import venv_sync

    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    root, venv = _checkout(tmp_path), _venv(tmp_path)
    write_pin(root, venv)

    def store_path(**_kwargs):
        raise AssertionError("a pinned checkout must not consult PM's committed generation")

    monkeypatch.setattr(pm, "venv_is_current", store_path)
    assert venv_sync.prepare_launch(root, ["gateway", "run"]) == venv / "bin" / "python"
    monkeypatch.setattr(sys, "prefix", str(venv))
    assert venv_sync.prepare_launch(root, ["gateway", "run"]) is None


def test_activation_keeps_the_pinned_venvs_own_packages(tmp_path, monkeypatch):
    from pm.environments import activate_dependencies, install_state_dir

    root, venv = _checkout(tmp_path), _venv(tmp_path)
    committed = _venv(tmp_path, "pm-generation")
    (committed / "lib" / "python3.14" / "site-packages").mkdir(parents=True)
    state = install_state_dir(root)
    state.mkdir(parents=True)
    (state / "facts.json").write_text(json.dumps({"packages": {"venv": {"environment": str(committed)}}}))
    write_pin(root, venv)
    monkeypatch.setattr(sys, "prefix", str(venv))
    before = list(sys.path)

    activate_dependencies(root)

    assert sys.path == before


def test_pinned_sync_installs_the_lock_into_the_venv_and_records_currency(tmp_path, fake_environment):
    root, venv = _checkout(tmp_path), _venv(tmp_path)

    selected = sync_pinned_venv(root, venv, ["sms"], explicit=True)

    assert selected == ["all", "sms"]
    assert fake_environment[0] == ("managed", venv, venv / "bin" / "python", True)
    assert ("install", ("aiohttp==3.12.0", "httpx2==2.12.0")) in fake_environment
    assert pinned_venv_current(root, ["sms"])
    assert not pinned_venv_current(root, ["voice"])
    (root / "uv.lock").write_text("lock-v2\n")
    assert not pinned_venv_current(root, ["sms"])


def test_a_pinned_sync_that_cannot_import_the_runtime_fails_and_stays_stale(tmp_path, fake_environment, monkeypatch):
    root, venv = _checkout(tmp_path), _venv(tmp_path)
    monkeypatch.setattr("pm.pinned_venv.import_failures", lambda python, modules=(): ["aiohttp: ImportError: x"])

    with pytest.raises(InstallError, match="aiohttp"):
        sync_pinned_venv(root, venv, None, explicit=True)
    assert not pinned_venv_current(root)


def test_pm_install_syncs_into_the_pinned_venv(tmp_path, monkeypatch):
    venv = _venv(tmp_path)
    write_pin(paths.repo_root(), venv)
    synced = []
    monkeypatch.setattr(install, "sync_pinned_venv",
                        lambda root, target, extras, *, explicit: synced.append((target, extras, explicit)) or ["all"])
    monkeypatch.setattr(install, "pinned_venv_current", lambda root, extras: False)

    assert install.venv_is_current(extras=["all"]) is False
    install.sync_venv(["all"], explicit=True)

    assert synced == [(venv, ["all"], True)]


def test_plugin_members_are_refused_rather_than_built_into_a_store_generation(tmp_path, monkeypatch):
    write_pin(paths.repo_root(), _venv(tmp_path))
    monkeypatch.setattr(install, "sync_pinned_venv", lambda *a, **k: pytest.fail("must not sync"))

    with pytest.raises(InstallError, match="pinned runtime venv"):
        install.sync_venv(["all"], explicit=True, plugins=[])
