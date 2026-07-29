"""Andy custom-runtime policy: canonical LIKE search, no live SQLite FTS."""

import ast
import sqlite3
from pathlib import Path

from hermes_state import SessionDB, _cjk_fts_config_enabled


def _fts_objects(path):
    conn = sqlite3.connect(str(path))
    try:
        return [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'messages_fts%'"
            ).fetchall()
        ]
    finally:
        conn.close()


def test_fresh_db_has_no_live_fts_and_searches_latin_and_cjk(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    try:
        db.create_session(session_id="s1", source="cli", model="m")
        db.append_message("s1", role="user", content="graphiti 일본 MCP 우선순위")
        assert db.search_messages("graphiti", limit=10)
        assert db.search_messages("일본", limit=10)
        assert db._describe_search_path("graphiti") == "like_scan"
        assert db._describe_search_path("일본 MCP") == "like_scan"
        assert db.fts_rebuild_status() is None
        assert db.optimize_fts_storage()["reason"] == "live_fts_retired"
    finally:
        db.close()

    assert _fts_objects(path) == []
    conn = sqlite3.connect(str(path))
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM state_meta WHERE key LIKE 'fts_%'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_open_retires_stale_fts_schema_and_metadata(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(db_path=path)
    db.close()

    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE messages_fts(content TEXT)")
        conn.execute("CREATE TABLE messages_fts_trigram(content TEXT)")
        conn.execute(
            "INSERT INTO state_meta(key, value) VALUES ('fts_rebuild_progress', '17')"
        )
        conn.commit()
    finally:
        conn.close()

    reopened = SessionDB(db_path=path)
    reopened.close()

    assert _fts_objects(path) == []
    conn = sqlite3.connect(str(path))
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM state_meta WHERE key LIKE 'fts_%'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_compatibility_flag_cannot_reactivate_live_fts(monkeypatch):
    for value in ("0", "1", "false", "true"):
        monkeypatch.setenv("HERMES_CJK_FTS", value)
        assert _cjk_fts_config_enabled() is False


def test_operator_repair_guidance_retires_fts_instead_of_rebuilding_it():
    """Recovery guidance must not resurrect the retired derived index."""
    doctor_source = (
        Path(__file__).resolve().parents[1] / "hermes_cli" / "doctor.py"
    ).read_text(encoding="utf-8")

    messages = [
        node.value
        for node in ast.walk(ast.parse(doctor_source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]

    assert "rebuild the FTS index" not in doctor_source
    assert any(
        "retire corrupted FTS artifacts" in message for message in messages
    )
