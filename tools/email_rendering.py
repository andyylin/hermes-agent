"""Shared rich HTML rendering helpers for outbound Hermes email.

The renderer accepts the Markdown-ish text Hermes automations normally emit and
turns it into email-safe HTML. It intentionally supports a compact subset rather
than arbitrary Markdown: headings, bullets/checklists, key/value lines, block
quotes, fenced code, inline emphasis/code/links, and simple tables.
"""
from __future__ import annotations

import re
from html import escape, unescape

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


def message_looks_like_html(body: str) -> bool:
    """Return True when an outgoing body contains renderable HTML."""
    return bool(_HTML_TAG_RE.search(body or ""))


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
