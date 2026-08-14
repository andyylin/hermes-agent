"""Regression tests for profile-scoped Discord policy resolution."""

import os

from gateway.config import PlatformConfig
from plugins.platforms.discord import adapter as discord_adapter
from plugins.platforms.discord.adapter import (
    _apply_yaml_config,
    _build_allowed_mentions,
    _discord_profile_bool,
    _discord_profile_policy_value,
)


class _FakeAllowedMentions:
    def __init__(self, *, everyone, roles, users, replied_user):
        self.everyone = everyone
        self.roles = roles
        self.users = users
        self.replied_user = replied_user


def test_single_profile_env_keeps_precedence_over_yaml(monkeypatch):
    monkeypatch.setattr(discord_adapter, "DISCORD_AVAILABLE", True)
    monkeypatch.setattr(
        discord_adapter.discord,
        "AllowedMentions",
        _FakeAllowedMentions,
        raising=False,
    )
    monkeypatch.setenv("DISCORD_ALLOW_MENTION_EVERYONE", "true")

    mentions = _build_allowed_mentions(
        PlatformConfig(
            enabled=True,
            extra={"allow_mentions": {"everyone": False}},
        )
    )

    assert mentions.everyone is True


def test_profile_scoped_mentions_ignore_poisoned_env(monkeypatch):
    monkeypatch.setattr(discord_adapter, "DISCORD_AVAILABLE", True)
    monkeypatch.setattr(
        discord_adapter.discord,
        "AllowedMentions",
        _FakeAllowedMentions,
        raising=False,
    )
    monkeypatch.setenv("DISCORD_ALLOW_MENTION_EVERYONE", "true")
    monkeypatch.setenv("DISCORD_ALLOW_MENTION_ROLES", "true")

    mentions = _build_allowed_mentions(
        PlatformConfig(
            enabled=True,
            extra={
                "_profile_scoped_policies": True,
                "allow_mentions": {
                    "everyone": False,
                    "roles": False,
                    "users": False,
                    "replied_user": False,
                },
            },
        )
    )

    assert mentions.everyone is False
    assert mentions.roles is False
    assert mentions.users is False
    assert mentions.replied_user is False


def test_scoped_yaml_seeds_sibling_policies_without_env_writes(monkeypatch):
    policy_env = {
        "require_mention": "DISCORD_REQUIRE_MENTION",
        "thread_require_mention": "DISCORD_THREAD_REQUIRE_MENTION",
        "bots_require_inline_mention": "DISCORD_BOTS_REQUIRE_INLINE_MENTION",
        "approval_mentions": "DISCORD_APPROVAL_MENTIONS",
        "auto_thread": "DISCORD_AUTO_THREAD",
        "history_backfill": "DISCORD_HISTORY_BACKFILL",
        "history_backfill_limit": "DISCORD_HISTORY_BACKFILL_LIMIT",
    }
    for env_name in policy_env.values():
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setattr(discord_adapter, "_profile_scoped_config_load", lambda: True)

    configured = {
        "require_mention": False,
        "thread_require_mention": True,
        "bots_require_inline_mention": True,
        "approval_mentions": True,
        "auto_thread": False,
        "history_backfill": False,
        "history_backfill_limit": 7,
    }
    seeded = _apply_yaml_config({}, configured)
    profile = PlatformConfig(enabled=True, extra=seeded)

    assert seeded["_profile_scoped_policies"] is True
    for key, env_name in policy_env.items():
        assert env_name not in os.environ
        if key == "history_backfill_limit":
            assert _discord_profile_policy_value(profile, key, env_name, 50) == 7
        else:
            assert _discord_profile_bool(profile, key, env_name, False) is bool(
                configured[key]
            )


def test_profile_scoped_sibling_policies_ignore_poisoned_env(monkeypatch):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "true")
    profile = PlatformConfig(
        enabled=True,
        extra={
            "_profile_scoped_policies": True,
            "require_mention": "false",
            "auto_thread": "false",
        },
    )

    assert _discord_profile_bool(
        profile, "require_mention", "DISCORD_REQUIRE_MENTION", True
    ) is False
    assert _discord_profile_bool(
        profile, "auto_thread", "DISCORD_AUTO_THREAD", True
    ) is False
