"""Regression tests for /sethome env-var resolution.

The `/sethome` command writes to a platform's home-target env var. Two platforms
don't follow the `{PLATFORM}_HOME_CHANNEL` convention: matrix uses
`MATRIX_HOME_ROOM` and email uses `EMAIL_HOME_ADDRESS`. Before PR #12698
`/sethome` hardcoded the `_HOME_CHANNEL` suffix, so Matrix and Email saves went
to env vars nothing read on startup — the home channel appeared to set
successfully but was lost on every new gateway session.
"""

from gateway.run import _home_target_env_var, _home_thread_env_var


def test_matrix_home_target_env_var_uses_home_room():
    assert _home_target_env_var("matrix") == "MATRIX_HOME_ROOM"


def test_email_home_target_env_var_uses_home_address():
    assert _home_target_env_var("email") == "EMAIL_HOME_ADDRESS"


def test_telegram_home_target_env_var_uses_home_channel():
    assert _home_target_env_var("telegram") == "TELEGRAM_HOME_CHANNEL"


def test_discord_home_target_env_var_uses_home_channel():
    assert _home_target_env_var("discord") == "DISCORD_HOME_CHANNEL"


def test_unknown_platform_home_target_env_var_falls_back_to_home_channel():
    assert _home_target_env_var("custom") == "CUSTOM_HOME_CHANNEL"


def test_case_insensitive_platform_name():
    assert _home_target_env_var("MATRIX") == "MATRIX_HOME_ROOM"
    assert _home_target_env_var("Email") == "EMAIL_HOME_ADDRESS"


def test_home_thread_env_var_uses_home_target_name_plus_thread_id():
    assert _home_thread_env_var("discord") == "DISCORD_HOME_CHANNEL_THREAD_ID"
    assert _home_thread_env_var("matrix") == "MATRIX_HOME_ROOM_THREAD_ID"
    assert _home_thread_env_var("email") == "EMAIL_HOME_ADDRESS_THREAD_ID"


def test_platform_has_home_channel_honors_config_backed_email_home(monkeypatch):
    from gateway import run as gateway_run
    from gateway.config import HomeChannel, Platform

    class Config:
        def get_home_channel(self, platform):
            assert platform == Platform.EMAIL
            return HomeChannel(platform=Platform.EMAIL, chat_id="andy@example.com", name="Andy")

    monkeypatch.delenv("EMAIL_HOME_ADDRESS", raising=False)
    monkeypatch.setattr(gateway_run, "load_gateway_config", lambda: Config())

    assert gateway_run._platform_has_home_channel(Platform.EMAIL) is True


def test_platform_has_home_channel_false_when_env_and_config_missing(monkeypatch):
    from gateway import run as gateway_run
    from gateway.config import Platform

    class Config:
        def get_home_channel(self, platform):
            return None

    monkeypatch.delenv("EMAIL_HOME_ADDRESS", raising=False)
    monkeypatch.setattr(gateway_run, "load_gateway_config", lambda: Config())

    assert gateway_run._platform_has_home_channel(Platform.EMAIL) is False
