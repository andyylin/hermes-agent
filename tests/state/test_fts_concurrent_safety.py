"""Concurrent multi-connection state.db safety (production stress class).

Production gate on a ~136k-message corpus showed that after concurrent
SessionDB writers + a searcher (ASCII FTS + CJK LIKE), all application
operations could succeed and canonical counts return to baseline, yet
``PRAGMA quick_check`` raised ``database disk image is malformed`` while
``integrity_check`` still returned ok. The residual bug class is concurrent
``wal_checkpoint(TRUNCATE)`` on short-lived connections (every close()) on
large DBs — the same class as issue #45383 that was only partially fixed by
switching *periodic* checkpoints to PASSIVE.

This suite is a deterministic synthetic stand-in for that gate: multi-thread
SessionDB connections share one file, write + search concurrently, close
(PASSIVE only), delete probes, then assert both PRAGMA checks pass and
canonical/FTS parity holds. It never reads private corpora.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from hermes_state import SessionDB


@pytest.fixture
def corpus_db(tmp_path):
    """Build a medium-size WAL-backed corpus with base FTS parity."""
    path = tmp_path / "state.db"
    # Schema via SessionDB, bulk body via raw SQL for speed.
    db = SessionDB(db_path=path)
    if not db._fts_enabled:
        db.close()
        pytest.skip("FTS5 unavailable in this build")
    db.close()

    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("BEGIN")
    n_sessions = 120
    per = 80  # 9600 messages — enough pages for concurrent close stress
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
    fts = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
    assert msgs == fts == n_sessions * per
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()
    return path, msgs


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
        try:
            fts = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
        except Exception as exc:
            fts = f"ERR {exc}"
        triggers = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='trigger' AND name LIKE 'messages_fts_%'"
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        "quick_check": qc,
        "integrity_check": ic,
        "messages": msgs,
        "fts": fts,
        "triggers": triggers,
    }


class TestConcurrentSessionDbSafety:
    def test_close_uses_passive_not_truncate_checkpoint(self, tmp_path):
        """close() must never issue TRUNCATE (multi-connection corruption class)."""
        db = SessionDB(db_path=tmp_path / "state.db")
        seen: list[str] = []
        real_conn = db._conn

        class _ConnProxy:
            """sqlite3.Connection.execute is read-only; wrap the connection."""

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
        """Automatic FTS optimize on the write path is retired (explicit only)."""
        db = SessionDB(db_path=tmp_path / "state.db")
        try:
            if not db._fts_enabled:
                pytest.skip("FTS5 unavailable in this build")
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
        finally:
            db.close()

    def test_concurrent_writers_and_searcher_preserve_db_image(
        self, corpus_db, monkeypatch
    ):
        """Production-shaped concurrent stress must leave PRAGMA checks clean."""
        path, baseline_msgs = corpus_db

        # No runtime FTS rebuild allowed — mirrors the production gate.
        def _no_rebuild(*_a, **_k):
            raise RuntimeError("rebuild forbidden in concurrent safety test")

        monkeypatch.setattr(
            SessionDB, "_rebuild_fts_indexes", staticmethod(_no_rebuild)
        )

        # Open/close a few times (startup must stay idle for healthy FTS).
        for _ in range(5):
            db = SessionDB(db_path=path)
            db.close()

        errors: list[str] = []
        probe_ids: list[str] = []
        lock = threading.Lock()
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
                for q in ("ascii", "payload", "writer"):
                    db.search_messages(q)
                for q in ("中文", "检索", "混合ascii 中文"):
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

        # Cleanup probes + controlled checkpoint (PASSIVE; no TRUNCATE).
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
        assert status["fts"] == baseline_msgs, status
        # Base FTS triggers only (trigram retired).
        assert status["triggers"] == 3, status
