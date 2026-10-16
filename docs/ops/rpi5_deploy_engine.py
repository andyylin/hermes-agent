"""Deploy one merged SHA onto the rpi5 checkout without interrupting a turn.

The live tree stays at its canonical path. ``pm.environments.install_key``
hashes the resolved checkout path, so a symlink swap between release
directories would orphan the committed PM environment on every deploy.
A release is staged as a detached ``git worktree`` under
``<home>/deploy/releases/<sha>`` and import-checked there. The swap is a
fast-forward of the live branch to that SHA. Rollback resets it to the
previous SHA, whose worktree is kept.

Order of a restart deploy:

1. Wait until every live gateway reports ``active_agents == 0`` while serving,
   continuously for ``idle_settle_seconds``. Nothing is drained yet.
2. Write the drain marker (``suppress_notification``). New turns are refused,
   and in-flight work finishes. If work is still active after
   ``drain_confirm_seconds``, clear the marker and go back to step 1. No turn
   is interrupted.
3. Stop the units, clear the marker (a same-epoch marker would park the fresh
   gateway in ``draining``), fast-forward, re-stamp, start.
4. Health: units active, ``/health`` ok, each live gateway's ``code_sha`` is the
   target. On failure, the same gate runs again and the previous SHA goes back.
"""

from __future__ import annotations

import fcntl
import fnmatch
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Optional

log = logging.getLogger("rpi5-deploy")

PROBE = Path(__file__).resolve().with_name("rpi5_deploy_probe.py")
SYSTEMCTL = "/usr/bin/systemctl"

# Same files as pm.environments.ACTIVATION_INPUTS. A change to any of them is
# the exceptional lane in ANDY_RUNTIME_PATCHES.md: never applied unattended.
DEPENDENCY_INPUTS = ("uv.lock", "pyproject.toml", "pm/lock.json")
# Never imported by a running gateway.
NOOP_PREFIXES = ("docs/", "website/", "tests/", "tests-js/", ".github/", "contributors/", "plugin-catalog/")
NOOP_ROOT_SUFFIXES = (".md",)
NOOP_NAMES = ("AGENTS.md", "LICENSE", ".gitignore")
# A running gateway re-reads skill files for new sessions: the skills prompt
# cache keys on the skill-file manifest (agent/prompt_builder.py, #92313), and
# open sessions keep their cached prompt.
HOT_PREFIXES = ("skills/", "optional-skills/")
DEPLOYER_FILES = (
    "docs/ops/rpi5_deploy.py",
    "docs/ops/rpi5_deploy_engine.py",
    "docs/ops/rpi5_deploy_probe.py",
)
UNIT_TEMPLATE_PREFIX = "docs/ops/systemd/"
SERVING_STATES = frozenset({"running", "degraded"})
_KIND_RANK = {"noop": 0, "hot": 1, "restart": 2, "manual": 3}


class DeployError(RuntimeError):
    pass


@dataclass(frozen=True)
class Unit:
    name: str
    scope: str = "system"


DEFAULT_FOLLOWER_UNIT = "hermes-webui.service"


@dataclass
class DeployConfig:
    checkout: Path
    home: Path
    repository: str = "andyylin/hermes-agent"
    branch: str = "main"
    remote: str = "origin"
    units: tuple[Unit, ...] = (Unit("hermes-gateway.service"),)
    follower_units: tuple[Unit, ...] = ()
    follower_units_configured: bool = False
    health_url: str = "http://127.0.0.1:8642/health"
    listen: str = "127.0.0.1"
    port: int = 9180
    https_port: int = 8443
    idle_poll_seconds: float = 5.0
    idle_settle_seconds: float = 20.0
    idle_report_seconds: float = 600.0
    drain_confirm_seconds: float = 120.0
    health_timeout_seconds: float = 120.0
    system_python: str = "/usr/bin/python3"

    @property
    def deploy_dir(self) -> Path:
        return self.home / "deploy"

    @property
    def releases_dir(self) -> Path:
        return self.deploy_dir / "releases"

    @classmethod
    def load(cls, home: Path, checkout: Optional[Path] = None) -> DeployConfig:
        path = home / "deploy" / "config.json"
        raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        raw.pop("home", None)
        units = tuple(Unit(**u) for u in raw.pop("units", [])) or cls.units
        follower_configured = "follower_units" in raw
        follower_units = tuple(Unit(**u) for u in raw.pop("follower_units", []))
        resolved = Path(raw.pop("checkout", None) or checkout or home / "hermes-agent")
        known = set(cls.__dataclass_fields__) - {"follower_units_configured"}
        unknown = sorted(set(raw) - set(known))
        if unknown:
            raise DeployError(f"unknown config keys in {path}: {', '.join(unknown)}")
        return cls(
            checkout=resolved, home=home, units=units, follower_units=follower_units,
            follower_units_configured=follower_configured, **raw,
        )


