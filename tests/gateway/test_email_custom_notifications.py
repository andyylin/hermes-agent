"""Regression tests for retained email notification behavior."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import MagicMock, patch

from gateway.config import PlatformConfig
from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_email = load_plugin_adapter("email")
EmailAdapter = _email.EmailAdapter


def _adapter(monkeypatch):
    monkeypatch.setenv("EMAIL_ADDRESS", "hermes@example.com")
    monkeypatch.setenv("EMAIL_PASSWORD", "test-password")
    return EmailAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "address": "hermes@example.com",
                "imap_host": "imap.example.com",
                "smtp_host": "smtp.example.com",
            },
        )
    )


def test_live_email_uses_explicit_subject_and_stable_thread_anchor(monkeypatch):
    adapter = _adapter(monkeypatch)
    smtp = MagicMock()
    monkeypatch.setattr(adapter, "_connect_smtp", lambda: smtp)

    adapter._send_email(
        "andy@example.net",
        "Daily briefing",
        metadata={
            "subject": "Morning Brief — 2026-08-02",
            "thread_anchor_key": "morning-brief",
        },
    )

    message = smtp.send_message.call_args.args[0]
    assert message["Subject"] == "Morning Brief — 2026-08-02"
    assert message["In-Reply-To"] == "<hermes-thread-morning-brief@example.com>"
    assert message["References"] == "<hermes-thread-morning-brief@example.com>"


def test_live_email_html_uses_sanitized_multipart_alternative(monkeypatch):
    adapter = _adapter(monkeypatch)
    smtp = MagicMock()
    monkeypatch.setattr(adapter, "_connect_smtp", lambda: smtp)

    adapter._send_email(
        "andy@example.net",
        '<section><span>Read </span><a href="https://example.com" '
        'onclick="steal()">this</a><script>bad()</script></section>',
        metadata={"subject": "Rich report"},
    )

    message = smtp.send_message.call_args.args[0]
    assert message.get_content_type() == "multipart/alternative"
    parts = {
        part.get_content_type(): part.get_payload(decode=True).decode("utf-8")
        for part in message.walk()
        if part.get_content_maintype() == "text"
    }
    assert "Read this" in parts["text/plain"]
    assert '<a href="https://example.com">this</a>' in parts["text/html"]
    assert "onclick" not in parts["text/html"]
    assert "<script" not in parts["text/html"]
    assert "bad()" not in parts["text/html"]


def test_live_email_explicit_subject_can_start_fresh_thread(monkeypatch):
    adapter = _adapter(monkeypatch)
    adapter._thread_context["andy@example.net"] = {
        "subject": "Old conversation",
        "message_id": "<old@example.net>",
    }
    smtp = MagicMock()
    monkeypatch.setattr(adapter, "_connect_smtp", lambda: smtp)

    adapter._send_email(
        "andy@example.net",
        "One-off alert",
        metadata={"subject": "New Alert", "suppress_threading": True},
    )

    message = smtp.send_message.call_args.args[0]
    assert message["Subject"] == "New Alert"
    assert "In-Reply-To" not in message
    assert "References" not in message


def test_standalone_html_is_multipart_with_plain_fallback_and_thread_anchor(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "test-password")
    smtp = MagicMock()
    with patch.object(_email.smtplib, "SMTP", return_value=smtp):
        result = asyncio.run(
            _email._standalone_send(
                MagicMock(extra={"address": "hermes@example.com", "smtp_host": "smtp.example.com"}),
                "andy@example.net",
                "<h1>Brief</h1><p>All good.</p>",
                subject={"subject": "Daily Brief", "thread_anchor_key": "daily-brief"},
            )
        )

    assert result["success"] is True
    message = smtp.send_message.call_args.args[0]
    assert message.get_content_type() == "multipart/alternative"
    parts = list(message.walk())
    assert any(part.get_content_type() == "text/plain" for part in parts)
    assert any(part.get_content_type() == "text/html" for part in parts)
    assert message["Subject"] == "Daily Brief"
    assert message["In-Reply-To"] == "<hermes-thread-daily-brief@example.com>"
    assert result["message_id"] == message["Message-ID"]


def test_standalone_detects_common_html_fragments(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "test-password")
    smtp = MagicMock()
    with patch.object(_email.smtplib, "SMTP", return_value=smtp):
        result = asyncio.run(
            _email._standalone_send(
                MagicMock(extra={"address": "hermes@example.com", "smtp_host": "smtp.example.com"}),
                "andy@example.net",
                '<span>See <a href="https://example.com">report</a></span>',
            )
        )

    assert result["success"] is True
    message = smtp.send_message.call_args.args[0]
    html_part = next(
        part for part in message.walk() if part.get_content_type() == "text/html"
    )
    html_body = html_part.get_payload(decode=True).decode("utf-8")
    assert '<a href="https://example.com">report</a>' in html_body
    assert "&lt;a" not in html_body


def test_cron_email_subject_template_and_thread_payload(monkeypatch):
    from cron import scheduler

    monkeypatch.setattr(scheduler, "_hermes_now", lambda: datetime(2026, 8, 2, 9, 0))
    payload = scheduler._format_cron_email_subject(
        {
            "id": "brief-job",
            "name": "Morning briefing",
            "email_subject_template": "{job_name} — {date}",
            "email_thread_key": "morning-brief",
        }
    )

    assert payload == {
        "subject": "Morning briefing — 2026-08-02",
        "thread_anchor_key": "morning-brief",
    }


def test_invalid_cron_email_subject_template_falls_back_to_default(monkeypatch):
    from cron import scheduler

    monkeypatch.setattr(scheduler, "_hermes_now", lambda: datetime(2026, 8, 2, 9, 0))
    assert scheduler._format_cron_email_subject(
        {"id": "brief-job", "email_subject_template": "{unknown}"}
    ) is None


def test_positional_cron_email_subject_template_falls_back_to_default(monkeypatch):
    from cron import scheduler

    monkeypatch.setattr(scheduler, "_hermes_now", lambda: datetime(2026, 8, 2, 9, 0))
    assert scheduler._format_cron_email_subject(
        {"id": "brief-job", "email_subject_template": "{0}"}
    ) is None


def test_thread_anchor_distinguishes_normalization_collisions():
    slash = _email._stable_thread_message_id("a/b", "example.com")
    dash = _email._stable_thread_message_id("a-b", "example.com")

    assert slash != dash


def test_thread_anchor_distinguishes_long_keys_with_same_prefix():
    prefix = "a" * 80
    first = _email._stable_thread_message_id(prefix + "-first", "example.com")
    second = _email._stable_thread_message_id(prefix + "-second", "example.com")

    assert first != second
