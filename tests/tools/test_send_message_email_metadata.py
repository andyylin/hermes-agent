from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gateway.config import Platform
from tools.send_message_tool import send_message_tool


def test_internal_email_subject_metadata_reaches_platform_sender() -> None:
    sender = AsyncMock(return_value={"success": True, "message_id": "email-1"})
    pconfig = SimpleNamespace(enabled=True, token="", extra={})
    config = SimpleNamespace(
        platforms={Platform.EMAIL: pconfig},
        get_home_channel=lambda _platform: None,
    )
    subject = {
        "subject": "[Hermes][LINE Digest] TOT",
        "thread_anchor_key": "line-tot-digest",
    }

    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("tools.interrupt.is_interrupted", return_value=False), \
         patch("tools.send_message_tool._registry_standalone_send", new=sender):
        result = json.loads(send_message_tool({
            "action": "send",
            "target": "email:andy@example.net",
            "message": "Digest body.",
            "_delivery_subject": subject,
        }))

    assert result["success"] is True
    assert sender.await_args is not None
    assert sender.await_args.kwargs["subject"] == subject
