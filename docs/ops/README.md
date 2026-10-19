# rpi5 operations

Runbooks and tooling for the Raspberry Pi 5 host (`rpi5` on the tailnet). The
host's runtime changes only through merged repo code.

- [`rpi5-vanilla-migration.md`](rpi5-vanilla-migration.md): the one-gateway
  vanilla cutover (`rpi5-vanilla-migration.sh`, `rpi5_migration.py`).
- Push-to-deploy, below (`rpi5_deploy.py`, `rpi5_deploy_engine.py`,
  `rpi5_deploy_probe.py`, `systemd/`, `.github/workflows/rpi5-deploy.yml`).

## Push-to-deploy

A merge to `main` reaches the Pi in seconds. A restart waits until every
gateway is idle.

1. `.github/workflows/rpi5-deploy.yml` runs on each push to `main`. It joins
   the tailnet as an ephemeral `tag:ci` node, then POSTs
   `{repository, ref, sha}` to `https://rpi5.<tailnet>.ts.net:8443/deploy`.
   The body is signed with HMAC-SHA256 over `<timestamp>.<body>`.
2. `tailscale serve` on the Pi terminates TLS and proxies to
   `hermes-deploy.service` on `127.0.0.1:9180`. The listener rejects a missing
   or bad signature, or a timestamp more than 300 s off, with 401. It rejects
   another repository or ref with 403. A SHA that is already deployed, older
   than deployed or pending, or rolled back before is a no-op (200) or 409, so
   a replayed request cannot downgrade the host. Otherwise it answers 202 and
   queues the SHA. The newest queued SHA wins.
3. The worker fetches `origin/main` and requires the SHA to be on it and to
   fast-forward from the deployed SHA. It also requires the live checkout to
   be clean, on `main`, and at the deployed SHA. Then it classifies
   `git diff --name-only deployed..target`:

   | Diff touches | What happens |
   |---|---|
   | only `docs/`, `website/`, `tests/`, `.github/`, root `*.md` (not `SOUL.md`), `AGENTS.md` | fast-forward, re-stamp, restart follower units, no gateway restart |
   | `skills/`, `optional-skills/` (plus the above) | fast-forward, re-stamp, `sync_skills`, restart follower units, no gateway restart |
   | anything else | staged release, idle gate, restart, health check, rollback |
   | `pyproject.toml`, `uv.lock`, `pm/lock.json` | not applied; `needs_manual` (the exceptional lane in `ANDY_RUNTIME_PATCHES.md`) |

   The skills path works because the skills prompt cache keys on the skill
   files' manifest (`agent/prompt_builder.py`, #92313). New sessions see the
   new skills, and open sessions keep their cached prompt.
4. Restart path:
   - **Release dir.** `git worktree add --detach <home>/deploy/releases/<sha>`.
     Under the committed PM interpreter, with a scratch `HERMES_HOME`, every
     changed `.py` is compiled and the gateway's entry modules are imported
     from that tree. A failure stops here, before anything is stopped. The
     live tree stays at `~/.hermes/hermes-agent`, because
     `pm.environments.install_key` hashes the resolved checkout path. The
     swap is a fast-forward of that checkout, not a symlink. The current and
     previous release worktrees are kept.
   - **Idle gate.** `rpi5_deploy_probe.py idle` reads each home's
     `gateway_state.json` through `gateway.status` (PID-reuse guard, heartbeat
     staleness). A live gateway counts as idle only when it is `running` or
     `degraded`, its heartbeat is fresh, and `active_agents` is 0.
     `active_agents` already sums chat turns, cron jobs, API runs, and
     deferred workers (`GatewayRunner._active_work_count`). That has to hold
     for 20 s. Then the deploy writes the official drain marker
     (`gateway.drain_control.write_drain_request`, `suppress_notification`),
     so new turns are refused and no "shutting down" broadcast goes out. If a
     turn slipped in, the deploy waits up to 120 s for it, then clears the
     marker and goes back to waiting. Nothing is ever interrupted, and there
     is no overall timeout.
   - **Swap.** Stop the configured units, clear the marker, fast-forward,
     re-stamp with `write_source_stamp` (as `hermes update` does), start.
     A gateway drop-in (for example `97-restart-webui.conf`) may restart
     `hermes-webui.service` with the gateway; the deployer skips a second
     follower restart when that unit already came up with the gateway.
   - **Follower units (noop/hot).** After the checkout moves and
     `state.json` is updated, the worker immediately `systemctl restart`s
     each configured follower and checks `is-active` (no idle gate; that
     applies only to gateway restarts). A failed follower restart is logged
     and recorded in `state.json` but does not roll back the checkout. The
     hermes-webui process imports `run_agent` in-process and records git HEAD
     at boot; it must restart whenever HEAD changes without a gateway restart.
   - **Health, 120 s.** Every unit is `is-active`, `GET /health` returns
     `status: ok`, and every live gateway reports `code_sha == target`.
     Otherwise the deploy runs the idle gate again and resets to the previous
     SHA, restarts, and re-checks. The failed SHA is refused from then on.
