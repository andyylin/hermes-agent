"""Durable delivery of cron output into the *originating* stored session.

Desktop/TUI/CLI-created cron jobs have no gateway ``{platform, chat_id}`` to
route through — their origin is a *session-kind* origin (see
``tools/cronjob_tools.py::_origin_from_env``): the durable ``SessionDB`` row
id that created the job. This module appends cron output directly into that
row (or its live compression-continuation tip), guards against duplicate
delivery on retries/reconnects, and publishes a session-scoped realtime event
so Desktop/TUI can mark it unread and fire a native notification.

Deliberately standalone: importable and usable from a plain ``hermes cron
run`` process with no gateway/tui_gateway running at all. Uses
``hermes_state.SessionDB`` directly rather than any SessionStore/adapter
machinery — mirrors the standalone-first design of ``gateway/mirror.py``.

Never mints a new session: a missing/invalid target is a scoped delivery
failure, not a fallback session creation (see TASK "no synthetic extra
session").
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_lock = threading.RLock()

# Display-metadata/display-kind tag for cron output landing in a session via
# this module. Kept distinct from ``internal_notification`` (async-delegation
# / background-watch re-entry into a LIVE agent turn) — this is a passive
# append with no agent turn attached; Desktop renders it as scheduled output,
# not a chat bubble impersonating the user (see role="user" note below).
CRON_DELIVERY_DISPLAY_KIND = "cron_delivery"

# Test-only override for the idempotency ledger path, mirrors
# cron/executions.py's EXECUTIONS_FILE pattern.
DELIVERIES_FILE: Optional[Path] = None


def _db_path() -> Path:
    from hermes_constants import get_hermes_home

    return DELIVERIES_FILE or (get_hermes_home().resolve() / "cron" / "session_deliveries.db")


def _connect() -> sqlite3.Connection:
    from cron.jobs import _ensure_cron_dir

    path = _db_path()
    _ensure_cron_dir(path.parent)
    return sqlite3.connect(path, timeout=5)


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_state import apply_wal_with_fallback

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    apply_wal_with_fallback(conn, db_label="cron/session_deliveries.db")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS session_deliveries (
             job_id TEXT NOT NULL,
             execution_id TEXT NOT NULL,
             session_id TEXT NOT NULL,
             message_id INTEGER,
             delivered_at TEXT NOT NULL,
             PRIMARY KEY (job_id, execution_id, session_id)
           )"""
    )


@contextmanager
def _transaction():
    with _lock:
        conn = _connect()
        try:
            _initialize_schema(conn)
            with conn:
                yield conn
        finally:
            conn.close()


def _find_prior_delivery(
    job_id: str, execution_id: str, session_id: str
) -> Optional[Dict[str, Any]]:
    """Return the prior delivery record for this (job, execution, session)."""
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM session_deliveries WHERE job_id=? AND execution_id=? "
            "AND session_id=?",
            (str(job_id), str(execution_id), str(session_id)),
        ).fetchone()
    return dict(row) if row is not None else None


def _record_delivery(
    job_id: str, execution_id: str, session_id: str, message_id: Optional[int]
) -> None:
    from hermes_time import now as _hermes_now

    with _transaction() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO session_deliveries
               (job_id, execution_id, session_id, message_id, delivered_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                str(job_id),
                str(execution_id),
                str(session_id),
                message_id,
                _hermes_now().isoformat(),
            ),
        )


