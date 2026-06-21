from plugins.platforms.email.adapter import EmailAdapter


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
