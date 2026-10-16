"""Push-to-deploy for the rpi5 host: listener, CI notifier, drift check, installer render.

``.github/workflows/rpi5-deploy.yml`` joins the tailnet on each push to
``main`` and runs ``notify``. That POSTs the merged SHA, signed with
HMAC-SHA256, to ``tailscale serve`` on the Pi, which proxies to ``serve`` on
loopback. ``serve`` verifies the signature, queues the SHA, and returns 202.
A single worker thread deploys it (``rpi5_deploy_engine.Deployer``).

This file runs on the system Python with the standard library only, so a
broken release cannot break the deployer. Hermes-side checks run through
``rpi5_deploy_probe.py`` under the committed PM interpreter.

Usage (the installer renders these into systemd units):
  rpi5_deploy.py serve --home H                  # listener (hermes-deploy.service)
  rpi5_deploy.py drift --home H [--fetch]        # exit 1 on drift (hermes-deploy-drift.timer)
  rpi5_deploy.py status --home H                 # state.json summary
  rpi5_deploy.py reconcile --home H              # record runtime_sha when live matches deployed
  rpi5_deploy.py notify --url U --sha S --repository R --ref REF   # CI; key in RPI5_DEPLOY_HMAC_KEY
  rpi5_deploy.py init --home H --checkout C      # deploy dir, key, config, state (never overwrites)
  rpi5_deploy.py render-install --home H --checkout C --user U --uid N --out DIR
  rpi5_deploy.py self-test --home H              # can this copy serve? (before a self-restart)

Exit codes: 0 ok; 1 drift, rejected notify, or failed self-test; 2 usage or configuration error.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rpi5_deploy_engine import (
    DeployConfig, DeployError, Deployer, Host, StateStore, deploy_lock, live_tree_problems,
    reconcile_runtime_sha, remediate_stale_followers, result_problems, runtime_problems,
)

log = logging.getLogger("rpi5-deploy")

SIGNATURE_HEADER = "X-Hermes-Deploy-Signature"
TIMESTAMP_HEADER = "X-Hermes-Deploy-Timestamp"
MAX_SKEW_SECONDS = 300
MAX_BODY_BYTES = 8192
MIN_KEY_BYTES = 32
NOTIFY_KEY_ENV = "RPI5_DEPLOY_HMAC_KEY"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_UNIT_NAME = re.compile(r"^[A-Za-z0-9@._-]+\.service$")
TEMPLATES = Path(__file__).resolve().parent / "systemd"
RESUMABLE_PHASES = {"waiting_idle", "draining", "swapping", "health", "rolling_back"}


class AuthError(Exception):
    pass


def sign(key: bytes, timestamp: str, body: bytes) -> str:
    """Signature over ``<timestamp>.<body>``, so a captured request cannot be replayed with a new time."""
    return "sha256=" + hmac.new(key, timestamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()


def verify(key: bytes, headers: Mapping[str, str], body: bytes, *, now: float) -> None:
    timestamp = headers.get(TIMESTAMP_HEADER) or ""
    signature = headers.get(SIGNATURE_HEADER) or ""
    if not timestamp or not signature:
        raise AuthError("missing signature")
    try:
        sent = int(timestamp)
    except ValueError:
        raise AuthError("malformed timestamp") from None
    if abs(now - sent) > MAX_SKEW_SECONDS:
        raise AuthError("timestamp outside the replay window")
    if not hmac.compare_digest(sign(key, timestamp, body), signature):
        raise AuthError("bad signature")


def parse_request(body: bytes, config: DeployConfig) -> str:
    """The SHA a verified body asks for. ValueError for a malformed body, PermissionError for another repo/ref."""
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, dict) or not _SHA.match(str(data.get("sha", ""))):
        raise ValueError("body needs a 40-hex sha")
    if data.get("repository") != config.repository or data.get("ref") != f"refs/heads/{config.branch}":
        raise PermissionError("repository or ref is not the deployed branch")
    return str(data["sha"])


def load_key(config: DeployConfig) -> bytes:
    path = config.deploy_dir / "hmac.key"
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise DeployError(f"{path} must be readable by its owner only")
    key = path.read_bytes().strip()
    if len(key) < MIN_KEY_BYTES:
        raise DeployError(f"{path} is shorter than {MIN_KEY_BYTES} bytes")
    return key


class Handler(BaseHTTPRequestHandler):
    server_version = "hermes-deploy"
    deployer: Deployer
    key: bytes
    clock: Callable[[], float] = time.time

    def log_message(self, fmt: str, *args) -> None:
        log.info("http %s", fmt % args)

    def _reply(self, code: int, payload: dict) -> None:
        body = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/status":
            self._reply(404, {"error": "not found"})
            return
        state = self.deployer.store.load()
        self._reply(200, {k: getattr(state, k) for k in ("deployed_sha", "runtime_sha", "pending_sha", "phase")})

    def do_POST(self) -> None:
        if self.path != "/deploy":
            self._reply(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            self._reply(413, {"error": "body size"})
            return
        body = self.rfile.read(length)
        try:
            verify(self.key, self.headers, body, now=self.clock())
        except AuthError as exc:
            log.warning("rejected deploy request: %s", exc)
            self._reply(401, {"error": str(exc)})
            return
        try:
            sha = parse_request(body, self.deployer.config)
        except PermissionError as exc:
            self._reply(403, {"error": str(exc)})
            return
        except ValueError as exc:
            self._reply(400, {"error": str(exc)})
            return
        code, message = self.deployer.request(sha)
        log.info("deploy request %s: %s %s", sha, code, message)
        self._reply(code, {"sha": sha, "result": message})


def make_server(deployer: Deployer, key: bytes, *, port: Optional[int] = None,
                clock: Callable[[], float] = time.time) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"deployer": deployer, "key": key, "clock": staticmethod(clock)})
    cfg = deployer.config
    return ThreadingHTTPServer((cfg.listen, cfg.port if port is None else port), handler)


def self_test(config: DeployConfig, *, checkout: Optional[Path] = None) -> bool:
    """Run ``self-test`` from *checkout*'s copy of this file, in a fresh interpreter."""
    script = (checkout or config.checkout) / "docs" / "ops" / "rpi5_deploy.py"
    done = subprocess.run([config.system_python, str(script), "self-test", "--home", str(config.home)],
                          capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, check=False)
    if done.returncode != 0:
        log.error("self-test of %s failed: %s", script, done.stderr.strip()[-400:])
    return done.returncode == 0


