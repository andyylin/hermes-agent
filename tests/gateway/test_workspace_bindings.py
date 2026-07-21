from pathlib import Path

import pytest

from gateway.session import (
    Platform,
    SessionContext,
    SessionSource,
    build_session_context_prompt,
)
from hermes_cli import projects_db as pdb


def _project_db(tmp_path: Path, slug: str = "kenya", folder_name: str = "kenya"):
    folder = tmp_path / folder_name
    folder.mkdir()
    db_path = tmp_path / "projects.db"
    with pdb.connect_closing(db_path) as conn:
        project_id = pdb.create_project(
            conn,
            name="Kenya",
            slug=slug,
            folders=[str(folder)],
        )
    return db_path, project_id, folder


def _matrix_source(room_id: str = "!kenya:example.com") -> SessionSource:
    return SessionSource(
        platform=Platform.MATRIX,
        chat_id=room_id,
        chat_type="group",
        user_id="@andy:example.com",
        thread_id="$thread",
    )


def test_matrix_room_binding_resolves_project_primary_folder(tmp_path):
    from gateway.workspace_bindings import resolve_workspace_binding

    db_path, project_id, folder = _project_db(tmp_path)
    config = {
        "gateway": {
            "workspace_bindings": {
                "matrix": {
                    "!kenya:example.com": {"project": "kenya"},
                }
            }
        }
    }

    binding = resolve_workspace_binding(
        config,
        _matrix_source(),
        projects_db_path=db_path,
    )

    assert binding is not None
    assert binding.project_id == project_id
    assert binding.project_slug == "kenya"
    assert binding.project_name == "Kenya"
    assert binding.cwd == str(folder.resolve())
    assert binding.allowed_folders == (str(folder.resolve()),)


def test_matrix_room_binding_accepts_json_string_from_config_cli(tmp_path):
    from gateway.workspace_bindings import resolve_workspace_binding

    db_path, project_id, folder = _project_db(tmp_path)
    config = {
        "gateway": {
            "workspace_bindings": {
                "matrix": '{"!kenya:example.com":{"project":"kenya"}}'
            }
        }
    }

    binding = resolve_workspace_binding(
        config,
        _matrix_source(),
        projects_db_path=db_path,
    )

    assert binding is not None
    assert binding.project_id == project_id
    assert binding.cwd == str(folder.resolve())


def test_unconfigured_room_has_no_binding(tmp_path):
    from gateway.workspace_bindings import resolve_workspace_binding

    db_path, _, _ = _project_db(tmp_path)
    config = {"gateway": {"workspace_bindings": {"matrix": {}}}}

    assert resolve_workspace_binding(
        config,
        _matrix_source("!other:example.com"),
        projects_db_path=db_path,
    ) is None


def test_non_matrix_source_does_not_use_matrix_binding(tmp_path):
    from gateway.workspace_bindings import resolve_workspace_binding

    db_path, _, _ = _project_db(tmp_path)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="!kenya:example.com",
        chat_type="group",
    )
    config = {
        "gateway": {
            "workspace_bindings": {
                "matrix": {"!kenya:example.com": {"project": "kenya"}}
            }
        }
    }

    assert resolve_workspace_binding(config, source, projects_db_path=db_path) is None


def test_non_matrix_source_uses_its_own_platform_binding(tmp_path):
    from gateway.workspace_bindings import resolve_workspace_binding

    db_path, project_id, folder = _project_db(tmp_path)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="channel-123",
        chat_type="group",
    )
    config = {
        "gateway": {
            "workspace_bindings": {
                "discord": {"channel-123": {"project": "kenya"}}
            }
        }
    }

    binding = resolve_workspace_binding(config, source, projects_db_path=db_path)

    assert binding is not None
    assert binding.project_id == project_id
    assert binding.cwd == str(folder.resolve())


def test_configured_room_fails_closed_for_null_binding(tmp_path):
    from gateway.workspace_bindings import WorkspaceBindingError, resolve_workspace_binding

    db_path, _, _ = _project_db(tmp_path)
    config = {
        "gateway": {
            "workspace_bindings": {"matrix": {"!kenya:example.com": None}}
        }
    }

    with pytest.raises(WorkspaceBindingError, match="has no project"):
        resolve_workspace_binding(config, _matrix_source(), projects_db_path=db_path)


@pytest.mark.parametrize(
    "workspace_bindings,match",
    [
        ([], "workspace_bindings is not a mapping"),
        ({"matrix": None}, "matrix are not a mapping"),
        ({"matrix": "[]"}, "matrix are not a mapping"),
    ],
)
def test_malformed_binding_shapes_fail_closed(tmp_path, workspace_bindings, match):
    from gateway.workspace_bindings import WorkspaceBindingError, resolve_workspace_binding

    db_path, _, _ = _project_db(tmp_path)
    config = {"gateway": {"workspace_bindings": workspace_bindings}}

    with pytest.raises(WorkspaceBindingError, match=match):
        resolve_workspace_binding(config, _matrix_source(), projects_db_path=db_path)


