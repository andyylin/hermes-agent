"""Behaviour of the rpi5 push-to-deploy listener.

Git is real: an origin repository and a live clone under ``tmp_path``. systemd,
``/health``, the Hermes-side probe and the clock are a fake host whose gateway
activity is a function of time and drain state.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "docs" / "ops"))

import rpi5_deploy as rd
import rpi5_deploy_engine as eng

KEY = b"k" * 64
REPOSITORY = "andyylin/hermes-agent"


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _commit(origin: Path, files: dict[str, str]) -> str:
    for rel, text in files.items():
        path = origin / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "change")
    return _git(origin, "rev-parse", "HEAD")


class FakeHost(eng.Host):
    """systemd, health, the probe and time. ``activity(host)`` is the gateway's active work count."""

    def __init__(self, config: eng.DeployConfig):
        super().__init__(config)
        self.clock = 0.0
        self.live = True
        self.draining = False
        self.drain_count = 0
        self.running_sha = self.git("rev-parse", "HEAD")
        self.activity: Callable[[FakeHost], int] = lambda host: 0
        self.stale: Callable[[FakeHost], bool] = lambda host: False
        self.bad_health: set[str] = set()
        self.events: list[tuple] = []
        self.last_active = 0
        self.interrupted = False
        self.stopped_at: Optional[float] = None
        self.on_sleep: Optional[Callable[[FakeHost], None]] = None

    def probe(self, python, checkout, command, *args, home=None):
        self.events.append(("probe", command, *args))
        if command == "venv-python":
            return {"ok": True, "python": "pm-python"}
        if command == "homes":
            return {"ok": True, "homes": [str(self.config.home)]}
        if command == "idle":
            self.last_active = self.activity(self) if self.live else 0
            state = "draining" if self.draining else "running"
            return {"ok": True, "homes": [{
                "home": str(self.config.home), "live": self.live, "state": state,
                "active_agents": self.last_active, "stale": self.stale(self), "code_sha": self.running_sha}]}
        if command == "drain":
            self.draining = args[0] == "on"
            self.drain_count += self.draining
        return {"ok": True}

    def systemctl(self, action, unit):
        self.events.append(("systemctl", action, unit.name))
        if action == "stop":
            self.interrupted |= self.last_active > 0
            self.stopped_at = self.clock if self.stopped_at is None else self.stopped_at
            self.live = False
        elif action == "start":
            self.live = True
            self.running_sha = self.git("rev-parse", "HEAD")
        return self.live if action == "is-active" else True

    def health(self, url):
        return self.running_sha not in self.bad_health

    def sleep(self, seconds):
        self.clock += seconds
        if self.on_sleep:
            self.on_sleep(self)

    def monotonic(self):
        return self.clock

    def actions(self) -> list[tuple]:
        return [e for e in self.events if e[0] == "systemctl" and e[1] != "is-active"]


