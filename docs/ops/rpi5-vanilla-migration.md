# rpi5 vanilla migration

Move the Raspberry Pi 5 gateway (aarch64, Debian, Python 3.14.7) off the
externally managed venv and onto one multiplexed gateway using vanilla v0.22.0
behavior. This revision reverts the external-env guard and the
`multiplex_profiles: false` opt-out. The steps below match the code after those
reverts.

Do this on the deployment checkout, `~/.hermes/hermes-agent` (install key
`5935b2ebc06c58fa`). Update that checkout to this revision before any restart.
A different path hashes to a different install key and would commit a second,
unused environment.

Never paste command output. `hermes backup --quick` snapshots contain `.env`.
Gateway logs and `config.yaml` can contain tokens. Record only exit codes, unit
states, and the interpreter path the script prints.

## What vanilla does here

`gateway.multiplex_profiles` defaults on. An unset flag is a boot request:
`resolve_multiplex_mode` (`hermes_cli/gateway_multiplex_mode.py`) runs the same
preflight as `hermes gateway migrate --multiplex` and multiplexes only when that
preflight is clear. Explicit `true` skips the preflight and multiplexes.

Explicit `false` is retired. It parses, it is logged, and it is then resolved
like an unset key. With no blocker, the gateway multiplexes and rewrites the key
to `true` (`source` `retired-opt-out`), leaves
`~/.hermes/.multiplex_opt_out_rewritten`, and prints the boxed notice in
`RETIRED_OPT_OUT_REASON`. `GATEWAY_MULTIPLEX_PROFILES=false` is not an opt-out
either (`explicit_multiplex_flag` reads it, then the same resolver ignores a
false result).

The only per-profile opt-out that still works is `gateway.standalone: true` in
that named profile's `config.yaml`. It is a temporary shim. It is ignored on
the default profile (`hermes_cli/profiles.py` `profile_is_standalone`). A
standalone profile is excluded from `profiles_to_serve` even when the host
gateway's multiplex flag is true.

`hermes pm install` will commit `installs/<key>/facts.json` from whatever
interpreter launches it. The deleted guard used to refuse that on an external
interpreter. Vanilla `Venv.apply` (`pm/packages.py`) still runs `environment.check()`
(`uv pip check`) and deletes the generation on failure, and
`_commit_selection` (`pm/install.py`) calls `facts.record_state` only after
`apply` returns. Startup import probes (`pm.recovery.validate_environment`) run
on `hermes pm repair`, which needs a selection that already exists. The script
below supplies the missing import gate before the first `facts.json` write.

PM's pinned interpreter for linux-arm64 glibc is CPython 3.14.7
(`pm/lock.json`). The committed environment uses that standalone interpreter.
It is the same Python version as the external venv, and it is not that venv.

## Current state

| Item | Value |
|---|---|
| External venv | `~/.hermes/venvs/hermes-py314-v0.21.7` (`VIRTUAL_ENV`) |
| Code | `PYTHONPATH=~/.hermes/hermes-agent` |
| Default unit | `hermes-gateway` |
| Profile units | `hermes-gateway-wife`, `hermes-gateway-dad`, `hermes-gateway-katie`, `hermes-gateway-alice` |
| Profile flag | `gateway.standalone: true` on wife, dad, katie, alice |
| WebUI | a unit whose drop-in is `98-py314-v0217.conf` |
| Runtime facts | `~/.hermes/installs/5935b2ebc06c58fa/facts.json` moved aside |
| Orphan generations | four directories under that install's `environments/`, mtimes 2026-10-15 12:22–12:41, including prefixes `cc8eed33` and `f098ffea` |

`get_service_name` (`hermes_cli/gateway.py`) is `hermes-gateway` for the default
home and `hermes-gateway-<profile>` otherwise. Confirm the four names with
`systemctl --user list-units 'hermes-gateway*'`. If they are absent, check the
system manager (`systemctl list-units 'hermes-gateway*'`). Do not guess a scope.

The dashboard unit the code installs is `hermes-dashboard.service`
(`hermes_cli/main_dashboard.py`), default port 9119. There is no
`hermes dashboard install` that rewrites an existing unit. Find the WebUI unit
by the drop-in name, not by assuming the code's name:

