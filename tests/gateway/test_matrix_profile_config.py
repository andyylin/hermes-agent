import pytest

from agent.secret_scope import set_multiplex_active
from gateway.config import PlatformConfig
from plugins.platforms.matrix.adapter import MatrixAdapter, _apply_yaml_config


@pytest.fixture(autouse=True)
def _reset_multiplex_mode():
    set_multiplex_active(False)
    yield
    set_multiplex_active(False)


def test_matrix_yaml_bridge_returns_profile_local_settings(monkeypatch):
    # Simulate a multiplex process whose default profile already populated
    # process-global Matrix env vars. The wife adapter must still use its own
    # config.extra values rather than inheriting the default bot's room scope.
    monkeypatch.setenv("MATRIX_ALLOWED_ROOMS", "!andy:example.com")
    monkeypatch.setenv("MATRIX_FREE_RESPONSE_ROOMS", "!andy:example.com")
    monkeypatch.setenv("MATRIX_AUTO_THREAD_ROOMS", "!andy:example.com")
    monkeypatch.setenv("MATRIX_SESSION_SCOPE", "thread")
    monkeypatch.setenv("MATRIX_AUTO_THREAD", "true")
    monkeypatch.setenv("MATRIX_DM_AUTO_THREAD", "false")
    monkeypatch.setenv("MATRIX_DM_MENTION_THREADS", "true")
    monkeypatch.setenv("MATRIX_PROCESS_NOTICES", "true")
    monkeypatch.setenv("MATRIX_ALLOW_ROOM_MENTIONS", "true")
    monkeypatch.setenv("MATRIX_REACTIONS", "true")
    set_multiplex_active(True)

    matrix_cfg = {
        "homeserver": "http://127.0.0.1:8008",
        "user_id": "@joi-kelly:example.com",
        "require_mention": True,
        "free_response_rooms": "!kelly:example.com",
        "allowed_rooms": "!kelly:example.com",
        "allowed_users": "@kelly:example.com",
        "session_scope": "room",
        "auto_thread": False,
        "auto_thread_rooms": "!kelly:example.com",
        "dm_auto_thread": True,
        "dm_mention_threads": False,
        "process_notices": False,
        "allow_room_mentions": False,
        "reactions": False,
        "device_id": "HERMES_WIFE_MATRIX_1",
        "e2ee_mode": "required",
        "home_room": "!kelly:example.com",
    }

    seeded = _apply_yaml_config({"matrix": matrix_cfg}, matrix_cfg)

    assert seeded is not None
    assert seeded["allowed_rooms"] == "!kelly:example.com"
    assert seeded["allowed_users"] == "@kelly:example.com"
    assert seeded["homeserver"] == "http://127.0.0.1:8008"
    assert seeded["user_id"] == "@joi-kelly:example.com"
    assert seeded["e2ee_mode"] == "required"

    adapter = MatrixAdapter(PlatformConfig(enabled=True, extra=seeded))
    assert adapter._allowed_rooms == {"!kelly:example.com"}
    assert adapter._allowed_user_ids == {"@kelly:example.com"}
    assert adapter._free_rooms == {"!kelly:example.com"}
    assert adapter._auto_thread_rooms == {"!kelly:example.com"}
    assert adapter._matrix_session_scope == "room"
    assert adapter._auto_thread is False
    assert adapter._dm_auto_thread is True
    assert adapter._dm_mention_threads is False
    assert adapter._process_notices is False
    assert adapter._allow_room_mentions is False
    assert adapter._reactions_enabled is False
    set_multiplex_active(False)
