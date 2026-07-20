"""Discord auto-thread title/retitle behaviour."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _runner_with_discord_adapter(adapter):
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.DISCORD: adapter}
    return runner


@pytest.mark.asyncio
async def test_rename_discord_thread_for_session_title_calls_adapter(monkeypatch):
    monkeypatch.setenv("DISCORD_SMART_THREAD_TITLES", "true")
    adapter = SimpleNamespace(rename_thread=AsyncMock(return_value=True))
    runner = _runner_with_discord_adapter(adapter)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="456",
        chat_type="thread",
        thread_id="456",
        parent_chat_id="123",
    )

    await runner._rename_discord_thread_for_session_title(source, "sess-1", "Better Thread Title")

    adapter.rename_thread.assert_awaited_once_with("456", "Better Thread Title")


@pytest.mark.asyncio
async def test_rename_discord_thread_for_session_title_passes_guard(monkeypatch):
    monkeypatch.setenv("DISCORD_SMART_THREAD_TITLES", "true")
    adapter = SimpleNamespace(rename_thread=AsyncMock(return_value=True))
    runner = _runner_with_discord_adapter(adapter)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="456",
        chat_type="thread",
        thread_id="456",
        parent_chat_id="123",
    )

    await runner._rename_discord_thread_for_session_title(
        source,
        "sess-1",
        "Better Thread Title",
        only_if_current_name="raw first message",
    )

    adapter.rename_thread.assert_awaited_once_with(
        "456",
        "Better Thread Title",
        only_if_current_name="raw first message",
    )


@pytest.mark.asyncio
async def test_rename_discord_thread_for_session_title_is_gated_by_config(monkeypatch):
    monkeypatch.setenv("DISCORD_SMART_THREAD_TITLES", "false")
    adapter = SimpleNamespace(rename_thread=AsyncMock(return_value=True))
    runner = _runner_with_discord_adapter(adapter)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="456",
        chat_type="thread",
        thread_id="456",
        parent_chat_id="123",
    )

    await runner._rename_discord_thread_for_session_title(source, "sess-1", "Better Thread Title")

    adapter.rename_thread.assert_not_awaited()

def test_discord_auto_thread_guard_names_include_visible_chat_name():
    runner = _runner_with_discord_adapter(SimpleNamespace())
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="456",
        chat_name="Guild / #hermes / How come n8n still sent me a error message email instead of routing through a...",
        chat_type="thread",
        thread_id="456",
        parent_chat_id="123",
        auto_thread_created=True,
        auto_thread_initial_name="[The user sent a text document: 'message",
    )

    assert runner._discord_auto_thread_guard_names(source) == [
        "[The user sent a text document: 'message",
        "How come n8n still sent me a error message email instead of routing through a...",
    ]
