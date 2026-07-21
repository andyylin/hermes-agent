from plugins.platforms.email.adapter import EmailAdapter
from tools.email_rendering import (
    append_notification_reference_footer,
    plain_text_to_html,
    sanitize_email_html,
)


BODY = (
    "## Digest\n\n"
    "> Bottom line: **act now**\n\n"
    "- **Kelly:** see [details](https://example.com?a=1&b=2)\n"
    "- [ ] Reply to `Alice`\n\n"
    "<script>alert(1)</script>"
)


def assert_rich_safe_html(html: str) -> None:
    assert "<!doctype html>" in html
    assert "<h2>Digest</h2>" in html
    assert "<blockquote" in html
    assert "<strong>act now</strong>" in html
    assert '<a href="https://example.com?a=1&amp;b=2">details</a>' in html
    assert "☐ Reply to <code>Alice</code>" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html


def test_email_adapter_plain_text_to_html_renders_rich_markdown_safely():
    assert_rich_safe_html(EmailAdapter._plain_text_to_html(BODY))


def test_shared_email_plain_text_to_html_renders_rich_markdown_safely():
    assert_rich_safe_html(plain_text_to_html(BODY))


def test_raw_html_is_sanitized_before_email_delivery():
    raw = (
        '<h2 onclick="steal()" style="background:url(https://evil.example)">Safe</h2>'
        '<script>alert("owned")</script>'
        '<a href="javascript:alert(1)">bad link</a>'
        '<a href="https://example.com/path?a=1&b=2">good link</a>'
    )

    safe = sanitize_email_html(raw)

    assert "<h2>Safe</h2>" in safe
    assert "onclick" not in safe
    assert "style=" not in safe
    assert "<script" not in safe
    assert "owned" not in safe
    assert "javascript:" not in safe
    assert '<a>bad link</a>' in safe
    assert '<a href="https://example.com/path?a=1&amp;b=2">good link</a>' in safe


def test_notification_reference_footer_is_copyable_in_plain_and_html():
    plain = append_notification_reference_footer(
        "## Alert\n\nBody",
        ref="HERMES-NOTIFY:test-alert:abc123:2026-06-22T08:10+08:00",
        job_id="abc123",
        source="/home/pi/.hermes/scripts/test.py",
    )
    assert "REF: `HERMES-NOTIFY:test-alert:abc123:2026-06-22T08:10+08:00`" in plain
    assert "Ask Hermes: `investigate this REF: HERMES-NOTIFY:test-alert:abc123:2026-06-22T08:10+08:00`" in plain

    html = append_notification_reference_footer(
        "<!doctype html><html><body><h2>Alert</h2></body></html>",
        ref="HERMES-NOTIFY:test-alert:abc123:2026-06-22T08:10+08:00",
        job_id="abc123",
        source="/home/pi/.hermes/scripts/test.py",
    )
    assert "<h3>Reference</h3>" in html
    assert "<code>HERMES-NOTIFY:test-alert:abc123:2026-06-22T08:10+08:00</code>" in html
    assert html.endswith("</body></html>")