5. **Self-update.** When a deploy changes the deployer's own files, the
   listener runs `self-test` from the new copy, then exits so systemd starts
   it. A changed `docs/ops/systemd/*.in` sets `reinstall_needed`. The drift
   check reports that until you re-run the installer.

The drift check (`hermes-deploy-drift.timer`, every 15 minutes) exits 1 and
logs `drift: ...` to the journal when any of these is true:

- the live checkout is not at the deployed SHA, or has modified or untracked
  files (paths only);
- a running gateway's `code_sha` is not the last restarted SHA;
- a deployed diff still needs a restart;
- the last deploy rolled back, failed, or needs manual work;
- a follower unit failed to restart on the last deploy;
- `origin/main` is ahead with nothing pending (a missed notify).

Before reporting, the drift job immediately restarts any follower whose
`ActiveEnterTimestamp` is earlier than the deployed commit time (covers a
manual `git pull` on the checkout while the tree still matches
`deployed_sha`). That remediation does not wait for gateway idle either.

The last report is in `~/.hermes/deploy/drift.json`. Check with
`systemctl status hermes-deploy-drift.service` or `systemctl --failed`.

State lives in `~/.hermes/deploy/`: `config.json`, `hmac.key` (0600),
`state.json`, `releases/`. To see where a deploy is, run
`python3 ~/.hermes/hermes-agent/docs/ops/rpi5_deploy.py status --home ~/.hermes`
or `journalctl -u hermes-deploy -f`. To retry a failed notify, re-run the
workflow job.

### One-time setup (Andy)

1. **Tailscale OAuth client.** In the admin console, go to Settings → OAuth
   clients → Generate. Use scope `auth_keys` (write) and tag `tag:ci`. Runner
   nodes are ephemeral and pre-authorized. (Instead, the action's `audience`
   input with Tailscale workload identity federation avoids a stored secret;
   swap `oauth-secret` for `audience` and add `id-token: write`.)
2. **Tailscale ACL.** Let `tag:ci` reach only the deploy port on the Pi. The
   Pi does not need to be tagged:

   ```jsonc
   "tagOwners": { "tag:ci": ["autogroup:admin"] },
   "hosts":     { "rpi5": "100.x.y.z" },
   "grants": [
     { "src": ["tag:ci"], "dst": ["rpi5"], "ip": ["tcp:8443"] }
   ]
   ```

   With legacy `acls`, use
   `{"action": "accept", "src": ["tag:ci"], "dst": ["rpi5:8443"]}`. Leave
   `tag:ci` out of every other rule. MagicDNS and HTTPS certificates must be
   on for `tailscale serve --https`.
