"""Profile-local durable audit ledger for cron execution attempts.

The ledger records what is known about each attempt; it is not a retry queue. Interrupted attempts
become ``unknown`` only after their owner process is proved gone — a start-time reading that fails
to match the claim-time fingerprint is not proof of death. Terminal states are immutable.

The gateway and every restart-safe worker open this one file at the same minute. Connections ask
for WAL and a busy timeout. Writes are ``BEGIN IMMEDIATE`` and retry ``SQLITE_BUSY``; process
liveness and config reads stay outside the transaction, because a rollback journal holds its write
lock until commit and a deferred read-then-write upgrade returns "database is locked" immediately.
"""

from __future__ import annotations

import logging
import math
import os
import random
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now
from cron.constants import CLAIM_TTL_INACTIVITY_HEADROOM
from hermes_cli.observability.shared_metrics_gateway import record_cron_finish

logger = logging.getLogger(__name__)

# Optional test override. Production resolves the path at transaction time so dashboard operations
# that temporarily enter another profile cannot leak that profile's records into the import-time
# home.
EXECUTIONS_FILE: Optional[Path] = None
MAX_TERMINAL_EXECUTIONS = 1000
HANDOFF_ADOPTION_GRACE_SECONDS = 30.0
# Floor for the live-owner stale-claim bound (#115692); see _live_owner_stale_after_seconds.
LIVE_OWNER_STALE_CLAIM_FLOOR_SECONDS = 7200.0
# Adopt must publish its ownership ack inside the handoff grace. Five 4s busy waits plus the
# jitter below stay under that 30s, so a stampede retries instead of exiting before the ack.
_BUSY_TIMEOUT_MS = 4000
_BUSY_ATTEMPTS = 5
_BUSY_RETRY_MIN_S = 0.020
_BUSY_RETRY_MAX_S = 0.150
_TERMINAL_STATES = ("completed", "failed", "unknown")
_lock = threading.RLock()
_PROCESS_ID = uuid.uuid4().hex


# --- executions ledger --------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    # Late imports: a scheduler daemon that outlives an on-disk upgrade already has the OLD
    # ``hermes_cli.sqlite_util`` / ``cron.jobs`` cached, so new names must be resolved at call time,
    # not at import time (the guarantee cron/ledger.py used to carry, see e24c8499).
    from cron.jobs import _ensure_cron_dir
    from hermes_cli.sqlite_util import open_db

    path = EXECUTIONS_FILE or (get_hermes_home().resolve() / "cron" / "executions.db")
    _ensure_cron_dir(path.parent)
    # wal=True still passes through the WAL-reset gate: a vulnerable library keeps DELETE rather
    # than corrupt the file. Journal-mode changes ignore busy_timeout and ``_set_journal_mode_no_wait``
    # zeros it, so the pragma is retried and the timeout is put back on the connection we return.
    conn = open_db(
        path,
        db_label="cron/executions.db",
        busy_timeout_ms=_BUSY_TIMEOUT_MS,
        wal=True,
        synchronous_full=True,
        wal_lock_retries=_BUSY_ATTEMPTS,
        initialize=_initialize_schema,
    )
    try:
        conn.execute(f"PRAGMA busy_timeout={int(_BUSY_TIMEOUT_MS)}")
    except BaseException:
        conn.close()
        raise
    return conn


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_cli.sqlite_util import add_column_if_missing

    conn.execute(
        """CREATE TABLE IF NOT EXISTS executions (
             id TEXT PRIMARY KEY,
             job_id TEXT NOT NULL,
             source TEXT NOT NULL,
             process_id TEXT NOT NULL,
             pid INTEGER NOT NULL,
             process_started_at INTEGER,
             status TEXT NOT NULL CHECK(status IN
               ('claimed','running','completed','failed','unknown')),
             handoff_pending INTEGER NOT NULL DEFAULT 0,
             handoff_started_at REAL,
             claimed_at TEXT NOT NULL,
             started_at TEXT,
             finished_at TEXT,
             error TEXT
           )"""
    )
    add_column_if_missing(
        conn, "executions", "handoff_pending",
        "handoff_pending INTEGER NOT NULL DEFAULT 0",
    )
    add_column_if_missing(
        conn, "executions", "handoff_started_at", "handoff_started_at REAL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_job_claimed "
        "ON executions(job_id, claimed_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_status_claimed "
        "ON executions(status, claimed_at DESC, id DESC)"
    )
    add_column_if_missing(conn, "executions", "delivery_outcome", "delivery_outcome TEXT")
    add_column_if_missing(conn, "executions", "scheduled_instant", "scheduled_instant TEXT")
    add_column_if_missing(conn, "executions", "progress_at", "progress_at TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_occurrence "
        "ON executions(job_id, scheduled_instant) WHERE status='completed'"
    )


