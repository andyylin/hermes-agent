"""Bounded process-group teardown for stdio MCP children.

Gateway stop used to stack ``server.shutdown`` (10s), the loop drain, and a
fixed orphan sleep before anything reached a child that ignored SIGTERM
(``npx`` → ``node``, n8n). The child then lived until systemd's stop timeout.
Shutdown now closes the session, SIGTERMs every stdio process group, waits one
shared grace, and SIGKILLs whatever is left.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from typing import Optional

logger = logging.getLogger("tools.mcp_tool")

DEFAULT_STDIO_SHUTDOWN_GRACE_S = 4.0
_GRACE_MAX_S = 30.0
_POLL_S = 0.05
# After SIGKILL, a leader that is still exiting must not be put back on the
# orphan ledger — the following sweep would pay the grace again.
_SIGKILL_SETTLE_S = 0.25
# How long shutdown waits for the run task to leave its session (stdin close)
# before cancelling it and signalling the process group.
STDIO_CLOSE_HEAD_S = 0.35
SESSION_CLOSE_TIMEOUT_S = 2.0

def stdio_shutdown_grace_seconds() -> float:
    """``mcp.stdio_shutdown_grace_seconds`` (seconds after SIGTERM before SIGKILL).

    Unreadable config, or a non-numeric value, falls back to the default.
    ``0`` means SIGKILL immediately after SIGTERM. Clamped to ``[0, 30]``.
    """
    try:
        from hermes_cli.config import load_config
        raw = (load_config().get("mcp") or {}).get(
            "stdio_shutdown_grace_seconds", DEFAULT_STDIO_SHUTDOWN_GRACE_S)
    except Exception:
        return DEFAULT_STDIO_SHUTDOWN_GRACE_S
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        logger.warning(
            "mcp.stdio_shutdown_grace_seconds=%r is not a number; using %.1f",
            raw, DEFAULT_STDIO_SHUTDOWN_GRACE_S)
        return DEFAULT_STDIO_SHUTDOWN_GRACE_S
    return min(_GRACE_MAX_S, max(0.0, float(raw)))


def gateway_mcp_shutdown_budget(timeout: Optional[float], n_profiles: int) -> tuple[float, float, float]:
    """``(join_timeout, profile_pass_timeout, wildcard_pass_timeout)``.

    An explicit ``timeout`` is a total budget split evenly across the profile
    passes and the trailing wildcard. When it is omitted, the join covers one
    shared grace on the wildcard pass (the only pass that signals stdio
    groups) so SIGKILL lands before the gateway thread is abandoned.
    """
    passes = max(1, n_profiles + 1)
    if timeout is not None:
        share = max(1.0, float(timeout) / passes)
        return float(timeout), share, share
    grace = stdio_shutdown_grace_seconds()
    profile = SESSION_CLOSE_TIMEOUT_S
    wildcard = grace + 2.0
    join = max(5.0, profile * n_profiles + wildcard)
    return join, profile, wildcard


def adopt_stdio_pgroup(pid: int) -> Optional[int]:
    """Return the child's process group, moving it off the gateway's group if needed.

    The MCP SDK spawns with ``start_new_session`` (the child is already its own
    leader, so grandchildren inherit that group). ``setpgid`` can move a child
    that has not exec'd yet; POSIX refuses it after exec (EACCES). A child that
    is still in the gateway's group stays there — ``killpg`` would signal the
    gateway — and shutdown signals its descendants individually instead.
    ``None`` on platforms without process groups.
    """
    getpgid = getattr(os, "getpgid", None)
    if getpgid is None:
        return None
    try:
        pgid = getpgid(pid)
    except ProcessLookupError:
        # Already gone. The SDK's setsid child was its own leader (pgid == pid).
        return pid
    except OSError:
        return None
    try:
        mine = os.getpgrp()
    except (AttributeError, OSError):
        return pgid
    if pgid != mine:
        return pgid
    setpgid = getattr(os, "setpgid", None)
    if setpgid is None:
        return pgid
    try:
        setpgid(pid, pid)
    except OSError as exc:
        logger.warning(
            "MCP stdio pid %d shares the gateway process group and setpgid failed (%s); "
            "shutdown will signal that pid and its descendants, not the group",
            pid, exc)
        return pgid
    return pid


def _my_pgid() -> Optional[int]:
    try:
        return os.getpgrp()
    except (AttributeError, OSError):
        return None


def _reap_zombie(pid: int) -> None:
    """Collect a direct-child zombie so a later liveness probe does not see it."""
    waitpid = getattr(os, "waitpid", None)
    if waitpid is None:
        return
    try:
        waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _leader_alive(pid: int) -> bool:
    _reap_zombie(pid)
    from gateway.status import _pid_exists
    return _pid_exists(pid)


def _group_still_running(posix: bool, pgid: Optional[int], my_pgid: Optional[int]) -> bool:
    if not posix:
        return False
    from tools.mcp_tool_lifecycle import _group_alive
    return _group_alive(pgid, my_pgid)


def _descendant_pids(pid: int) -> list[int]:
    """Children of ``pid``, snapshotted while it is still alive.

    A leader that exits on SIGTERM reparents ``npx``'s ``node`` grandchild onto
    init. The pid list captured here is what the SIGKILL pass still signals
    when the leader's process group is the gateway's and ``killpg`` is unsafe.
    """
    try:
        import psutil
    except ImportError:
        return []
    try:
        return [child.pid for child in psutil.Process(pid).children(recursive=True)]
    except (psutil.Error, OSError):
        return []


def _shared_group_descendants(pids, pgids, my_pgid: Optional[int]) -> list[int]:
    if my_pgid is None:
        return []
    descendants = []
    for pid in pids:
        if pgids.get(pid) == my_pgid:
            descendants.extend(_descendant_pids(pid))
    return descendants


def _signal_pid_list(pids, sig: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _signal_members(pids, pgids, starts, sig: int, *, posix: bool, my_pgid: Optional[int]) -> None:
    """SIGTERM/SIGKILL every taken group. POSIX uses the spawn-time pgroup; other platforms per-pid."""
    if posix:
        from tools.mcp_tool_lifecycle import _signal_mcp_process
        for pid, owner in pids.items():
            _signal_mcp_process(pid, sig, owner, pgids.get(pid), my_pgid, starts.get(pid))
        return
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if os.name == "nt":
        from tools.mcp_tool_lifecycle import _kill_windows_process_tree
        for pid in pids:
            _kill_windows_process_tree(pid, sig)


def _needs_signal(pids, pgids, *, posix: bool, my_pgid: Optional[int]) -> bool:
    for pid in pids:
        if _leader_alive(pid) or _group_still_running(posix, pgids.get(pid), my_pgid):
            return True
    return False


def _wait_until_quiet(grace: float, still_busy) -> None:
    """Poll until ``still_busy`` is false or ``grace`` elapses. One wait, not one per pid."""
    if grace <= 0 or not still_busy():
        return
    deadline = time.monotonic() + grace
    while still_busy():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(_POLL_S, remaining))


def _restore_survivors(survivors) -> None:
    """Put leaders that ignored even SIGKILL back on the orphan ledger and leave them supervised."""
    from tools.mcp_tool_common import _core
    from tools.mcp_tool_lifecycle import (
        _orphan_stdio_pid_servers, _orphan_stdio_pids, _stdio_pgids, _stdio_starttimes,
    )
    with _core._lock:
        for pid, owner, pgid, start in survivors:
            _orphan_stdio_pids.add(pid)
            _orphan_stdio_pid_servers[pid] = owner
            if pgid is not None:
                _stdio_pgids[pid] = pgid
            if start is not None:
                _stdio_starttimes[pid] = start


def _release_dead_groups(released) -> None:
    if not released:
        return
    from tools.mcp_tool_common import _core
    _core._update_death_supervisor("unregister", released)


def escalate_taken_groups(pids, pgids, starts, *, grace: float, posix: Optional[bool] = None) -> None:
    """SIGTERM every taken group, wait one grace, SIGKILL whoever is still running.

    ``posix`` defaults to this host. Tests pass ``posix=False`` to exercise the
    per-pid path without pretending the machine is Windows. A leader that is
    still alive after SIGKILL goes back on the orphan ledger and stays on the
    death supervisor; a leader that exited is unregistered (a zombie grandchild
    from the SIGKILL is not a reason to pay the grace a second time).
    """
    if not pids:
        return
    if posix is None:
        posix = os.name == "posix"
    my_pgid = _my_pgid() if posix else None
    sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
    # Leaders that share our group: killpg would include us. Snapshot their
    # descendants now so a leader that exits on SIGTERM cannot hide them.
    shared = _shared_group_descendants(pids, pgids, my_pgid) if posix else []

    def _busy() -> bool:
        if _needs_signal(pids, pgids, posix=posix, my_pgid=my_pgid):
            return True
        return any(_leader_alive(pid) for pid in shared)

    def _deliver(sig: int) -> None:
        _signal_members(pids, pgids, starts, sig, posix=posix, my_pgid=my_pgid)
        _signal_pid_list(shared, sig)

    _deliver(signal.SIGTERM)
    _wait_until_quiet(grace, _busy)
    if _busy():
        _deliver(sigkill)
        for pid, owner in pids.items():
            if _leader_alive(pid):
                logger.warning(
                    "Force-killed MCP process %d (%s) after %.1fs SIGTERM grace", pid, owner, grace)
        _wait_until_quiet(
            _SIGKILL_SETTLE_S,
            lambda: any(_leader_alive(pid) for pid in pids) or any(_leader_alive(pid) for pid in shared))

    survivors = []
    released = []
    for pid, owner in pids.items():
        pgid = pgids.get(pid)
        if _leader_alive(pid):
            survivors.append((pid, owner, pgid, starts.get(pid)))
        elif pgid is not None:
            released.append(pgid)
    _restore_survivors(survivors)
    _release_dead_groups(released)


def reap_stdio_servers(
    names: Optional[set] = None,
    grace: Optional[float] = None,
    *,
    include_active: bool = True,
    server_name: Optional[str] = None,
    posix: Optional[bool] = None,
) -> None:
    """Reap stdio groups owned by ``names`` (or every tracked group when ``names`` is None).

    Empty ledger returns immediately — callers must not pay the grace when there
    is nothing to kill. All taken groups are signalled together, then one wait.
    """
    from tools.mcp_tool_lifecycle import _take_reapable_pids
    if grace is None:
        grace = stdio_shutdown_grace_seconds()
    else:
        grace = min(_GRACE_MAX_S, max(0.0, float(grace)))
    pids, pgids, starts = _take_reapable_pids(
        include_active, server_name, None if server_name is not None else names)
    escalate_taken_groups(pids, pgids, starts, grace=grace, posix=posix)
