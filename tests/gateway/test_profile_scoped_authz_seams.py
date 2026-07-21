"""End-to-end profile-scope regression tests for shared gateway authorization."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.secret_scope import reset_secret_scope, set_multiplex_active, set_secret_scope
from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource


@pytest.fixture
def scoped_multiplex(monkeypatch):
    for key in (
        "DISCORD_ALLOW_BOTS",
        "TELEGRAM_GROUP_ALLOWED_CHATS",
        "DISCORD_ALLOWED_USERS",
        "GATEWAY_ALLOWED_USERS",
    ):
        monkeypatch.delenv(key, raising=False)
    set_multiplex_active(True)
    scope = {}
    token = set_secret_scope(scope)
    try:
        yield scope
    finally:
        reset_secret_scope(token)
        set_multiplex_active(False)


def _runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.adapters = {}
    runner._profile_adapters = {}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    return runner


def test_bot_admission_ignores_poisoned_process_env(monkeypatch, scoped_multiplex):
    monkeypatch.setenv("DISCORD_ALLOW_BOTS", "all")
    source = SessionSource(
        platform=Platform.DISCORD,
        user_id=None,
        chat_id="discord-channel",
        user_name="foreign-bot",
        chat_type="group",
        is_bot=True,
        profile="coder",
    )
    assert _runner()._is_user_authorized(source) is False


def test_group_allowlist_ignores_poisoned_process_env(monkeypatch, scoped_multiplex):
    monkeypatch.setenv("TELEGRAM_GROUP_ALLOWED_CHATS", "foreign-chat")
    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id=None,
        chat_id="foreign-chat",
        user_name="anonymous-admin",
        chat_type="group",
        profile="coder",
    )
    assert _runner()._is_user_authorized(source) is False


def test_pairing_behavior_ignores_poisoned_process_env(monkeypatch, scoped_multiplex):
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "foreign-user")
    monkeypatch.setenv("GATEWAY_ALLOWED_USERS", "foreign-user")
    assert _runner()._get_unauthorized_dm_behavior(
        Platform.DISCORD,
        profile="coder",
    ) == "pair"


def test_missing_profile_pairing_store_fails_closed(scoped_multiplex):
    runner = _runner()
    runner.pairing_store.is_approved.return_value = True
    runner.pairing_stores = {}
    source = SessionSource(
        platform=Platform.DISCORD, user_id="paired-on-default", chat_id="dm",
        user_name="user", chat_type="dm", profile="coder",
    )
    assert runner._pairing_store_for(source) is None
    assert runner._is_user_authorized(source) is False


def test_authority_helpers_ignore_poisoned_process_env(monkeypatch, scoped_multiplex):
    from gateway import pairing
    from plugins.platforms.discord.adapter import _component_check_auth
    from plugins.platforms.matrix.adapter import _matrix_env

    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "foreign")
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")
    interaction = SimpleNamespace(
        user=SimpleNamespace(id="foreign", roles=[]),
        client=SimpleNamespace(_hermes_pairing_store=None),
    )
    assert pairing._pairing_env("DISCORD_ALLOWED_USERS") == ""
    assert _component_check_auth(interaction, set(), set()) is False
    assert _matrix_env("GATEWAY_ALLOW_ALL_USERS") == ""


def test_platform_yaml_policy_stays_in_profile_scope(monkeypatch, scoped_multiplex):
    from plugins.platforms.discord.adapter import _apply_yaml_config as apply_discord
    from plugins.platforms.matrix.adapter import _apply_yaml_config as apply_matrix

    monkeypatch.delenv("DISCORD_ALLOWED_CHANNELS", raising=False)
    monkeypatch.delenv("MATRIX_ALLOWED_USERS", raising=False)
    discord_seeded = apply_discord(
        {"discord": {"allowed_channels": ["profile-channel"]}},
        {"allowed_channels": ["profile-channel"]},
    )
    matrix_seeded = apply_matrix(
        {"matrix": {"allowed_users": ["@profile:example.org"]}},
        {"allowed_users": ["@profile:example.org"]},
    )

    assert "DISCORD_ALLOWED_CHANNELS" not in __import__("os").environ
    assert "MATRIX_ALLOWED_USERS" not in __import__("os").environ
    assert scoped_multiplex["DISCORD_ALLOWED_CHANNELS"] == "profile-channel"
    assert scoped_multiplex["MATRIX_ALLOWED_USERS"] == "@profile:example.org"
    assert discord_seeded["allowed_channels"] == ["profile-channel"]
    assert matrix_seeded["allowed_users"] == ["@profile:example.org"]


def test_platform_policy_helpers_ignore_poisoned_global_env(monkeypatch, scoped_multiplex):
    from plugins.platforms.discord.adapter import _discord_env
    from plugins.platforms.matrix.adapter import _matrix_env

    monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "foreign-channel")
    monkeypatch.setenv("MATRIX_ALLOWED_USERS", "@foreign:example.org")
    scoped_multiplex["DISCORD_ALLOWED_CHANNELS"] = "profile-channel"
    scoped_multiplex["MATRIX_ALLOWED_USERS"] = "@profile:example.org"

    assert _discord_env("DISCORD_ALLOWED_CHANNELS") == "profile-channel"
    assert _matrix_env("MATRIX_ALLOWED_USERS") == "@profile:example.org"
