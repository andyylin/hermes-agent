from __future__ import annotations

from unittest.mock import MagicMock

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource

# Import the plugin module so Platform("line") is accepted as a dynamic platform.
from tests.gateway._plugin_adapter_loader import load_plugin_adapter

load_plugin_adapter("line")


def _runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    runner.adapters = {}
    return runner


def test_line_group_authorized_by_allowed_group_chat_id(monkeypatch):
    monkeypatch.setenv("LINE_ALLOWED_GROUPS", "Callowed")
    monkeypatch.setenv("LINE_ALLOWED_USERS", "Uandy")
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

    source = SessionSource(
        platform=Platform("line"),
        chat_id="Callowed",
        chat_type="group",
        user_id="Uother",
        user_name="Uother",
    )

    assert _runner()._is_user_authorized(source) is True


def test_line_dm_not_authorized_by_allowed_group(monkeypatch):
    monkeypatch.setenv("LINE_ALLOWED_GROUPS", "Callowed")
    monkeypatch.setenv("LINE_ALLOWED_USERS", "Uandy")
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

    source = SessionSource(
        platform=Platform("line"),
        chat_id="Uother",
        chat_type="dm",
        user_id="Uother",
        user_name="Uother",
    )

    assert _runner()._is_user_authorized(source) is False


def test_line_archive_group_is_authorized_to_dispatch(monkeypatch):
    monkeypatch.delenv("LINE_ALLOWED_GROUPS", raising=False)
    monkeypatch.setenv("LINE_ARCHIVE_GROUPS", "Carchive")
    monkeypatch.setenv("LINE_ALLOWED_USERS", "Uandy")
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

    source = SessionSource(
        platform=Platform("line"),
        chat_id="Carchive",
        chat_type="group",
        user_id="Uother",
        user_name="Uother",
    )

    assert _runner()._is_user_authorized(source) is True


def test_line_archive_group_uses_profile_scoped_adapter_config(monkeypatch):
    monkeypatch.delenv("LINE_ALLOWED_GROUPS", raising=False)
    monkeypatch.delenv("LINE_ARCHIVE_GROUPS", raising=False)
    monkeypatch.setenv("LINE_ALLOWED_USERS", "Uandy")
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

    platform = Platform("line")
    source = SessionSource(
        platform=platform,
        chat_id="Cprofile",
        chat_type="group",
        user_id="Uother",
        user_name="Uother",
    )
    runner = _runner()
    adapter = MagicMock()
    adapter.config.extra = {"archive_groups": ["Cprofile"]}
    runner.adapters = {platform: adapter}

    assert runner._is_user_authorized(source) is True
