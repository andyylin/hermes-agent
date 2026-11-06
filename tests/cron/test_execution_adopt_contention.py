"""Concurrent adopt on the cron execution ledger.

Several restart-safe workers adopt in the same minute. On a SQLite build with the WAL-reset
bug the ledger stays in DELETE journal mode, so one writer's transaction blocks the others and
``adopt_claimed_execution`` used to raise ``database is locked`` before the ownership ack.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

from hermes_cli.sqlite_runtime import is_sqlite_wal_reset_vulnerable


def _point_ledger(monkeypatch, tmp_path):
    from cron import executions

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    return executions


def test_connection_sets_busy_timeout_and_journal_mode(monkeypatch, tmp_path):
    """Every executions.db connection waits on locks and asks for WAL.

    A vulnerable library must stay on DELETE (the WAL-reset gate). A fixed library must
    actually be in WAL — that is the concurrency the adopters need.
    """
    executions = _point_ledger(monkeypatch, tmp_path)
    executions.create_execution("probe", source="builtin")

    conn = executions._connect()
    try:
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        conn.close()

    assert busy == executions._BUSY_TIMEOUT_MS
    if is_sqlite_wal_reset_vulnerable(sqlite3.sqlite_version_info):
        assert mode == "delete"
    else:
        assert mode == "wal"


def test_adopt_retries_while_another_writer_holds_the_ledger(monkeypatch, tmp_path):
    """A writer that outlives one busy timeout must not fail the adopt.

    The holder is a second connection, outside the ledger's process lock, the way a
    gateway recovery and a worker adopt meet. The first attempt has to observe
    SQLITE_BUSY; the retry then owns the row.
    """
    executions = _point_ledger(monkeypatch, tmp_path)
    monkeypatch.setattr(executions, "_BUSY_TIMEOUT_MS", 80)
    record = executions.create_execution("stampeded", source="builtin")
    assert executions.mark_execution_handoff_pending(record["id"]) is not None

    started = threading.Event()
    release = threading.Event()
    holder_errors: list[BaseException] = []
    retries: list[int] = []

    def _hold() -> None:
        conn = sqlite3.connect(executions.EXECUTIONS_FILE, timeout=0.05)
        try:
            conn.execute("BEGIN IMMEDIATE")
            started.set()
            release.wait()
            conn.rollback()
        except Exception as exc:
            holder_errors.append(exc)
            started.set()
            release.set()
        finally:
            conn.close()

    def _note_retry(attempt: int) -> None:
        retries.append(attempt)
        release.set()

    monkeypatch.setattr(executions, "_sleep_busy", _note_retry)
    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    assert started.wait(timeout=5), "holder did not take the write lock"
    try:
        adopted = executions.adopt_claimed_execution(record["id"])
    finally:
        release.set()
        holder.join(timeout=5)

    assert not holder_errors
    assert not holder.is_alive()
    assert retries, "adopt returned without seeing the other writer's lock"
    assert adopted is not None
    assert adopted["status"] == "running"
    assert executions.get_execution(record["id"])["status"] == "running"


def test_concurrent_workers_adopt_distinct_claims(monkeypatch, tmp_path):
    """Separate processes, one claim each — the restart-safe worker topology."""
    executions = _point_ledger(monkeypatch, tmp_path)
    ids = []
    for index in range(4):
        record = executions.create_execution(f"job-{index}", source="builtin")
        assert executions.mark_execution_handoff_pending(record["id"]) is not None
        ids.append(record["id"])

    repo = Path(__file__).resolve().parents[2]
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "import cron.executions as executions\n"
        "executions.EXECUTIONS_FILE = Path(sys.argv[1])\n"
        "adopted = executions.adopt_claimed_execution(sys.argv[2])\n"
        "if adopted is None or adopted['status'] != 'running':\n"
        "    raise SystemExit(2)\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo)
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(executions.EXECUTIONS_FILE), execution_id],
            cwd=str(repo),
            env=env,
        )
        for execution_id in ids
    ]
    codes = [proc.wait(timeout=30) for proc in procs]

    assert codes == [0, 0, 0, 0]
    for execution_id in ids:
        assert executions.get_execution(execution_id)["status"] == "running"