def recover(deployer: Deployer) -> None:
    """A listener that died mid-deploy may have left units stopped or a drain marker in place."""
    state = deployer.store.load()
    if state.phase not in RESUMABLE_PHASES:
        return
    log.warning("previous listener stopped during %s; starting units and clearing drain markers", state.phase)
    for unit in deployer.config.units:
        deployer.host.systemctl("start", unit)
    if state.phase in {"draining", "swapping", "rolling_back"}:
        try:
            python = deployer._pm_python()
            homes = [a for h in deployer.hermes_homes(python) for a in ("--home", h)]
            deployer.host.probe(python, deployer.config.checkout, "drain", "off", *homes)
        except DeployError as exc:
            log.error("could not clear drain markers: %s", exc)
    deployer.store.update(phase="idle")


def worker(deployer: Deployer, stop: threading.Event, on_self_update: Callable[[], None]) -> None:
    while not stop.is_set():
        deployer.wake.wait(timeout=60)
        deployer.wake.clear()
        while (result := deployer.run_pending()) is not None:
            log.info("deploy %s: %s %s", result.sha, result.outcome, result.detail)
            if result.ok and result.self_update and self_test(deployer.config):
                log.info("deployer code changed; exiting so systemd starts the new copy")
                on_self_update()
                return


def cmd_serve(config: DeployConfig) -> int:
    key = load_key(config)
    deployer = Deployer(config, Host(config), StateStore(config.deploy_dir))
    recover(deployer)
    server = make_server(deployer, key)
    stop = threading.Event()
    # health: allow HX012 -- stdlib-only ops process outside Hermes; no profile scope exists here
    thread = threading.Thread(target=worker, args=(deployer, stop, server.shutdown), name="deploy-worker",
                              daemon=True)
    thread.start()
    if deployer.store.load().pending_sha:
        deployer.wake.set()
    log.info("listening on %s:%d for %s %s", config.listen, config.port, config.repository, config.branch)
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
    return 0


