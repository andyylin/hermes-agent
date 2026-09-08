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


def test_email_url_image_preserves_notification_metadata(monkeypatch):
    adapter = _adapter(monkeypatch)
    adapter._thread_context["andy@example.net"] = {
        "subject": "Old conversation",
        "message_id": "<old@example.net>",
    }
    smtp = MagicMock()
    monkeypatch.setattr(adapter, "_connect_smtp", lambda: smtp)

    asyncio.run(
        adapter.send_image(
            "andy@example.net",
            "https://example.com/report.png",
            caption="Daily chart",
            metadata={
                "subject": "Daily Media Brief",
                "thread_anchor_key": "daily-media",
            },
        )
    )

    message = smtp.send_message.call_args.args[0]
    assert message["Subject"] == "Daily Media Brief"
    assert message["In-Reply-To"] == "<hermes-thread-daily-media@example.com>"
    assert message["References"] == "<hermes-thread-daily-media@example.com>"


def test_email_multiple_images_preserve_notification_metadata(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch)
    adapter._thread_context["andy@example.net"] = {
        "subject": "Old conversation",
        "message_id": "<old@example.net>",
    }
    image = tmp_path / "chart.png"
    image.write_bytes(b"fake image")
    smtp = MagicMock()
    monkeypatch.setattr(adapter, "_connect_smtp", lambda: smtp)

    asyncio.run(
        adapter.send_multiple_images(
            "andy@example.net",
            [(image.as_uri(), "Daily chart")],
            metadata={
                "subject": "Daily Media Brief",
                "suppress_threading": True,
            },
        )
    )

    message = smtp.send_message.call_args.args[0]
    assert message["Subject"] == "Daily Media Brief"
    assert "In-Reply-To" not in message
    assert "References" not in message


def test_email_document_preserves_notification_metadata(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch)
    adapter._thread_context["andy@example.net"] = {
        "subject": "Old conversation",
        "message_id": "<old@example.net>",
    }
    document = tmp_path / "report.pdf"
    document.write_bytes(b"fake pdf")
    smtp = MagicMock()
    monkeypatch.setattr(adapter, "_connect_smtp", lambda: smtp)

    result = asyncio.run(
        adapter.send_document(
            "andy@example.net",
            str(document),
            caption="Daily report",
            metadata={
                "subject": "Daily Media Brief",
                "thread_anchor_key": "daily-media",
            },
        )
    )

    assert result.success is True
    message = smtp.send_message.call_args.args[0]
    assert message["Subject"] == "Daily Media Brief"
    assert message["In-Reply-To"] == "<hermes-thread-daily-media@example.com>"
    assert message["References"] == "<hermes-thread-daily-media@example.com>"


def test_email_media_fallbacks_preserve_notification_metadata(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch)
    adapter._thread_context["andy@example.net"] = {
        "subject": "Old conversation",
        "message_id": "<old@example.net>",
    }
    media = tmp_path / "media.bin"
    media.write_bytes(b"media")
    smtp = MagicMock()
    monkeypatch.setattr(adapter, "_connect_smtp", lambda: smtp)

    methods = (
        (adapter.send_image_file, {"image_path": str(media)}),
        (adapter.send_voice, {"audio_path": str(media)}),
        (adapter.send_video, {"video_path": str(media)}),
    )
    for method, media_arg in methods:
        smtp.send_message.reset_mock()
        asyncio.run(
            method(
                chat_id="andy@example.net",
                metadata={
                    "subject": "Fallback Media Brief",
                    "suppress_threading": True,
                },
                **media_arg,
            )
        )
        message = smtp.send_message.call_args.args[0]
        assert message["Subject"] == "Fallback Media Brief"
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


def test_standalone_email_sends_media_only_attachment_with_notification_headers(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("EMAIL_PASSWORD", "test-password")
    media = tmp_path / "daily-report.pdf"
    media.write_bytes(b"fake pdf")
    smtp = MagicMock()

    with patch.object(_email.smtplib, "SMTP", return_value=smtp):
        result = asyncio.run(
            _email._standalone_send(
                MagicMock(
                    extra={
                        "address": "hermes@example.com",
                        "smtp_host": "smtp.example.com",
                    }
                ),
                "andy@example.net",
                "",
                media_files=[(str(media), False)],
                subject={
                    "subject": "Daily Media Brief",
                    "thread_anchor_key": "daily-media",
                },
            )
        )

    assert result["success"] is True
    message = smtp.send_message.call_args.args[0]
    assert message["Subject"] == "Daily Media Brief"
    assert message["In-Reply-To"] == "<hermes-thread-daily-media@example.com>"
    attachments = [
        part
        for part in message.walk()
        if part.get_content_disposition() == "attachment"
    ]
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "daily-report.pdf"


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