@dataclass(frozen=True)
class Plan:
    kind: str
    paths: tuple[str, ...]
    reasons: tuple[str, ...] = ()
    sync_skills: bool = False
    self_update: bool = False
    reinstall: bool = False


def _path_kind(path: str) -> str:
    if path in DEPENDENCY_INPUTS:
        return "manual"
    pure = PurePosixPath(path)
    if path.startswith(HOT_PREFIXES):
        return "hot"
    if path.startswith(NOOP_PREFIXES) or pure.name in NOOP_NAMES:
        return "noop"
    if len(pure.parts) == 1 and pure.suffix in NOOP_ROOT_SUFFIXES and pure.name != "SOUL.md":
        return "noop"
    return "restart"


def classify(paths: list[str]) -> Plan:
    """The least disruptive way to apply a diff. ``manual`` beats ``restart`` beats ``hot`` beats ``noop``."""
    kinds = {p: _path_kind(p) for p in paths}
    kind = max(kinds.values(), key=_KIND_RANK.__getitem__, default="noop")
    reasons = tuple(p for p, k in kinds.items() if k == "manual")
    return Plan(
        kind=kind,
        paths=tuple(paths),
        reasons=tuple(f"dependency input changed: {p}" for p in reasons),
        sync_skills=any(k == "hot" for k in kinds.values()),
        self_update=any(p in DEPLOYER_FILES for p in paths),
        reinstall=any(p.startswith(UNIT_TEMPLATE_PREFIX) for p in paths),
    )


@dataclass
class DeployState:
    deployed_sha: Optional[str] = None
    runtime_sha: Optional[str] = None
    previous_sha: Optional[str] = None
    pending_sha: Optional[str] = None
    phase: str = "idle"
    last_result: dict[str, Any] = field(default_factory=dict)
    failed_shas: list[str] = field(default_factory=list)
    reinstall_needed: bool = False
    updated_at: Optional[str] = None


class StateStore:
    """``state.json`` under the deploy dir. Writes are atomic; the HTTP thread and the worker share one lock."""

    def __init__(self, deploy_dir: Path):
        self.path = deploy_dir / "state.json"
        self._lock = threading.Lock()

    def load(self) -> DeployState:
        if not self.path.is_file():
            return DeployState()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return DeployState(**{k: v for k, v in raw.items() if k in DeployState.__dataclass_fields__})

    def update(self, **fields: Any) -> DeployState:
        with self._lock:
            state = self.load()
            for key, value in fields.items():
                setattr(state, key, value)
            state.updated_at = datetime.now(UTC).isoformat()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(asdict(state), handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp, self.path)
            return state


