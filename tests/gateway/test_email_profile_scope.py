"""Profile-scope regression tests for live email configuration."""

from unittest.mock import patch

from agent.secret_scope import reset_secret_scope, set_multiplex_active, set_secret_scope
from gateway.config import PlatformConfig
from plugins.platforms.email.adapter import EmailAdapter, check_email_requirements


def _install_scope(values):
    set_multiplex_active(True)
    return set_secret_scope(values)


def _reset_scope(token):
    reset_secret_scope(token)
    set_multiplex_active(False)


def test_requirements_use_profile_scope_not_process_env():
    with patch.dict(
        "os.environ",
        {
            "EMAIL_ADDRESS": "wrong@example.com",
            "EMAIL_PASSWORD": "wrong-password",
            "EMAIL_IMAP_HOST": "wrong.imap.example.com",
            "EMAIL_SMTP_HOST": "wrong.smtp.example.com",
        },
        clear=False,
    ):
        token = _install_scope({})
        try:
            assert check_email_requirements() is False
        finally:
            _reset_scope(token)


def test_live_adapter_uses_profile_scoped_connection_settings():
    with patch.dict(
        "os.environ",
        {
            "EMAIL_ADDRESS": "wrong@example.com",
            "EMAIL_PASSWORD": "wrong-password",
            "EMAIL_IMAP_HOST": "wrong.imap.example.com",
            "EMAIL_IMAP_PORT": "1993",
            "EMAIL_SMTP_HOST": "wrong.smtp.example.com",
            "EMAIL_SMTP_PORT": "1587",
        },
        clear=False,
    ):
        token = _install_scope({
            "EMAIL_ADDRESS": "scoped@example.com",
            "EMAIL_PASSWORD": "scoped-password",
            "EMAIL_IMAP_HOST": "scoped.imap.example.com",
            "EMAIL_IMAP_PORT": "993",
            "EMAIL_SMTP_HOST": "scoped.smtp.example.com",
            "EMAIL_SMTP_PORT": "587",
            "EMAIL_ALLOWED_USERS": "allowed@example.com",
        })
        try:
            adapter = EmailAdapter(PlatformConfig(enabled=True, extra={}))
            assert adapter._address == "scoped@example.com"
            assert adapter._password == "scoped-password"
            assert adapter._imap_host == "scoped.imap.example.com"
            assert adapter._imap_port == 993
            assert adapter._smtp_host == "scoped.smtp.example.com"
            assert adapter._smtp_port == 587
            assert adapter._allowlist_in_effect() is True
            assert adapter._allow_all_senders() is False
        finally:
            _reset_scope(token)
