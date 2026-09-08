"""Originating-session cron delivery for Desktop/TUI/CLI origins.

Andy's success criterion (2026-09-03, TASK cron-session-delivery-20260903):
when a job created from a Desktop (or TUI) conversation finishes,
``deliver=origin`` must land the output in that same stored session --
without ever raising "unknown platform 'webui'" and without minting a
synthetic extra session.

Covers:
- ``_origin_from_env()`` captures a session-kind origin for local/UI surfaces
  (Desktop/TUI/CLI: no HERMES_SESSION_PLATFORM/CHAT_ID, but HERMES_SESSION_ID
  is set).
- ``deliver=origin`` from source=desktop / empty platform / legacy 'webui'
  does NOT raise "unknown platform".
- one-shot job created from a Desktop-like origin appears in that stored
  session.
- missing stored session -> scoped delivery failure, no new session created.
- job + execution idempotency: retries/reconnects do not duplicate appends.
- role alternation on replay (appended row is role='user').
- Telegram/Discord fan-out resolves independently of the session target.
"""

from unittest.mock import patch

import pytest

from cron.scheduler import (
    SESSION_DELIVERY_PLATFORM,
    _deliver_result,
    _resolve_delivery_targets,
    _resolve_session_origin,
)
from tools.cronjob_tools import _origin_from_env


def _session_env(env: dict):
    """Patch gateway.session_context.get_session_env with a dict lookup."""
    return patch(
        "gateway.session_context.get_session_env",
        side_effect=lambda name, default="": env.get(name, default),
    )


# ── origin capture (tools/cronjob_tools.py::_origin_from_env) ──────────────


class TestSessionOriginCapture:
    def test_desktop_origin_has_no_platform_or_chat_id(self):
        env = {
            "HERMES_SESSION_SOURCE": "desktop",
            "HERMES_SESSION_ID": "stored-sess-desktop-1",
        }
        with _session_env(env):
            origin = _origin_from_env()
        assert origin is not None
        assert origin["kind"] == "session"
        assert origin["session_id"] == "stored-sess-desktop-1"
        assert origin["source"] == "desktop"

    def test_tui_origin_captured(self):
        env = {
            "HERMES_SESSION_SOURCE": "tui",
            "HERMES_SESSION_ID": "stored-sess-tui-1",
        }
        with _session_env(env):
            origin = _origin_from_env()
        assert origin == {
            "kind": "session",
            "session_id": "stored-sess-tui-1",
            "source": "tui",
        }

    def test_no_session_id_at_all_is_none(self):
        """API/script-created jobs with no session context capture no origin."""
        with _session_env({}):
            origin = _origin_from_env()
        assert origin is None

    def test_messaging_origin_takes_priority_over_session_id(self):
        """A real gateway origin (platform+chat_id) must not be shadowed by a
        stray HERMES_SESSION_ID leaking from the same context."""
        env = {
            "HERMES_SESSION_PLATFORM": "telegram",
            "HERMES_SESSION_CHAT_ID": "-1001",
            "HERMES_SESSION_ID": "stored-sess-should-be-ignored",
        }
        with _session_env(env):
            origin = _origin_from_env()
        assert origin["platform"] == "telegram"
        assert "kind" not in origin

    def test_resolve_session_origin_reads_captured_shape(self):
        job = {"origin": {"kind": "session", "session_id": "sess-1", "source": "cli"}}
        assert _resolve_session_origin(job) == job["origin"]

    def test_resolve_session_origin_none_for_messaging_origin(self):
        job = {"origin": {"platform": "telegram", "chat_id": "-1001"}}
        assert _resolve_session_origin(job) is None

    def test_resolve_session_origin_none_for_missing_session_id(self):
        job = {"origin": {"kind": "session"}}
        assert _resolve_session_origin(job) is None


# ── deliver=origin resolution: no "unknown platform" for local surfaces ────


class TestNoUnknownPlatformFailure:
    @pytest.mark.parametrize("source", ["desktop", "tui", "", "webui", "cli"])
    def test_local_surface_deliver_origin_resolves_to_session_target(self, source):
        job = {
            "id": "j1",
            "deliver": "origin",
            "origin": {"kind": "session", "session_id": "sess-abc", "source": source},
        }
        targets = _resolve_delivery_targets(job)
        assert len(targets) == 1
        assert targets[0]["platform"] == SESSION_DELIVERY_PLATFORM
        assert targets[0]["chat_id"] == "sess-abc"

    def test_legacy_webui_platform_tag_skipped_not_raised(self):
        """A hand-edited/legacy job whose origin.platform is a non-messaging
        UI tag (the literal field incident) must be skipped, not raise."""
        job = {
            "id": "j2",
            "deliver": "origin",
            "origin": {"platform": "webui", "chat_id": "some-chat"},
        }
        err = _deliver_result(job, "some output", adapters=None, loop=None)
        assert err is None

    def test_bare_local_ui_deliver_token_never_reports_unknown_platform(self):
        """A bare unconfigured deliver token legitimately reports 'no
        delivery target resolved' (pre-existing, generic behavior for any
        unconfigured platform name) — it must never be the specific
        "unknown platform" crash this module fixes."""
        job = {
            "id": "j3",
            "deliver": "webui",
            "origin": None,
        }
        err = _deliver_result(job, "some output", adapters=None, loop=None)
        assert err is None or "unknown platform" not in err