@contextmanager
def deploy_lock(deploy_dir: Path, *, wait: bool) -> Iterator[bool]:
    """Cross-process lock: the drift check skips while a deploy holds it."""
    deploy_dir.mkdir(parents=True, exist_ok=True)
    with (deploy_dir / "deploy.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Host:
    """Every side effect outside git. Tests replace systemctl, health, probe, sleep and the clock."""

    def __init__(self, config: DeployConfig):
        self.config = config

    def run(self, argv: list[str], *, cwd: Optional[Path] = None, env: Optional[dict] = None,
            timeout: float = 600) -> subprocess.CompletedProcess:
        return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, check=False)

    def git(self, *args: str, cwd: Optional[Path] = None) -> str:
        done = self.run(["git", *args], cwd=cwd or self.config.checkout)
        if done.returncode != 0:
            raise DeployError(f"git {args[0]} failed: {done.stderr.strip()[:400]}")
        return done.stdout.strip()

    def probe(self, python: str, checkout: Path, command: str, *args: str,
              home: Optional[Path] = None) -> dict[str, Any]:
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
               "HOME": os.environ.get("HOME", str(Path.home()))}
        if home is not None:
            env["HERMES_HOME"] = str(home)
        done = self.run([python, "-I", str(PROBE), "--checkout", str(checkout), command, *args], env=env)
        try:
            payload = json.loads(done.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            payload = {"ok": False}
        if done.returncode != 0 or not payload.get("ok", False):
            raise DeployError(f"probe {command} failed: {payload or done.stderr.strip()[-400:]}")
        return payload

    def _systemctl_argv(self, action: str, unit: Unit) -> list[str]:
        if unit.scope == "user":
            return [SYSTEMCTL, "--user", action, unit.name]
        if action == "is-active":
            return [SYSTEMCTL, action, unit.name]
        return ["sudo", "-n", SYSTEMCTL, action, unit.name]

    def systemctl(self, action: str, unit: Unit) -> bool:
        return self.run(self._systemctl_argv(action, unit), timeout=900).returncode == 0

    def unit_listed(self, unit: Unit) -> bool:
        if unit.scope == "user":
            argv = [SYSTEMCTL, "--user", "list-unit-files", unit.name, "--no-legend", "--no-pager"]
        else:
            argv = [SYSTEMCTL, "list-unit-files", unit.name, "--no-legend", "--no-pager"]
        done = self.run(argv, timeout=60)
        return done.returncode == 0 and unit.name in done.stdout

    def unit_show(self, unit: Unit, property_name: str) -> str:
        if unit.scope == "user":
            argv = [SYSTEMCTL, "--user", "show", unit.name, f"-p{property_name}", "--value"]
        else:
            argv = [SYSTEMCTL, "show", unit.name, f"-p{property_name}", "--value"]
        done = self.run(argv, timeout=60)
        if done.returncode != 0:
            return ""
        return done.stdout.strip()

    def unit_active_enter_time(self, unit: Unit) -> Optional[datetime]:
        raw = self.unit_show(unit, "ActiveEnterTimestamp")
        if not raw or raw == "n/a":
            return None
        try:
            return datetime.strptime(raw, "%a %Y-%m-%d %H:%M:%S %Z").replace(tzinfo=UTC)
        except ValueError:
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return None

    def restart_unit(self, unit: Unit) -> bool:
        return self.systemctl("restart", unit)

    def git_commit_time(self, sha: str) -> datetime:
        stamp = self.git("show", "-s", "--format=%cI", sha)
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))

    def health(self, url: str) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                body = json.loads(response.read(65536) or b"{}")
        except (urllib.error.URLError, OSError, ValueError):
            return False
        return response.status == 200 and body.get("status") == "ok"

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass
class Result:
    outcome: str
    sha: str
    detail: str = ""
    plan_kind: str = ""
    self_update: bool = False
    follower_restarts: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome in {"deployed", "already_deployed", "superseded"}


def _busy(home: dict[str, Any], *, strict: bool) -> bool:
    """Strict (before a deploy) fails closed: a live gateway is idle only when it is serving, fresh,
    and has no work. Lenient (before a rollback) only waits for real work: the release being
    rolled back may never reach ``running`` or may leave a parked ``draining`` state."""
    if not home.get("live"):
        return False
    if int(home.get("active_agents") or 0) > 0:
        return True
    return strict and (bool(home.get("stale")) or home.get("state") not in SERVING_STATES)


