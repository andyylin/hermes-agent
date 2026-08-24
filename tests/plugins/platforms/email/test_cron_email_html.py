from plugins.platforms.email.adapter import _standalone_plain_text_to_html


def test_work_brief_markdown_becomes_html():
    body = """## morning-evening-brief

**Job ID:** `217e74bd5079`

## Needs reply

**Anna — leave list.** [source](https://mattermost.example/x)

```
HERMES-ACT:MMB-T2E0
```
"""
    html = _standalone_plain_text_to_html(body)
    assert "<h3>morning-evening-brief</h3>" in html
    assert "<h3>Needs reply</h3>" in html
    assert "<strong>Job ID:</strong>" in html
    assert "<code>217e74bd5079</code>" in html
    assert 'href="https://mattermost.example/x"' in html
    assert "<pre><code>HERMES-ACT:MMB-T2E0" in html
    assert "</code></pre>" in html
    assert "**Window:**" not in html
    assert "Cronjob Response:" not in html