```bash
systemctl --user list-units --all --plain 'hermes-*'
find "${XDG_CONFIG_HOME:-$HOME/.config}/systemd" /etc/systemd/system \
  -path '*98-py314-v0217.conf' 2>/dev/null
```

## Pre-flight

1. Confirm the checkout is this revision (`pm/deployment_guard.py` is absent,
   and `hermes_cli/gateway_multiplex_mode.py` contains `RETIRED_OPT_OUT_REASON`
   and does not contain `CONFIG_OPT_OUT_REASON`).
2. Idle check, each profile, before stopping anything:

   ```bash
   hermes gateway status
   hermes -p wife gateway status
   hermes -p dad gateway status
   hermes -p katie gateway status
   hermes -p alice gateway status
   ```

   Wait until no profile has a running agent turn. Cron fires and in-flight
   tool calls count as busy.
3. Quick snapshots. These include `.env`. Do not open them on a shared screen
   and do not pass `--keep` in a way that prunes the copies you just made
   (default keep is 20). Run once per home so each profile is covered; a single
   default-home snapshot does not include `profiles/<name>/`:

   ```bash
   hermes backup --quick
   hermes -p wife backup --quick
   hermes -p dad backup --quick
   hermes -p katie backup --quick
   hermes -p alice backup --quick
   ```

   `hermes backup` without `--quick` excludes `installs/` and `tools/`
   (`PM_RUNTIME_ROOT_DIRS` in `hermes_cli/home_data_layout.py`) and any directory
   named `venv` or `.venv`. It does not exclude `venvs/`, so a full zip walks
   the external environment. Use `--quick` plus the unit copies below, and
   leave `~/.hermes/venvs/hermes-py314-v0.21.7` on disk as the rollback
   interpreter. Quick snapshots also omit `memories/`. Copy each profile's
   `memories/` directory aside if you want that in the rollback bundle. The
   migration itself does not rewrite memory files.
4. Copy every gateway and WebUI unit and drop-in, user and system scope, to a
   directory outside `~/.hermes` (for example `~/rpi5-unit-backup/`). Include
   `98-py314-v0217.conf`.
5. Leave the moved-aside `facts.json` where it is. Do not move it back.
6. Read blockers only:

   ```bash
   hermes gateway migrate --multiplex --dry-run
   ```

   Dry-run returns before any service action (`hermes_cli/gateway_migrate.py`).
   Do not run migrate without `--dry-run`. Apply uninstalls secondary units
   (`action` `uninstall`), which removes boot enablement. This migration
   disables units and keeps the files so rollback can enable them again.

   Fix anything the dry-run names before the cutover. Duplicate bot credentials
   are reported as profile names and key names, never values
   (`duplicate_credential_lines`). A port-binding platform (LINE, SMS, webhooks)
   that has no `/p/<profile>/` ingress is a blocker. The dry-run prints the
   multiplexed callback URLs. Update the vendor callbacks to those URLs as part
   of cutover, and keep the old URLs written down for rollback. Do not copy the
   dry-run output into a ticket if it contains a token.

## Environment

Extras the profiles need, and how this tree packages them (`pyproject.toml`,
`pm/extras.py` `ANCHORS`):

| Need | Where it comes from |
|---|---|
| telegram | extra `telegram` (`python-telegram-bot`; anchor `telegram`) |
| discord | extra `discord` (`discord.py`; anchor `discord`) |
| matrix, encryption, olm | extra `matrix` (`mautrix[encryption]` pulls `python-olm`). Anchors are `mautrix`, `asyncpg`, `aiosqlite`, `markdown`, `aiohttp_socks`. `olm` is not an anchor; the script imports it separately |
| mattermost, line | `aiohttp` only (no extra of their own). `all` and `matrix` both pin `aiohttp` |
| email | stdlib `imaplib` / `smtplib` |
| sms | extra `sms` is inside `all` (`aiohttp`). The adapter uses aiohttp REST, not the Twilio SDK |
| WebUI / API | extra `web` (`fastapi`) is inside `all` |
| MCP Python client | extra `mcp` is inside `all` |
| langfuse | not an extra. Catalog plugin, installed only when a profile selects it |
| gbrain and other bun/stdio MCP servers | stay outside the venv. Do not pip-install them |