def test_configured_room_fails_closed_for_missing_project(tmp_path):
    from gateway.workspace_bindings import WorkspaceBindingError, resolve_workspace_binding

    db_path, _, _ = _project_db(tmp_path)
    config = {
        "gateway": {
            "workspace_bindings": {
                "matrix": {"!kenya:example.com": {"project": "missing"}}
            }
        }
    }

    with pytest.raises(WorkspaceBindingError, match="missing"):
        resolve_workspace_binding(config, _matrix_source(), projects_db_path=db_path)


def test_configured_room_fails_closed_when_primary_folder_disappears(tmp_path):
    from gateway.workspace_bindings import WorkspaceBindingError, resolve_workspace_binding

    db_path, _, folder = _project_db(tmp_path)
    folder.rmdir()
    config = {
        "gateway": {
            "workspace_bindings": {
                "matrix": {"!kenya:example.com": {"project": "kenya"}}
            }
        }
    }

    with pytest.raises(WorkspaceBindingError, match="does not exist"):
        resolve_workspace_binding(config, _matrix_source(), projects_db_path=db_path)


def test_bound_project_is_rendered_in_session_context(tmp_path):
    folder = tmp_path / "kenya"
    folder.mkdir()
    context = SessionContext(
        source=_matrix_source(),
        connected_platforms=[Platform.MATRIX],
        home_channels={},
        session_key="agent:matrix:room",
        session_id="session-123",
        workspace_project_id="p_kenya",
        workspace_project_slug="2026-kenya-trip",
        workspace_project_name="2026 Kenya Trip",
        workspace_cwd=str(folder),
        workspace_allowed_folders=(str(folder),),
    )

    prompt = build_session_context_prompt(context)

    assert "2026 Kenya Trip" in prompt
    assert "2026-kenya-trip" in prompt
    assert str(folder) in prompt
    assert "independent conversation" in prompt


def test_gateway_session_environment_uses_bound_project_cwd(tmp_path):
    from agent.runtime_cwd import resolve_agent_cwd
    from gateway.run import GatewayRunner

    folder = tmp_path / "kenya"
    folder.mkdir()
    context = SessionContext(
        source=_matrix_source(),
        connected_platforms=[Platform.MATRIX],
        home_channels={},
        session_key="agent:matrix:room",
        session_id="session-123",
        workspace_project_id="p_kenya",
        workspace_project_slug="2026-kenya-trip",
        workspace_project_name="2026 Kenya Trip",
        workspace_cwd=str(folder),
        workspace_allowed_folders=(str(folder),),
    )
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}

    tokens = runner._set_session_env(context)
    try:
        assert resolve_agent_cwd() == folder.resolve()
    finally:
        runner._clear_session_env(tokens)


def test_strict_gateway_config_loader_rejects_malformed_yaml(tmp_path, monkeypatch):
    from gateway.run import _load_gateway_config
    from gateway.workspace_bindings import WorkspaceBindingError

    (tmp_path / "config.yaml").write_text("gateway: [\n", encoding="utf-8")
    monkeypatch.setattr("gateway.run.get_hermes_home_override", lambda: None)
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)

    with pytest.raises(WorkspaceBindingError, match="cannot read"):
        _load_gateway_config(strict=True)


@pytest.mark.asyncio
async def test_gateway_runner_leaves_unbound_room_unchanged(monkeypatch):
    from gateway.run import GatewayRunner

    monkeypatch.setattr(
        "gateway.run._load_gateway_config", lambda **_kwargs: {"gateway": {}}
    )

    class FakeSessionDB:
        def __init__(self):
            self.replacements = []

        async def get_session(self, _session_id):
            return {"cwd": "/home/pi"}

        async def replace_session_cwd(self, session_id, cwd):
            self.replacements.append((session_id, cwd))

    runner = object.__new__(GatewayRunner)
    runner._session_db = FakeSessionDB()
    context = SessionContext(
        source=_matrix_source("!unbound:example.com"),
        connected_platforms=[Platform.MATRIX],
        home_channels={},
        session_key="agent:matrix:unbound",
        session_id="session-unbound",
    )

    resolved = await runner._apply_workspace_binding(context)

    assert resolved is None
    assert context.workspace_cwd == ""
    assert runner._session_db.replacements == []


