"""Mattermost DM media delivery must thread under the triggering post, not as a new CRT root."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.event import MessageEvent
from gateway.session import Platform, SessionSource
from plugins.platforms.mattermost.adapter import MattermostAdapter


def _dm_event(post_id: str, thread_id: str | None) -> MessageEvent:
    source = SessionSource(
        platform=Platform.MATTERMOST,
        chat_id="chan_dm",
        chat_type="dm",
        thread_id=thread_id,
        message_id=post_id,
    )
    return MessageEvent(text="", source=source, message_id=post_id)


@pytest.mark.asyncio
async def test_deliver_media_attachments_passes_reply_anchor_to_send_document(tmp_path) -> None:
    adapter = MattermostAdapter(
        PlatformConfig(enabled=True, token="mm-token", extra={"url": "https://mm.example"}))
    adapter.send_document = AsyncMock(return_value=SendResult(success=True, message_id="doc-1"))
    adapter._notify_media_delivery_failure = AsyncMock()
    archive = tmp_path / "bed-flange-floor-cover-v1.zip"
    archive.write_bytes(b"PK")
    event = _dm_event("hqceufwrcj83ubgc6hjeftus4a", "hqceufwrcj83ubgc6hjeftus4a")
    results = []

    await adapter._deliver_media_attachments(
        event, [(str(archive), False)], [], force_document_attachments=False, human_delay=0,
        metadata={"thread_id": "hqceufwrcj83ubgc6hjeftus4a"}, record_delivery=results.append)

    assert [r.success for r in results] == [True]
    kwargs = adapter.send_document.await_args.kwargs
    assert kwargs["reply_to"] == "hqceufwrcj83ubgc6hjeftus4a"
    assert kwargs["metadata"]["thread_id"] == "hqceufwrcj83ubgc6hjeftus4a"


@pytest.mark.asyncio
async def test_deliver_media_attachments_passes_reply_anchor_to_send_multiple_images(tmp_path) -> None:
    adapter = MattermostAdapter(
        PlatformConfig(enabled=True, token="mm-token", extra={"url": "https://mm.example"}))
    adapter.send_multiple_images = AsyncMock(return_value=SendResult(success=True, message_id="img-1"))
    image = tmp_path / "preview.png"
    image.write_bytes(b"\x89PNG")
    event = _dm_event("hqceufwrcj83ubgc6hjeftus4a", "hqceufwrcj83ubgc6hjeftus4a")
    results = []

    await adapter._deliver_media_attachments(
        event, [(str(image), False)], [], force_document_attachments=False, human_delay=0,
        metadata={"thread_id": "hqceufwrcj83ubgc6hjeftus4a"}, record_delivery=results.append)

    assert [r.success for r in results] == [True]
    kwargs = adapter.send_multiple_images.await_args.kwargs
    assert kwargs["reply_to"] == "hqceufwrcj83ubgc6hjeftus4a"