@pytest.fixture
def site(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q")
    base = _commit(origin, {
        "gateway/app.py": "VALUE = 1\n", "docs/notes.md": "notes\n",
        "skills/demo/SKILL.md": "---\nname: demo\n---\n", "pyproject.toml": "[project]\nname='x'\n"})
    home = tmp_path / "home"
    home.mkdir()
    checkout = home / "hermes-agent"
    _git(tmp_path, "clone", "-q", str(origin), str(checkout))
    config = eng.DeployConfig(checkout=checkout, home=home, repository=REPOSITORY, idle_settle_seconds=20,
                              drain_confirm_seconds=30, health_timeout_seconds=30, idle_poll_seconds=5)
    host = FakeHost(config)
    store = eng.StateStore(config.deploy_dir)
    store.update(deployed_sha=base, runtime_sha=base)
    deployer = eng.Deployer(config, host, store)

    class Site:
        pass

    s = Site()
    s.origin, s.base, s.config, s.host, s.store, s.deployer = origin, base, config, host, store, deployer
    s.head = lambda: _git(checkout, "rev-parse", "HEAD")
    return s


def _deploy(site, sha: str) -> eng.Result:
    assert site.deployer.request(sha)[0] == 202
    return site.deployer.run_pending()


def test_restart_waits_for_every_gateway_to_go_idle_before_stopping(site):
    target = _commit(site.origin, {"gateway/app.py": "VALUE = 2\n"})
    site.host.activity = lambda host: 2 if host.clock < 60 else 0

    result = _deploy(site, target)

    assert result.outcome == "deployed"
    assert not site.host.interrupted
    stop_at = site.host.events.index(("systemctl", "stop", "hermes-gateway.service"))
    drain_on = site.host.events.index(("probe", "drain", "on", "--home", str(site.config.home)))
    assert drain_on < stop_at
    assert site.host.clock >= 60 + site.config.idle_settle_seconds
    assert ("probe", "drain", "off", "--home", str(site.config.home)) in site.host.events[stop_at:]
    assert site.head() == target
    state = site.store.load()
    assert (state.deployed_sha, state.runtime_sha, state.previous_sha) == (target, target, site.base)
    assert (site.config.releases_dir / target).is_dir()


def test_work_that_starts_during_the_drain_is_never_interrupted(site):
    target = _commit(site.origin, {"gateway/app.py": "VALUE = 2\n"})
    site.host.activity = lambda host: 1 if host.draining and host.drain_count == 1 else 0

    result = _deploy(site, target)

    assert result.outcome == "deployed"
    assert not site.host.interrupted
    assert site.host.drain_count == 2


def test_a_stale_heartbeat_counts_as_busy(site):
    target = _commit(site.origin, {"gateway/app.py": "VALUE = 2\n"})
    site.host.stale = lambda host: host.clock < 100

    assert _deploy(site, target).outcome == "deployed"

    assert site.host.stopped_at >= 100 + site.config.idle_settle_seconds


def test_failed_health_rolls_back_and_refuses_the_same_sha_again(site):
    target = _commit(site.origin, {"gateway/app.py": "VALUE = 2\n"})
    site.host.bad_health = {target}

    result = _deploy(site, target)

    assert result.outcome == "rolled_back"
    assert site.head() == site.base
    assert site.host.running_sha == site.base
    state = site.store.load()
    assert state.deployed_sha == site.base
    assert target in state.failed_shas
    assert site.deployer.request(target)[0] == 409
    starts = [a for a in site.host.actions() if a[1] == "start"]
    assert len(starts) == 2


def test_a_rollback_that_also_fails_health_is_a_drift_alert(site):
    target = _commit(site.origin, {"gateway/app.py": "VALUE = 2\n"})
    site.host.bad_health = {target, site.base}

    assert _deploy(site, target).outcome == "rollback_failed"
    assert rd.cmd_drift(site.config, fetch=False, host=site.host) == 1


def test_a_skills_only_change_is_applied_without_a_restart(site):
    target = _commit(site.origin, {"skills/demo/SKILL.md": "---\nname: demo\ndescription: not this\n---\n"})

    result = _deploy(site, target)

    assert result.outcome == "deployed" and result.plan_kind == "hot"
    assert site.host.actions() == []
    assert ("probe", "skills-sync") in site.host.events
    state = site.store.load()
    assert (state.deployed_sha, state.runtime_sha) == (target, site.base)
    assert rd.cmd_drift(site.config, fetch=False, host=site.host) == 0


def test_a_dependency_change_is_never_applied_unattended(site):
    target = _commit(site.origin, {"pyproject.toml": "[project]\nname='y'\n", "gateway/app.py": "VALUE = 3\n"})

    result = _deploy(site, target)

    assert result.outcome == "needs_manual"
    assert site.head() == site.base
    assert site.host.actions() == []


def test_a_newer_merge_supersedes_one_still_waiting_for_idle(site):
    first = _commit(site.origin, {"gateway/app.py": "VALUE = 2\n"})
    second = _commit(site.origin, {"gateway/app.py": "VALUE = 3\n"})
    site.host.activity = lambda host: 1 if host.clock < 50 else 0

    def push_second(host):
        if host.clock == 25:
            site.deployer.request(second)

    site.host.on_sleep = push_second
    assert site.deployer.request(first)[0] == 202
    assert site.deployer.run_pending().outcome == "superseded"
    assert site.head() == site.base
    assert site.deployer.run_pending().outcome == "deployed"
    assert site.head() == second
    assert site.deployer.request(first)[0] == 409


def test_drift_reports_modified_live_files(site):
    assert rd.cmd_drift(site.config, fetch=False, host=site.host) == 0
    (site.config.checkout / "gateway" / "app.py").write_text("VALUE = 99\n", encoding="utf-8")

    assert rd.cmd_drift(site.config, fetch=False, host=site.host) == 1
    report = json.loads((site.config.deploy_dir / "drift.json").read_text(encoding="utf-8"))
    assert any("gateway/app.py" in p for p in report["problems"])


@pytest.mark.parametrize(
    ("path", "sidecar"),
    [
        ("install-stamp.external-claim.json", True),
        ("install-stamp.json.external-backup", True),
        ("subdir/install-stamp.external-claim.json", True),
        ("install-stamp.json", False),
        ("gateway/app.py", False),
    ],
)
def test_install_stamp_external_sidecar_paths(path, sidecar):
    assert eng._install_stamp_external_sidecar(path) is sidecar


def test_drift_ignores_install_stamp_external_claim_sidecars(site):
    checkout = site.config.checkout
    (checkout / "install-stamp.external-claim.json").write_text("{}\n", encoding="utf-8")
    (checkout / "install-stamp.json.external-backup").write_text("{}\n", encoding="utf-8")

    assert rd.cmd_drift(site.config, fetch=False, host=site.host) == 0


def test_drift_reports_a_merge_that_never_deployed(site):
    _commit(site.origin, {"gateway/app.py": "VALUE = 2\n"})

    assert rd.cmd_drift(site.config, fetch=False, host=site.host) == 0
    assert rd.cmd_drift(site.config, fetch=True, host=site.host) == 1


@pytest.fixture
def server(site):
    srv = rd.make_server(site.deployer, KEY, port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield site, f"http://127.0.0.1:{srv.server_address[1]}/deploy"
    srv.shutdown()
    srv.server_close()


def _post(url: str, body: bytes, headers: dict) -> int:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _body(sha: str, repository: str = REPOSITORY) -> bytes:
    return json.dumps({"repository": repository, "ref": "refs/heads/main", "sha": sha}).encode()


@pytest.mark.parametrize("case", ["unsigned", "wrong-key", "expired", "tampered"])
def test_unauthenticated_requests_are_rejected_and_never_queued(server, case):
    site, url = server
    target = _commit(site.origin, {"gateway/app.py": "VALUE = 2\n"})
    body = _body(target)
    stamp = str(int(time.time()) - (rd.MAX_SKEW_SECONDS + 60 if case == "expired" else 0))
    signature = rd.sign(b"x" * 64 if case == "wrong-key" else KEY, stamp, body)
    headers = {} if case == "unsigned" else {rd.TIMESTAMP_HEADER: stamp, rd.SIGNATURE_HEADER: signature}
    if case == "tampered":
        body = _body(site.base)

    assert _post(url, body, headers) == 401
    assert site.store.load().pending_sha is None


def test_a_signed_request_for_another_repository_is_refused(server):
    site, url = server
    target = _commit(site.origin, {"gateway/app.py": "VALUE = 2\n"})
    body = _body(target, repository="someone/else")
    stamp = str(int(time.time()))

    assert _post(url, body, {rd.TIMESTAMP_HEADER: stamp, rd.SIGNATURE_HEADER: rd.sign(KEY, stamp, body)}) == 403
    assert site.store.load().pending_sha is None


def test_the_ci_notifier_is_accepted_by_the_listener(server, capsys):
    site, url = server
    target = _commit(site.origin, {"gateway/app.py": "VALUE = 2\n"})

    assert rd.notify(url, sha=target, repository=REPOSITORY, ref="refs/heads/main", key=KEY) == 0
    assert site.store.load().pending_sha == target
    assert "queued" in capsys.readouterr().out


def test_a_replayed_request_for_the_deployed_sha_changes_nothing(server):
    site, url = server
    body = _body(site.base)
    stamp = str(int(time.time()))

    assert _post(url, body, {rd.TIMESTAMP_HEADER: stamp, rd.SIGNATURE_HEADER: rd.sign(KEY, stamp, body)}) == 200
    assert site.store.load().pending_sha is None


@pytest.mark.parametrize(("paths", "kind"), [
    (["docs/ops/README.md", "website/docs/x.md", "README.md", "gateway/AGENTS.md"], "noop"),
    (["skills/a/SKILL.md", "docs/x.md"], "hot"),
    (["skills/a/SKILL.md", "agent/prompt_builder.py"], "restart"),
    (["SOUL.md"], "restart"),
    (["uv.lock", "gateway/run.py"], "manual"),
])
def test_changes_are_applied_the_least_disruptive_safe_way(paths, kind):
    assert eng.classify(paths).kind == kind


def test_the_unattended_gate_covers_every_file_pm_resyncs_on():
    from pm.environments import ACTIVATION_INPUTS

    assert set(eng.DEPENDENCY_INPUTS) == set(ACTIVATION_INPUTS)


def test_sudoers_refuses_a_unit_name_that_could_widen_the_grant(tmp_path):
    config = eng.DeployConfig(checkout=tmp_path, home=tmp_path, units=(eng.Unit("x.service, ALL"),))
    with pytest.raises(eng.DeployError):
        rd.sudoers_text(config, "pi")


def test_the_listener_refuses_a_key_others_can_read(tmp_path):
    config = eng.DeployConfig(checkout=tmp_path, home=tmp_path)
    config.deploy_dir.mkdir()
    key = config.deploy_dir / "hmac.key"
    key.write_bytes(KEY)
    key.chmod(0o644)
    with pytest.raises(eng.DeployError):
        rd.load_key(config)
    key.chmod(0o600)
    assert rd.load_key(config) == KEY


def _probe(tmp_path: Path, *args: str) -> dict:
    done = subprocess.run(
        [sys.executable, str(eng.PROBE), "--checkout", str(REPO_ROOT), *args],
        capture_output=True, text=True, env={**os.environ, "HERMES_HOME": str(tmp_path)}, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_probe_reads_gateway_work_through_hermes_own_status_contract(tmp_path):
    from gateway.status import get_process_start_time

    record = {"pid": os.getpid(), "start_time": get_process_start_time(os.getpid()), "kind": "hermes-gateway",
              "argv": ["hermes", "gateway", "run"], "gateway_state": "running", "active_agents": 2,
              "updated_at": datetime.now(UTC).isoformat()}
    (tmp_path / "gateway_state.json").write_text(json.dumps(record), encoding="utf-8")

    (home,) = _probe(tmp_path, "idle", "--home", str(tmp_path))["homes"]

    assert home["live"] is True
    assert home["active_agents"] == 2
    assert eng._busy(home, strict=True)


def test_the_probe_drain_marker_suppresses_the_shutdown_broadcast(tmp_path):
    from gateway.drain_control import drain_notification_suppressed, drain_requested

    _probe(tmp_path, "drain", "on", "--home", str(tmp_path))
    assert drain_requested(home=tmp_path)
    assert drain_notification_suppressed(home=tmp_path)

    _probe(tmp_path, "drain", "off", "--home", str(tmp_path))
    assert not drain_requested(home=tmp_path)


def test_the_probe_lists_every_profile_home_hermes_knows(tmp_path):
    (tmp_path / "config.yaml").write_text("{}\n", encoding="utf-8")
    wife = tmp_path / "profiles" / "wife"
    wife.mkdir(parents=True)
    (wife / "config.yaml").write_text("{}\n", encoding="utf-8")

    homes = _probe(tmp_path, "homes")["homes"]

    assert [Path(h).resolve() for h in homes] == [tmp_path.resolve(), wife.resolve()]