`all` does not include `telegram`, `discord`, or `matrix`. `messaging` is
redundant once those three plus `all` are requested. On a fresh selection,
`hermes pm install --extra telegram` alone records only `telegram`
(`pm/cli.py` `_install_python_environments` calls `sync_venv(extras or ["all"])`).
Pass all four.

Langfuse is in use only when that profile's `plugins.enabled` contains
`langfuse` (or the bundled `observability/langfuse` id). Keys in `.env` alone
are not use (`hermes_cli/left_core_migration.py` `langfuse_in_use`). Check
before installing:

```bash
hermes config get plugins.enabled
hermes -p wife config get plugins.enabled
hermes -p dad config get plugins.enabled
hermes -p katie config get plugins.enabled
hermes -p alice config get plugins.enabled
```

The script does not install langfuse. If a profile lists it, install after the
commit, once per profile that lists it:

```bash
hermes -p <name> plugins install langfuse
```

That official command may sync plugin dependencies into a new generation and
rewrite `facts.json`. Re-run the script with `--check` afterward.

From the deployment checkout, with this revision checked out and the external
venv not activated:

```bash
chmod +x docs/ops/rpi5-vanilla-migration.sh
docs/ops/rpi5-vanilla-migration.sh
```

The script refuses a checkout that still has PR 46 or PR 47, and it refuses an
install key other than `5935b2ebc06c58fa`. It unsets `VIRTUAL_ENV` and
`PYTHONHOME` and drops `~/.hermes/venvs/*/bin` from `PATH`. It drives
`python -m pm.build_env` with PM's pinned toolchain (`--python` is not passed).
The throwaway lives under `mktemp` and is deleted before `hermes pm install`,
whether the import gate passes or fails. A failed gate leaves `facts.json`
absent. Exit 4 means the driver interpreter or
`~/.hermes/hermes-agent/.hermes/bin/hermes` is missing. Do not substitute the
external venv's `hermes` on `PATH`. Publish the store interpreter with the
driver (`PYTHONPATH` pointed at the checkout, `python -m hermes_cli.main pm
install python`) and re-run the script.

Then the official launcher (`~/.hermes/hermes-agent/.hermes/bin/hermes`) runs:

```bash
hermes pm install --extra all --extra telegram --extra discord --extra matrix
```

Two builds are intentional. The first never writes runtime `facts.json`. The
second is the only commit, and it pip-checks before `record_state`.

Re-check without building:

```bash
docs/ops/rpi5-vanilla-migration.sh --check
```

`--check` exits 0 when the committed interpreter imports the modules, 1 when
`facts.json` is still absent, and 2 when a selection exists but imports fail.
Exit 2 does not rebuild. Do not delete `facts.json` to force a retry until you
have read the failure; a bad committed tree should be moved aside (same as the
previous aside), not overwritten by hand.

Confirm the printed interpreter path is under
`~/.hermes/installs/5935b2ebc06c58fa/environments/<32-hex>/venv/bin/python` and
that `<32-hex>` is not `cc8eed33…`, `f098ffea…`, or either of the other two
directories from the 12:22–12:41 window.

The old gateways keep running on the external venv until they are restarted.
Building this environment does not switch them.

## One gateway

Do these in order. Explicit `true` skips the preflight, so the four profile
processes must already be stopped before the flag is set. Stopping them does
not make the already-running default process re-read config.

1. Disable and stop the four profile units. Do not disable `hermes-gateway`.
   Do not delete unit files.

   ```bash
   systemctl --user disable --now hermes-gateway-wife.service
   systemctl --user disable --now hermes-gateway-dad.service
   systemctl --user disable --now hermes-gateway-katie.service
   systemctl --user disable --now hermes-gateway-alice.service
   ```

   Use `sudo systemctl disable --now` instead when the units are on the system
   manager. `disable --now` removes boot enablement and stops the process. The
   unit file remains, which is what rollback enables.

