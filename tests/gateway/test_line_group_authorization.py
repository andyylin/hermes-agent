from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from gateway.config import Platform, PlatformConfig
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


def _line_adapter(*, allowed=(), archive=(), read_only=(), require_prefix=()) -> Any:
    return SimpleNamespace(
        allowed_groups=set(allowed),
        archive_groups=set(archive),
        read_only_groups=set(read_only),
        require_prefix_groups=set(require_prefix),
        config=PlatformConfig(enabled=True, extra={}),
    )


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

    runner = _runner()
    runner.adapters = {Platform("line"): _line_adapter(allowed={"Callowed"})}
    assert runner._is_user_authorized(source) is True


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

    runner = _runner()
    runner.adapters = {Platform("line"): _line_adapter(archive={"Carchive"})}
    assert runner._is_user_authorized(source) is True


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
    adapter = _line_adapter(archive={"Cprofile"})
    runner.adapters = {platform: adapter}

    assert runner._is_user_authorized(source) is True


def test_line_prefix_group_is_authorized_from_profile_scoped_adapter(monkeypatch):
    monkeypatch.delenv("LINE_ALLOWED_GROUPS", raising=False)
    monkeypatch.delenv("LINE_ARCHIVE_GROUPS", raising=False)
    monkeypatch.setenv("LINE_ALLOWED_USERS", "Uandy")
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

    platform = Platform("line")
    source = SessionSource(
        platform=platform,
        chat_id="Cprefix-only",
        chat_type="group",
        user_id="Uother",
        user_name="Uother",
    )
    runner = _runner()
    runner.adapters = {
        platform: _line_adapter(require_prefix={"Cprefix-only"})
    }

    assert runner._is_user_authorized(source) is True


def test_line_process_env_does_not_cross_profile_adapter_boundary(monkeypatch):
    monkeypatch.setenv("LINE_ALLOWED_GROUPS", "Cother-profile")
    monkeypatch.setenv("LINE_ALLOWED_USERS", "Uandy")
    monkeypatch.setenv("LINE_ALLOW_ALL_USERS", "false")
    monkeypatch.delenv("LINE_ARCHIVE_GROUPS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

    platform = Platform("line")
    source = SessionSource(
        platform=platform,
        chat_id="Cother-profile",
        chat_type="group",
        user_id="Uother",
        user_name="Uother",
    )
    runner = _runner()
    adapter = _line_adapter(allowed={"Cactive-profile"})
    runner.adapters = {platform: adapter}

    assert runner._is_user_authorized(source) is False


def test_line_unauthorized_dm_behavior_ignores_poisoned_global_policy(monkeypatch):
    from agent import secret_scope as ss

    monkeypatch.setenv("LINE_ALLOWED_GROUPS", "Cother-profile")
    monkeypatch.setenv("LINE_ARCHIVE_GROUPS", "Cother-profile")
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)

    platform = Platform("line")
    runner = _runner()
    runner.adapters = {platform: _line_adapter()}

    ss.set_multiplex_active(True)
    token = ss.set_secret_scope({})
    try:
        assert runner._get_unauthorized_dm_behavior(platform) == "pair"
    finally:
        ss.reset_secret_scope(token)
        ss.set_multiplex_active(False)


def test_line_unauthorized_dm_behavior_uses_active_adapter_policy(monkeypatch):
    monkeypatch.delenv("LINE_ALLOWED_GROUPS", raising=False)
    monkeypatch.delenv("LINE_ARCHIVE_GROUPS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)

    platform = Platform("line")
    runner = _runner()
    runner.adapters = {platform: _line_adapter(allowed={"Cactive-profile"})}

    assert runner._get_unauthorized_dm_behavior(platform) == "ignore"


def test_line_read_only_group_is_not_dispatched_through_gateway(monkeypatch):
    monkeypatch.delenv("LINE_ALLOWED_GROUPS", raising=False)
    monkeypatch.setenv("LINE_READ_ONLY_GROUPS", "Creadonly")
    monkeypatch.setenv("LINE_ALLOWED_USERS", "Uandy")
    monkeypatch.setenv("LINE_ALLOW_ALL_USERS", "false")
    monkeypatch.delenv("LINE_ARCHIVE_GROUPS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

    platform = Platform("line")
    source = SessionSource(
        platform=platform,
        chat_id="Creadonly",
        chat_type="group",
        user_id="Uother",
        user_name="Uother",
    )
    runner = _runner()
    adapter = _line_adapter(read_only={"Creadonly"})
    runner.adapters = {platform: adapter}

    # Read-only messages are archived and returned inside LineAdapter before
    # MessageEvent dispatch, so gateway authorization must stay closed.
    assert runner._is_user_authorized(source) is False