class Deployer:
    def __init__(self, config: DeployConfig, host: Host, store: StateStore):
        self.config = config
        self.host = host
        self.store = store
        self.wake = threading.Event()

    def request(self, sha: str) -> tuple[int, str]:
        """Queue *sha*; the newest request wins. Older, already-deployed and failed SHAs are refused."""
        state = self.store.load()
        if sha in state.failed_shas:
            return 409, f"{sha} failed health before and was rolled back"
        current = state.deployed_sha
        if current == sha:
            return 200, "already deployed"
        if current and self._is_ancestor(sha, current):
            return 409, f"{sha} is older than deployed {current}"
        if state.pending_sha and state.pending_sha != sha and self._is_ancestor(sha, state.pending_sha):
            return 409, f"{sha} is older than pending {state.pending_sha}"
        self.store.update(pending_sha=sha)
        self.wake.set()
        return 202, "queued"

    def run_pending(self) -> Optional[Result]:
        """Deploy whatever is pending; called by the worker thread and by tests."""
        sha = self.store.load().pending_sha
        if not sha:
            return None
        with deploy_lock(self.config.deploy_dir, wait=True):
            try:
                result = self.deploy(sha)
            except DeployError as exc:
                log.error("deploy %s failed: %s", sha, exc)
                result = Result("error", sha, str(exc))
        state = self.store.load()
        pending = None if state.pending_sha == sha else state.pending_sha
        self.store.update(pending_sha=pending, phase="idle", last_result=asdict(result))
        return result

    def deploy(self, target: str) -> Result:
        current = self._current_sha()
        if target == current:
            return Result("already_deployed", target)
        self._phase("validating")
        self._fetch_and_verify(current, target)
        plan = classify(self._changed_paths(current, target))
        log.info("deploy %s -> %s: %s (%d paths)", current[:12], target[:12], plan.kind, len(plan.paths))
        if plan.kind == "manual":
            return Result("needs_manual", target, "; ".join(plan.reasons), plan.kind)
        python = self._pm_python()
        if plan.kind != "restart":
            self._fast_forward(target, python)
            if plan.sync_skills:
                self._sync_skills(python)
            self._record_deployed(target, current, plan, restarted=False)
            followers = self._restart_followers(python)
            return Result(
                "deployed", target, "applied without restart", plan.kind, plan.self_update, followers,
            )
        self._stage_release(target, plan, python)
        return self._restart_deploy(current, target, plan, python)

    def _restart_deploy(self, current: str, target: str, plan: Plan, python: str) -> Result:
        drained = self._quiesce(python, target=target)
        if drained is None:
            return Result("superseded", target, "a newer SHA arrived while waiting for idle", plan.kind)
        self._phase("swapping")
        if not self._swap(target, python, drained):
            return Result("error", target, "a unit refused to stop; checkout unchanged, units started again")
        if plan.sync_skills:
            self._sync_skills(python)
        if self._healthy(python, target):
            self._record_deployed(target, current, plan, restarted=True)
            skip = self._followers_restarted_with_gateway()
            followers = self._restart_followers(python, skip=skip, wait_idle=False)
            return Result("deployed", target, "restarted", plan.kind, plan.self_update, followers)
        return self._rollback(current, target, python)

    def _rollback(self, previous: str, failed: str, python: str) -> Result:
        log.error("health failed for %s; rolling back to %s", failed, previous)
        self._phase("rolling_back")
        state = self.store.load()
        self.store.update(failed_shas=[*state.failed_shas, failed][-20:])
        drained = self._quiesce(python, target=None, strict=False) or []
        if self._swap(previous, python, drained, reset=True) and self._healthy(python, previous):
            self.store.update(runtime_sha=previous)
            return Result("rolled_back", failed, f"health failed; {previous} restored")
        return Result("rollback_failed", failed, f"health failed for {failed} and for restored {previous}")

    def _current_sha(self) -> str:
        return self.store.load().deployed_sha or self.host.git("rev-parse", "HEAD")

    def _is_ancestor(self, older: str, newer: str) -> bool:
        done = self.host.run(["git", "merge-base", "--is-ancestor", older, newer], cwd=self.config.checkout)
        return done.returncode == 0

    def _fetch_and_verify(self, current: str, target: str) -> None:
        cfg = self.config
        tracking = f"refs/remotes/{cfg.remote}/{cfg.branch}"
        self.host.git("fetch", "--no-tags", cfg.remote, f"+refs/heads/{cfg.branch}:{tracking}")
        self.host.git("cat-file", "-e", f"{target}^{{commit}}")
        if not self._is_ancestor(target, tracking):
            raise DeployError(f"{target} is not on {cfg.remote}/{cfg.branch}")
        if not self._is_ancestor(current, target):
            raise DeployError(f"{target} does not fast-forward from deployed {current}")
        problems = live_tree_problems(self.host, cfg, current)
        if problems:
            raise DeployError("live checkout drifted: " + "; ".join(problems))

    def _changed_paths(self, current: str, target: str) -> list[str]:
        out = self.host.git("diff", "--name-only", "--no-renames", current, target)
        return [line for line in out.splitlines() if line]

    def _pm_python(self) -> str:
        payload = self.host.probe(self.config.system_python, self.config.checkout, "venv-python")
        return str(payload["python"])

    def _stage_release(self, target: str, plan: Plan, python: str) -> Path:
        release = self.config.releases_dir / target
        if not release.is_dir():
            release.parent.mkdir(parents=True, exist_ok=True)
            self.host.git("worktree", "add", "--detach", str(release), target)
        compiled = [p for p in plan.paths if p.endswith(".py") and (release / p).is_file()]
        with tempfile.TemporaryDirectory(prefix="rpi5-deploy-home.") as scratch:
            args = [arg for path in compiled for arg in ("--compile", str(release / path))]
            self.host.probe(python, release, "imports", *args, home=Path(scratch))
        return release

    def _quiesce(self, python: str, *, target: Optional[str], strict: bool = True) -> Optional[list[Path]]:
        """Block until no live gateway has work, then hold new turns off with the drain marker.

        Returns the drained homes, or None when *target* was superseded while waiting."""
        while True:
            self._phase("waiting_idle")
            homes = self._wait_idle(python, target, strict)
            if homes is None:
                return None
            live = [Path(h["home"]) for h in homes if h.get("live")]
            if not live:
                return []
            self._phase("draining")
            self.host.probe(python, self.config.checkout, "drain", "on", *_homes_args(live))
            if self._drain_settles(python, live):
                return live
            log.info("work started during drain; releasing the drain marker and waiting again")
            self.host.probe(python, self.config.checkout, "drain", "off", *_homes_args(live))

    def _wait_idle(self, python: str, target: Optional[str], strict: bool) -> Optional[list[dict]]:
        cfg = self.config
        idle_since: Optional[float] = None
        started = last_report = self.host.monotonic()
        while True:
            if target is not None and self._superseded(target):
                return None
            homes = self._probe_homes(python)
            now = self.host.monotonic()
            busy = homes is None or any(_busy(h, strict=strict) for h in homes)
            if busy:
                idle_since = None
                if now - last_report >= cfg.idle_report_seconds:
                    log.info("still waiting for idle gateways after %.0fs", now - started)
                    last_report = now
            elif idle_since is None:
                idle_since = now
            if idle_since is not None and now - idle_since >= cfg.idle_settle_seconds:
                return homes
            self.host.sleep(cfg.idle_poll_seconds)

    def _drain_settles(self, python: str, live: list[Path]) -> bool:
        deadline = self.host.monotonic() + self.config.drain_confirm_seconds
        wanted = {str(p) for p in live}
        while self.host.monotonic() < deadline:
            homes = self._probe_homes(python) or []
            ready = [h for h in homes if h["home"] in wanted
                     and (not h.get("live") or (h.get("state") == "draining" and not h.get("active_agents")))]
            if homes and len(ready) == len(wanted):
                return True
            self.host.sleep(self.config.idle_poll_seconds)
        return False

    def hermes_homes(self, python: str) -> list[str]:
        """The default home plus every named profile, as ``hermes profile list`` sees them."""
        payload = self.host.probe(python, self.config.checkout, "homes", home=self.config.home)
        return [str(home) for home in payload.get("homes") or [self.config.home]]

    def _probe_homes(self, python: str) -> Optional[list[dict]]:
        try:
            homes = self.hermes_homes(python)
            payload = self.host.probe(python, self.config.checkout, "idle", *_homes_args(homes))
        except DeployError as exc:
            log.warning("idle probe failed, treating as busy: %s", exc)
            return None
        return list(payload.get("homes") or [])

    def _superseded(self, target: str) -> bool:
        pending = self.store.load().pending_sha
        return bool(pending) and pending != target

    def _swap(self, sha: str, python: str, drained: list[Path], *, reset: bool = False) -> bool:
        """Stop, move the checkout, re-stamp, start. Units are always started again.

        False only when a unit refused to stop, so the checkout was not moved. A failure after the
        move is left to the health check, which then rolls back."""
        units = self.config.units
        stopped = all([self.host.systemctl("stop", unit) for unit in reversed(units)])
        try:
            if drained:
                self.host.probe(python, self.config.checkout, "drain", "off", *_homes_args(drained))
            if stopped:
                self.host.git(*(("reset", "--keep", sha) if reset else ("merge", "--ff-only", sha)))
                self.host.probe(python, self.config.checkout, "stamp")
        except DeployError as exc:
            log.error("swap to %s incomplete: %s", sha, exc)
        finally:
            started = [self.host.systemctl("start", unit) for unit in units]
        if not all(started):
            log.error("a unit failed to start on %s", sha)
        return stopped

    def _fast_forward(self, target: str, python: str) -> None:
        self.host.git("merge", "--ff-only", target)
        self.host.probe(python, self.config.checkout, "stamp")

    def _sync_skills(self, python: str) -> None:
        for home in self.hermes_homes(python):
            self.host.probe(python, self.config.checkout, "skills-sync", home=Path(home))

    def _healthy(self, python: str, sha: str) -> bool:
        self._phase("health")
        deadline = self.host.monotonic() + self.config.health_timeout_seconds
        while self.host.monotonic() < deadline:
            if self._health_once(python, sha):
                return True
            self.host.sleep(self.config.idle_poll_seconds)
        return False

    def _health_once(self, python: str, sha: str) -> bool:
        if not all(self.host.systemctl("is-active", unit) for unit in self.config.units):
            return False
        if not self.host.health(self.config.health_url):
            return False
        homes = self._probe_homes(python)
        live = [h for h in homes or [] if h.get("live")]
        return bool(live) and all(h.get("code_sha") == sha and h.get("state") in SERVING_STATES for h in live)

    def _record_deployed(self, target: str, current: str, plan: Plan, *, restarted: bool) -> None:
        state = self.store.load()
        runtime = target if restarted else (state.runtime_sha or current)
        self.store.update(
            deployed_sha=target, previous_sha=current, runtime_sha=runtime,
            reinstall_needed=state.reinstall_needed or plan.reinstall,
        )
        self._prune_releases(keep={target, current})

    def _prune_releases(self, keep: set[str]) -> None:
        root = self.config.releases_dir
        if not root.is_dir():
            return
        for release in root.iterdir():
            if release.name in keep:
                continue
            try:
                self.host.git("worktree", "remove", "--force", str(release))
            except DeployError as exc:
                log.warning("could not prune release %s: %s", release.name, exc)
        self.host.run(["git", "worktree", "prune"], cwd=self.config.checkout)

    def _phase(self, phase: str) -> None:
        self.store.update(phase=phase)

    def resolved_follower_units(self) -> tuple[Unit, ...]:
        if self.config.follower_units_configured:
            return self.config.follower_units
        unit = Unit(DEFAULT_FOLLOWER_UNIT)
        return (unit,) if self.host.unit_listed(unit) else ()

    def _restart_followers(
        self, python: str, *, skip: frozenset[str] = frozenset(), wait_idle: bool = True,
    ) -> dict[str, str]:
        results: dict[str, str] = {}
        followers = [unit for unit in self.resolved_follower_units() if unit.name not in skip]
        if not followers:
            return results
        if wait_idle:
            self._phase("waiting_idle")
            if self._wait_idle(python, None, strict=True) is None:
                log.warning("idle wait ended without a home snapshot; restarting followers anyway")
        for unit in followers:
            if not self.host.restart_unit(unit):
                log.error("follower restart failed for %s", unit.name)
                results[unit.name] = "restart failed"
                continue
            if not self.host.systemctl("is-active", unit):
                log.error("follower %s is not active after restart", unit.name)
                results[unit.name] = "not active after restart"
                continue
            results[unit.name] = "ok"
        return results

    def _followers_restarted_with_gateway(self) -> frozenset[str]:
        """Follower units whose start time matches a recent gateway restart (e.g. drop-in 97-restart-webui)."""
        if not self.config.units:
            return frozenset()
        gateway_enter = self.host.unit_active_enter_time(self.config.units[0])
        if gateway_enter is None:
            return frozenset()
        restarted = set()
        for unit in self.resolved_follower_units():
            entered = self.host.unit_active_enter_time(unit)
            if entered is not None and entered >= gateway_enter:
                restarted.add(unit.name)
        return frozenset(restarted)