2. Clear the shim on the four named profiles. Do not set `gateway.standalone`
   on the default profile; it is ignored there.

   ```bash
   hermes -p wife config unset gateway.standalone
   hermes -p dad config unset gateway.standalone
   hermes -p katie config unset gateway.standalone
   hermes -p alice config unset gateway.standalone
   ```

3. On the default profile, set the flag explicitly so the next boot does not
   depend on the retired false value:

   ```bash
   hermes config set gateway.multiplex_profiles true
   ```

   Do not set it to `false`. After this revision a false value is rewritten to
   `true` on the next gateway boot when nothing blocks multiplex.

4. Move the WebUI drop-in aside (rename, do not delete), then
   `systemctl --user daemon-reload` (or `sudo systemctl daemon-reload` for a
   system unit). The published launcher (`hermes_cli/_launchers.py`
   `_launcher_script`, run as store Python with `-I`) pops `PYTHONHOME` and
   `PYTHONPATH`. `-I` ignores every `PYTHON*` variable. It does not pop
   `VIRTUAL_ENV`, and it does not need to: CPython selects a venv from the
   interpreter's `pyvenv.cfg`, not from `VIRTUAL_ENV`, and this interpreter is
   PM's store Python. Dependency selection then follows the committed
   generation. A drop-in that sets `VIRTUAL_ENV` or `PYTHONPATH` cannot pin the
   external venv once `ExecStart` is that launcher. A unit whose `ExecStart`
   is the venv's `python`, or a `PATH` lookup of `hermes`, still honors the
   drop-in. Point that `ExecStart` at:

   ```text
   /home/<account>/.hermes/hermes-agent/.hermes/bin/hermes dashboard
   ```

   Keep a copy of the previous `ExecStart` in the unit backup. The code's unit
   name is `hermes-dashboard.service`. There is no installer that regenerates
   it; edit the unit you found by the drop-in name. Do not restart the WebUI
   until the gateway restart below has succeeded.

5. Refresh only the default gateway unit, after `facts.json` exists:

   ```bash
   hermes gateway install
   ```

   This rewrites `hermes-gateway.service` when it does not match
   `generate_systemd_unit` and enables it. It does not restart a running
   process. Do not run `hermes gateway install` under `-p wife` (or dad, katie,
   alice). That writes or enables the profile unit this migration just disabled.

6. Restart the default gateway, then the WebUI:

   ```bash
   hermes gateway restart
   systemctl --user restart <webui-unit>.service
   ```

   `hermes gateway restart` is the official restart. It is the moment the
   default process leaves the external venv.

### Restart notices

`GatewayRunner._notify_active_sessions_of_shutdown`
(`gateway/run_shutdown.py`) sends each active chat through that session's own
profile adapter (`_delivery_adapter_for`, then `_authorization_adapter`). It
does not fall back to the default bot. Home channels are iterated from
`_served_home_channel_configs` and sent through that profile's adapter. The
dedup key includes the profile for Telegram private chats. No connected adapter
means no notice.

Stopping the four profile units, while each still serves only itself, sends
that profile's own shutdown and home-channel notices. The later restart of the
one `hermes-gateway` notifies every served profile through its own bot.

Idle the agents first so active-chat notices are absent. Home-channel notices
still go out unless a drain marker sets `suppress_notification=true`
(`drain_notification_suppressed`). Expect one home-channel notice per profile
that has a home channel configured, from that profile's bot, when its unit
stops, and another set when the multiplexed gateway later restarts.

## Verification

1. Script: `docs/ops/rpi5-vanilla-migration.sh --check` exits 0.
2. The gateway process image is the committed interpreter, not
   `~/.hermes/venvs/hermes-py314-v0.21.7/...`. Compare `/proc/<pid>/exe` only.
   Do not dump `/proc/<pid>/environ`.
3. `hermes gateway status` reports one gateway serving default, wife, dad,
   katie, and alice. Adapters each profile had before the cutover show
   connected. A profile with no adapter process of its own is expected: the
   host gateway holds the connections.