3. **Install on the Pi.** Do this from the deployment checkout, at a revision
   that contains this tooling. Run it once, and again whenever
   `docs/ops/systemd/` changes:

   ```bash
   cd ~/.hermes/hermes-agent
   docs/ops/rpi5-vanilla-migration.sh --install-deploy-listener
   ```

   This creates `~/.hermes/deploy/` with a new `hmac.key`, a `config.json`,
   and `state.json` recording the current HEAD as deployed. It never
   overwrites them. It installs `hermes-deploy.service`,
   `hermes-deploy-drift.{service,timer}`, and
   `/etc/sudoers.d/hermes-deploy`, which allows `systemctl stop`/`start` of
   the configured system units and nothing else (validated with
   `visudo -cf`). Then it runs
   `tailscale serve --bg --https=8443 http://127.0.0.1:9180`. It refuses
   while a deploy is in flight.
4. **Check `~/.hermes/deploy/config.json`.** The defaults restart
   `hermes-gateway.service` (system) and check
   `http://127.0.0.1:8642/health`. **Follower units** restart on any
   checkout HEAD change when the gateway did not restart (noop/hot deploys),
   and are remediated by the drift timer when they booted before the
   deployed commit. If `follower_units` is omitted, the deployer uses
   `hermes-webui.service` when that unit is installed; set
   `"follower_units": []` to disable. Each entry matches `units` (for
   example `{"name": "hermes-webui.service", "scope": "system"}`). Use
   `"scope": "user"` for a `systemctl --user` unit. Set `health_url` to the
   API server's bind address if it does not listen on loopback. Re-run step
   3 after you add a system follower, so `restart` is allowed in sudoers.
   Other keys: `idle_settle_seconds`, `drain_confirm_seconds`,
   `health_timeout_seconds`, `port`, `https_port`.
5. **Repository secrets and variable** (Settings → Secrets and variables →
   Actions):

   | Name | Kind | Value |
   |---|---|---|
   | `TS_OAUTH_CLIENT_ID` | secret | OAuth client id from step 1 |
   | `TS_OAUTH_SECRET` | secret | OAuth client secret from step 1 |
   | `RPI5_DEPLOY_HMAC_KEY` | secret | contents of `~/.hermes/deploy/hmac.key` |
   | `RPI5_DEPLOY_URL` | variable | `https://rpi5.<tailnet>.ts.net:8443/deploy` |

   To set the key without printing it, from a machine with `gh`:

   ```bash
   ssh pi@rpi5 cat .hermes/deploy/hmac.key | gh secret set RPI5_DEPLOY_HMAC_KEY -R andyylin/hermes-agent
   ```

6. **Verify.** Run the `rpi5 deploy` workflow by hand (workflow_dispatch). It
   should print `200 {"result": "already deployed", ...}`. Then
   `systemctl start hermes-deploy-drift.service` should exit 0.

To rotate the key, delete `~/.hermes/deploy/hmac.key`, re-run step 3, and
update the secret.

### Not covered

- `config.yaml`, `.env`, `SOUL.md` and memories live in the profile homes,
  not in this repo, so a deploy never edits them. Prompt text is Python
  (`agent/prompt_builder.py`) and goes through the restart path. Reloading
  config without a restart would need gateway support that does not exist
  today.
- A dependency change (`pyproject.toml`, `uv.lock`, `pm/lock.json`) is left
  for the exceptional lane: `hermes pm install` plus a staged activation.
  The next push after that deploys normally. Run it as
  `/home/pi/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main pm install`
  so it syncs the pinned runtime venv (`pm.pinned_venv`), then check that
  interpreter before any restart:
  `/home/pi/.hermes/hermes-agent/venv/bin/python -I docs/ops/rpi5_deploy_probe.py --checkout /home/pi/.hermes/hermes-agent preflight`
  must print `"ok": true`. Every deploy runs the same check before it stops a
  unit.
- The web dashboard bundle (`hermes_cli/web_dist`) is gitignored, and
  `hermes dashboard` rebuilds it on launch. The rpi5 WebUI is the separate
  hermex app.