def _homes_args(homes: list) -> list[str]:
    return [arg for home in homes for arg in ("--home", str(home))]


_INSTALL_STAMP_EXTERNAL_SIDECAR = "install-stamp*.external*"


def _install_stamp_external_sidecar(path: str) -> bool:
    """Claim sidecars from external→self re-stamp; not deploy drift."""
    return fnmatch.fnmatch(PurePosixPath(path.strip()).name, _INSTALL_STAMP_EXTERNAL_SIDECAR)


def live_tree_problems(host: Host, cfg: DeployConfig, deployed: Optional[str]) -> list[str]:
    """Ways the live checkout differs from the deployed SHA. Paths only, never file content."""
    problems = []
    branch = host.git("rev-parse", "--abbrev-ref", "HEAD")
    if branch != cfg.branch:
        problems.append(f"checkout is on {branch}, expected {cfg.branch}")
    head = host.git("rev-parse", "HEAD")
    if deployed and head != deployed:
        problems.append(f"HEAD is {head}, deployed is {deployed}")
    status = host.run(["git", "status", "--porcelain", "-z", "--untracked-files=normal"], cwd=cfg.checkout)
    if status.returncode != 0:
        return [*problems, f"git status failed: {status.stderr.strip()[:400]}"]
    dirty = [
        path for entry in status.stdout.split("\0") if len(entry) > 3
        for path in [entry[3:]]
        if not _install_stamp_external_sidecar(path)
    ]
    if dirty:
        shown = ", ".join(dirty[:20]) + (" ..." if len(dirty) > 20 else "")
        problems.append(f"{len(dirty)} modified or untracked path(s): {shown}")
    return problems