def deliver_cron_output_to_session(
    job: Dict[str, Any],
    content: str,
    session_id: str,
    *,
    execution_id: Optional[str] = None,
) -> Optional[str]:
    """Append cron output to the stored session that created the job.

    Returns ``None`` on success (including a no-op "already delivered"
    replay of a prior successful attempt), or an error string on failure.
    Never creates a new session — a missing/invalid ``session_id`` is a
    scoped failure, and the cron output stays available in the job's
    ``last_output`` regardless (see ``cron/scheduler.py::_deliver_result``).
    """
    job_id = str(job.get("id") or "?")
    session_id = str(session_id)

    if not session_id or session_id == "None":
        return "session delivery target has no session_id"

    # Idempotency: job ID + execution ID is the durable retry/reconnect key
    # (TASK requirement #4). Keyed on the ORIGINAL target session_id, not the
    # resolved compression tip below, so a retry that recomputes the tip
    # still recognizes the earlier attempt as done.
    if execution_id:
        prior = _find_prior_delivery(job_id, execution_id, session_id)
        if prior is not None:
            logger.debug(
                "Job '%s': execution %s already delivered to session %s "
                "(message_id=%s) — skipping duplicate append",
                job_id, execution_id, session_id, prior.get("message_id"),
            )
            return None

    try:
        from hermes_state import SessionDB
    except Exception as e:
        return f"session delivery failed: could not import SessionDB: {e}"

    db = SessionDB()
    try:
        target_session_id = session_id
        try:
            tip = db.get_compression_tip(session_id)
            if tip:
                target_session_id = tip
        except Exception:
            logger.debug(
                "Job '%s': compression-tip lookup failed for session %s; "
                "delivering to the original id",
                job_id, session_id, exc_info=True,
            )

        row = db.get_session(target_session_id)
        if not row:
            return (
                f"stored session '{target_session_id}' not found — "
                "skipping delivery (output saved in last_output)"
            )

        job_name = job.get("name") or job_id
        display_metadata = {
            "source": "cron",
            "job_id": job_id,
            "job_name": job_name,
            "execution_id": execution_id,
        }

        # role="user": the mirrored text is NOT the agent speaking (mirrors
        # gateway.mirror.mirror_to_session's documented rule for out-of-band
        # brief text) — an assistant-role append here after the session's
        # last real assistant turn would create assistant->assistant
        # alternation. display_kind/display_metadata are display-only and
        # are stripped from provider-bound payload copies on replay, so the
        # plain text below IS what a future model turn sees.
        message_id = db.append_message(
            session_id=target_session_id,
            role="user",
            content=content,
            display_kind=CRON_DELIVERY_DISPLAY_KIND,
            display_metadata=display_metadata,
        )
    except Exception as e:
        logger.warning(
            "Job '%s': session delivery to %s failed: %s",
            job_id, session_id, e, exc_info=True,
        )
        return f"session delivery to '{session_id}' failed: {e}"
    finally:
        db.close()

    if execution_id:
        try:
            _record_delivery(job_id, execution_id, session_id, message_id)
        except Exception:
            logger.debug(
                "Job '%s': failed to record delivery idempotency record "
                "(delivery itself succeeded)",
                job_id, exc_info=True,
            )

    try:
        _publish_session_message_event(
            session_id=target_session_id,
            message_id=message_id,
            job=job,
            execution_id=execution_id,
            content=content,
        )
    except Exception:
        # Realtime publish is an accelerator, never the source of truth
        # (Desktop's REST/session refresh reveals the row regardless) — a
        # failure here must never turn a successful append into a reported
        # delivery failure.
        logger.debug(
            "Job '%s': session.message.created publish failed (append "
            "already durable)",
            job_id, exc_info=True,
        )

    return None


def _short_preview(content: str, limit: int = 200) -> str:
    text = " ".join((content or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\u2026"


def _publish_session_message_event(
    *,
    session_id: str,
    message_id: Optional[int],
    job: Dict[str, Any],
    execution_id: Optional[str],
    content: str,
) -> None:
    """Best-effort realtime notification for an in-process tui_gateway.

    The cron ticker runs in the same process as the ``tui_gateway`` backend
    when Desktop spawns a local backend (``HERMES_DESKTOP=1``). When that
    server is importable and has live connections, publish a
    ``session.message.created`` event; otherwise this is a silent no-op —
    Desktop's REST/session refresh (or the next reconnect) still reveals the
    durably-appended row.
    """
    from gateway.session_events import publish_session_message_created

    publish_session_message_created(
        session_id=session_id,
        message_id=message_id,
        source="cron",
        job_id=job.get("id"),
        job_name=job.get("name"),
        execution_id=execution_id,
        preview=_short_preview(content),
    )
