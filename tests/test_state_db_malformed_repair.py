"""Recovery from a malformed state.db schema (duplicate sqlite_master rows).

This is the corruption class behind the user-reported symptom where Desktop /
Dashboard show "no sessions yet" while hundreds of session JSON files sit on
disk, and the backend logs:

    sqlite3.DatabaseError: malformed database schema (messages_fts) -
    table messages_fts already exists

The error fires on the *first* statement of any connection (PRAGMA
journal_mode in apply_wal_with_fallback), before _init_schema runs. Live FTS
is fully retired: repair drops FTS objects and never recreates them. Search
uses canonical LIKE.
"""
import sqlite3
import uuid
from pathlib import Path

import pytest

import hermes_state
from tests.state.legacy_fts_ddl import LEGACY_FTS_SQL
from hermes_state import (
    SessionDB,
    is_malformed_db_error,
    repair_state_db_schema,
)


def _build_healthy_db(db_path: Path) -> str:
    db = SessionDB(db_path=db_path)
    sid = db.create_session(session_id=str(uuid.uuid4()), source="cli")
    for i in range(5):
        db.append_message(sid, role="user", content=f"hello world {i}")
        db.append_message(sid, role="assistant", content=f"reply about pizza {i}")
    db.close()
    return sid


def _inject_legacy_fts(db_path: Path) -> None:
    """Install pre-retirement FTS objects on a no-FTS healthy DB."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(LEGACY_FTS_SQL)
    try:
        conn.execute(
            "INSERT INTO messages_fts(rowid, content) "
            "SELECT id, COALESCE(content, '') || ' ' || "
            "COALESCE(tool_name, '') || ' ' || COALESCE(tool_calls, '') "
            "FROM messages"
        )
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def _corrupt_duplicate_fts(db_path: Path) -> None:
    """Inject a duplicate messages_fts row into sqlite_master.

    Reproduces 'malformed database schema (messages_fts) - table
    messages_fts already exists'.
    """
    _inject_legacy_fts(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA writable_schema=ON")
    conn.execute(
        "INSERT INTO sqlite_master (type, name, tbl_name, rootpage, sql) "
        "SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master "
        "WHERE name='messages_fts'"
    )
    conn.commit()
    conn.close()


def _fts_object_count(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    n = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'messages_fts%'"
    ).fetchone()[0]
    conn.close()
    return n


def test_duplicate_fts_makes_every_statement_fail(tmp_path):
    """Document the failure: not even PRAGMA journal_mode survives."""
    db_path = tmp_path / "state.db"
    _build_healthy_db(db_path)
    _corrupt_duplicate_fts(db_path)

    conn = sqlite3.connect(str(db_path))
    with pytest.raises(sqlite3.DatabaseError) as exc_info:
        conn.execute("PRAGMA journal_mode").fetchone()
    conn.close()
    assert is_malformed_db_error(exc_info.value)


def test_repair_preserves_sessions_and_messages(tmp_path):
    db_path = tmp_path / "state.db"
    _build_healthy_db(db_path)
    _corrupt_duplicate_fts(db_path)

    report = repair_state_db_schema(db_path)
    assert report["repaired"] is True
    assert report["strategy"] in {"dedup_schema", "drop_fts"}
    assert report["backup_path"] and Path(report["backup_path"]).exists()

    conn = sqlite3.connect(str(db_path))
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 10
    conn.close()


def test_repaired_db_search_works_via_canonical_like(tmp_path):
    db_path = tmp_path / "state.db"
    _build_healthy_db(db_path)
    _corrupt_duplicate_fts(db_path)
    repair_state_db_schema(db_path)

    db = SessionDB(db_path=db_path)
    try:
        assert db._fts_enabled is False
        assert _fts_object_count(db_path) == 0
        hits = db.search_messages("pizza")
        assert len(hits) == 5
    finally:
        db.close()


def test_sessiondb_auto_heals_on_open(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    sid = _build_healthy_db(db_path)
    _corrupt_duplicate_fts(db_path)

    monkeypatch.setattr(hermes_state, "_repair_attempted_paths", set())

    db = SessionDB(db_path=db_path)
    try:
        assert db._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert db._conn.execute(
            "SELECT id FROM sessions WHERE id=?", (sid,)
        ).fetchone() is not None
        assert _fts_object_count(db_path) == 0
        assert len(db.search_messages("pizza")) == 5
    finally:
        db.close()


def test_auto_heal_attempted_once_per_process(tmp_path, monkeypatch):
    """A still-broken DB must not loop: the second open just raises."""
    db_path = tmp_path / "state.db"
    _build_healthy_db(db_path)
    _corrupt_duplicate_fts(db_path)
    monkeypatch.setattr(hermes_state, "_repair_attempted_paths", set())

    calls = {"n": 0}

    def fake_repair(path, **kw):
        calls["n"] += 1
        return {"repaired": False, "strategy": None, "backup_path": None, "error": "x"}

    monkeypatch.setattr(hermes_state, "repair_state_db_schema", fake_repair)

    with pytest.raises(sqlite3.DatabaseError):
        SessionDB(db_path=db_path)
    with pytest.raises(sqlite3.DatabaseError):
        SessionDB(db_path=db_path)
    assert calls["n"] == 1


def test_is_malformed_db_error_discriminates():
    assert is_malformed_db_error(
        sqlite3.DatabaseError("malformed database schema (messages_fts) - ...")
    )
    assert is_malformed_db_error(sqlite3.DatabaseError("database disk image is malformed"))
    assert not is_malformed_db_error(sqlite3.OperationalError("database is locked"))
    assert not is_malformed_db_error(ValueError("nope"))


def test_strategy_drop_fts_when_dedup_insufficient(tmp_path, monkeypatch):
    """Escalation drops retired FTS objects; search uses canonical LIKE."""
    db_path = tmp_path / "state.db"
    _build_healthy_db(db_path)
    _corrupt_duplicate_fts(db_path)

    real_check = hermes_state._db_opens_cleanly
    calls = {"n": 0}

    def flaky_check(path):
        calls["n"] += 1
        try:
            probe = sqlite3.connect(str(path))
            still_has_fts = probe.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE name LIKE 'messages_fts%'"
            ).fetchone()[0]
            probe.close()
        except sqlite3.DatabaseError:
            return "pretend still broken (schema unreadable)"
        if still_has_fts:
            return "pretend earlier passes were insufficient"
        return real_check(path)

    monkeypatch.setattr(hermes_state, "_db_opens_cleanly", flaky_check)
    report = repair_state_db_schema(db_path)
    monkeypatch.undo()

    assert report["repaired"] is True
    assert report["strategy"] == "drop_fts"
    assert calls["n"] >= 2

    db = SessionDB(db_path=db_path)
    try:
        assert db._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 10
        assert _fts_object_count(db_path) == 0
        assert len(db.search_messages("pizza")) == 5
    finally:
        db.close()


def test_unrepairable_file_fails_safely(tmp_path, monkeypatch):
    """A file too damaged to recover must report failure and keep a backup."""
    db_path = tmp_path / "state.db"
    db_path.write_bytes(b"SQLite format 3\x00" + b"\x00\xde\xad\xbe\xef" * 200)

    report = repair_state_db_schema(db_path)
    assert report["repaired"] is False
    assert report["error"]
    assert report["backup_path"] and Path(report["backup_path"]).exists()


def test_non_malformed_error_is_not_auto_repaired(tmp_path, monkeypatch):
    """Auto-heal only for the malformed-schema class."""
    db_path = tmp_path / "state.db"
    db_path.write_bytes(b"this is definitely not a sqlite database")
    monkeypatch.setattr(hermes_state, "_repair_attempted_paths", set())

    called = {"n": 0}
    orig = hermes_state.repair_state_db_schema

    def spy(*a, **kw):
        called["n"] += 1
        return orig(*a, **kw)

    monkeypatch.setattr(hermes_state, "repair_state_db_schema", spy)
    with pytest.raises(sqlite3.DatabaseError):
        SessionDB(db_path=db_path)
    assert called["n"] == 0


def test_repair_on_clean_db_is_noop(tmp_path):
    """Repair must not damage a healthy no-FTS DB."""
    db_path = tmp_path / "state.db"
    _build_healthy_db(db_path)

    report = repair_state_db_schema(db_path, backup=False)
    assert report["repaired"] is True

    conn = sqlite3.connect(str(db_path))
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 10
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()
    assert _fts_object_count(db_path) == 0


def test_legacy_corrupt_fts_dropped_not_rebuilt(tmp_path):
    """Leftover corrupt FTS is scrubbed; writes/search stay on canonical tables."""
    from hermes_state import _db_opens_cleanly

    db_path = tmp_path / "state.db"
    _build_healthy_db(db_path)
    _inject_legacy_fts(db_path)
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("UPDATE messages_fts_data SET block = X'DEADBEEFDEADBEEF'")
    conn.close()

    # Base-table reads still succeed with corrupt FTS shadows present.
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 10
    conn.close()

    report = repair_state_db_schema(db_path)
    assert report["repaired"] is True
    assert report["strategy"] in ("drop_fts", "already_healthy", "dedup_schema")
    assert _db_opens_cleanly(db_path) is None
    assert _fts_object_count(db_path) == 0

    db = SessionDB(db_path=db_path)
    try:
        assert db._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 10
        sid = db._conn.execute("SELECT id FROM sessions LIMIT 1").fetchone()[0]
        db.append_message(sid, role="user", content="post repair pizza message")
        assert db._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 11
        assert len(db.search_messages("pizza")) >= 5
        assert _fts_object_count(db_path) == 0
    finally:
        db.close()


def test_repair_noop_db_uses_already_healthy_shortcut(tmp_path):
    """A healthy DB returns the cheap already_healthy strategy, no surgery."""
    db_path = tmp_path / "state.db"
    _build_healthy_db(db_path)
    report = repair_state_db_schema(db_path, backup=False)
    assert report["repaired"] is True
    assert report["strategy"] == "already_healthy"


def test_select_cached_agent_history_prefers_longer_live_transcript():
    """Gateway guard keeps the live transcript when persisted history lags."""
    from gateway.run import _select_cached_agent_history

    persisted = [{"role": "user", "content": "only one"}]
    live = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    ]
    # Persisted lags → keep the longer live copy.
    out = _select_cached_agent_history(persisted, live)
    assert out == live
    assert out is not live  # returns a copy, not the live list

    # Persisted is current/longer → leave it untouched (identity preserved).
    longer_persisted = live + [{"role": "assistant", "content": "four"}]
    out2 = _select_cached_agent_history(longer_persisted, live)
    assert out2 is longer_persisted

    # No live transcript / not a list → no-op.
    assert _select_cached_agent_history(persisted, None) is persisted
    assert _select_cached_agent_history(persisted, "nope") is persisted
