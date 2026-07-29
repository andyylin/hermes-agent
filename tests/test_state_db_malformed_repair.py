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


# ── FTS read-corruption class (#66724) ───────────────────────────────────
# Even when writes succeed, partial FTS5 shadow-table damage makes MATCH /
# snippet / rank queries fail with DatabaseError("database disk image is
# malformed") while plain reads of the FTS5 table still parse. The read
# probe in _db_opens_cleanly must surface this corruption class as a reason
# so the repair path triggers, but it must NOT misclassify the supported
# degraded-runtime path (no fts5 module / no trigram tokenizer) as
# corruption — doing so would route a healthy degraded DB through the
# repair fallback that deletes the messages_fts% schema.


def _corrupt_fts_shadow_segments(db_path: Path) -> None:
    """Overwrite the FTS5 shadow b-tree blocks for ``messages_fts`` only.

    Distinct from ``_corrupt_fts_index_data`` which targets the writes-side
    trigger path; this targets the MATCH query path so the read probe is
    what fires.
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("UPDATE messages_fts_data SET block = X'BADC0FFEE0DDF00D'")
    conn.close()


@pytest.mark.skip(reason="live FTS read probes are retired in the Andy custom runtime")
def test_fts_read_corruption_detected_by_read_probe(tmp_path):
    """Partial shadow-table damage is caught by the FTS5 read probe.

    Without the read probe, ``_db_opens_cleanly`` reports the DB healthy
    even though ``session_search`` and ``/resume`` title resolution fail
    with ``database disk image is malformed`` — the exact silent-fail
    behavior reported in #66724.
    """
    from hermes_state import _db_opens_cleanly

    db_path = tmp_path / "state.db"
    _build_healthy_db(db_path)
    assert _db_opens_cleanly(db_path) is None

    _corrupt_fts_shadow_segments(db_path)

    reason = _db_opens_cleanly(db_path)
    assert reason is not None
    assert "messages_fts" in reason
    # Message varies by SQLite build (same variance documented in
    # SessionDB._is_fts_write_corruption_error): older builds raise the
    # generic "database disk image is malformed"; newer builds raise the
    # FTS5-specific 'fts5: corrupt structure record for table "..."'.
    # Both are the same corruption class.
    reason_l = reason.lower()
    assert (
        "malformed" in reason_l
        or "database disk image" in reason_l
        or ("fts5" in reason_l and "corrupt" in reason_l)
    )


@pytest.mark.skip(reason="live FTS read probes are retired in the Andy custom runtime")
def test_fts_read_corruption_repaired_in_place(tmp_path):
    """``repair_state_db_schema`` rebuilds the FTS index so reads resume."""
    from hermes_state import _db_opens_cleanly

    db_path = tmp_path / "state.db"
    _build_healthy_db(db_path)
    _corrupt_fts_shadow_segments(db_path)

    assert _db_opens_cleanly(db_path) is not None  # unhealthy before

    report = repair_state_db_schema(db_path)
    assert report["repaired"] is True
    assert _db_opens_cleanly(db_path) is None  # healthy after rebuild

    # Search back online.
    db = SessionDB(db_path=db_path)
    try:
        hits = db._conn.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'pizza'"
        ).fetchone()[0]
        assert hits >= 5
    finally:
        db.close()


# ── Degraded-runtime compatibility (regression for #66906 review) ────────
# The read probe must NOT misclassify a supported degraded runtime (no
# fts5 module / no trigram tokenizer) as corruption. If it did, a healthy
# degraded DB would be sent into the repair path, whose final fallback
# deletes the messages_fts% schema — breaking the very FTS tables that
# may have been inherited from a prior build that did have FTS5.


class _NoFts5RuntimeCursor(sqlite3.Cursor):
    """Simulate a runtime without the fts5 module: fts5 table exists but
    MATCH queries raise the canonical capability error."""

    def execute(self, sql, parameters=()):
        probe = sql.strip()
        if "MATCH" in probe and '""' in probe and "messages_fts " in probe:
            raise sqlite3.OperationalError("no such module: fts5")
        return super().execute(sql, parameters)


class _NoFts5RuntimeConnection(sqlite3.Connection):
    def cursor(self, factory=None):
        return super().cursor(factory or _NoFts5RuntimeCursor)


class _NoTrigramRuntimeCursor(sqlite3.Cursor):
    """Simulate a runtime with FTS5 but without the trigram tokenizer."""

    def execute(self, sql, parameters=()):
        probe = sql.strip()
        if "MATCH" in probe and '""' in probe and "messages_fts_trigram" in probe:
            raise sqlite3.OperationalError("no such tokenizer: trigram")
        return super().execute(sql, parameters)


class _NoTrigramRuntimeConnection(sqlite3.Connection):
    def cursor(self, factory=None):
        return super().cursor(factory or _NoTrigramRuntimeCursor)


@pytest.mark.skip(reason="live FTS capability probes are retired in the Andy custom runtime")
def test_fts_read_probe_returns_none_when_fts5_module_missing(tmp_path, monkeypatch):
    """Capability error on MATCH must not surface as corruption.

    Simulates a healthy DB on a SQLite build without the fts5 module:
    the messages_fts table exists (from a previous init on a build with
    fts5) and MATCH queries raise the canonical "no such module: fts5".
    _db_opens_cleanly must NOT classify this as corruption — otherwise
    repair would be triggered and its final fallback would delete the
    messages_fts% schema, breaking the search feature entirely.
    """
    from hermes_state import _db_opens_cleanly

    db_path = tmp_path / "state.db"
    _build_healthy_db(db_path)

    real_connect = sqlite3.connect

    def connect_no_fts5(*args, **kwargs):
        kwargs["factory"] = _NoFts5RuntimeConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr("hermes_state.sqlite3.connect", connect_no_fts5)

    # Healthy degraded DB → probe returns None. Repair path must NOT fire.
    assert _db_opens_cleanly(db_path) is None


@pytest.mark.skip(reason="live FTS capability probes are retired in the Andy custom runtime")
def test_fts_read_probe_returns_none_when_trigram_missing(tmp_path, monkeypatch):
    """Capability error on trigram MATCH must not surface as corruption."""
    from hermes_state import _db_opens_cleanly

    db_path = tmp_path / "state.db"
    _build_healthy_db(db_path)

    real_connect = sqlite3.connect

    def connect_no_trigram(*args, **kwargs):
        kwargs["factory"] = _NoTrigramRuntimeConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr("hermes_state.sqlite3.connect", connect_no_trigram)

    assert _db_opens_cleanly(db_path) is None


# ── FTS write-corruption class (#50502) ──────────────────────────────────
def test_repair_retires_corrupt_legacy_fts_without_losing_canonical_rows(tmp_path):
    """A readable DB with broken legacy FTS is repaired from canonical rows."""
    from hermes_state import _db_opens_cleanly

    # A readable state.db can still reject every message write through legacy
    # messages_fts* triggers when the derived index is corrupt. Plain
    # `SELECT COUNT(*)` reads succeed, so repair must retire those artifacts.

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


def _corrupt_btree_index(db_path: Path, index_name: str) -> None:
    """Make a real B-tree index stale so integrity_check reports
    'wrong # of entries in index <name>'.

    writable_schema hack: temporarily rewrite the index definition in
    sqlite_master to a partial index (``WHERE 0``), REINDEX so its b-tree is
    rebuilt EMPTY, then restore the original full definition. The stored
    b-tree now has zero entries while the schema says it must cover every
    row — exactly the on-disk state issue #63386 reported for
    idx_sessions_handoff_state, produced without any mocking.
    """
    raw = sqlite3.connect(str(db_path))
    orig_sql = raw.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    ).fetchone()[0]

    def _set_index_sql(conn, sql):
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute(
            "UPDATE sqlite_master SET sql=? WHERE type='index' AND name=?",
            (sql, index_name),
        )
        ver = conn.execute("PRAGMA schema_version").fetchone()[0]
        conn.execute(f"PRAGMA schema_version={ver + 1}")
        conn.execute("PRAGMA writable_schema=OFF")
        conn.commit()

    _set_index_sql(raw, orig_sql + " WHERE 0")
    raw.close()

    # Fresh connection so the doctored schema is re-parsed, then rebuild the
    # index under the WHERE 0 definition — empty b-tree on disk.
    raw = sqlite3.connect(str(db_path))
    raw.execute(f"REINDEX {index_name}")
    raw.commit()
    # Restore the original (full) definition: schema and b-tree now disagree.
    _set_index_sql(raw, orig_sql)
    raw.close()


def test_repair_rebuilds_stale_btree_indexes(tmp_path):
    """repair_state_db_schema repairs a REAL stale B-tree index via REINDEX.

    End-to-end, no mocks: a genuinely stale index (empty b-tree under a full
    index definition — the #63386 'wrong # of entries in index' class) is
    detected by the real _db_opens_cleanly, repaired by Strategy 0.5
    (REINDEX), and the DB verifies clean afterwards with real integrity
    checks.
    """
    db_path = tmp_path / "state.db"
    _build_healthy_db(db_path)

    _corrupt_btree_index(db_path, "idx_messages_session")

    # The real detector must see the real corruption...
    reason = hermes_state._db_opens_cleanly(db_path)
    assert reason is not None
    assert "wrong # of entries in index idx_messages_session" in reason

    # ...and the real repair ladder must fix it via REINDEX.
    report = repair_state_db_schema(db_path)
    assert report["repaired"] is True
    assert report["strategy"] == "reindex_btree"

    # Post-repair the DB is genuinely healthy: detector and raw
    # integrity_check both agree, and the repaired index answers queries.
    assert hermes_state._db_opens_cleanly(db_path) is None
    raw = sqlite3.connect(str(db_path))
    assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    n = raw.execute(
        "SELECT count(*) FROM messages INDEXED BY idx_messages_session "
        "WHERE session_id IS NOT NULL"
    ).fetchone()[0]
    raw.close()
    assert n == 10  # every row visible through the rebuilt index


def test_repair_stale_btree_index_preserves_rows(tmp_path):
    """The REINDEX strategy is non-destructive: sessions/messages survive."""
    db_path = tmp_path / "state.db"
    sid = _build_healthy_db(db_path)
    _corrupt_btree_index(db_path, "idx_messages_session")

    report = repair_state_db_schema(db_path, backup=False)
    assert report["strategy"] == "reindex_btree"

    db = SessionDB(db_path=db_path)
    try:
        msgs = db.get_messages(sid)
        assert len(msgs) == 10
        assert msgs[0]["content"] == "hello world 0"
    finally:
        db.close()


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
