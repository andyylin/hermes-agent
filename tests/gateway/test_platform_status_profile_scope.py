"""Multiplex adapter status/YAML probes must not inherit process-global state."""

import os

import pytest

from agent.secret_scope import reset_secret_scope, set_multiplex_active, set_secret_scope
from gateway.config import Platform, PlatformConfig, load_gateway_config


@pytest.fixture(autouse=True)
def _reset_multiplex_state():
    set_multiplex_active(False)
    yield
    set_multiplex_active(False)


def test_status_probes_ignore_poisoned_global_credentials_in_empty_profile(monkeypatch):
    from plugins.platforms.email.adapter import _is_connected as email_connected
    from plugins.platforms.telegram.adapter import _is_connected as telegram_connected
    from plugins.platforms.whatsapp.adapter import _is_connected as whatsapp_connected

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "poisoned-default-token")
    monkeypatch.setenv("EMAIL_ADDRESS", "poisoned@example.com")
    monkeypatch.setenv("WHATSAPP_ENABLED", "true")
    set_multiplex_active(True)
    token = set_secret_scope({})
    try:
        assert telegram_connected(PlatformConfig(enabled=False)) is False
        assert email_connected(PlatformConfig(enabled=False)) is False
        assert whatsapp_connected(PlatformConfig(enabled=False)) is False
    finally:
        reset_secret_scope(token)


def test_status_probes_use_only_the_active_profile_scope(monkeypatch):
    from plugins.platforms.email.adapter import _is_connected as email_connected
    from plugins.platforms.telegram.adapter import _is_connected as telegram_connected
    from plugins.platforms.whatsapp.adapter import _is_connected as whatsapp_connected

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "poisoned-default-token")
    monkeypatch.setenv("EMAIL_ADDRESS", "poisoned@example.com")
    monkeypatch.setenv("WHATSAPP_ENABLED", "false")
    set_multiplex_active(True)
    token = set_secret_scope(
        {
            "TELEGRAM_BOT_TOKEN": "profile-token",
            "EMAIL_ADDRESS": "profile@example.com",
            "WHATSAPP_ENABLED": "true",
        }
    )
    try:
        assert telegram_connected(PlatformConfig(enabled=False)) is True
        assert email_connected(PlatformConfig(enabled=False)) is True
        assert whatsapp_connected(PlatformConfig(enabled=False)) is True
    finally:
        reset_secret_scope(token)


def test_whatsapp_yaml_bridge_is_profile_local_in_multiplex(monkeypatch):
    from plugins.platforms.whatsapp.adapter import _apply_yaml_config

    monkeypatch.setenv("WHATSAPP_DM_POLICY", "poisoned-default")
    monkeypatch.delenv("WHATSAPP_ALLOWED_USERS", raising=False)
    set_multiplex_active(True)

    seeded = _apply_yaml_config(
        {},
        {"dm_policy": "allowlist", "allow_from": ["profile-user"]},
    )

    assert seeded == {"dm_policy": "allowlist", "allow_from": ["profile-user"]}
    assert os.environ["WHATSAPP_DM_POLICY"] == "poisoned-default"
    assert "WHATSAPP_ALLOWED_USERS" not in os.environ


def test_top_level_telegram_mention_bridge_does_not_mutate_global_env_in_multiplex(
    monkeypatch, tmp_path
):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text("require_mention: true\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("TELEGRAM_REQUIRE_MENTION", raising=False)
    set_multiplex_active(True)
    token = set_secret_scope({})
    try:
        config = load_gateway_config()
    finally:
        reset_secret_scope(token)

    assert config.platforms[Platform.TELEGRAM].extra["require_mention"] is True
    assert "TELEGRAM_REQUIRE_MENTION" not in os.environ
