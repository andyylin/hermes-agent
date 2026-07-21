"""Shared rich HTML rendering helpers for outbound Hermes email.

The renderer accepts the Markdown-ish text Hermes automations normally emit and
turns it into email-safe HTML. It intentionally supports a compact subset rather
than arbitrary Markdown: headings, bullets/checklists, key/value lines, block
quotes, fenced code, inline emphasis/code/links, and simple tables.
"""
from __future__ import annotations

import re
from datetime import datetime
from html import escape, unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit

_HTML_TAG_RE = re.compile(
    r"</?(?:html|body|h[1-6]|p|ul|ol|li|br|strong|em|table|thead|tbody|tr|th|td|div|span|blockquote|pre|code)\b",
    re.IGNORECASE,
)
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_BARE_URL_RE = re.compile(r"(?<![\"'=])(https?://[^\s<>)]+)")
_KEY_VALUE_RE = re.compile(r"^([A-Za-z][\w /().-]{0,60}?):\s+(.+)$")
_ORDERED_RE = re.compile(r"^\d+[.)]\s+(.+)$")
_BULLET_RE = re.compile(r"^[-*]\s+(?:\[([ xX])\]\s+)?(.+)$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_REF_RE = re.compile(r"\bREF:\s*HERMES-NOTIFY:", re.IGNORECASE)


def notification_ref_slug(value: str, *, fallback: str = "notification") -> str:
    """Return a compact copy/paste-safe slug for notification reference codes."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value or "").strip("-").lower()
    return slug[:64] or fallback


def make_notification_ref(*parts: str, now: datetime | None = None) -> str:
    """Build a stable-enough reference token for outbound notification email."""
    timestamp = (now or datetime.now().astimezone()).isoformat(timespec="minutes")
    safe_parts = [notification_ref_slug(part) for part in parts if str(part or "").strip()]
    if not safe_parts:
        safe_parts = ["email"]
    return "HERMES-NOTIFY:" + ":".join(safe_parts + [timestamp])


def append_notification_reference_footer(
    body: str,
    *,
    ref: str | None = None,
    subject: str | None = None,
    source: str | None = None,
    state: str | None = None,
    logs: str | None = None,
    job_id: str | None = None,
    script: str | None = None,
    ask: str = "investigate this REF",
) -> str:
    """Append a copy/paste resume reference to notification email bodies.

    The footer is added to both plain/Markdown-ish bodies and existing HTML
    bodies. If a body already has a HERMES-NOTIFY reference, it is left alone
    to avoid duplicated footers when cron wraps and then sends through the
    generic email transport.
    """
    content = body or ""
    if _REF_RE.search(content):
        return content
    ref = ref or make_notification_ref(subject or "email")
    rows = [("REF", ref)]
    if job_id:
        rows.append(("Job ID", job_id))
    if script:
        rows.append(("Script", script))
    if source:
        rows.append(("Source", source))
    if state:
        rows.append(("State", state))
    if logs:
        rows.append(("Logs", logs))
    rows.append(("Ask Hermes", f"{ask}: {ref}"))

    if message_looks_like_html(content):
        html_rows = "".join(
            f"<tr><th>{escape(label)}</th><td><code>{escape(value)}</code></td></tr>"
            for label, value in rows
        )
        footer = (
            "<hr>"
            "<h3>Reference</h3>"
            '<table class="notification-reference"><tbody>'
            f"{html_rows}"
            "</tbody></table>"
        )
        if re.search(r"</body>\s*</html>\s*$", content, flags=re.IGNORECASE):
            return re.sub(r"</body>\s*</html>\s*$", footer + "</body></html>", content, flags=re.IGNORECASE)
        return content + footer

    lines = ["", "### Reference"]
    lines.extend(f"{label}: `{value}`" for label, value in rows)
    return content.rstrip() + "\n\n" + "\n".join(lines)


def message_looks_like_html(body: str) -> bool:
    """Return True when an outgoing body contains renderable HTML."""
    return bool(_HTML_TAG_RE.search(body or ""))


_SAFE_EMAIL_TAGS = frozenset({
    "html", "body", "h1", "h2", "h3", "h4", "h5", "h6", "p", "ul",
    "ol", "li", "br", "strong", "em", "table", "thead", "tbody", "tr",
    "th", "td", "div", "span", "blockquote", "pre", "code", "a", "hr",
})
_VOID_EMAIL_TAGS = frozenset({"br", "hr"})
_DROP_EMAIL_TAGS = frozenset({
    "script", "style", "iframe", "object", "embed", "form", "input",
    "button", "meta", "link", "svg", "math",
})


class _EmailHTMLSanitizer(HTMLParser):
    """Small allowlist sanitizer for model- or automation-supplied HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.output: list[str] = []
        self.blocked_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if self.blocked_depth:
            if tag in _DROP_EMAIL_TAGS:
                self.blocked_depth += 1
            return
        if tag in _DROP_EMAIL_TAGS:
            self.blocked_depth = 1
            return
        if tag not in _SAFE_EMAIL_TAGS:
            return

        safe_attrs: list[tuple[str, str]] = []
        for raw_name, raw_value in attrs:
            name = (raw_name or "").lower()
            value = raw_value or ""
            if name == "class":
                safe_attrs.append((name, value))
            elif name in {"colspan", "rowspan"} and value.isdigit():
                safe_attrs.append((name, value))
            elif tag == "a" and name == "href":
                scheme = urlsplit(value.strip()).scheme.lower()
                if scheme in {"http", "https", "mailto"}:
                    safe_attrs.append((name, value.strip()))
            elif name == "title":
                safe_attrs.append((name, value))

        rendered_attrs = "".join(
            f' {name}="{escape(value, quote=True)}"'
            for name, value in safe_attrs
        )
        self.output.append(f"<{tag}{rendered_attrs}>")

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)
        if not self.blocked_depth and tag.lower() in _SAFE_EMAIL_TAGS - _VOID_EMAIL_TAGS:
            self.output.append(f"</{tag.lower()}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.blocked_depth:
            if tag in _DROP_EMAIL_TAGS:
                self.blocked_depth -= 1
            return
        if tag in _SAFE_EMAIL_TAGS and tag not in _VOID_EMAIL_TAGS:
            self.output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.blocked_depth:
            self.output.append(escape(data))


def sanitize_email_html(html: str) -> str:
    """Strip executable/remote-loading HTML while preserving safe structure."""
    sanitizer = _EmailHTMLSanitizer()
    sanitizer.feed(html or "")
    sanitizer.close()
    return "".join(sanitizer.output)


def html_to_plain_text(html: str) -> str:
    """Best-effort plain-text alternative for multipart/alternative email."""
    text = re.sub(r"<\s*br\s*/?>", "\n", html or "", flags=re.IGNORECASE)
    text = re.sub(r"<\s*/\s*(?:p|div|h[1-6]|li|tr|blockquote|pre)\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*li\b[^>]*>", "- ", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*t[hd]\b[^>]*>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def render_inline_markdown(text: str) -> str:
    """Render a small escaped inline Markdown subset for email HTML."""
    rendered = escape(text or "")
    rendered = re.sub(
        r"`([^`]+)`",
        lambda m: f"<code>{m.group(1)}</code>",
        rendered,
    )
    rendered = _LINK_RE.sub(
        lambda m: f'<a href="{escape(unescape(m.group(2)), quote=True)}">{m.group(1)}</a>',
        rendered,
    )
    rendered = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", rendered)
    rendered = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", rendered)
    rendered = _BARE_URL_RE.sub(
        lambda m: f'<a href="{escape(unescape(m.group(1)), quote=True)}">{m.group(1)}</a>',
        rendered,
    )
    return rendered


def _looks_like_title(line: str, *, is_first_content: bool, next_line_blank: bool) -> bool:
    stripped = line.strip()
    if not is_first_content or not next_line_blank:
        return False
    if len(stripped) > 90:
        return False
    if _HEADING_RE.match(stripped) or _BULLET_RE.match(stripped) or _ORDERED_RE.match(stripped):
        return False
    if _KEY_VALUE_RE.match(stripped):
        return False
    if stripped.startswith((">", "```", "{")):
        return False
    return bool(re.search(r"[A-Za-z]", stripped))


def plain_text_to_html(body: str) -> str:
    """Render Hermes Markdown-ish/plain notification text as rich email HTML."""
    source = (body or "").strip()
    if not source:
        return "<html><body><p></p></body></html>"

    html_blocks: list[str] = []
    list_items: list[str] = []
    ordered_items: list[str] = []
    quote_lines: list[str] = []
    para_lines: list[str] = []
    code_lines: list[str] = []
    kv_rows: list[tuple[str, str]] = []
    in_code_fence = False
    current_list = None  # "ul" or "ol"
    saw_content = False

    def flush_para() -> None:
        nonlocal para_lines
        if para_lines:
            html_blocks.append("<p>" + "<br>\n".join(para_lines) + "</p>")
            para_lines = []

    def flush_quote() -> None:
        nonlocal quote_lines
        if quote_lines:
            html_blocks.append(
                '<blockquote style="border-left:4px solid #d0d7de; margin:16px 0; '
                'padding:8px 12px; color:#57606a; background:#f6f8fa;">'
                + "<br>\n".join(quote_lines)
                + "</blockquote>"
            )
            quote_lines = []

    def flush_kv() -> None:
        nonlocal kv_rows
        if kv_rows:
            rows = "".join(
                f"<tr><th>{render_inline_markdown(k)}</th><td>{render_inline_markdown(v)}</td></tr>"
                for k, v in kv_rows
            )
            html_blocks.append(f'<table class="kv"><tbody>{rows}</tbody></table>')
            kv_rows = []

    def flush_list() -> None:
        nonlocal list_items, ordered_items, current_list
        if list_items:
            html_blocks.append("<ul>" + "".join(list_items) + "</ul>")
            list_items = []
        if ordered_items:
            html_blocks.append("<ol>" + "".join(ordered_items) + "</ol>")
            ordered_items = []
        current_list = None

    def flush_code() -> None:
        nonlocal code_lines
        if code_lines:
            html_blocks.append(
                '<pre style="background:#f6f8fa; padding:12px; border-radius:6px; '
                'overflow:auto; font-family:SFMono-Regular,Consolas,monospace; '
                'font-size:13px; line-height:1.4;"><code>'
                + escape("\n".join(code_lines))
                + "</code></pre>"
            )
            code_lines = []

    def flush_all() -> None:
        flush_para()
        flush_quote()
        flush_kv()
        flush_list()
        flush_code()

    lines = source.splitlines()
    total = len(lines)
    i = 0
    while i < total:
        raw_line = lines[i].rstrip()
        stripped = raw_line.strip()
        next_line_blank = i + 1 < total and not lines[i + 1].strip()

        if stripped.startswith("```"):
            if in_code_fence:
                flush_code()
                in_code_fence = False
            else:
                flush_all()
                in_code_fence = True
            saw_content = True
            i += 1
            continue
        if in_code_fence:
            code_lines.append(raw_line)
            i += 1
            continue
        if not stripped:
            flush_all()
            i += 1
            continue

        if _looks_like_title(stripped, is_first_content=not saw_content, next_line_blank=next_line_blank):
            flush_all()
            html_blocks.append(f"<h2>{render_inline_markdown(stripped)}</h2>")
            saw_content = True
            i += 1
            continue

        heading = _HEADING_RE.match(stripped)
        if heading:
            flush_all()
            level = min(len(heading.group(1)), 3)
            html_blocks.append(f"<h{level}>{render_inline_markdown(heading.group(2))}</h{level}>")
            saw_content = True
            i += 1
            continue

        quote = re.match(r"^>\s?(.*)$", stripped)
        if quote:
            flush_para(); flush_kv(); flush_list()
            quote_lines.append(render_inline_markdown(quote.group(1)))
            saw_content = True
            i += 1
            continue

        bullet = _BULLET_RE.match(stripped)
        if bullet:
            flush_para(); flush_quote(); flush_kv()
            if current_list == "ol":
                flush_list()
            current_list = "ul"
            checked, item = bullet.groups()
            marker = "☑ " if checked and checked.lower() == "x" else "☐ " if checked else ""
            list_items.append(f"<li>{marker}{render_inline_markdown(item)}</li>")
            saw_content = True
            i += 1
            continue

        ordered = _ORDERED_RE.match(stripped)
        if ordered:
            flush_para(); flush_quote(); flush_kv()
            if current_list == "ul":
                flush_list()
            current_list = "ol"
            ordered_items.append(f"<li>{render_inline_markdown(ordered.group(1))}</li>")
            saw_content = True
            i += 1
            continue

        # Indented continuation under the previous list item: keep source_id /
        # evidence / next lines visually attached to the bullet instead of
        # spilling into sad little paragraphs.
        if raw_line.startswith(("  ", "\t")) and current_list == "ul" and list_items:
            continuation = render_inline_markdown(stripped)
            list_items[-1] = list_items[-1].removesuffix("</li>") + f"<br><span class=\"subline\">{continuation}</span></li>"
            saw_content = True
            i += 1
            continue
        if raw_line.startswith(("  ", "\t")) and current_list == "ol" and ordered_items:
            continuation = render_inline_markdown(stripped)
            ordered_items[-1] = ordered_items[-1].removesuffix("</li>") + f"<br><span class=\"subline\">{continuation}</span></li>"
            saw_content = True
            i += 1
            continue

        kv = _KEY_VALUE_RE.match(stripped)
        if kv:
            flush_para(); flush_quote(); flush_list()
            kv_rows.append((kv.group(1), kv.group(2)))
            saw_content = True
            i += 1
            continue

        flush_quote(); flush_kv(); flush_list()
        para_lines.append(render_inline_markdown(stripped))
        saw_content = True
        i += 1

    flush_all()
    body_html = "\n".join(html_blocks)
    return (
        '<!doctype html><html><body style="font-family:-apple-system,BlinkMacSystemFont,'
        "'Segoe UI',Roboto,Helvetica,Arial,sans-serif; line-height:1.45; color:#24292f; "
        'font-size:15px; max-width:860px; margin:0 auto; padding:16px;">'
        '<style>a{color:#0969da} code{background:#f6f8fa; padding:2px 4px; '
        'border-radius:4px; font-family:SFMono-Regular,Consolas,monospace} '
        'h1,h2,h3{line-height:1.25; margin:20px 0 8px} '
        'ul,ol{padding-left:24px} li{margin:6px 0} '
        '.subline{color:#57606a} table{border-collapse:collapse; margin:12px 0; width:100%;} '
        'th,td{border:1px solid #d0d7de; padding:6px 8px; vertical-align:top;} '
        'th{background:#f6f8fa; text-align:left; white-space:nowrap; width:1%;}</style>'
        f"{body_html}</body></html>"
    )
