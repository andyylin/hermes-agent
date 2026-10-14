"""Gateway shutdown kills stdio MCP process groups within one shared grace.

A child that ignores SIGTERM (npx → node, n8n) used to outlive session close
and hold ``systemctl restart`` until systemd's stop timeout. These tests spawn
that shape of tree and assert the reap finishes inside the grace plus margin.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from tests.waiting import wait_until

_IGNORING_TREE = """\
import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
gc_path, ready_path = sys.argv[1], sys.argv[2]
pid = os.fork()
if pid == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with open(gc_path, "w") as handle:
        handle.write(str(os.getpid()))
    while True:
        time.sleep(3600)
with open(ready_path, "w") as handle:
    handle.write(str(os.getpid()))
while True:
    time.sleep(3600)
"""

_STAMP_TREE = """\
import os, signal, sys, time
gc_path, ready_path, stamp_path = sys.argv[1], sys.argv[2], sys.argv[3]
pid = os.fork()
if pid == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with open(gc_path, "w") as handle:
        handle.write(str(os.getpid()))
    while True:
        time.sleep(3600)

def _stamp(signum, frame):
    with open(stamp_path, "a") as handle:
        handle.write(str(time.time()))
        handle.write(chr(10))

signal.signal(signal.SIGTERM, _stamp)
with open(ready_path, "w") as handle:
    handle.write(str(os.getpid()))
while True:
    time.sleep(3600)
