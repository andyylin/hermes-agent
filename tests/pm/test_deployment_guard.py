"""Plugin sync must not replace an externally managed interpreter.

(a) A plugin with no Python declaration does not build a dependency environment.
(b) With no committed facts.json, a member change does not create one unless
    the operator opts in.
(c) A generation that is committed keeps enabled platform extras and is not
    recorded when it would drop one.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import hermes_yaml as yaml
import pytest

from pm.package import InstallError
from pm.plugin_inputs import Selection


def _layout(tmp_path, monkeypatch, *, extras=(), opt_in=False, platforms=None):
    core = tmp_path / "core"
    core.mkdir()
    body = (
        "[project]\n"
        'name = "fake-core"\nversion = "0.1.0"\n'
        'requires-python = ">=3.11"\ndependencies = []\n'
    )
    if extras:
        body += "\n[project.optional-dependencies]\n"
        body += "".join(f"{name} = []\n" for name in extras)
    (core / "pyproject.toml").write_text(body, encoding="utf-8")
    (core / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    plugins = {"enabled": [], "disabled": []}
    if opt_in:
        plugins["commit_dependency_environment"] = True
    config: dict = {"plugins": plugins}
    if platforms is not None:
        config["platforms"] = platforms
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "tools"))
    import pm.extras as extras_mod
    import pm.paths

    monkeypatch.setattr(pm.paths, "repo_root", lambda: core)
    monkeypatch.setattr(extras_mod, "_PLATFORM_GATES", None)
    return core, home


def _plugin(home: Path, name: str, *, pyproject: bool) -> Path:
    path = home / "plugins" / name
    path.mkdir(parents=True)
    (path / "plugin.yaml").write_text(f"name: {name}\n", encoding="utf-8")
    if pyproject:
        (path / "pyproject.toml").write_text(
            "[project]\n"
            f'name = "{name}"\nversion = "0.1.0"\n'
            'requires-python = ">=3.11"\ndependencies = []\n',
            encoding="utf-8",
        )
    return path


def _selection(home: Path, enabled, disabled=()) -> Selection:
    return Selection({
        "home": str(home),
        "enabled": list(enabled),
        "disabled": list(disabled),
    })


def _as_user_venv(monkeypatch, tmp_path):
    """The incident's interpreter: a venv PM did not build."""
    venv = tmp_path / "venvs" / "hermes-py314"
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(sys, "base_prefix", "/usr")


def _watch_apply(monkeypatch):
    calls = []

    def apply(self, extras, **kwargs):
        calls.append(list(extras))
        return {}

    monkeypatch.setattr("pm.packages.Venv.apply", apply)
    return calls


def _enabled(home: Path) -> list:
    return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))["plugins"]["enabled"]


def test_plugin_without_python_deps_does_not_sync_even_when_opted_in(tmp_path, monkeypatch):
    _as_user_venv(monkeypatch, tmp_path)
    core, home = _layout(tmp_path, monkeypatch, opt_in=True)
    _plugin(home, "stdlib-only", pyproject=False)
    calls = _watch_apply(monkeypatch)
    from pm.environments import install_state_dir, runtime_facts_path
    from pm.install import sync_venv

    sync_venv(explicit=True, commit_environment=True, plugins=_selection(home, ["stdlib-only"]))

    assert calls == []
    assert _enabled(home) == ["stdlib-only"]
    assert not runtime_facts_path(core).is_file()
    assert not (install_state_dir(core) / "environments").exists()

    calls.clear()
    sync_venv(explicit=True, commit_environment=True, plugins=_selection(home, [], ["stdlib-only"]))
    assert calls == []
    assert "stdlib-only" not in _enabled(home)
    assert not runtime_facts_path(core).is_file()


def test_member_enable_on_external_runtime_refuses_without_opt_in(tmp_path, monkeypatch):
    _as_user_venv(monkeypatch, tmp_path)
    core, home = _layout(tmp_path, monkeypatch)
    _plugin(home, "needs-sync", pyproject=True)
    calls = _watch_apply(monkeypatch)
    before = (home / "config.yaml").read_bytes()
    from hermes_cli.plugins_admission import AdmissionRefused, admit_plugin_set_change
    from pm.environments import runtime_facts_path
    from pm.install import sync_venv

    def in_process(*args, project_root=None, **kwargs):
        assert project_root is None or Path(project_root).resolve() == core.resolve()
        return sync_venv(*args, **kwargs)

    monkeypatch.setattr("pm.client.sync_venv", in_process)
    with pytest.raises(AdmissionRefused, match="externally managed") as refused:
        admit_plugin_set_change({"needs-sync"}, set(), active_plugins_dir=home / "plugins")
    message = str(refused.value)
    assert "commit_dependency_environment" in message
    assert "--commit-environment" in message
    assert (home / "config.yaml").read_bytes() == before
    assert calls == []
    assert not runtime_facts_path(core).is_file()


