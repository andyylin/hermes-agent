"""Session-scoped realtime events for out-of-band appends (cron delivery, ...).

Standalone by design: importable and safely callable from any process,
including a plain ``hermes cron run`` with no gateway or ``tui_gateway``
backend running at all. Publishing is a pure accelerator — a session's REST/
resume path is always the source of truth for what got durably appended, so
a failed or skipped publish here must never be treated as a delivery
failure by the caller.
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

SESSION_MESSAGE_CREATED_EVENT = "session.message.created"


def publish_session_message_created(
    *,
    session_id: str,
    message_id: Optional[int],
    source: str,
    preview: str,
    job_id: Optional[Any] = None,
    job_name: Optional[Any] = None,
    execution_id: Optional[str] = None,
) -> bool:
    """Best-effort publish of a ``session.message.created`` event.

    Only does anything when ``tui_gateway.server`` is already imported in
    THIS process — i.e. the cron ticker is running inside a Desktop-spawned
    local backend (``HERMES_DESKTOP=1``) or the TUI gateway process itself.
    A standalone ``hermes cron run`` or a separate gateway process has no
    live WS/stdio clients to reach here at all; those surfaces pick up the
    durably-appended row on their next REST/session refresh instead.

    Returns True if a publish attempt was made (not necessarily received by
    any client), False if this process has no realtime surface to publish
    through.
    """
    module = sys.modules.get("tui_gateway.server")
    if module is None:
        return False

    broadcaster = getattr(module, "broadcast_stored_session_event", None)
    if not callable(broadcaster):
        return False

    payload = {
        "session_id": str(session_id),
        "message_id": message_id,
        "source": source,
        "job_id": job_id,
        "job_name": job_name,
        "execution_id": execution_id,
        "preview": preview,
        "timestamp": time.time(),
    }

    try:
        broadcaster(SESSION_MESSAGE_CREATED_EVENT, str(session_id), payload)
    except Exception:
        logger.debug(
            "session.message.created publish failed for session %s",
            session_id, exc_info=True,
        )
        return False
    return True