def behind_problems(host: Host, config: DeployConfig, deployed: str, pending: Optional[str]) -> list[str]:
    tracking = f"refs/remotes/{config.remote}/{config.branch}"
    host.git("fetch", "--no-tags", config.remote, f"+refs/heads/{config.branch}:{tracking}")
    tip = host.git("rev-parse", tracking)
    if tip != deployed and not pending:
        return [f"deployed {deployed} is behind {config.remote}/{config.branch} {tip} and nothing is pending"]
    return []


def drift_problems(config: DeployConfig, host: Host, *, fetch: bool) -> list[str]:
    store = StateStore(config.deploy_dir)
    state = store.load()
    if not state.deployed_sha:
        return ["state.json has no deployed_sha; run rpi5-vanilla-migration.sh --install-deploy-listener"]
    deployer = Deployer(config, host, store)
    problems = live_tree_problems(host, config, state.deployed_sha) + result_problems(state)
    try:
        reconcile_runtime_sha(deployer)
        state = store.load()
        problems += runtime_problems(deployer, state)
        if fetch:
            problems += behind_problems(host, config, state.deployed_sha, state.pending_sha)
    except DeployError as exc:
        problems.append(str(exc))
    return problems


def cmd_drift(config: DeployConfig, *, fetch: bool, host: Optional[Host] = None) -> int:
    with deploy_lock(config.deploy_dir, wait=False) as acquired:
        if not acquired:
            print(f"deploy in progress ({StateStore(config.deploy_dir).load().phase}); skipped")
            return 0
        resolved_host = host or Host(config)
        deployer = Deployer(config, resolved_host, StateStore(config.deploy_dir))
        remediate_stale_followers(deployer)
        problems = drift_problems(config, resolved_host, fetch=fetch)
    report = {"checked_at": int(time.time()), "problems": problems}
    (config.deploy_dir / "drift.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for problem in problems:
        log.error("drift: %s", problem)
    if not problems:
        print("no drift")
    return 1 if problems else 0


def notify(url: str, *, sha: str, repository: str, ref: str, key: bytes, attempts: int = 6,
           sleep: Callable[[float], None] = time.sleep) -> int:
    body = json.dumps({"repository": repository, "ref": ref, "sha": sha}, sort_keys=True).encode("utf-8")
    for attempt in range(1, attempts + 1):
        timestamp = str(int(time.time()))
        request = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json", TIMESTAMP_HEADER: timestamp,
            SIGNATURE_HEADER: sign(key, timestamp, body)})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                print(f"{response.status} {response.read(2048).decode('utf-8', 'replace').strip()}")
                return 0
        except urllib.error.HTTPError as exc:
            print(f"{exc.code} {exc.read(2048).decode('utf-8', 'replace').strip()}")
            if exc.code < 500:
                return 1
        except (urllib.error.URLError, OSError) as exc:
            print(f"attempt {attempt}: {exc}")
        if attempt < attempts:
            sleep(min(2 ** attempt, 30))
    return 1


def cmd_init(config: DeployConfig) -> int:
    deploy_dir = config.deploy_dir
    deploy_dir.mkdir(parents=True, exist_ok=True)
    deploy_dir.chmod(0o700)
    key_path = deploy_dir / "hmac.key"
    if not key_path.exists():
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(secrets.token_hex(32) + "\n")
        print(f"created {key_path}")
    config_path = deploy_dir / "config.json"
    if not config_path.exists():
        payload = {"checkout": str(config.checkout), "units": [asdict(u) for u in config.units],
                   "health_url": config.health_url, "system_python": sys.executable}
        config_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"created {config_path}")
    store = StateStore(deploy_dir)
    state = store.load()
    if not state.deployed_sha:
        head = Host(config).git("rev-parse", "HEAD")
        store.update(deployed_sha=head, runtime_sha=head)
        print(f"recorded deployed_sha {head}")
    store.update(reinstall_needed=False)
    return 0