4. API health (this is the API server, `gateway/platforms/api_server.py`
   `DEFAULT_PORT` 8642, not the dashboard on 9119):

   ```bash
   curl -fsS http://100.64.0.0:8642/health
   ```

   `GET /health` returns a small status payload. Do not probe
   `/health/detailed` for this check; that route is for authenticated dashboard
   probing and can expose more state.
5. Cron, default and each named profile:

   ```bash
   hermes cron status
   hermes -p wife cron status
   hermes -p dad cron status
   hermes -p katie cron status
   hermes -p alice cron status
   ```

   Jobs stay in each profile's `cron/jobs.json`. The multiplexed gateway is the
   process that fires them.
6. LINE and SMS callbacks, if those platforms are enabled, hit the
   `/p/<profile>/...` URLs the dry-run printed.
7. GBrain: send one read and one write on each profile and confirm the slug
   lands only in that profile's bound prefix. See the isolation section for why
   identical MCP stanzas would fail this.

## Isolation

Multiplex is one process and five homes. Scope is a contextvar, not a copy of
the environment.

Credentials and home. `GatewayRunner._profile_runtime_scope` (`gateway/run.py`)
calls `set_hermes_home_override` and `set_secret_scope` for the turn.
`set_secret_scope` makes that profile's `.env` the credential source and does
not write `os.environ`. `os.environ` stays the launch profile for the life of
the process. Under an installed multiplex scope, `get_scoped_secret` and
`platform_gate_env` fail closed: they do not borrow `os.environ`
(`agent/secret_scope.py`). A profile that lacks a key gets nothing, not another
profile's value.

`state.db`. `hermes_state._default_db_path` calls `get_hermes_home()` at call
time. The default profile uses the launch home's `state.db`. A named profile
uses `profiles/<name>/state.db`. Session keys are `agent:<profile>:...`; the
default profile stays `agent:main:...` (`gateway/session.py`).

Memory. `MemoryManager.initialize` injects `hermes_home` from `get_hermes_home()`
(`agent/memory_manager.py`). `MEMORY.md` and `USER.md` live under
`get_hermes_home()/memories`. Eviction and flush must run inside the owning
profile's scope. An unbound flush writes the default profile's memory store.

MCP connections are per owner, and they can still be shared. Under multiplex,
ledger keys are `(owner_scope, name)` (`tools/mcp_tool_scope.py`). Sharing is
opt-in in `_register_connected_into_current_scope`
(`tools/mcp_tool_registration.py`). A later profile adopts an already-connected
server only when all of the following hold:

- `_connection_identity` matches: `config_fingerprint` plus `env`, `headers`,
  `auth`, client cert, client key, `ssl_verify`, and `strict_redirect_headers`.
  `config_fingerprint` (`tools/mcp_schema_cache.py`) hashes `command`, `args`,
  `url`, `transport`, and tool include/exclude. It deliberately excludes
  credentials so the schema cache survives a token rotation. The identity tuple
  puts credentials back.
- Auth type is not `oauth`. OAuth tokens stay in the owner's store and are
  never reused (`_same_server_route`).
- The adopter's resolved identity digest equals the digest stored when the
  owner connected. `_connect_inputs` (`tools/mcp_tool_transport.py`) resolves,
  in the current profile's scope, stdio `[command, env, cwd]` or HTTP
  `[url, headers]` after the live endpoint and identity header. A resolver
  failure returns no digest, and adoption is refused for that server only.

If any of those differ, the profile opens its own connection. The stdio child
environment is `_build_safe_env` (`tools/mcp_tool_config.py`): a safe baseline
from `os.environ`, plus secret-source values from the active profile's secret
scope (`get_secret`), plus that server's own `env`. A profile that does not
have the secret gets nothing. It does not receive another profile's token.
Trust is recorded per scope (`_record_scope_trust`).

Hermes does not special-case GBrain. Andy's rule (each profile reads and writes
only its own GBrain) holds only when the stanzas do not compare equal.

