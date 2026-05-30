"""Tests for Discord auto-thread title renaming from generated session titles."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource


def _make_source(
    *,
    thread_id: str | None = "999",
    thread_initial_name: str | None = "Please investigate a very long first message",
) -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        user_id="42",
        chat_id=thread_id or "123",
        chat_name="Hermes Server / #general / Hermes",
        chat_type="thread" if thread_id else "group",
        thread_id=thread_id,
        user_name="tester",
        thread_initial_name=thread_initial_name,
    )


def _make_runner(*, extra: dict | None = None):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.DISCORD: PlatformConfig(
                enabled=True,
                token="***",
                extra=extra or {},
            )
        }
    )
    adapter = SimpleNamespace(rename_thread=AsyncMock(return_value=True))
    runner.adapters = {Platform.DISCORD: adapter}
    return runner, adapter


@pytest.mark.asyncio
async def test_discord_thread_renames_placeholder_to_sanitized_generated_title():
    runner, adapter = _make_runner()

    await runner._rename_discord_thread_for_session_title(
        _make_source(thread_id="999"),
        "sess-discord",
        "  Investigate    Dashboard Build Timeout Across Workers  ",
    )

    adapter.rename_thread.assert_awaited_once()
    kwargs = adapter.rename_thread.await_args.kwargs
    assert kwargs["thread_id"] == "999"
    assert kwargs["name"] == "Investigate Dashboard Build Timeout Across Workers"
    assert kwargs["expected_current_name"] == "Please investigate a very long first message"
    assert len(kwargs["name"]) <= 70


@pytest.mark.asyncio
async def test_non_discord_thread_does_not_auto_rename_thread():
    runner, adapter = _make_runner()

    await runner._rename_discord_thread_for_session_title(
        _make_source(thread_id=None),
        "sess-discord",
        "Generated Summary Title",
    )

    adapter.rename_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_discord_thread_without_initial_name_does_not_auto_rename():
    runner, adapter = _make_runner()

    await runner._rename_discord_thread_for_session_title(
        _make_source(thread_id="999", thread_initial_name=None),
        "sess-discord",
        "Generated Summary Title",
    )

    adapter.rename_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_title_renames_discord_thread_without_initial_name_match():
    runner, adapter = _make_runner()

    await runner._rename_discord_thread_for_session_title(
        _make_source(thread_id="999", thread_initial_name=None),
        "sess-discord",
        "Manually Chosen Workflow Title",
        require_initial_name_match=False,
    )

    adapter.rename_thread.assert_awaited_once()
    kwargs = adapter.rename_thread.await_args.kwargs
    assert kwargs["thread_id"] == "999"
    assert kwargs["name"] == "Manually Chosen Workflow Title"
    assert kwargs["expected_current_name"] is None


@pytest.mark.asyncio
async def test_platform_thread_rename_syncs_back_to_session_title():
    runner, _adapter = _make_runner()
    entry = SimpleNamespace(session_id="sess-discord")
    finder = MagicMock(return_value=entry)
    sanitize_title = MagicMock(side_effect=lambda title: " ".join(title.split()))
    set_session_title = MagicMock(return_value=True)
    setattr(runner, "session_store", SimpleNamespace(find_session_by_thread=finder))
    setattr(
        runner,
        "_session_db",
        SimpleNamespace(
            sanitize_title=sanitize_title,
            set_session_title=set_session_title,
        ),
    )

    await runner._handle_platform_thread_title_change(
        Platform.DISCORD,
        "999",
        "  Renamed    Workflow Thread  ",
    )

    finder.assert_called_once_with(Platform.DISCORD, "999")
    set_session_title.assert_called_once_with(
        "sess-discord",
        "Renamed Workflow Thread",
    )
