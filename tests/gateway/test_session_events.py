"""``gateway.session_events.publish_session_message_created`` — best-effort
realtime notification hook for out-of-band session appends (cron delivery).

Must be a pure no-op (never raise, never block) when there is no live
``tui_gateway.server`` in this process — a plain ``hermes cron run`` or a
standalone messaging-gateway process has no realtime surface to publish
through, and that must never be mistaken for a delivery failure.
"""

import sys
import types

import pytest

from gateway.session_events import (
    SESSION_MESSAGE_CREATED_EVENT,
    publish_session_message_created,
)


@pytest.fixture(autouse=True)
def _no_stray_tui_gateway_server(monkeypatch):
    """Ensure ambient test-suite imports of tui_gateway.server don't leak in."""
    monkeypatch.delitem(sys.modules, "tui_gateway.server", raising=False)
    yield
    monkeypatch.delitem(sys.modules, "tui_gateway.server", raising=False)


def test_no_publish_when_tui_gateway_server_not_imported():
    result = publish_session_message_created(
        session_id="sess-1", message_id=1, source="cron", preview="hi",
    )
    assert result is False


def test_publishes_through_broadcaster_when_present(monkeypatch):
    calls = []

    fake_module = types.ModuleType("tui_gateway.server")
    fake_module.broadcast_stored_session_event = (
        lambda event, session_id, payload: calls.append((event, session_id, payload))
    )
    monkeypatch.setitem(sys.modules, "tui_gateway.server", fake_module)

    result = publish_session_message_created(
        session_id="sess-1",
        message_id=42,
        source="cron",
        preview="short preview",
        job_id="job-1",
        job_name="my job",
        execution_id="exec-1",
    )

    assert result is True
    assert len(calls) == 1
    event, session_id, payload = calls[0]
    assert event == SESSION_MESSAGE_CREATED_EVENT
    assert session_id == "sess-1"
    assert payload["session_id"] == "sess-1"
    assert payload["message_id"] == 42
    assert payload["source"] == "cron"
    assert payload["preview"] == "short preview"
    assert payload["job_id"] == "job-1"
    assert payload["job_name"] == "my job"
    assert payload["execution_id"] == "exec-1"
    assert "timestamp" in payload


def test_broadcaster_exception_is_swallowed(monkeypatch):
    fake_module = types.ModuleType("tui_gateway.server")

    def _boom(event, session_id, payload):
        raise RuntimeError("transport gone")

    fake_module.broadcast_stored_session_event = _boom
    monkeypatch.setitem(sys.modules, "tui_gateway.server", fake_module)

    result = publish_session_message_created(
        session_id="sess-1", message_id=1, source="cron", preview="hi",
    )
    assert result is False


def test_missing_broadcaster_attribute_is_a_noop(monkeypatch):
    fake_module = types.ModuleType("tui_gateway.server")
    monkeypatch.setitem(sys.modules, "tui_gateway.server", fake_module)

    result = publish_session_message_created(
        session_id="sess-1", message_id=1, source="cron", preview="hi",
    )
    assert result is False
