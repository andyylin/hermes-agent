"""`hermes pm install --extra NAME` is the one command every missing-extra hint names.

`--extra` adds. It installs the lock's tool roots (and any extra that names an
installable package) and then enables the extra in the venv. It must not call
the installer with ``names=[]``.
"""
from types import SimpleNamespace

import pytest

from pm import install_hint
from pm.package import InstallError
from pm.registry import get_package, tool_roots
from pm.store import current_target

# The tools a lock bump has to publish. ffmpeg is absent on musl; the others
# are part of the required closure on every target.
_LOCK_TOOLS = ("node", "npm", "pnpm", "python", "ripgrep", "ffmpeg")


def _lock_tools(published: list[str]) -> list[str]:
    """The lock's tool roots, and the required tools this host can install are in ``published``."""
    from pm.cli import _lockfile

    roots = tool_roots(_lockfile().names())
    target = current_target()
    for name in _LOCK_TOOLS:
        if get_package(name).missing_reason(target) is None:
            assert name in roots
            assert name in published
    return roots


def test_hint_names_a_command_the_cli_accepts(monkeypatch):
    from pm import cli, runtime
    from pm import install as install_mod

    synced = []
    published: list[list[str]] = []
    monkeypatch.setattr(runtime, "is_runtime", lambda: True)
    monkeypatch.setattr(install_mod, "sync_venv", lambda extras, **kwargs: synced.append((list(extras), kwargs)))
    monkeypatch.setattr(cli, "_install_names",
                        lambda names, target=None, **kwargs: published.append(list(names)) or 0)
    monkeypatch.setattr(install_mod, "activate", lambda **kwargs: [])

    argv = install_hint("anthropic").split()[2:]
    assert cli.main(argv) == 0
    assert synced == [(["anthropic"], {"explicit": True})]
    tools = [name for batch in published for name in batch]
    assert tools == _lock_tools(tools)
    assert "anthropic" not in tools


def _install(monkeypatch, **kwargs):
    from pm import cli
    from pm import install as install_mod

    published: list[str] = []
    synced: list[list[str]] = []

    def fake_install(names, target=None, *, verify=True):
        published.extend(names)
        return 0

    monkeypatch.setattr(cli, "_install_names", fake_install)
    monkeypatch.setattr(install_mod, "sync_venv", lambda extras=None, **kw: synced.append(list(extras or [])))
    monkeypatch.setattr(install_mod, "activate", lambda **kw: [])
    args = SimpleNamespace(names=[], extra=[], target=None, tools_only=False)
    for key, value in kwargs.items():
        setattr(args, key, value)
    assert cli.cmd_install(args) == 0
    return published, synced


def test_extra_publishes_the_lock_tools_plus_the_package(monkeypatch):
    """`pm install --extra just` is the lock's tools plus just, never names=[]."""
    published, synced = _install(monkeypatch, extra=["just"])
    assert published == [*_lock_tools(published), "just"]
    assert synced == [["just"]]


def test_python_extra_keeps_the_lock_tools(monkeypatch):
    """`pm install --extra otlp` still publishes node, pnpm, ffmpeg, and the rest."""
    published, synced = _install(monkeypatch, extra=["otlp", "mcp", "otlp"])
    assert published == _lock_tools(published)
    assert "otlp" not in published
    assert synced == [["otlp", "mcp"]]


def test_extra_adds_to_explicit_names(monkeypatch):
    published, synced = _install(monkeypatch, names=["npm"], extra=["just"])
    assert published == ["npm", "just"]
    assert synced == [["just"]]


def test_bare_install_closure_is_unchanged(monkeypatch):
    from pm.cli import _lockfile
    from pm.defaults import default_packages

    published, synced = _install(monkeypatch, names=None)
    roots = _lock_tools(published)
    assert published == [*roots, *default_packages(_lockfile().names())]
    assert synced == [["all"]]


def test_extra_syncs_only_the_named_extras(monkeypatch):
    from pm import cli
    from pm import install as install_mod

    synced = []
    monkeypatch.setattr(install_mod, "sync_venv", lambda extras, **kwargs: synced.append(list(extras)))
    monkeypatch.setattr(install_mod, "activate", lambda **kwargs: [])
    monkeypatch.setattr(cli, "_install_names", lambda names, target=None, **kwargs: 0)
    assert cli.cmd_install(SimpleNamespace(names=[], extra=["otlp", "mcp", "otlp"], target=None, tools_only=False)) == 0
    assert synced == [["otlp", "mcp"]]


def test_tool_roots_rejects_an_empty_name_list():
    with pytest.raises(InstallError, match="empty tool set"):
        tool_roots([])


def test_empty_publish_is_rejected_and_leaves_published_tools(monkeypatch, tmp_path, capsys):
    from pm import cli

    store = tmp_path / "tools"
    store.mkdir()
    facts = store / "facts.json"
    body = b'{"packages":{"node":{"version":"kept"}}}\n'
    facts.write_bytes(body)
    binary = store / "node-kept" / "bin" / "node"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"node-bytes")
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(store))

    def _boom(*_args, **_kwargs):
        raise AssertionError("empty publish touched the store")

    monkeypatch.setattr(cli, "ensure", _boom)
    monkeypatch.setattr(cli, "stage_only", _boom)
    assert cli._install_names([]) == 1
    assert facts.read_bytes() == body
    assert binary.read_bytes() == b"node-bytes"
    message = capsys.readouterr().out
    assert "empty tool set" in message
    assert "left in place" in message


def test_install_refuses_an_empty_lock_instead_of_publishing_nothing(monkeypatch, tmp_path, capsys):
    from pm import cli
    from pm.lock import Lockfile

    lock_path = tmp_path / "lock.json"
    lock_path.write_text('{"schema": 1, "packages": {}}\n', encoding="utf-8")
    monkeypatch.setattr(cli, "_lockfile", lambda: Lockfile(lock_path))
    called: list[list[str]] = []
    monkeypatch.setattr(cli, "_install_names", lambda names, target=None, **kwargs: called.append(list(names)) or 0)
    assert cli.cmd_install(SimpleNamespace(names=[], extra=["otlp"], target=None, tools_only=False)) == 1
    assert called == []
    message = capsys.readouterr().out
    assert "empty tool set" in message
    assert "left in place" in message


def test_cold_runtime_refusal_names_the_extra(monkeypatch, tmp_path):
    from pm import client
    from pm import receipt

    monkeypatch.setattr("pm.install.lazy_installs_allowed", lambda: False)
    monkeypatch.setattr(client, "runtime_environment", dict)
    monkeypatch.setattr("pm.registry.package_definitions", lambda names: [])
    monkeypatch.setattr(client, "runtime_command", lambda *args, **kwargs: (_ for _ in ()).throw(
        InstallError("pm-runtime", "not installed or outdated and lazy installs are disabled")))
    for name in ("begin", "record_refusal", "record_step", "finalize"):
        monkeypatch.setattr(receipt, name, lambda *args, **kwargs: None)

    with pytest.raises(InstallError) as info:
        client._request("sync_venv", {"extras": ["bedrock"], "explicit": False, "repair": False},
                        project_root=tmp_path)
    assert install_hint("bedrock") in info.value.remedy
    assert "bedrock" in info.value.cause