@pytest.mark.asyncio
async def test_gateway_runner_persists_and_applies_workspace_binding(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner
    from gateway.workspace_bindings import WorkspaceBinding

    folder = tmp_path / "kenya"
    folder.mkdir()
    binding = WorkspaceBinding(
        platform="matrix",
        conversation_id="!kenya:example.com",
        project_id="p_kenya",
        project_slug="2026-kenya-trip",
        project_name="2026 Kenya Trip",
        cwd=str(folder),
        allowed_folders=(str(folder),),
    )
    monkeypatch.setattr(
        "gateway.workspace_bindings.resolve_workspace_binding",
        lambda config, source: binding,
    )
    monkeypatch.setattr(
        "gateway.run._load_gateway_config", lambda **_kwargs: {"gateway": {}}
    )

    class FakeSessionDB:
        def __init__(self):
            self.replacements = []

        async def get_session(self, session_id):
            return {"id": session_id, "cwd": "/home/pi"}

        async def replace_session_cwd(self, session_id, cwd):
            self.replacements.append((session_id, cwd))

    runner = object.__new__(GatewayRunner)
    runner._session_db = FakeSessionDB()
    context = SessionContext(
        source=_matrix_source(),
        connected_platforms=[Platform.MATRIX],
        home_channels={},
        session_key="agent:matrix:room",
        session_id="session-123",
    )

    resolved = await runner._apply_workspace_binding(context)

    assert resolved is binding
    assert context.workspace_project_id == "p_kenya"
    assert context.workspace_cwd == str(folder)
    assert runner._session_db.replacements == [("session-123", str(folder))]


@pytest.mark.asyncio
async def test_gateway_runner_persists_bound_cwd_with_real_async_session_db(
    tmp_path, monkeypatch
):
    import hermes_state
    from gateway.run import GatewayRunner
    from gateway.workspace_bindings import WorkspaceBinding
    from hermes_state import AsyncSessionDB

    folder = tmp_path / "kenya"
    folder.mkdir()
    binding = WorkspaceBinding(
        platform="matrix",
        conversation_id="!kenya:example.com",
        project_id="p_kenya",
        project_slug="2026-kenya-trip",
        project_name="2026 Kenya Trip",
        cwd=str(folder),
        allowed_folders=(str(folder),),
    )
    monkeypatch.setattr(
        "gateway.workspace_bindings.resolve_workspace_binding",
        lambda config, source: binding,
    )
    monkeypatch.setattr(
        "gateway.run._load_gateway_config", lambda **_kwargs: {"gateway": {}}
    )

    session_db = AsyncSessionDB(
        hermes_state.SessionDB(db_path=tmp_path / "state.db")
    )
    await session_db.create_session("session-real", "matrix")
    await session_db.replace_session_cwd("session-real", "/home/pi")
    runner = object.__new__(GatewayRunner)
    runner._session_db = session_db
    context = SessionContext(
        source=_matrix_source(),
        connected_platforms=[Platform.MATRIX],
        home_channels={},
        session_key="agent:matrix:room",
        session_id="session-real",
    )

    await runner._apply_workspace_binding(context)

    row = await session_db.get_session("session-real")
    assert row is not None
    assert row["cwd"] == str(folder)


@pytest.mark.asyncio
async def test_multiplex_workspace_binding_loads_config_inside_profile_scope(
    tmp_path, monkeypatch
):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from gateway.run import GatewayRunner
    from gateway.workspace_bindings import WorkspaceBinding

    folder = tmp_path / "kelly"
    folder.mkdir()
    binding = WorkspaceBinding(
        platform="matrix",
        conversation_id="!private:example.com",
        project_id="p_kelly",
        project_slug="kelly-private-workspace",
        project_name="Kelly Private Workspace",
        cwd=str(folder),
        allowed_folders=(str(folder),),
    )
    active_scope = {"name": "default"}
    seen_configs = []

    @contextmanager
    def fake_scope(_profile_home):
        old = active_scope["name"]
        active_scope["name"] = "wife"
        try:
            yield
        finally:
            active_scope["name"] = old

    monkeypatch.setattr("gateway.run._profile_runtime_scope", fake_scope)
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda **_kwargs: {"loaded_for": active_scope["name"]},
    )

    def fake_resolve(config, source):
        seen_configs.append(config)
        return binding

    monkeypatch.setattr(
        "gateway.workspace_bindings.resolve_workspace_binding",
        fake_resolve,
    )
    class FakeSessionDB:
        async def get_session(self, _session_id):
            return {"cwd": "/home/pi"}

        async def replace_session_cwd(self, _session_id, _cwd):
            return None

    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=True)
    runner._session_db = FakeSessionDB()
    runner._resolve_profile_home_for_source = lambda _source: tmp_path / "wife"
    source = _matrix_source()
    source.profile = "wife"
    context = SessionContext(
        source=source,
        connected_platforms=[Platform.MATRIX],
        home_channels={},
        session_key="agent:wife:matrix:room",
        session_id="session-wife",
    )

    await runner._apply_workspace_binding(context)

    assert seen_configs == [{"loaded_for": "wife"}]