def _is_lock_error(exc: BaseException) -> bool:
    from hermes_state_errors import is_sqlite_lock_error

    return isinstance(exc, sqlite3.OperationalError) and is_sqlite_lock_error(exc)


def _sleep_busy(attempt: int) -> None:
    """Jittered pause so stampeding workers do not retry in lockstep.

    SQLite's own busy handler backs off deterministically; several adopters that time out
    together otherwise collide again on the next attempt. ``attempt`` is 0-based.
    """
    ceiling = min(_BUSY_RETRY_MAX_S, _BUSY_RETRY_MIN_S * (2 ** attempt))
    time.sleep(random.uniform(_BUSY_RETRY_MIN_S, max(_BUSY_RETRY_MIN_S, ceiling)))


@contextmanager
def _transaction(*, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    from hermes_cli.sqlite_util import transaction

    with _lock, transaction(_connect(), immediate=immediate) as conn:
        yield conn


def _write(fn):
    """Run ``fn(conn)`` in a short ``BEGIN IMMEDIATE`` transaction, retrying lock errors.

    The callback is the whole transaction: a busy failure rolls it back, and the callback runs
    again. Callers must not have committed a side effect before ``fn`` returns.
    """
    for attempt in range(_BUSY_ATTEMPTS):
        try:
            with _transaction(immediate=True) as conn:
                return fn(conn)
        except sqlite3.OperationalError as exc:
            if not _is_lock_error(exc) or attempt + 1 == _BUSY_ATTEMPTS:
                raise
            _sleep_busy(attempt)
    raise RuntimeError("executions.db write retries exhausted")  # pragma: no cover


def _fetch(conn: sqlite3.Connection, execution_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
    return dict(row) if row is not None else None


def _emit_execution_state(
    record: Optional[dict[str, Any]], *, delivery_outcome: Optional[str] = None
) -> None:
    """Project durable state to monitoring without affecting ledger behavior."""
    try:
        from agent.monitoring.cron_health import emit_execution_state

        emit_execution_state(record, delivery_outcome=delivery_outcome)
    except Exception:
        pass


def _process_start_time(pid: int) -> Optional[int]:
    try:
        from gateway.status import get_process_start_time
        return get_process_start_time(pid)
    except Exception:
        return None


def _owner_is_live(pid: int, started_at: Optional[int]) -> bool:
    try:
        from gateway.status import _pid_exists
        if not _pid_exists(pid):
            return False
    except Exception:
        return True  # fail safe: inability to prove death must not rewrite state
    if started_at is None:
        return pid == os.getpid()
    current = _process_start_time(pid)
    if current is None:
        return True  # cannot compare -> cannot prove death; a misread must not rewrite state
    # Drifted same-host readings (#117505) are not proof of death; a live misread is still
    # bounded by the stale-claim sweep below.
    from gateway.status import start_time_fingerprints_match
    return start_time_fingerprints_match(started_at, current)


def _live_owner_stale_after_seconds() -> Optional[float]:
    """Age past which a claimed/running row with a LIVE owner is treated as wedged.

    Derived from the existing knobs, never a bare wall-clock constant:
    ``max(3 × HERMES_CRON_TIMEOUT, cron script timeout, 7200)``. Returns ``None`` (never reclaim
    live owners — today's behaviour) when the inactivity timeout is 0/unlimited or not a finite
    positive number: with no bound to derive from, fail closed.
    """
    from cron.scheduler import _cron_inactivity_seconds
    from cron.scheduler_script import _get_script_timeout

    inactivity = float(_cron_inactivity_seconds())
    if not math.isfinite(inactivity) or inactivity <= 0:
        return None
    return max(
        inactivity * CLAIM_TTL_INACTIVITY_HEADROOM,
        float(_get_script_timeout()),
        LIVE_OWNER_STALE_CLAIM_FLOOR_SECONDS,
    )


def _claim_age_seconds(claimed_at: str) -> float:
    """Seconds since ``claimed_at`` (NOT NULL, always the aware ISO string from hermes_time.now)."""
    return (_hermes_now() - datetime.fromisoformat(claimed_at)).total_seconds()


def _stale_age_seconds(claimed_at: str, progress_at: Optional[str]) -> float:
    """Seconds since the owner last proved it was making progress.

    A wedged worker (#115692) stops stamping ``progress_at``; a long but healthy run keeps
    stamping it, so the derived stale bound measures silence, not run length. Rows written
    before the column existed have no stamp and fall back to claim age.
    """
    return _claim_age_seconds(progress_at or claimed_at)


def touch_execution_progress(execution_id: str) -> bool:
    """Stamp ``progress_at`` on a running attempt this process owns. Returns False when the row
    is no longer ours or no longer running (the caller treats that as informational only)."""
    now = _hermes_now().isoformat()

    def _op(conn: sqlite3.Connection) -> bool:
        cur = conn.execute(
            """UPDATE executions SET progress_at=?
               WHERE id=? AND status IN ('claimed','running') AND process_id=? AND pid=?""",
            (now, execution_id, _PROCESS_ID, os.getpid()),
        )
        return cur.rowcount == 1

    return _write(_op)


def _prune_unlocked(conn: sqlite3.Connection) -> None:
    conn.execute(
        """DELETE FROM executions WHERE id IN (
             SELECT id FROM executions
             WHERE status IN ('completed','failed','unknown')
             ORDER BY julianday(finished_at) DESC, finished_at DESC,
                      julianday(claimed_at) DESC, claimed_at DESC, id DESC LIMIT -1 OFFSET ?
           )""",
        (max(0, int(MAX_TERMINAL_EXECUTIONS)),),
    )


def create_execution(
    job_id: str, *, source: str, scheduled_instant: Optional[str] = None,
) -> dict[str, Any]:
    """Persist a claimed attempt before executor/provider dispatch."""
    from cron.occurrences import scheduled_instant as canonical_instant

    now = _hermes_now().isoformat()
    execution_id = uuid.uuid4().hex
    pid = os.getpid()
    started_at = _process_start_time(pid)
    instant = canonical_instant(scheduled_instant)

    def _op(conn: sqlite3.Connection) -> dict[str, Any]:
        conn.execute(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, process_started_at,
                status, claimed_at, scheduled_instant)
               VALUES (?, ?, ?, ?, ?, ?, 'claimed', ?, ?)""",
            (execution_id, str(job_id), str(source), _PROCESS_ID, pid,
             started_at, now, instant),
        )
        return _fetch(conn, execution_id)  # type: ignore[return-value]

    record = _write(_op)
    _emit_execution_state(record)
    return record


def set_execution_occurrence(execution_id: str, instant: Optional[str]) -> None:
    """Bind the store-claimed snapshot before a provider hands it to a worker."""
    from cron.occurrences import scheduled_instant

    bound = scheduled_instant(instant)
    pid = os.getpid()

    def _op(conn: sqlite3.Connection) -> None:
        cur = conn.execute(
            "UPDATE executions SET scheduled_instant=? WHERE id=? AND status='claimed' "
            "AND handoff_pending=0 AND process_id=? AND pid=?",
            (bound, execution_id, _PROCESS_ID, pid),
        )
        if cur.rowcount != 1:
            raise RuntimeError("Cron occurrence could not be bound before dispatch")

    _write(_op)


def mark_execution_handoff_pending(execution_id: str) -> Optional[dict[str, Any]]:
    """Fence restart recovery while an external worker is adopting a claim."""
    started = time.time()
    pid = os.getpid()

    def _op(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
        cur = conn.execute(
            """UPDATE executions
               SET handoff_pending=1, handoff_started_at=?
               WHERE id=? AND status='claimed'
                 AND process_id=? AND pid=?""",
            (started, execution_id, _PROCESS_ID, pid),
        )
        if cur.rowcount != 1:
            return None
        return _fetch(conn, execution_id)

    record = _write(_op)
    if record is not None:
        _emit_execution_state(record)
    return record


def adopt_claimed_execution(execution_id: str) -> Optional[dict[str, Any]]:
    """Atomically transfer and start an attempt in its worker process.

    The dispatching gateway creates the row before spawning a restart-safe
    worker.  Adoption is the single ``claimed`` → ``running`` gate: only the
    winner may acknowledge ownership or run side effects.
    """
    pid = os.getpid()
    process_started_at = _process_start_time(pid)
    now = _hermes_now().isoformat()

    def _op(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
        cur = conn.execute(
            """UPDATE executions
               SET process_id=?, pid=?, process_started_at=?,
                   status='running', started_at=?, handoff_pending=0,
                   handoff_started_at=NULL
               WHERE id=? AND status='claimed' AND handoff_pending=1""",
            (_PROCESS_ID, pid, process_started_at, now, execution_id),
        )
        if cur.rowcount != 1:
            return None
        return _fetch(conn, execution_id)

    # Lock errors used to kill the worker here, before it could write the ownership ack.
    record = _write(_op)
    if record is not None:
        _emit_execution_state(record)
    return record


def mark_execution_running(execution_id: str) -> Optional[dict[str, Any]]:
    """Transition one claimed attempt to running exactly once."""
    now = _hermes_now().isoformat()
    pid = os.getpid()

    def _op(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
        cur = conn.execute(
            """UPDATE executions
               SET status='running', started_at=?, handoff_pending=0,
                   handoff_started_at=NULL
               WHERE id=? AND status='claimed' AND handoff_pending=0
                 AND process_id=? AND pid=?""",
            (now, execution_id, _PROCESS_ID, pid),
        )
        if cur.rowcount != 1:
            return None
        return _fetch(conn, execution_id)

    record = _write(_op)
    if record is not None:
        _emit_execution_state(record)
    return record


def finish_execution(
    execution_id: str, *, success: bool, error: Optional[str] = None,
    delivery_outcome: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Write a terminal result once; terminal attempts cannot be rewritten."""
    now = _hermes_now().isoformat()
    status = "completed" if success else "failed"
    detail = None if success else (str(error) if error else "unknown failure")
    pid = os.getpid()

    def _op(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
        cur = conn.execute(
            """UPDATE executions
               SET status=?, finished_at=?, error=?, handoff_pending=0,
                   handoff_started_at=NULL, delivery_outcome=?
               WHERE id=? AND status IN ('claimed','running')
                 AND process_id=? AND pid=?""",
            (status, now, detail, delivery_outcome, execution_id, _PROCESS_ID, pid),
        )
        if cur.rowcount != 1:
            return None
        _prune_unlocked(conn)
        return _fetch(conn, execution_id)

    record = _write(_op)
    if record is None:
        return None
    _emit_execution_state(record, delivery_outcome=delivery_outcome)
    record_cron_finish(record, delivery_outcome)
    return record


_OWNER_GONE_REASON = (
    "Scheduler restarted after this execution's owner exited before a durable "
    "terminal state; whether side effects ran is unknown."
)
_OWNER_WEDGED_REASON = (
    "Owner process is still alive but the claim outlived the derived stale bound; "
    "treated as wedged (#115692). The process was not terminated; whether side effects "
    "ran is unknown."
)


def settle_unstarted_execution(execution_id: str, job_id: str, error: str) -> None:
    """Close the receipt of a run that never started: a ``claimed`` row never resolves. Best-effort
    so a ledger write cannot mask the failure the caller logs or re-raises."""
    try:
        finish_execution(execution_id, success=False, error=error)
    except (sqlite3.Error, OSError) as record_err:
        logger.error("Job '%s': failed to close execution receipt %s (%s): %s",
                     job_id, execution_id, error, record_err)


def _read_inflight_executions() -> list[dict[str, Any]]:
    """Snapshot claimed/running rows and close the connection before any liveness work."""
    with _transaction() as conn:
        rows = conn.execute(
            """SELECT id, status, process_id, pid, process_started_at,
                      handoff_pending, handoff_started_at, claimed_at, progress_at
               FROM executions
               WHERE status IN ('claimed','running')"""
        ).fetchall()
        return [dict(row) for row in rows]


def _rows_to_abandon(rows: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
    """Decide which inflight rows are abandoned. No executions.db connection is held.

    A live owner is normally a legitimately running job. A worker permanently deadlocked
    (e.g. futex_wait behind a route/proxy flip, #115692) also passes the liveness check, so a
    claim SILENT for longer than the derived bound is treated as wedged and released — the
    external-worker wait loop polls this ledger for a terminal status, so the job can fire
    again. Silence is measured from the owner's last ``progress_at`` stamp, so a healthy
    multi-hour run is never reclaimed while it is still working. The wedged worker PROCESS is
    NOT terminated here (leaked until host restart); rows owned by this process are skipped
    and remain out of scope.

    The stale bound is derived on the first live-owned row only: it reads config, and the idle
    gateway tick must stay config-free (tests/cron/test_idle_tick_config_skip.py).
    """
    stale_after: Optional[float] = None
    stale_after_resolved = False
    planned: list[tuple[dict[str, Any], str]] = []
    for row in rows:
        if row["process_id"] == _PROCESS_ID:
            continue
        reason = _OWNER_GONE_REASON
        if _owner_is_live(int(row["pid"]), row["process_started_at"]):
            if not stale_after_resolved:
                stale_after = _live_owner_stale_after_seconds()
                stale_after_resolved = True
            if (
                stale_after is None
                or _stale_age_seconds(row["claimed_at"], row["progress_at"]) <= stale_after
            ):
                continue
            reason = _OWNER_WEDGED_REASON
        handoff_started_at = row["handoff_started_at"]
        if (
            row["handoff_pending"]
            and handoff_started_at is not None
            and time.time() - float(handoff_started_at) < HANDOFF_ADOPTION_GRACE_SECONDS
        ):
            continue
        planned.append((row, reason))
    return planned


def _abandon_execution(row: dict[str, Any], reason: str, now: str) -> Optional[dict[str, Any]]:
    """CAS one snapshotted row to ``unknown``. A concurrent adopt changes the owner and misses."""

    def _op(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
        cur = conn.execute(
            """UPDATE executions
               SET status='unknown', finished_at=?, error=?,
                   handoff_pending=0, handoff_started_at=NULL
               WHERE id=? AND status=? AND process_id=? AND pid=?
                 AND handoff_pending=?
                 AND handoff_started_at IS ?""",
            (now, reason, row["id"], row["status"], row["process_id"], row["pid"],
             row["handoff_pending"], row["handoff_started_at"]),
        )
        if cur.rowcount != 1:
            return None
        return _fetch(conn, row["id"])

    return _write(_op)


def recover_interrupted_executions() -> int:
    """Mark abandoned attempts unknown without scheduling retries: rows whose owner is provably
    dead, plus rows whose live owner holds a claim older than the derived stale bound (the
    process is not killed)."""
    now = _hermes_now().isoformat()
    recovered: list[dict[str, Any]] = []
    for row, reason in _rows_to_abandon(_read_inflight_executions()):
        record = _abandon_execution(row, reason, now)
        if record is not None:
            recovered.append(record)
    if recovered:
        _write(_prune_unlocked)
    for record in recovered:
        _emit_execution_state(record)
    return len(recovered)


def terminalize_dead_owner(execution_id: str, *, reason: str) -> bool:
    """Record one attempt as ``unknown`` with a cause this process actually observed.

    ``recover_interrupted_executions`` sweeps every attempt whose owner is provably
    dead, and knows nothing but that absence — so all it can write is
    ``_OWNER_GONE_REASON``, which asserts a scheduler restart. A waiter that held the
    worker's ``Popen`` knows more: the owner was that external worker, and it exited
    with a known status. Without this, a manual run whose worker dies is filed as
    "Scheduler restarted ..." (a restart that never happened) and, because the sweep
    leaves the row terminal, the waiter reports success and never records the run —
    the job's ``fire_claim`` then blocks the next manual fire for the whole lease
    (#128509).

    The attempt stays ``unknown``, not ``failed``: whether side effects ran is still
    unknown. Only the CAUSE becomes truthful. Returns False — leaving the caller to
    fall back to the generic sweep — when the row is absent, already terminal, owned by
    this process, inside the handoff adoption grace, or owned by a live process: a
    worker that is still running must never be terminalized out from under itself.
    """
    now = _hermes_now().isoformat()
    with _transaction() as conn:
        row = conn.execute(
            """SELECT id, status, process_id, pid, process_started_at,
                      handoff_pending, handoff_started_at
               FROM executions WHERE id=?""",
            (execution_id,),
        ).fetchone()
        snapshot = dict(row) if row is not None else None
    if _terminalize_refused(snapshot):
        return False

    def _op(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
        cur = conn.execute(
            """UPDATE executions
               SET status='unknown', finished_at=?, error=?,
                   handoff_pending=0, handoff_started_at=NULL
               WHERE id=? AND status=? AND process_id=? AND pid=?""",
            (now, reason, snapshot["id"], snapshot["status"], snapshot["process_id"],
             snapshot["pid"]),
        )
        if cur.rowcount != 1:
            return None
        record = _fetch(conn, execution_id)
        _prune_unlocked(conn)
        return record

    record = _write(_op)
    if record is None:
        return False
    _emit_execution_state(record)
    return True


def _terminalize_refused(row: Optional[dict[str, Any]]) -> bool:
    """True when ``row`` must not be marked unknown. Liveness runs with no ledger lock held."""
    if row is None or row["status"] not in ("claimed", "running"):
        return True
    if row["process_id"] == _PROCESS_ID:
        return True
    if _owner_is_live(int(row["pid"]), row["process_started_at"]):
        return True
    handoff_started_at = row["handoff_started_at"]
    return bool(
        row["handoff_pending"]
        and handoff_started_at is not None
        and time.time() - float(handoff_started_at) < HANDOFF_ADOPTION_GRACE_SECONDS
    )


def list_executions(
    *, job_id: Optional[str] = None, limit: int = 50, before_claimed_at: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Return indexed, newest-first execution history with cursor pagination."""
    clauses: list[str] = []
    params: list[Any] = []
    if job_id is not None:
        clauses.append("job_id=?")
        params.append(str(job_id))
    if before_claimed_at is not None:
        # Same (instant, text) key as the ORDER BY, so a page never skips or repeats a row.
        clauses.append("(julianday(claimed_at), claimed_at) < (julianday(?), ?)")
        params.extend([str(before_claimed_at)] * 2)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    # Stamps carry the local offset, which changes at DST and on a timezone change, so text order
    # is not time order. julianday() compares instants (ms); the text breaks same-ms ties.
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM executions" + where
            + " ORDER BY julianday(claimed_at) DESC, claimed_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def get_execution(execution_id: str) -> Optional[dict[str, Any]]:
    """Return one exact execution attempt, or ``None`` when it is absent."""
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM executions WHERE id=?",
            (str(execution_id),),
        ).fetchone()
    return dict(row) if row is not None else None


def latest_execution(job_id: str) -> Optional[dict[str, Any]]:
    rows = list_executions(job_id=job_id, limit=1)
    return rows[0] if rows else None


def live_inflight_execution(job_id: str) -> Optional[dict[str, Any]]:
    """The job's latest attempt while it is still claimed/running under a LIVE owner, else ``None``.

    This is scheduler OWNERSHIP, not recent activity: a run inside a long tool call writes no
    heartbeat yet stays owned, while a run whose process died (watchdog kill, crash) does not.
    Read-only — unlike ``recover_interrupted_executions`` it never rewrites a row.
    """
    record = latest_execution(job_id)
    if not record or record.get("status") not in ("claimed", "running"):
        return None
    if not _owner_is_live(int(record["pid"]), record.get("process_started_at")):
        return None
    return record


def latest_executions(job_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Load latest execution for many jobs in one query."""
    clean = [str(job_id) for job_id in dict.fromkeys(job_ids) if job_id]
    if not clean:
        return {}
    placeholders = ",".join("?" for _ in clean)
    # One windowed sort: a per-row correlated ORDER BY julianday() cannot use the index and
    # grows quadratically with history (~90 ms at 1000 rows).
    with _transaction() as conn:
        rows = conn.execute(
            f"""SELECT e.* FROM executions e WHERE e.id IN (
                  SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                             PARTITION BY job_id
                             ORDER BY julianday(claimed_at) DESC, claimed_at DESC, id DESC
                           ) AS rn
                    FROM executions WHERE job_id IN ({placeholders}))
                  WHERE rn=1)""",
            clean,
        ).fetchall()
    return {row["job_id"]: dict(row) for row in rows}
