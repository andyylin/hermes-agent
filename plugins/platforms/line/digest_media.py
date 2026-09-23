"""LINE read-only group digest media store (TTL-purged, not the gateway image cache).

Inbound LINE images land in ``cache/images`` (24h housekeeping). Quiet-window digests
often run later, so we keep a separate copy under ``data/line-read-only/media/`` for
archive/read-only groups only. Housekeeping purges this store at
``LINE_DIGEST_MEDIA_MAX_AGE_HOURS`` (default 72h) — long enough for a quiet window plus
a missed cycle, not indefinite retention.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

LINE_DIGEST_MEDIA_MAX_AGE_HOURS = int(os.getenv("LINE_DIGEST_MEDIA_MAX_AGE_HOURS", "72"))
_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def digest_media_root(hermes_home: Path) -> Path:
    return Path(hermes_home) / "data" / "line-read-only" / "media"


def _safe_id(value: str, fallback: str = "unknown") -> str:
    cleaned = _SAFE_RE.sub("_", value or "")
    return cleaned or fallback


def durable_media_path(
    hermes_home: Path,
    chat_id: str,
    message_id: str,
    src_path: str,
    *,
    msg_type: str = "image",
) -> Path:
    """Canonical durable path for one archived LINE media object."""
    ext = Path(src_path).suffix.lower() if src_path else ""
    if not ext:
        ext = {"image": ".jpg", "video": ".mp4", "audio": ".m4a", "file": ".bin"}.get(msg_type, ".bin")
    return digest_media_root(hermes_home) / _safe_id(chat_id) / f"{_safe_id(message_id)}{ext}"


def persist_digest_media(
    hermes_home: Path,
    chat_id: str,
    message_id: str,
    src_path: str,
    *,
    msg_type: str = "image",
) -> Optional[str]:
    """Copy *src_path* into the digest media store; return durable path or None.

    Idempotent: if the durable file already exists, reuse it. Leaves the ephemeral
    gateway cache file untouched for short-term gateway use.
    """
    if not src_path or not message_id:
        return None
    src = Path(src_path)
    if not src.is_file():
        logger.debug("LINE digest media: source missing %s", src_path)
        return None
    dest = durable_media_path(hermes_home, chat_id, message_id, src_path, msg_type=msg_type)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(dest.parent, 0o700)
        if dest.exists() and dest.stat().st_size > 0:
            return str(dest)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        shutil.copy2(src, tmp)
        os.chmod(tmp, 0o600)
        tmp.replace(dest)
        return str(dest)
    except OSError as exc:
        logger.warning("LINE digest media: persist failed %s -> %s: %s", src, dest, exc)
        return None


def remap_media_urls_for_archive(
    hermes_home: Path,
    chat_id: str,
    message_id: str,
    media_urls: List[str],
    *,
    msg_type: str = "image",
) -> List[str]:
    """Return media_urls rewritten to durable digest-store paths when possible."""
    if not media_urls:
        return []
    out: List[str] = []
    for url in media_urls:
        durable = persist_digest_media(
            hermes_home, chat_id, message_id, url, msg_type=msg_type)
        out.append(durable or url)
    return out


def cleanup_line_digest_media(max_age_hours: int = LINE_DIGEST_MEDIA_MAX_AGE_HOURS) -> int:
    """Delete digest-media files older than *max_age_hours*; return count removed.

    Walks ``data/line-read-only/media/**`` only — never touches ``cache/images``.
    """
    try:
        from hermes_constants import get_hermes_home
        root = digest_media_root(Path(get_hermes_home()).resolve())
    except Exception:
        root = digest_media_root(Path.home().joinpath(".hermes").resolve())
    if not root.is_dir():
        return 0
    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    for dirpath in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        try:
            next(dirpath.iterdir())
        except StopIteration:
            try:
                dirpath.rmdir()
            except OSError:
                pass
        except OSError:
            pass
    return removed