A shared gbrain is the bug to prevent. If two profiles' `mcp_servers.gbrain`
stanzas match on command, args, env, headers, and the resolved cwd, both adopt
one stdio process. Both then read and write whatever `--bound-slug-prefixes`
that one process was started with. The fingerprint includes `args`, so a
distinct `--bound-slug-prefixes` value per profile makes the identity differ
and forces a separate bun process. Do not point two profiles at one
`gbrain serve --http` URL with the same headers. A different `env` block also
splits identity, but the bound prefix is the control that matches the rule.
`bun` stays outside the PM venv; nothing in this migration installs it.

Check each profile's gbrain args before restarting into multiplex. The five
stanzas must not be byte-identical, and each `--bound-slug-prefixes` value must
name only that profile's slugs.

## Orphan generations

Do this only after verification succeeds.

The four leftovers are generation directories under
`~/.hermes/installs/5935b2ebc06c58fa/environments/`, not other install keys.
Names are `uuid4().hex`. Identify all four by mtime in 2026-10-15 12:22–12:41.
Two of the prefixes are `cc8eed33` and `f098ffea`. Do not delete by prefix
guess; list the directory and match mtimes.

`hermes pm gc` calls `collect_generations` (`hermes_cli/runtime_state.py`). A
generation is removed only when all of these are true: it is not the selected
generation (the parent of the `environment` path in the new `facts.json`), it
contains `.lease-managed`, that marker is at least 86400 seconds old, no live
lease lock is held, and this process holds the runtime lock. Same-day orphans
from 12:22–12:41 are kept until the marker is a day old. That is the safe
behavior. Do not `rm -rf` them.

```bash
hermes pm gc
```

Expect a line that removed 0 dependency generations if you run it the same day.
Run `hermes pm gc` again after 24 hours. Before and after, confirm the
`environment` path in `facts.json` is not one of those four directories. `gc`
also sweeps unreferenced tool-store entries and install-state dirs of deleted
checkouts (`collect_orphan_install_states`). It will not remove
`5935b2ebc06c58fa` while this checkout is the live tree.

## Rollback

Rollback returns to the external venv and the five separate units. It does not
depend on `gateway.multiplex_profiles: false`. That value is ignored and gets
rewritten to `true`. `gateway.standalone: true` on wife, dad, katie, and alice
is what keeps those profiles off the host gateway.

1. Stop the multiplexed gateway and the WebUI so they release tokens and ports:

   ```bash
   hermes gateway stop
   systemctl --user stop <webui-unit>.service
   ```

2. Put the shim back. This is the opt-out that still works:

   ```bash
   hermes -p wife config set gateway.standalone true
   hermes -p dad config set gateway.standalone true
   hermes -p katie config set gateway.standalone true
   hermes -p alice config set gateway.standalone true
   ```

3. Move the new `~/.hermes/installs/5935b2ebc06c58fa/facts.json` aside under a
   new name. Do not restore an older committed selection over it. With no
   runtime facts, the official launcher will not select the PM generation.
   The external venv is what the restored unit files invoke.

4. Restore the backed-up unit files and `98-py314-v0217.conf` over the copies
   `hermes gateway install` rewrote. Restoring only the drop-in is not enough:
   the main unit's `ExecStart` is now the store-python launcher, which ignores
   `PYTHONPATH` and does not run the external venv. The pre-migration
   `ExecStart` has to come back with the drop-in.

5. `systemctl --user daemon-reload` (and the system manager, if you restored
   system units).

6. Enable and start the four profile units, then the default gateway and the
   WebUI:

   ```bash
   systemctl --user enable --now hermes-gateway-wife.service
   systemctl --user enable --now hermes-gateway-dad.service
   systemctl --user enable --now hermes-gateway-katie.service
   systemctl --user enable --now hermes-gateway-alice.service
   hermes gateway start
   systemctl --user start <webui-unit>.service
   ```

7. Restore vendor callbacks to the pre-multiplex URLs if you changed them.
8. Confirm each profile's own gateway is connected and
   `curl -fsS http://100.64.0.0:8642/health` succeeds on the process that
   owned that port before. Confirm `VIRTUAL_ENV` is not required reading: the
   process image should be the external venv interpreter again.

Do not run `hermes gateway migrate --multiplex` in either direction. Apply
uninstalls the profile units. Disable and enable preserve them.
