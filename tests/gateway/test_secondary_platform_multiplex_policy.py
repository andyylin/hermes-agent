"""Profile-scoped policy regression coverage for bundled secondary adapters."""

import pytest

from agent.secret_scope import (
    UnscopedSecretError,
    reset_secret_scope,
    set_multiplex_active,
    set_secret_scope,
)
from gateway.pairing import _pairing_env
from plugins.platforms.line.adapter import _scoped_env as line_env
from plugins.platforms.matrix.adapter import _matrix_secret
from plugins.platforms.slack.adapter import _scoped_env as slack_env
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