def test_member_enable_builds_when_commit_environment_is_set(tmp_path, monkeypatch):
    _as_user_venv(monkeypatch, tmp_path)
    core, home = _layout(tmp_path, monkeypatch)
    _plugin(home, "needs-sync", pyproject=True)
    calls = _watch_apply(monkeypatch)
    from pm.environments import runtime_facts_path
    from pm.install import sync_venv

    sync_venv(
        explicit=True, commit_environment=True, plugins=_selection(home, ["needs-sync"]))

    assert calls
    assert runtime_facts_path(core).is_file()
    assert _enabled(home) == ["needs-sync"]


def test_config_opt_in_allows_member_enable_to_build(tmp_path, monkeypatch):
    _as_user_venv(monkeypatch, tmp_path)
    core, home = _layout(tmp_path, monkeypatch, opt_in=True)
    _plugin(home, "needs-sync", pyproject=True)
    calls = _watch_apply(monkeypatch)
    from pm.install import sync_venv

    sync_venv(explicit=True, plugins=_selection(home, ["needs-sync"]))
    assert calls
    assert _enabled(home) == ["needs-sync"]


def test_disabling_a_member_on_external_runtime_publishes_config_only(tmp_path, monkeypatch):
    _as_user_venv(monkeypatch, tmp_path)
    core, home = _layout(tmp_path, monkeypatch)
    _plugin(home, "needs-sync", pyproject=True)
    (home / "config.yaml").write_text(
        "plugins:\n  enabled: [needs-sync]\n  disabled: []\n", encoding="utf-8")
    calls = _watch_apply(monkeypatch)
    from pm.environments import runtime_facts_path
    from pm.install import sync_venv

    sync_venv(explicit=True, plugins=_selection(home, [], ["needs-sync"]))

    assert calls == []
    assert _enabled(home) == []
    loaded = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert loaded["plugins"]["disabled"] == ["needs-sync"]
    assert not runtime_facts_path(core).is_file()


def test_enabled_platforms_join_the_committed_selection(tmp_path, monkeypatch):
    core, home = _layout(
        tmp_path, monkeypatch,
        extras=("telegram", "discord", "matrix", "slack"),
        platforms={
            "telegram": {"enabled": True},
            "discord": {"enabled": True},
            "matrix": {"enabled": True},
            "slack": {"enabled": False},
            "irc": {"enabled": True},
        },
    )
    from pm.install import _target_selection
    from pm.packages import Venv

    enabled, _stamp, _inputs = _target_selection(
        Venv(core), {}, extras=None, inputs={"plugin_dirs": []},
        repair=False, shipped=None, frozen=None,
    )
    assert {"telegram", "discord", "matrix"} <= set(enabled)
    assert "slack" not in enabled

    with pytest.raises(InstallError, match="refusing to commit a dependency environment that drops"):
        _target_selection(
            Venv(core), {}, extras=None, inputs={"plugin_dirs": []},
            repair=False, shipped=["all"], frozen=["all"],
        )


def _fake_generation(monkeypatch, present):
    calls = []

    def apply(self, extras, **kwargs):
        from pm.environments import install_state_dir

        calls.append(list(extras))
        generation = install_state_dir(self.project_root()) / "environments" / "deadbeef"
        environment = generation / "venv"
        generation.mkdir(parents=True)
        (generation / ".lease-managed").touch()
        environment.mkdir()
        lock = generation / "workspace" / "uv.lock"
        lock.parent.mkdir(parents=True)
        lock.write_text("lock\n", encoding="utf-8")
        return {"environment": environment, "resolved_lock": lock}

    monkeypatch.setattr("pm.packages.Venv.apply", apply)
    monkeypatch.setattr("pm.features.installed_extras", lambda *args, **kwargs: list(present))
    monkeypatch.setattr("pm.deployment_guard._module_present", lambda *args, **kwargs: True)
    return calls


def test_commit_refuses_environment_that_drops_an_enabled_platform(tmp_path, monkeypatch):
    _as_user_venv(monkeypatch, tmp_path)
    core, home = _layout(
        tmp_path, monkeypatch, opt_in=True,
        extras=("telegram", "discord", "matrix"),
        platforms={
            "telegram": {"enabled": True},
            "discord": {"enabled": True},
            "matrix": {"enabled": True},
        },
    )
    _plugin(home, "needs-sync", pyproject=True)
    before = (home / "config.yaml").read_bytes()
    calls = _fake_generation(monkeypatch, ["discord"])
    monkeypatch.setattr("pm.deployment_guard._module_present", lambda *args, **kwargs: False)
    from pm.environments import install_state_dir, runtime_facts_path
    from pm.install import sync_venv

    with pytest.raises(InstallError, match="refusing to commit a dependency environment that drops"):
        sync_venv(explicit=True, plugins=_selection(home, ["needs-sync"]))

    assert {"telegram", "discord", "matrix"} <= set(calls[0])
    assert (home / "config.yaml").read_bytes() == before
    assert not runtime_facts_path(core).is_file()
    assert not (install_state_dir(core) / "environments" / "deadbeef").exists()


