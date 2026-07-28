"""Concurrent multi-connection safety after full live FTS retirement.

Production gate: concurrent SessionDB writers + searcher on a large corpus
corrupted live base FTS while canonical tables stayed healthy. Live FTS is
now fully retired — no tables, no triggers, no MATCH. This suite asserts the
post-retirement invariants under multi-connection write/search/close load.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from hermes_state import SessionDB


def _fts_object_count(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    n = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'messages_fts%'"
    ).fetchone()[0]
    conn.close()
    return n


def _pragma_status(path: Path) -> dict:
    conn = sqlite3.connect(str(path))
    try:
        try:
            qc = conn.execute("PRAGMA quick_check").fetchone()[0]
        except Exception as exc:
            qc = f"RAISE {type(exc).__name__}: {exc}"
        try:
            ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
        except Exception as exc:
            ic = f"RAISE {type(exc).__name__}: {exc}"
        msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        fts_objs = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'messages_fts%'"
        ).fetchone()[0]
        triggers = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='trigger' AND name LIKE 'messages_fts%'"
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        "quick_check": qc,
        "integrity_check": ic,
        "messages": msgs,
        "fts_objects": fts_objs,
        "fts_triggers": triggers,
    }


@pytest.fixture
def corpus_db(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.close()

    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("BEGIN")
    n_sessions = 80
    per = 40
    now = time.time()
    for s in range(n_sessions):
        sid = f"seed-{s:04d}"
        conn.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
            (sid, "cli", now),
        )
        conn.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES (?, ?, ?, ?)",
            [
                (
                    sid,
                    "user" if i % 2 == 0 else "assistant",
                    f"seed s{s} m{i} alpha beta deploy docker 中文{s}",
                    now + i * 0.001,
                )
                for i in range(per)
            ],
        )
    conn.commit()
    msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert msgs == n_sessions * per
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()
    assert _fts_object_count(path) == 0
    return path, msgs


class TestConcurrentSessionDbSafety:
    def test_close_uses_passive_not_truncate_checkpoint(self, tmp_path):
        """close() must never issue TRUNCATE (multi-connection corruption class)."""
        db = SessionDB(db_path=tmp_path / "state.db")
        seen: list[str] = []
        real_conn = db._conn

        class _ConnProxy:
            def execute(self, sql, *args, **kwargs):
                text = sql if isinstance(sql, str) else str(sql)
                if "wal_checkpoint" in text.lower():
                    seen.append(text)
                return real_conn.execute(sql, *args, **kwargs)

            def close(self):
                return real_conn.close()

            def __getattr__(self, name):
                return getattr(real_conn, name)

        db._conn = _ConnProxy()  # type: ignore[assignment]
        db.close()
        assert seen, "close() should attempt a WAL checkpoint"
        assert all("PASSIVE" in s.upper() for s in seen)
        assert not any("TRUNCATE" in s.upper() for s in seen)

    def test_write_path_does_not_auto_optimize_fts(self, tmp_path, monkeypatch):
        db = SessionDB(db_path=tmp_path / "state.db")
        try:
            assert db._OPTIMIZE_EVERY_N_WRITES == 0
            calls = {"n": 0}

            def _boom():
                calls["n"] += 1
                raise AssertionError("auto optimize must not run")

            monkeypatch.setattr(db, "optimize_fts", _boom)
            db.create_session("s1", source="cli")
            for i in range(30):
                db.append_message("s1", "user", f"msg {i}")
            assert calls["n"] == 0
            assert _fts_object_count(tmp_path / "state.db") == 0
        finally:
            db.close()

    def test_concurrent_reopen_write_search_no_fts_clean_pragmas(
        self, corpus_db
    ):
        """Multi-connection open/write/search: no FTS objects; PRAGMAs clean.

        Mirrors the production gate shape: sequential opens scrub any legacy
        FTS first, then concurrent SessionDB connections create/append/search
        without racing healthy-open FTS DDL.
        """
        path, baseline_msgs = corpus_db

        # Five sequential opens (legacy FTS already absent) — must stay clean.
        for _ in range(5):
            db = SessionDB(db_path=path)
            assert db._fts_enabled is False
            assert _fts_object_count(path) == 0
            db.close()
        assert _fts_object_count(path) == 0

        errors: list[str] = []
        probe_ids: list[str] = []
        lock = threading.Lock()
        # Barrier after each connection is open so open-time work overlaps.
        barrier = threading.Barrier(3)

        def writer(wid: int) -> None:
            try:
                db = SessionDB(db_path=path)
                barrier.wait(timeout=30)
                sid = f"probe-w{wid}"
                db.create_session(sid, source="cli")
                with lock:
                    probe_ids.append(sid)
                for i in range(12):
                    role = "user" if i % 2 == 0 else "assistant"
                    db.append_message(
                        sid, role, f"writer{wid} msg {i} ascii payload"
                    )
                db.close()
            except Exception as exc:
                errors.append(f"writer{wid}: {exc!r}")

        def searcher() -> None:
            try:
                db = SessionDB(db_path=path)
                barrier.wait(timeout=30)
                sid = "probe-s"
                db.create_session(sid, source="cli")
                with lock:
                    probe_ids.append(sid)
                db.append_message(
                    sid, "user", "mixed ascii 中文检索词 concurrent"
                )
                for q in ("ascii", "payload", "writer", "alpha"):
                    db.search_messages(q)
                for q in ("中文", "检索", "deploy"):
                    db.search_messages(q)
                db.close()
            except Exception as exc:
                errors.append(f"searcher: {exc!r}")

        threads = [
            threading.Thread(target=writer, args=(1,)),
            threading.Thread(target=writer, args=(2,)),
            threading.Thread(target=searcher),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert errors == [], f"concurrent ops failed: {errors}"

        db = SessionDB(db_path=path)
        for sid in list(probe_ids):
            db.delete_session(sid)
        with db._lock:
            db._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        db.close()

        status = _pragma_status(path)
        assert status["quick_check"] == "ok", status
        assert status["integrity_check"] == "ok", status
        assert status["messages"] == baseline_msgs, status
        assert status["fts_objects"] == 0, status
        assert status["fts_triggers"] == 0, status
