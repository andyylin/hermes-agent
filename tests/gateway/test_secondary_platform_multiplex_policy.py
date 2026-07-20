"""Profile-scoped policy regression coverage for bundled secondary adapters."""

import os

import pytest

from agent.secret_scope import (
    UnscopedSecretError,
    profile_env_bool,
    profile_env_float,
    profile_env_int,
    reset_secret_scope,
    set_multiplex_active,
    set_secret_scope,
)
from gateway.config import _getenv as gateway_env
from gateway.pairing import _pairing_env, _sync_allowlist_add, _sync_allowlist_remove
from plugins.platforms.line.adapter import _scoped_env as line_env
from plugins.platforms.line.adapter import _startup_env as line_startup_env
from plugins.platforms.feishu.adapter import _apply_yaml_config as apply_feishu_yaml_config
from plugins.platforms.matrix.adapter import _matrix_secret
from plugins.platforms.slack.adapter import _apply_yaml_config as apply_slack_yaml_config
from plugins.platforms.slack.adapter import _scoped_env as slack_env
from plugins.platforms.telegram.adapter import _apply_yaml_config as apply_telegram_yaml_config
from plugins.platforms.wecom.adapter import _scoped_env as wecom_env


@pytest.fixture(autouse=True)
def _reset_scope():
    set_multiplex_active(False)
    yield
    set_multiplex_active(False)


@pytest.mark.parametrize(
    ("resolver", "name"),
    [
        (wecom_env, "WECOM_ALLOWED_USERS"),
        (line_env, "LINE_ALLOWED_USERS"),
        (slack_env, "SLACK_ALLOWED_USERS"),
    ],
)
def test_policy_resolvers_fail_closed_without_a_multiplex_scope(monkeypatch, resolver, name):
    monkeypatch.setenv(name, "process-poison")
    set_multiplex_active(True)

    with pytest.raises(UnscopedSecretError):
        resolver(name)


@pytest.mark.parametrize(
    ("resolver", "name"),
    [
        (wecom_env, "WECOM_ALLOWED_USERS"),
        (line_env, "LINE_ALLOWED_USERS"),
        (slack_env, "SLACK_ALLOWED_USERS"),
    ],
)
def test_policy_resolvers_use_only_the_active_profile_scope(monkeypatch, resolver, name):
    monkeypatch.setenv(name, "process-poison")
    set_multiplex_active(True)
    token = set_secret_scope({name: "profile-value"})

    try:
        assert resolver(name) == "profile-value"
    finally:
        reset_secret_scope(token)


@pytest.mark.parametrize(
    ("resolver", "name"),
    [
        (wecom_env, "WECOM_ALLOWED_USERS"),
        (line_env, "LINE_ALLOWED_USERS"),
        (slack_env, "SLACK_ALLOWED_USERS"),
    ],
)
def test_policy_resolvers_preserve_single_profile_environment_fallback(monkeypatch, resolver, name):
    monkeypatch.setenv(name, "legacy-value")

    assert resolver(name) == "legacy-value"


@pytest.mark.parametrize(
    ("resolver", "name"),
    [
        (_matrix_secret, "MATRIX_ACCESS_TOKEN"),
        (_pairing_env, "DISCORD_ALLOWED_USERS"),
        (line_startup_env, "LINE_CHANNEL_ACCESS_TOKEN"),
    ],
)
def test_startup_policy_resolvers_ignore_process_env_without_a_multiplex_scope(monkeypatch, resolver, name):
    monkeypatch.setenv(name, "process-poison")
    set_multiplex_active(True)

    assert resolver(name) == ""


@pytest.mark.parametrize(
    ("resolver", "name"),
    [
        (_matrix_secret, "MATRIX_ACCESS_TOKEN"),
        (_pairing_env, "DISCORD_ALLOWED_USERS"),
        (line_startup_env, "LINE_CHANNEL_ACCESS_TOKEN"),
    ],
)
def test_startup_policy_resolvers_use_the_active_profile_scope(monkeypatch, resolver, name):
    monkeypatch.setenv(name, "process-poison")
    set_multiplex_active(True)
    token = set_secret_scope({name: "profile-value"})

    try:
        assert resolver(name) == "profile-value"
    finally:
        reset_secret_scope(token)


def test_slack_yaml_bridge_does_not_mutate_process_policy_in_multiplex_mode(monkeypatch):
    names = ("SLACK_REQUIRE_MENTION", "SLACK_ALLOW_BOTS", "SLACK_ALLOWED_CHANNELS")
    for name in names:
        monkeypatch.delenv(name, raising=False)
    set_multiplex_active(True)

    extra = apply_slack_yaml_config(
        {},
        {"allow_bots": True, "allowed_channels": ["C1"], "require_mention": False},
    )

    assert extra == {
        "allow_bots": True,
        "allowed_channels": ["C1"],
        "require_mention": False,
    }
    assert all(name not in os.environ for name in names)


@pytest.mark.parametrize(
    ("apply_yaml_config", "config", "names", "expected"),
    [
        (
            apply_feishu_yaml_config,
            {"allow_bots": "mentions", "default_group_policy": "allowlist"},
            ("FEISHU_ALLOW_BOTS",),
            {"allow_bots": "mentions", "default_group_policy": "allowlist"},
        ),
        (
            apply_telegram_yaml_config,
            {"allow_from": ["42"], "require_mention": False},
            ("TELEGRAM_ALLOWED_USERS", "TELEGRAM_REQUIRE_MENTION"),
            {
                "allow_from": ["42"],
                "require_mention": False,
            },
        ),
    ],
)
def test_yaml_bridges_return_profile_policy_without_mutating_process_env(
    monkeypatch,
    apply_yaml_config,
    config,
    names,
    expected,
):
    for name in names:
        monkeypatch.delenv(name, raising=False)
    set_multiplex_active(True)

    assert apply_yaml_config({}, config) == expected
    assert all(name not in os.environ for name in names)


def test_typed_profile_env_uses_scope_instead_of_poisoned_process_values(monkeypatch):
    monkeypatch.setenv("PROFILE_INT", "999")
    monkeypatch.setenv("PROFILE_FLOAT", "999.0")
    monkeypatch.setenv("PROFILE_BOOL", "false")
    set_multiplex_active(True)
    token = set_secret_scope(
        {"PROFILE_INT": "7", "PROFILE_FLOAT": "1.5", "PROFILE_BOOL": "true"}
    )

    try:
        assert profile_env_int("PROFILE_INT", 0) == 7
        assert profile_env_float("PROFILE_FLOAT", 0.0) == 1.5
        assert profile_env_bool("PROFILE_BOOL", False) is True
    finally:
        reset_secret_scope(token)


def test_typed_profile_env_preserves_invalid_value_fallbacks():
    token = set_secret_scope({"PROFILE_INT": "garbage", "PROFILE_FLOAT": "garbage"})

    try:
        assert profile_env_int("PROFILE_INT", 7) == 7
        assert profile_env_float("PROFILE_FLOAT", 1.5) == 1.5
    finally:
        reset_secret_scope(token)


def test_generic_gateway_config_env_fails_closed_without_multiplex_scope(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "process-poison")
    set_multiplex_active(True)

    assert gateway_env("TELEGRAM_ALLOWED_USERS", "") == ""


def test_pairing_allowlist_sync_does_not_mutate_process_env_in_multiplex_mode(monkeypatch):
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "alpha")
    set_multiplex_active(True)

    _sync_allowlist_add("discord", "beta")
    _sync_allowlist_remove("discord", "alpha")

    assert os.environ["DISCORD_ALLOWED_USERS"] == "alpha"
