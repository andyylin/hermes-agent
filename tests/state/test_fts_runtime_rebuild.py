"""Legacy FTS scrub + write-path DatabaseError propagation (live FTS retired).

Live FTS indexes and write triggers are no longer created. These tests verify:
- fresh DBs have no FTS objects and search uses canonical LIKE
- leftover legacy FTS objects on a pre-retirement DB are scrubbed on open
- write-path DatabaseError is never swallowed/retried
- unrelated write errors still propagate
"""

import sqlite3

import pytest

from tests.state.legacy_fts_ddl import LEGACY_FTS_SQL
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    d = SessionDB(db_path=tmp_path / "state.db")
    yield d
    try:
        d.close()
    except Exception:
        pass


def _message_contents(db_path):
    raw = sqlite3.connect(str(db_path))
    rows = raw.execute("SELECT content FROM messages ORDER BY id").fetchall()
    raw.close()
    return [r[0] for r in rows]


def _fts_object_count(db_path) -> int:
    raw = sqlite3.connect(str(db_path))
    n = raw.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'messages_fts%'"
    ).fetchone()[0]
    raw.close()
    return n


def _install_legacy_fts(db_path) -> None:
    """Simulate a pre-retirement DB with base FTS table + write triggers."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(LEGACY_FTS_SQL)
    try:
        conn.execute(
            "INSERT INTO messages_fts(rowid, content) "
            "SELECT id, COALESCE(content, '') FROM messages"
        )
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


class TestRuntimeFtsRebuild:
    def test_fresh_db_has_no_fts_and_search_uses_canonical_like(self, db, tmp_path):
        assert db._fts_enabled is False
        assert _fts_object_count(tmp_path / "state.db") == 0
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "hello searchable world")
        hits = db.search_messages("searchable")
        assert len(hits) == 1
        assert _fts_object_count(tmp_path / "state.db") == 0

    def test_append_and_search_after_legacy_fts_open(self, tmp_path):
        """Open retires legacy FTS; writes/search stay on canonical tables."""
        path = tmp_path / "state.db"
        seeded = SessionDB(db_path=path)
        seeded.create_session("s1", source="test")
        seeded.append_message("s1", "user", "before corruption")
        seeded.close()

        _install_legacy_fts(path)
        assert _fts_object_count(path) > 0

        restored = SessionDB(db_path=path)
        try:
            assert restored._fts_enabled is False
            assert _fts_object_count(path) == 0
            msg_id = restored.append_message("s1", "user", "searchable needle text")
            assert msg_id is not None
            assert _message_contents(path) == [
                "before corruption",
                "searchable needle text",
            ]
            hits = restored.search_messages("needle")
            assert len(hits) == 1
        finally:
            restored.close()

    def test_rebuild_fts_is_noop_and_scrubs_leftovers(self, tmp_path):
        path = tmp_path / "state.db"
        db = SessionDB(db_path=path)
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "seed")
        db.close()

        _install_legacy_fts(path)
        assert _fts_object_count(path) > 0
        db = SessionDB(db_path=path)
        try:
            assert db.rebuild_fts() == 0
            assert db.optimize_fts() == 0
            assert _fts_object_count(path) == 0
            assert len(db.search_messages("seed")) == 1
        finally:
            db.close()

    def test_database_error_on_write_propagates_immediately(self, db):
        """Canonical DatabaseError is not scrubbed or retried."""
        db.create_session("s1", source="test")
        attempts = {"n": 0}

        def _boom(conn):
            attempts["n"] += 1
            raise sqlite3.DatabaseError("database disk image is malformed")

        with pytest.raises(sqlite3.DatabaseError, match="malformed"):
            db._execute_write(_boom)
        assert attempts["n"] == 1

    def test_non_fts_errors_still_propagate(self, db):
        db.create_session("s1", source="test")

        def _bad(conn):
            raise sqlite3.IntegrityError("NOT NULL constraint failed: x.y")

        with pytest.raises(sqlite3.IntegrityError):
            db._execute_write(_bad)

    def test_lock_retry_path_unchanged(self, db):
        """A locked error still follows the jitter-retry path."""
        calls = {"n": 0}

        def _flaky(conn):
            calls["n"] += 1
            if calls["n"] < 3:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        assert db._execute_write(_flaky) == "ok"
        assert calls["n"] == 3
