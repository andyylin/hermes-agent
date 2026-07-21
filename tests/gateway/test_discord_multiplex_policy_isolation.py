"""Regression coverage for profile-bound Discord policy in multiplex gateways."""

import os

import pytest

from agent.secret_scope import set_multiplex_active
from gateway.platforms.base import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter, _apply_yaml_config


@pytest.fixture(autouse=True)
def _reset_multiplex_mode(monkeypatch):
    for name in (
        "DISCORD_ALLOWED_CHANNELS",
        "DISCORD_ALLOWED_USERS",
        "DISCORD_ALLOW_ALL_USERS",
        "DISCORD_AUTO_THREAD",
        "DISCORD_REPLY_TO_MODE",
        "DISCORD_REQUIRE_MENTION",
    ):
        monkeypatch.delenv(name, raising=False)
    set_multiplex_active(False)
    yield
    set_multiplex_active(False)


def _adapter(discord_config: dict) -> DiscordAdapter:
    extra = _apply_yaml_config({"discord": discord_config}, discord_config) or {}
    return DiscordAdapter(PlatformConfig(enabled=True, token="token", extra=extra))


def test_multiplex_adapters_keep_distinct_authorization_and_routing_policy(monkeypatch):
    monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "process-poison")
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    set_multiplex_active(True)

    alpha = _adapter(
        {
            "allowed_channels": ["alpha-channel"],
            "allow_from": ["alpha-user"],
            "auto_thread": False,
            "require_mention": False,
            "reply_to_mode": "all",
        }
    )
    beta = _adapter(
        {
            "allowed_channels": ["beta-channel"],
            "allow_from": ["beta-user"],
            "auto_thread": True,
            "require_mention": True,
            "reply_to_mode": "off",
        }
    )

    assert alpha._discord_env("DISCORD_ALLOWED_CHANNELS", "") == "alpha-channel"
    assert beta._discord_env("DISCORD_ALLOWED_CHANNELS", "") == "beta-channel"
    assert alpha._discord_env("DISCORD_ALLOWED_USERS", "") == "alpha-user"
    assert beta._discord_env("DISCORD_ALLOWED_USERS", "") == "beta-user"
    assert alpha._discord_env("DISCORD_AUTO_THREAD", "true") == "false"
    assert beta._discord_env("DISCORD_AUTO_THREAD", "false") == "true"
    assert alpha._discord_require_mention() is False
    assert beta._discord_require_mention() is True
    assert alpha._reply_to_mode == "all"
    assert beta._reply_to_mode == "off"


def test_multiplex_adapter_does_not_inherit_absent_policy_from_process_env(monkeypatch):
    monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "other-profile-channel")
    monkeypatch.setenv("DISCORD_ALLOW_ALL_USERS", "true")
    set_multiplex_active(True)

    adapter = _adapter({})

    assert adapter._discord_env("DISCORD_ALLOWED_CHANNELS", "") == ""
    assert adapter._discord_env("DISCORD_ALLOW_ALL_USERS", "") == ""


def test_multiplex_yaml_bridge_does_not_mutate_process_environment(monkeypatch):
    set_multiplex_active(True)

    adapter = _adapter(
        {
            "allowed_channels": ["private-channel"],
            "allow_from": ["private-user"],
            "require_mention": False,
        }
    )

    assert "DISCORD_ALLOWED_CHANNELS" not in os.environ
    assert "DISCORD_ALLOWED_USERS" not in os.environ
    assert "DISCORD_REQUIRE_MENTION" not in os.environ
    assert adapter._discord_env("DISCORD_ALLOWED_CHANNELS", "") == "private-channel"


def test_single_profile_explicit_environment_overrides_yaml_snapshot(monkeypatch):
    monkeypatch.setenv("DISCORD_ALLOWED_CHANNELS", "environment-channel")
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    set_multiplex_active(False)

    adapter = _adapter(
        {
            "allowed_channels": ["yaml-channel"],
            "require_mention": False,
        }
    )

    assert adapter._discord_env("DISCORD_ALLOWED_CHANNELS", "") == "environment-channel"
    assert adapter._discord_require_mention() is True


def test_multiplex_discord_ignores_global_gateway_allow_all(monkeypatch):
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")
    set_multiplex_active(True)
    adapter = _adapter({})
    adapter._is_pairing_approved_user = lambda _user_id: False

    assert adapter._is_allowed_user("poisoned-user", author=None, guild=None, is_dm=True) is False
