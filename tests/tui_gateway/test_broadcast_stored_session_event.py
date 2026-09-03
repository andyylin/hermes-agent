"""``broadcast_stored_session_event`` — realtime notification for out-of-band
appends (cron session delivery) addressed by *stored* SessionDB id.

Stored ids and live runtime ``sid`` keys are different keyspaces (see
``apps/desktop/src/lib/session-ids.ts``). This resolves the live runtime sid
when the stored session happens to be open in this process right now (so the
client's existing explicit-session-id routing picks it up with zero special
casing), and falls back to a session-less global broadcast otherwise.
"""

import pytest

from tui_gateway import server


@pytest.fixture(autouse=True)
def _clean_sessions():
    # Isolate from any state a neighboring test left in the module-level dict.
    before = dict(server._sessions)
    server._sessions.clear()
    yield
    server._sessions.clear()
    server._sessions.update(before)


def test_resolves_live_runtime_sid_for_open_stored_session(monkeypatch):
    server._sessions["runtime-abc"] = {"session_key": "stored-123"}

    captured = []
    monkeypatch.setattr(
        server, "_broadcast_global_event",
        lambda *a, **k: captured.append(("global", a, k)),
    )

    frames = []
    monkeypatch.setattr(server, "write_json", lambda frame: frames.append(frame))
    monkeypatch.setattr(server, "_live_transports", set())

    server.broadcast_stored_session_event(
        "session.message.created", "stored-123", {"message_id": 7},
    )

    assert not captured, "must not fall back to the global broadcast"
    assert len(frames) == 1
    params = frames[0]["params"]
    assert params["type"] == "session.message.created"
    assert params["session_id"] == "runtime-abc"
    assert params["payload"] == {"message_id": 7}


def test_falls_back_to_global_broadcast_when_no_live_session(monkeypatch):
    captured = []
    monkeypatch.setattr(
        server, "_broadcast_global_event",
        lambda ev, payload=None: captured.append((ev, payload)),
    )

    server.broadcast_stored_session_event(
        "session.message.created", "stored-does-not-exist", {"message_id": 1},
    )

    assert captured == [("session.message.created", {"message_id": 1})]


def test_fans_out_to_all_live_transports(monkeypatch):
    server._sessions["runtime-abc"] = {"session_key": "stored-123"}

    written = []

    class _Transport:
        def write(self, frame):
            written.append(frame)

    t1, t2 = _Transport(), _Transport()
    monkeypatch.setattr(server, "_live_transports", {t1, t2})

    server.broadcast_stored_session_event(
        "session.message.created", "stored-123", {"message_id": 2},
    )

    assert len(written) == 2
    assert all(f["params"]["session_id"] == "runtime-abc" for f in written)


def test_one_wedged_transport_does_not_block_the_others(monkeypatch):
    server._sessions["runtime-abc"] = {"session_key": "stored-123"}

    written = []

    class _Boom:
        def write(self, frame):
            raise RuntimeError("transport gone")

    class _Ok:
        def write(self, frame):
            written.append(frame)

    monkeypatch.setattr(server, "_live_transports", {_Boom(), _Ok()})

    server.broadcast_stored_session_event(
        "session.message.created", "stored-123", {"message_id": 3},
    )

    assert len(written) == 1