"""

def _dead(pid: int) -> bool:
    import psutil
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    return proc.status() == psutil.STATUS_ZOMBIE


def _kill_group(pgid: int) -> None:
    """SIGKILL ``pgid`` unless it is this process's group.

    A failed ``setpgid`` leaves the child in our group. Killing that group
    kills the test runner.
    """
    if not hasattr(os, "killpg"):
        return
    try:
        if pgid == os.getpgrp():
            return
    except OSError:
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


def _kill_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _clear_stdio_ledger() -> None:
    from tools.mcp_tool import _lock
    from tools.mcp_tool_lifecycle import (
        _orphan_stdio_pid_servers, _orphan_stdio_pids, _stdio_pgids, _stdio_pids, _stdio_starttimes,
    )
    with _lock:
        _stdio_pids.clear()
        _orphan_stdio_pids.clear()
        _orphan_stdio_pid_servers.clear()
        _stdio_pgids.clear()
        _stdio_starttimes.clear()


def _register(pid: int, name: str, pgid: int) -> None:
    from tools.mcp_tool import _lock
    from tools.mcp_tool_lifecycle import _leader_start_time, _stdio_pgids, _stdio_pids, _stdio_starttimes
    start = _leader_start_time(pid)
    with _lock:
        _stdio_pids[pid] = name
        _stdio_pgids[pid] = pgid
        if start is not None:
            _stdio_starttimes[pid] = start


def _spawn(script: str, args: list[str], *, new_session: bool, stdin=None) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script, *args],
        start_new_session=new_session,
        stdin=subprocess.DEVNULL if stdin is None else stdin,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _await_file(path) -> None:
    wait_until(lambda: path.exists() and path.stat().st_size > 0, timeout=15, what=f"{path.name} written")


@pytest.fixture
def clean_ledger():
    _clear_stdio_ledger()
    yield
    _clear_stdio_ledger()


def test_grace_reads_mcp_stdio_shutdown_grace_seconds():
    """The runtime reader consumes ``mcp.stdio_shutdown_grace_seconds`` from config.yaml."""
    from hermes_constants import get_hermes_home
    from tools.mcp_tool_stdio_shutdown import (
        DEFAULT_STDIO_SHUTDOWN_GRACE_S, stdio_shutdown_grace_seconds,
    )

    home = get_hermes_home()
    assert stdio_shutdown_grace_seconds() == DEFAULT_STDIO_SHUTDOWN_GRACE_S
    (home / "config.yaml").write_text("mcp:\n  stdio_shutdown_grace_seconds: 1.25\n", encoding="utf-8")
    assert stdio_shutdown_grace_seconds() == 1.25
    (home / "config.yaml").write_text("mcp:\n  stdio_shutdown_grace_seconds: false\n", encoding="utf-8")
    assert stdio_shutdown_grace_seconds() == DEFAULT_STDIO_SHUTDOWN_GRACE_S


def test_explicit_shutdown_budget_is_split_evenly():
    from tools.mcp_tool_stdio_shutdown import gateway_mcp_shutdown_budget
    assert gateway_mcp_shutdown_budget(8.0, 3) == (8.0, 2.0, 2.0)


def test_default_shutdown_budget_covers_one_shared_grace():
    from tools.mcp_tool_stdio_shutdown import gateway_mcp_shutdown_budget, stdio_shutdown_grace_seconds
    grace = stdio_shutdown_grace_seconds()
    join, profile, wildcard = gateway_mcp_shutdown_budget(None, 3)
    assert profile == 2.0
    assert wildcard == grace + 2.0
    assert join >= 3 * profile + wildcard


def test_nonposix_signals_each_pid_and_not_the_group(monkeypatch):
    """``posix=False`` is data: per-pid kill, never killpg, on this host."""
    from tools.mcp_tool_stdio_shutdown import escalate_taken_groups

    killed: list[tuple[int, int]] = []
    alive = {424242: True}

    def _kill(pid, sig):
        killed.append((int(pid), int(sig)))
        if int(sig) == int(getattr(signal, "SIGKILL", signal.SIGTERM)):
            alive[int(pid)] = False

    monkeypatch.setattr(os, "kill", _kill)
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: alive.get(int(pid), False))
    def _no_killpg(*_args, **_kwargs):
        raise AssertionError("killpg")

    if hasattr(os, "killpg"):
        monkeypatch.setattr(os, "killpg", _no_killpg)

    escalate_taken_groups({424242: "n8n"}, {424242: 424242}, {}, grace=0.0, posix=False)

    sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
    assert killed[0] == (424242, signal.SIGTERM)
    assert (424242, sigkill) in killed


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass
def test_sigterm_ignoring_tree_dies_within_grace(tmp_path, clean_ledger):
    """Parent and grandchild both ignore SIGTERM; SIGKILL reaches the group inside the grace."""
    pytest.importorskip("psutil")
    from tools.mcp_tool_stdio_shutdown import reap_stdio_servers

    grace = 0.4
    gc_path = tmp_path / "gc.pid"
    ready = tmp_path / "ready"
    proc = _spawn(_IGNORING_TREE, [str(gc_path), str(ready)], new_session=True)
    pgid = os.getpgid(proc.pid)
    try:
        _await_file(ready)
        _await_file(gc_path)
        grandchild = int(gc_path.read_text().strip())
        assert os.getpgid(grandchild) == pgid
        _register(proc.pid, "n8n", pgid)
        started = time.monotonic()
        reap_stdio_servers({"n8n"}, grace=grace)
        elapsed = time.monotonic() - started
        wait_until(lambda: _dead(proc.pid) and _dead(grandchild), timeout=5, what="ignoring tree exited")
        assert elapsed < grace + 2.0
    finally:
        _kill_group(pgid)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass
def test_every_group_shares_one_grace(tmp_path, clean_ledger):
    """Two SIGTERM-ignoring groups are signalled together, then killed after one grace."""
    pytest.importorskip("psutil")
    from tools.mcp_tool_stdio_shutdown import reap_stdio_servers

    grace = 1.0
    trees = []
    try:
        for name in ("n8n", "other"):
            directory = tmp_path / name
            directory.mkdir()
            gc_path, ready, stamp = directory / "gc", directory / "ready", directory / "stamp"
            proc = _spawn(_STAMP_TREE, [str(gc_path), str(ready), str(stamp)], new_session=True)
            trees.append((name, proc, os.getpgid(proc.pid), gc_path, ready, stamp))
        for name, proc, pgid, gc_path, ready, _stamp in trees:
            _await_file(ready)
            _await_file(gc_path)
            _register(proc.pid, name, pgid)
        started = time.monotonic()
        reap_stdio_servers({"n8n", "other"}, grace=grace)
        elapsed = time.monotonic() - started
        stamps = []
        for name, proc, pgid, gc_path, _ready, stamp in trees:
            grandchild = int(gc_path.read_text().strip())
            wait_until(
                lambda proc=proc, grandchild=grandchild: _dead(proc.pid) and _dead(grandchild),
                timeout=5, what=f"{name} tree exited")
            _await_file(stamp)
            stamps.append(float(stamp.read_text().splitlines()[0]))
        assert max(stamps) - min(stamps) < grace / 2
        assert elapsed < grace + 2.0
    finally:
        for _name, proc, pgid, _gc, _ready, _stamp in trees:
            _kill_group(pgid)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass
def test_adopt_moves_an_unexeced_child_before_it_forks(tmp_path, clean_ledger):
    """``setpgid`` works only before exec. A forked child becomes its own leader;
    the grandchild it then forks inherits that group, and reap kills both."""
    pytest.importorskip("psutil")
    from tools.mcp_tool_stdio_shutdown import adopt_stdio_pgroup, reap_stdio_servers

    gc_path = tmp_path / "gc"
    go_read, go_write = os.pipe()
    park_read, park_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        # Never return into pytest. A forked interpreter that keeps running
        # the test body would reap the parent and itself.
        try:
            os.close(go_write)
            os.close(park_write)
            os.read(go_read, 1)
            grandchild = os.fork()
            if grandchild == 0:
                with open(gc_path, "w") as handle:
                    handle.write(f"{os.getpid()} {os.getpgid(0)}\n")
                os.read(park_read, 1)
            else:
                os.read(park_read, 1)
        finally:
            os._exit(0)

    os.close(go_read)
    os.close(park_read)
    adopted = None
    try:
        adopted = adopt_stdio_pgroup(pid)
        assert adopted is not None and adopted != os.getpgrp()
        assert os.getpgid(pid) == adopted
        os.write(go_write, b"x")
        _await_file(gc_path)
        grandchild_pid, grandchild_pgid = (int(part) for part in gc_path.read_text().split())
        assert grandchild_pgid == adopted
        _register(pid, "n8n", adopted)
        started = time.monotonic()
        reap_stdio_servers({"n8n"}, grace=0.4)
        elapsed = time.monotonic() - started
        wait_until(lambda: _dead(pid) and _dead(grandchild_pid), timeout=5, what="adopted tree exited")
        assert elapsed < 2.4
    finally:
        os.close(go_write)
        os.close(park_write)
        if adopted is not None:
            _kill_group(adopted)
        _kill_pid(pid)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass
def test_shared_group_after_exec_kills_the_tree_not_us(tmp_path, clean_ledger):
    """After exec, ``setpgid`` is refused. Reap still kills the grandchild, and not us."""
    pytest.importorskip("psutil")
    from tools.mcp_tool_stdio_shutdown import adopt_stdio_pgroup, reap_stdio_servers

    gc_path = tmp_path / "gc"
    ready = tmp_path / "ready"
    proc = _spawn(_IGNORING_TREE, [str(gc_path), str(ready)], new_session=False)
    grandchild = None
    try:
        _await_file(ready)
        _await_file(gc_path)
        grandchild = int(gc_path.read_text().strip())
        assert os.getpgid(proc.pid) == os.getpgrp()
        assert os.getpgid(grandchild) == os.getpgrp()
        adopted = adopt_stdio_pgroup(proc.pid)
        assert adopted == os.getpgrp()
        _register(proc.pid, "n8n", adopted)
        started = time.monotonic()
        reap_stdio_servers({"n8n"}, grace=0.4)
        elapsed = time.monotonic() - started
        wait_until(
            lambda: _dead(proc.pid) and _dead(grandchild), timeout=5, what="shared-group tree exited")
        assert elapsed < 2.4
    finally:
        _kill_pid(proc.pid)
        if grandchild is not None:
            _kill_pid(grandchild)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass
def test_shutdown_mcp_servers_reaps_groups_before_returning(tmp_path, clean_ledger, monkeypatch):
    """The public shutdown path reaps every registered stdio group inside one grace."""
    pytest.importorskip("psutil")
    import tools.mcp_tool as mcp_mod
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    from tools.mcp_tool_loop import _ensure_mcp_loop, _stop_mcp_loop

    monkeypatch.setattr(
        "tools.mcp_tool_stdio_shutdown.stdio_shutdown_grace_seconds", lambda: 0.4)

    class _Server:
        def __init__(self, name: str) -> None:
            self.name = name
            self._task = None
            self._shutdown_event = threading.Event()
            self._reconnect_event = threading.Event()
            self._registered_tool_names: list[str] = []

        async def shutdown(self):
            return None

    trees = []
    try:
        for name in ("n8n", "other"):
            directory = tmp_path / name
            directory.mkdir()
            gc_path, ready = directory / "gc", directory / "ready"
            proc = _spawn(_IGNORING_TREE, [str(gc_path), str(ready)], new_session=True)
            trees.append((name, proc, os.getpgid(proc.pid), gc_path))
        for name, proc, pgid, gc_path in trees:
            _await_file(gc_path)
            _register(proc.pid, name, pgid)
        with mcp_mod._lock:
            mcp_mod._servers.clear()
            for name, _proc, _pgid, _gc in trees:
                mcp_mod._servers[name] = _Server(name)
        _ensure_mcp_loop()
        started = time.monotonic()
        shutdown_mcp_servers()
        elapsed = time.monotonic() - started
        for name, proc, _pgid, gc_path in trees:
            grandchild = int(gc_path.read_text().strip())
            wait_until(
                lambda proc=proc, grandchild=grandchild: _dead(proc.pid) and _dead(grandchild),
                timeout=5, what=f"{name} exited during shutdown_mcp_servers")
        assert elapsed < 2.5
    finally:
        for _name, proc, pgid, _gc in trees:
            _kill_group(pgid)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        with mcp_mod._lock:
            mcp_mod._servers.clear()
        _stop_mcp_loop()