def runtime_problems(deployer: Deployer, state: DeployState) -> list[str]:
    """The running gateways against ``runtime_sha``, and whether the deployed diff still owes a restart."""
    problems = []
    if state.runtime_sha and state.deployed_sha and state.runtime_sha != state.deployed_sha:
        owed = classify(deployer._changed_paths(state.runtime_sha, state.deployed_sha))
        if owed.kind == "restart":
            problems.append(f"deployed {state.deployed_sha} needs a restart; gateways run {state.runtime_sha}")
    python = deployer._pm_python()
    homes = deployer._probe_homes(python)
    if homes is None:
        return [*problems, "could not read gateway state"]
    for home in homes:
        sha = home.get("code_sha")
        if home.get("live") and sha and sha != state.runtime_sha:
            problems.append(f"gateway in {home['home']} runs {sha}, expected {state.runtime_sha}")
    return problems


def result_problems(state: DeployState) -> list[str]:
    problems = []
    outcome = state.last_result.get("outcome")
    if outcome in {"rolled_back", "rollback_failed", "needs_manual", "error"}:
        problems.append(f"last deploy {outcome}: {state.last_result.get('sha')} {state.last_result.get('detail', '')}")
    followers = state.last_result.get("follower_restarts") or {}
    failed = [f"{name}: {detail}" for name, detail in sorted(followers.items()) if detail != "ok"]
    if failed:
        problems.append("follower restarts failed: " + "; ".join(failed))
    if state.reinstall_needed:
        problems.append("unit templates changed; re-run rpi5-vanilla-migration.sh --install-deploy-listener")
    return problems


