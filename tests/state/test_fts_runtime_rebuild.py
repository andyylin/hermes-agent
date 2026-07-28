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


def _corrupt_fts(db_path):
    raw = sqlite3.connect(str(db_path))
    raw.execute(
        "UPDATE messages_fts_data SET block = X'DEADBEEFDEADBEEFDEADBEEFDEADBEEF'"
    )
    raw.commit()
    raw.close()


def _corrupt_trigram_fts(db_path):
    raw = sqlite3.connect(str(db_path))
    raw.execute(
        "UPDATE messages_fts_trigram_data "
        "SET block = X'DEADBEEFDEADBEEFDEADBEEFDEADBEEF'"
    )
    raw.commit()
    raw.close()


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

    def test_search_messages_self_heals_after_fts_corruption(self, db, tmp_path):
        """A read-only session that only SEARCHES (no write after corruption)
        must self-heal too. The MATCH read raises the corruption class
        (DatabaseError / 'fts5: corrupt structure record'), NOT the
        OperationalError that search_messages caught — so before this fix the
        search crashed until a write or restart rebuilt the index.
        """
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "a searchable needle here")

        _corrupt_fts(tmp_path / "state.db")
        # Injected via a raw connection, so no write on THIS instance has
        # consumed the one-shot rebuild yet.
        assert db._fts_runtime_rebuild_attempted is False

        results = db.search_messages("needle")

        assert db._fts_runtime_rebuild_attempted is True  # the search rebuilt it
        assert results  # non-empty: the rebuilt index matched the query
        assert any("needle" in (r.get("snippet") or "") for r in results)

    def test_trigram_search_self_heals_after_fts_corruption(self, db, tmp_path):
        """The CJK/trigram MATCH branch has the same read-corruption exposure
        as the main FTS5 branch: it caught only OperationalError (query
        syntax), so a corrupt trigram shadow table raised DatabaseError
        straight out of search_messages. It must self-heal via the shared
        one-shot rebuild and answer from the rebuilt trigram index.
        """
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        if not db._trigram_available:
            pytest.skip("trigram tokenizer unavailable in this build")
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "关于大别山项目的进展报告")

        _corrupt_trigram_fts(tmp_path / "state.db")
        assert db._fts_runtime_rebuild_attempted is False

        # >=3 CJK chars per token → routed to the trigram branch.
        results = db.search_messages("大别山项目")

        assert db._fts_runtime_rebuild_attempted is True  # search rebuilt it
        assert results
        # The rebuilt trigram index answered (trigram snippets use >>> <<<),
        # i.e. we did not silently degrade to the LIKE fallback.
        assert any(">>>" in (r.get("snippet") or "") for r in results)

    def test_trigram_search_falls_back_to_like_when_rebuild_consumed(
        self, db, tmp_path
    ):
        """When the one-shot rebuild was already consumed, a corrupt trigram
        index must NOT crash search_messages — it degrades to the LIKE
        substring fallback, which reads only the canonical messages table.
        """
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
        if not db._trigram_available:
            pytest.skip("trigram tokenizer unavailable in this build")
        db.create_session("s1", source="test")
        db.append_message("s1", "user", "关于大别山项目的进展报告")

        # Consume the one-shot guard, then corrupt again.
        _corrupt_trigram_fts(tmp_path / "state.db")
        db.append_message("s1", "user", "seed to trigger write-path heal")
        assert db._fts_runtime_rebuild_attempted is True
        _corrupt_trigram_fts(tmp_path / "state.db")

        # Before the fix this raised sqlite3.DatabaseError.
        results = db.search_messages("大别山项目")
        assert results  # LIKE fallback found the canonical row
        assert any("大别山项目" in (r.get("snippet") or "") for r in results)

    def test_rebuild_is_one_shot_per_instance(self, db, tmp_path):
        if not db._fts_enabled:
            pytest.skip("FTS5 unavailable in this build")
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