def sudoers_text(config: DeployConfig, user: str, host: Optional[Host] = None) -> str:
    from rpi5_deploy_engine import SYSTEMCTL

    commands = []
    resolved = Deployer(config, host or Host(config), StateStore(config.deploy_dir)).resolved_follower_units()
    managed = list(dict.fromkeys((*config.units, *resolved)))
    for unit in managed:
        if not _UNIT_NAME.match(unit.name):
            raise DeployError(f"refusing unit name {unit.name!r}")
        if unit.scope == "system":
            commands += [
                f"{SYSTEMCTL} stop {unit.name}",
                f"{SYSTEMCTL} start {unit.name}",
                f"{SYSTEMCTL} restart {unit.name}",
            ]
    if not re.match(r"^[a-z_][a-z0-9_-]*$", user):
        raise DeployError(f"refusing user name {user!r}")
    header = "# Rendered by docs/ops/rpi5_deploy.py render-install. Stop/start only, for the deploy listener.\n"
    return header + (f"{user} ALL=(root) NOPASSWD: " + ", ".join(commands) + "\n" if commands else "")


def cmd_render_install(config: DeployConfig, *, user: str, uid: int, out: Path) -> int:
    values = {"@CHECKOUT@": str(config.checkout), "@HOME@": str(config.home), "@USER@": user,
              "@UID@": str(uid), "@PYTHON@": config.system_python}
    out.mkdir(parents=True, exist_ok=True)
    for template in sorted(TEMPLATES.glob("*.in")):
        text = template.read_text(encoding="utf-8")
        for token, value in values.items():
            text = text.replace(token, value)
        (out / template.stem).write_text(text, encoding="utf-8")
        print(f"unit={template.stem}")
    (out / "hermes-deploy.sudoers").write_text(sudoers_text(config, user, Host(config)), encoding="utf-8")
    print(f"port={config.port}")
    print(f"https_port={config.https_port}")
    return 0


def cmd_status(config: DeployConfig) -> int:
    print(json.dumps(asdict(StateStore(config.deploy_dir).load()), indent=2, sort_keys=True))
    return 0


def cmd_reconcile(config: DeployConfig, *, host: Optional[Host] = None) -> int:
    with deploy_lock(config.deploy_dir, wait=False) as acquired:
        if not acquired:
            print(f"deploy in progress ({StateStore(config.deploy_dir).load().phase}); skipped", file=sys.stderr)
            return 1
        deployer = Deployer(config, host or Host(config), StateStore(config.deploy_dir))
        ok, message = reconcile_runtime_sha(deployer)
    if not ok:
        print(message, file=sys.stderr)
        return 1
    print(message)
    return 0


def cmd_self_test(config: DeployConfig) -> int:
    load_key(config)
    StateStore(config.deploy_dir).load()
    print("ok")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "drift", "status", "reconcile", "init", "render-install", "self-test"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--home", required=True, type=Path, help="default Hermes home on the host")
        cmd.add_argument("--checkout", type=Path, help="live checkout (default: config.json, then <home>/hermes-agent)")
        if name == "drift":
            cmd.add_argument("--fetch", action="store_true", help="also alert when origin is ahead")
        if name == "render-install":
            cmd.add_argument("--user", required=True)
            cmd.add_argument("--uid", required=True, type=int)
            cmd.add_argument("--out", required=True, type=Path)
    notify_cmd = sub.add_parser("notify")
    for flag in ("--url", "--sha", "--repository", "--ref"):
        notify_cmd.add_argument(flag, required=True)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    if args.command == "notify":
        key = os.environ.get(NOTIFY_KEY_ENV, "").strip().encode("utf-8")
        if len(key) < MIN_KEY_BYTES:
            print(f"{NOTIFY_KEY_ENV} is missing or too short", file=sys.stderr)
            return 2
        return notify(args.url, sha=args.sha, repository=args.repository, ref=args.ref, key=key)
    try:
        config = DeployConfig.load(args.home.resolve(), args.checkout)
        commands = {
            "serve": lambda: cmd_serve(config),
            "drift": lambda: cmd_drift(config, fetch=args.fetch),
            "status": lambda: cmd_status(config),
            "reconcile": lambda: cmd_reconcile(config),
            "init": lambda: cmd_init(config),
            "render-install": lambda: cmd_render_install(config, user=args.user, uid=args.uid, out=args.out),
            "self-test": lambda: cmd_self_test(config),
        }
        return commands[args.command]()
    except (DeployError, OSError, ValueError) as exc:
        print(f"{args.command}: {exc}", file=sys.stderr)
        return 1 if args.command == "self-test" else 2


if __name__ == "__main__":
    raise SystemExit(main())