# ── end-to-end session delivery via _deliver_result ─────────────────────────


class TestSessionDeliveryEndToEnd:
    @pytest.fixture()
    def stored_session(self):
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            db.create_session(session_id="sess-e2e-1", source="tui", model="test-model")
        finally:
            db.close()
        return "sess-e2e-1"

    def test_oneshot_job_delivers_into_originating_session(self, stored_session):
        job = {
            "id": "j-e2e-1",
            "name": "reminder",
            "execution_id": "exec-1",
            "deliver": "origin",
            "origin": {"kind": "session", "session_id": stored_session, "source": "desktop"},
        }
        err = _deliver_result(
            job, "Your reminder fired.", adapters=None, loop=None,
            execution_id=job["execution_id"],
        )
        assert err is None

        from hermes_state import SessionDB

        db = SessionDB()
        try:
            messages = db.get_messages_as_conversation(stored_session)
        finally:
            db.close()

        user_rows = [m for m in messages if m["role"] == "user"]
        assert len(user_rows) == 1
        assert user_rows[0]["content"] == "Your reminder fired."
        # Role alternation: appended as role='user', never assistant (would
        # create assistant->assistant on replay against the prior turn).
        assert user_rows[0]["role"] == "user"
        assert user_rows[0]["display_kind"] == "cron_delivery"
        assert user_rows[0]["display_metadata"]["source"] == "cron"
        assert user_rows[0]["display_metadata"]["job_id"] == "j-e2e-1"

    def test_explicit_session_deliver_token(self, stored_session):
        job = {
            "id": "j-e2e-2",
            "name": "explicit",
            "execution_id": "exec-2",
            "deliver": f"session:{stored_session}",
            "origin": None,
        }
        err = _deliver_result(
            job, "explicit target output", adapters=None, loop=None,
            execution_id=job["execution_id"],
        )
        assert err is None

        from hermes_state import SessionDB

        db = SessionDB()
        try:
            messages = db.get_messages_as_conversation(stored_session)
        finally:
            db.close()
        assert any(m["content"] == "explicit target output" for m in messages)

    def test_missing_stored_session_is_scoped_failure_no_new_session(self):
        job = {
            "id": "j-e2e-3",
            "name": "orphaned",
            "execution_id": "exec-3",
            "deliver": "origin",
            "origin": {"kind": "session", "session_id": "sess-does-not-exist", "source": "desktop"},
        }
        err = _deliver_result(
            job, "output for a dead session", adapters=None, loop=None,
            execution_id=job["execution_id"],
        )
        assert err is not None
        assert "sess-does-not-exist" in err

        from hermes_state import SessionDB

        db = SessionDB()
        try:
            assert db.get_session("sess-does-not-exist") is None
        finally:
            db.close()

    def test_idempotent_retry_does_not_duplicate_append(self, stored_session):
        job = {
            "id": "j-e2e-4",
            "name": "retry-me",
            "execution_id": "exec-shared",
            "deliver": "origin",
            "origin": {"kind": "session", "session_id": stored_session, "source": "desktop"},
        }
        err1 = _deliver_result(
            job, "delivered once", adapters=None, loop=None,
            execution_id=job["execution_id"],
        )
        err2 = _deliver_result(
            job, "delivered once", adapters=None, loop=None,
            execution_id=job["execution_id"],
        )
        assert err1 is None
        assert err2 is None

        from hermes_state import SessionDB

        db = SessionDB()
        try:
            messages = db.get_messages_as_conversation(stored_session)
        finally:
            db.close()
        matching = [m for m in messages if m["content"] == "delivered once"]
        assert len(matching) == 1, "retry with the same execution_id must not duplicate the append"

    def test_different_execution_ids_both_deliver(self, stored_session):
        """Two distinct runs of the same job are two distinct deliveries."""
        base_job = {
            "id": "j-e2e-5",
            "name": "recurring",
            "deliver": "origin",
            "origin": {"kind": "session", "session_id": stored_session, "source": "desktop"},
        }
        err1 = _deliver_result(
            {**base_job, "execution_id": "exec-run-1"}, "run 1 output", adapters=None, loop=None,
            execution_id="exec-run-1",
        )
        err2 = _deliver_result(
            {**base_job, "execution_id": "exec-run-2"}, "run 2 output", adapters=None, loop=None,
            execution_id="exec-run-2",
        )
        assert err1 is None
        assert err2 is None

        from hermes_state import SessionDB

        db = SessionDB()
        try:
            messages = db.get_messages_as_conversation(stored_session)
        finally:
            db.close()
        contents = {m["content"] for m in messages}
        assert "run 1 output" in contents
        assert "run 2 output" in contents


# ── fan-out independence ─────────────────────────────────────────────────────


class TestFanOutIndependence:
    def test_session_and_platform_targets_resolve_independently(self):
        job = {
            "id": "j-fanout",
            "deliver": "session:sess-xyz,telegram:-100:17",
            "origin": None,
        }
        targets = _resolve_delivery_targets(job)
        platforms = {t["platform"] for t in targets}
        assert SESSION_DELIVERY_PLATFORM in platforms
        assert "telegram" in platforms
        session_target = next(t for t in targets if t["platform"] == SESSION_DELIVERY_PLATFORM)
        assert session_target["chat_id"] == "sess-xyz"