def stale_follower_units(host: Host, deployed_sha: str, followers: tuple[Unit, ...]) -> list[Unit]:
    """Followers that started before the deployed commit (stale agent runtime in-process)."""
    try:
        commit_at = host.git_commit_time(deployed_sha)
    except DeployError:
        return list(followers)
    stale = []
    for unit in followers:
        entered = host.unit_active_enter_time(unit)
        if entered is None or entered < commit_at:
            stale.append(unit)
    return stale


def remediate_stale_followers(deployer: Deployer) -> dict[str, str]:
    """Restart followers that booted before the live checkout's deployed SHA (manual pull recovery)."""
    state = deployer.store.load()
    if not state.deployed_sha:
        return {}
    head = deployer.host.git("rev-parse", "HEAD")
    if head != state.deployed_sha:
        return {}
    if live_tree_problems(deployer.host, deployer.config, state.deployed_sha):
        return {}
    followers = deployer.resolved_follower_units()
    stale = stale_follower_units(deployer.host, state.deployed_sha, followers)
    results: dict[str, str] = {}
    for unit in stale:
        if not deployer.host.restart_unit(unit):
            log.error("drift: follower restart failed for %s", unit.name)
            results[unit.name] = "restart failed"
            continue
        if not deployer.host.systemctl("is-active", unit):
            log.error("drift: follower %s is not active after restart", unit.name)
            results[unit.name] = "not active after restart"
            continue
        results[unit.name] = "ok"
        log.info("drift: restarted stale follower %s", unit.name)
    return results