def test_commit_records_environment_that_keeps_enabled_platforms(tmp_path, monkeypatch):
    _as_user_venv(monkeypatch, tmp_path)
    core, home = _layout(
        tmp_path, monkeypatch, opt_in=True,
        extras=("telegram", "discord", "matrix"),
        platforms={
            "telegram": {"enabled": True},
            "discord": {"enabled": True},
            "matrix": {"enabled": True},
        },
    )
    _plugin(home, "needs-sync", pyproject=True)
    _fake_generation(monkeypatch, ["telegram", "discord", "matrix"])
    from pm.environments import runtime_facts_path
    from pm.install import sync_venv
    from pm.lock import Facts

    sync_venv(explicit=True, plugins=_selection(home, ["needs-sync"]))

    fact = Facts(runtime_facts_path(core)).get("venv")
    assert {"telegram", "discord", "matrix"} <= set(fact["extras"])
    assert fact.get("environment")
    assert _enabled(home) == ["needs-sync"]


def test_external_runtime_is_an_interpreter_pm_did_not_build(tmp_path, monkeypatch):
    core, _home = _layout(tmp_path, monkeypatch)
    from pm.deployment_guard import externally_managed_runtime
    from pm.environments import install_state_dir, runtime_facts_path

    user_venv = tmp_path / "venvs" / "hermes-py314"
    monkeypatch.setattr(sys, "prefix", str(user_venv))
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    assert externally_managed_runtime(core) is True
    managed = tmp_path / "installs" / "abc" / "test-environment" / "gen" / "venv"
    monkeypatch.setattr(sys, "prefix", str(managed))
    monkeypatch.setattr(sys, "base_prefix", str(managed))
    assert externally_managed_runtime(core) is False
    generation = install_state_dir(core) / "environments" / "built" / "venv"
    monkeypatch.setattr(sys, "prefix", str(generation))
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    assert externally_managed_runtime(core) is False
    monkeypatch.setattr(sys, "prefix", str(user_venv))
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    facts = runtime_facts_path(core)
    facts.parent.mkdir(parents=True, exist_ok=True)
    facts.write_text("{}\n", encoding="utf-8")
    assert externally_managed_runtime(core) is False


def test_commit_environment_reader_accepts_only_boolean_true(tmp_path, monkeypatch):
    _core, home = _layout(tmp_path, monkeypatch)
    from pm.deployment_guard import commit_environment_opted_in

    assert commit_environment_opted_in() is False
    for text, expected in (
        ("plugins:\n  commit_dependency_environment: true\n", True),
        ("plugins:\n  commit_dependency_environment: false\n", False),
        ("plugins:\n  commit_dependency_environment: 'true'\n", False),
    ):
        (home / "config.yaml").write_text(text, encoding="utf-8")
        assert commit_environment_opted_in() is expected


def test_client_records_the_caller_interpreter(tmp_path, monkeypatch):
    """The worker must be told about the process that invoked the plugin command."""
    core, home = _layout(tmp_path, monkeypatch)
    captured: dict = {}

    def capture(operation, arguments, **kwargs):
        captured["operation"] = operation
        captured["arguments"] = dict(arguments)
        captured["project"] = kwargs.get("project_root")

    monkeypatch.setattr("pm.client._request", capture)
    from pm.client import sync_venv

    _as_user_venv(monkeypatch, tmp_path)
    sync_venv(explicit=True, plugins=_selection(home, ["needs-sync"]))
    assert captured["operation"] == "sync_venv"
    assert captured["arguments"]["externally_managed"] is True
    assert captured["project"] is None

    managed = tmp_path / "installs" / "abc" / "pm-runtime" / "generations" / "gen" / "venv"
    monkeypatch.setattr(sys, "prefix", str(managed))
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    sync_venv(explicit=True, plugins=_selection(home, ["needs-sync"]), project_root=core)
    assert captured["arguments"]["externally_managed"] is False
    assert captured["project"] == core


def test_worker_honors_the_caller_classification(tmp_path, monkeypatch):
    """A PM worker's own prefix must not turn an opted caller into an external one."""
    _as_user_venv(monkeypatch, tmp_path)
    _core, home = _layout(tmp_path, monkeypatch)
    _plugin(home, "needs-sync", pyproject=True)
    calls = _watch_apply(monkeypatch)
    from pm.install import sync_venv

    sync_venv(
        explicit=True, externally_managed=False,
        plugins=_selection(home, ["needs-sync"]))
    assert calls


def test_plugin_commands_accept_commit_environment_flag():
    from hermes_cli.subcommands.plugins import build_plugins_parser

    parser = argparse.ArgumentParser()
    build_plugins_parser(parser.add_subparsers(), cmd_plugins=lambda _args: None)
    for argv in (
        ["plugins", "install", "example", "--commit-environment"],
        ["plugins", "enable", "example", "--commit-environment"],
        ["plugins", "disable", "example", "--commit-environment"],
    ):
        assert parser.parse_args(argv).commit_environment is True
    assert parser.parse_args(["plugins", "enable", "example"]).commit_environment is False
