"""Subscriber flush behavior for the core Relay runtime."""

import asyncio
from types import SimpleNamespace

from agent import relay_runtime


def test_flush_relay_subscribers_uses_blocking_flush_without_running_loop():
    calls = []
    subscribers = SimpleNamespace(flush=lambda: calls.append("sync"))

    relay_runtime._flush_relay_subscribers(SimpleNamespace(subscribers=subscribers))

    assert calls == ["sync"]


def test_flush_relay_subscribers_schedules_async_flush_on_running_loop():
    calls = []

    def blocking_flush():
        raise AssertionError("blocking flush must not run on an asyncio loop")

    async def flush_async():
        calls.append("async")

    subscribers = SimpleNamespace(flush=blocking_flush, flush_async=flush_async)

    async def exercise():
        relay_runtime._flush_relay_subscribers(
            SimpleNamespace(subscribers=subscribers)
        )
        await asyncio.sleep(0)

    asyncio.run(exercise())

    assert calls == ["async"]