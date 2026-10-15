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
| Profile flag | `gateway.standalone: true` is already set on katie and alice only. Wife and dad do not have it |
| Default drop-ins | `hermes-gateway.service.d/`: `zzz-main-v0217.conf`, `zz-py314-runtime.conf`, `92-canonical-runtime.conf` (old venv Python), `memory-limits.conf` (2G cap) |
| Kid credentials | Katie and Alice: unit `ExecStartPre` runs `render_kid_matrix_env.py` (Bitwarden) into a runtime env file only that unit loads. That renderer is retired. Matrix keys move to `secrets.bitwarden` in each kid's `config.yaml`, pointed at the shared Hermes - Aoba project with `key_prefix` `KATIE_MATRIX_` / `ALICE_MATRIX_` and `strip_prefix: true`. Each kid's `.env` gets dad's `BWS_ACCESS_TOKEN` and nothing else |
| Alice MCP | `game-workshop` (`alice_game_workshop.py`) reads `ALICE_GAMES_ROOT` (no fallback) and `ALICE_PROFILE_ROOT` from its own process env. Plain `.env` values are not passed to MCP servers |
| Wife and dad | profile `.env` has no bot tokens. They already use a Bitwarden secret source in their own `config.yaml` plus `BWS_ACCESS_TOKEN` in that profile's `.env`. Wife and dad units also have `disable-kanban.conf` |
| Kid unit extras | Katie and Alice prepend `~/.hermes/profiles/<kid>/bin` (tirith) to `PATH`, set their own memory limits, and set `HERMES_KANBAN_DISPATCH_IN_GATEWAY=false` |
| WebUI | separate `hermes-webui` app, `ExecStart` `hermex-webui-start.sh`, not `hermes dashboard`. Drop-in `98-py314-v0217.conf` may pin an interpreter |
| SMS | enabled on the default profile, port-binding, currently failing (`SMS_WEBHOOK_URL` unset) |
| Host memory | 16 GB RAM, about 9 GB free. Five gateways plus gbrain use about 3.1 GB RSS |
| Runtime facts | `~/.hermes/installs/5935b2ebc06c58fa/facts.json` moved aside |
| Generations | directories under that install's `environments/`. Do not treat the 12:22–12:41 window as the orphan list. `cee636e2` is inside that window and stays when the new `facts.json` points at it |

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
4. Copy every gateway and WebUI unit and drop-in, user and system scope, to
   `~/rpi5-unit-backup/` (outside `~/.hermes`). Include `98-py314-v0217.conf`
   and the four default-unit drop-ins named above. `--sideline-dropins` later
   moves those four into `~/rpi5-unit-backup/gateway-dropins/`; the copy here
   is the one you can put back if that directory is wrong.
5. Leave the moved-aside `facts.json` where it is. Do not move it back.
6. Inventory runtime state, then dry-run. Only katie and alice have
   `gateway.standalone: true`. Wife and dad do not, so this first pass must
   already include them:

   ```bash
   docs/ops/rpi5-vanilla-migration.sh --preflight
   ```

   The script greps hermes and hermex unit files and drop-ins under the user
   and system unit directories for `EnvironmentFile=`, `ExecStartPre=`, and
   `Environment=`. It prints paths, `ExecStartPre` programs, and `Environment=`
   names. It does not print values. Save that text; it is the map for the
   credential section. Katie and Alice should show `render_kid_matrix_env.py`
   and an `EnvironmentFile`. Alice should show the names `ALICE_GAMES_ROOT`
   and `ALICE_PROFILE_ROOT` (the values stay in the unit until
   `--alice-mcp-env`). Katie and Alice should show a profile `bin` on `PATH`,
   their `MemoryHigh` / `MemoryMax`, and `HERMES_KANBAN_DISPATCH_IN_GATEWAY`.
   Wife and dad should show `disable-kanban.conf`. The plan tells you to leave
   their Bitwarden source in place. It does not tell you to render unit files
   into `.env`.

   It then runs `hermes gateway migrate --multiplex --dry-run` and exits
   non-zero on blockers. Dry-run itself returns before any service action and
   exits 0 even when the plan lists blockers (`cmd_migrate` in
   `hermes_cli/gateway_migrate.py`). The script reads the plan. The raw plan
   stays in a mode-0600 temp file and is deleted. Do not run migrate without
   `--dry-run`. Apply uninstalls secondary units (`action` `uninstall`), which
   removes boot enablement. This migration disables units and keeps the files
   so rollback can enable them again.

   While `gateway.standalone: true` is set, `profiles_to_serve` omits that
   profile. On this pass the dry-run should list katie and alice as "left
   alone" and should already show wife and dad. The script aborts if wife or
   dad are left alone, and if the plan says only one profile exists. A clean
   exit here is not a clean bill of health for katie and alice: their
   credentials and port-binders are not in the plan yet.

   Fix anything the dry-run names before the cutover. Duplicate bot credentials
   are reported as profile names and key names, never values
   (`duplicate_credential_lines`). A secondary port-binder that does not
   declare `serves_profile_prefix` is a blocker: the multiplexer would skip
   that whole profile (`_check_secondary_port_binders`). The dry-run prints
   multiplexed callback URLs for binders that do have the prefix. Update the
   vendor callbacks to those URLs as part of cutover, and keep the old URLs
   written down for rollback. Do not copy the dry-run output into a ticket if
   it contains a token.

   SMS on the default profile is not that blocker. `sms` is in
   `PORT_BINDING_PLATFORM_VALUES` (`gateway/config.py`) and
   `SmsAdapter.serves_profile_prefix` is true
   (`plugins/platforms/sms/adapter.py`), so a secondary that enables SMS is a
   notice (callback moves to `/p/<profile>/webhooks/twilio` on the default
   listener), not a skip-the-profile blocker. The check does not look at the
   default profile's own SMS; the default profile is the one that binds.
   `connect()` returns false before `bind_listener` when `SMS_WEBHOOK_URL` is
   unset and `SMS_INSECURE_NO_SIGNATURE` is not true (`sms_missing_webhook_url`,
   not retryable). That is the current failure. It does not refuse multiplex.
   It does leave a fatal adapter in the shared process. Disable it on the
   default profile before the fold pass, and disable any platform the fold
   pass names as a blocker:

   ```bash
   hermes config set platforms.sms.enabled false
   ```

   Use `hermes -p <name> config set platforms.<platform>.enabled false` when
   the blocker names a secondary. Re-run the fold pass after each disable.
   Do not add an ingress in this migration.

7. After the four profile units are stopped and `gateway.standalone` is unset
   on katie and alice only (both are in [One gateway](#one-gateway)), run the
   fold pass and do not continue while it exits non-zero:

   ```bash
   docs/ops/rpi5-vanilla-migration.sh --preflight-fold
   ```

   This aborts if wife, dad, katie, or alice are still listed as left alone,
   if the plan says only one profile exists, or if there are blockers. Wife
   and dad were never standalone; katie and alice must have had the key
   removed before this pass. Explicit
   `gateway.multiplex_profiles: true` skips this preflight at boot
   (`resolve_multiplex_mode`), so this command is the last check that still
   runs it.

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
| WebUI / API | extra `web` (`fastapi`, `uvicorn`) is inside `all`. The store-interpreter re-exec activates that environment before `hermex-webui-start.sh` runs. Do not set `HERMES_DISABLE_LAZY_INSTALLS` |
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
This Pi has `gcc` and `g++` and does not have `clang++`. Native extras
(`python-olm`) probe `clang++` unless `CC` and `CXX` are set, so the script
exports `CC=gcc` and `CXX=g++` for both that throwaway build and
`hermes pm install`. The throwaway lives under `mktemp` and is deleted before
`hermes pm install`,
whether the import gate passes or fails. A failed gate leaves `facts.json`
absent. Exit 4 means the driver interpreter or
`~/.hermes/hermes-agent/.hermes/bin/hermes` is missing. Do not substitute the
external venv's `hermes` on `PATH`.

Install the store interpreter when it is absent:

```bash
PYTHONPATH="$HOME/.hermes/hermes-agent" python -m hermes_cli.main pm install python
```

`pm install python` calls `publish_launchers`. That function leaves a tree
whose `install-stamp.json` says `updateMechanism: external` alone, which is
how the old external venv left this checkout. The command can succeed while
`.hermes/bin/hermes` is still missing. Apply and `--gc` then run
`claim-managed` on that checkout. The claim runs only when the stamp says
`external`, the tree is a git checkout with `pyproject.toml`, and PM's store
interpreter is already installed. It copies the stamp to
`install-stamp.json.external-backup`, rewrites it with `write_source_stamp`
(the stamp a fresh self-managed checkout publishes: `updateMechanism: self`,
`source: git`), calls `publish_launchers` with `create=False`, and requires
`.hermes/bin/hermes` to be executable. A failed publish puts the backup back.
`--rollback` restores that backup and removes a launcher the claim created.

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

1. Edit profile config while the four units are still up, so a failed
   Bitwarden check does not take the bots down. The authoritative list is:

   ```bash
   docs/ops/rpi5-vanilla-migration.sh --config-edits
   ```

   One process, five scopes. `GatewayRunner._profile_runtime_scope`
   (`gateway/run.py`) builds each scope with `build_profile_secret_scope`
   (`agent/secret_scope.py`): that profile's `.env`, then
   `hydrate_profile_secret_sources` (Bitwarden `bws secret list` for
   `secrets.bitwarden.project_id`, seeded from that profile's `.env`), then
   the managed env (`/etc/hermes/.env`) last. Managed env is the same map for
   every profile. With a scope installed, `get_secret` does not read
   `os.environ` on a miss. Do not put another profile's tokens on
   `hermes-gateway.service`.

   Wife and dad already have this Bitwarden shape. Do not change their
   `config.yaml`. Do not set `key_prefix` on dad or wife. With no prefix, a
   bulk source injects every secret in the project (`BitwardenSource.shape`
   is `bulk`), including `KATIE_MATRIX_*` and `ALICE_MATRIX_*` under those
   names. The Matrix adapter reads `MATRIX_*`, so dad does not gain a Matrix
   adapter from those keys. `TELEGRAM_BOT_TOKEN` in the same project stays
   dad's.

   Katie and Alice share that project. The secrets are already stored as
   `KATIE_MATRIX_*` and `ALICE_MATRIX_*` (13 each) in **Hermes - Aoba**
   (`9d36a60d-2e7e-41c0-a90f-b47b0036e9c2`). Do not create a new project.
   `key_prefix` loads only keys that start with the prefix.
   `strip_prefix: true` drops the leading owner segment and keeps the final
   segment, so `KATIE_MATRIX_` / `ALICE_MATRIX_` export as `MATRIX_*`.
   `TELEGRAM_BOT_TOKEN` does not start with either prefix and is not loaded.
   Retire `render_kid_matrix_env.py`. Do not merge its output into `.env`,
   and do not add its `ExecStartPre` to the shared unit.

   ```bash
   docs/ops/rpi5-vanilla-migration.sh --renderer-keys /path/to/render_kid_matrix_env.py
   ```

   That prints `MATRIX_*` names and nothing else. Confirm them against the
   script on the host (the left-hand side it writes). Do not paste values.
   `BWS_ACCESS_TOKEN` stays out of the project; Hermes skips it even if it
   is stored there.

   Copy the `secrets.bitwarden` keys wife or dad already set (`enabled`,
   `access_token_env`, `server_url`, and the rest of that block), point both
   kids at Hermes - Aoba, and set each kid's prefix:

   ```bash
   hermes -p katie config set secrets.bitwarden.enabled true
   hermes -p katie config set secrets.bitwarden.access_token_env BWS_ACCESS_TOKEN
   hermes -p katie config set secrets.bitwarden.project_id 9d36a60d-2e7e-41c0-a90f-b47b0036e9c2
   hermes -p katie config set secrets.bitwarden.key_prefix KATIE_MATRIX_
   hermes -p katie config set secrets.bitwarden.strip_prefix true
   hermes -p alice config set secrets.bitwarden.enabled true
   hermes -p alice config set secrets.bitwarden.access_token_env BWS_ACCESS_TOKEN
   hermes -p alice config set secrets.bitwarden.project_id 9d36a60d-2e7e-41c0-a90f-b47b0036e9c2
   hermes -p alice config set secrets.bitwarden.key_prefix ALICE_MATRIX_
   hermes -p alice config set secrets.bitwarden.strip_prefix true
   ```

   Set `secrets.bitwarden.server_url` only when wife or dad set one. Empty
   means US Cloud. Copy dad's `BWS_ACCESS_TOKEN` line into each kid's
   profile `.env`. Do not run `hermes secrets bitwarden token` (that would
   mint a new token). Do not print the line, and do not pass the token on a
   command line:

   ```bash
   python3 - <<'PY'
   from pathlib import Path
   root = Path.home() / ".hermes"
   dad = (root / "profiles" / "dad" / ".env").read_text(encoding="utf-8")
   line = next(item for item in dad.splitlines() if item.startswith("BWS_ACCESS_TOKEN="))
   for kid in ("katie", "alice"):
       path = root / "profiles" / kid / ".env"
       text = path.read_text(encoding="utf-8") if path.exists() else ""
       kept = [item for item in text.splitlines() if not item.startswith("BWS_ACCESS_TOKEN=")]
       kept.append(line)
       path.write_text("\n".join(kept) + "\n", encoding="utf-8")
       print(f"{kid}: BWS_ACCESS_TOKEN copied")
   PY
   ```

   The profile `.env` gets `BWS_ACCESS_TOKEN` only. Do not write `MATRIX_*`
   values into it.

   Alice's MCP server is separate from that scope. `alice_game_workshop.py`
   reads `ALICE_GAMES_ROOT` (no fallback) and `ALICE_PROFILE_ROOT` from its
   own environment. `_build_safe_env` (`tools/mcp_tool_config.py`) passes the
   safe baseline, secret-source names, and the server block's own `env`. It
   does not pass plain `.env` entries. Putting those two paths in Alice's
   `.env` does not start the server. Write them on
   `mcp_servers.game-workshop.env`, taking both values from her unit. The
   recorded live games root is `/srv/alice-portal/games`. The profile root is
   not a constant.

   ```bash
   docs/ops/rpi5-vanilla-migration.sh --alice-mcp-env
   docs/ops/rpi5-vanilla-migration.sh --alice-mcp-env --write
   ```

   The first command prints the two paths and does not write. Confirm
   `ALICE_GAMES_ROOT` is `/srv/alice-portal/games` and
   `ALICE_PROFILE_ROOT` is the path her unit sets. `--write` refuses when the
   games root differs, and it refuses when `game-workshop` is missing. It
   does not print other `Environment=` values.

   Per-unit settings that do not become `config.yaml` keys:

   - PATH. Katie and Alice prepend `~/.hermes/profiles/<kid>/bin` (tirith).
     The terminal tool already appends `<profile home>/bin` when that
     directory exists (`_managed_runtime_path_entries` in
     `tools/environments/local.py`), and under multiplex that home is the
     served profile. There is no config key to set. Do not put either bin on
     the shared unit `PATH`. MCP servers do not receive it. Core no longer
     calls tirith (`security.tirith_*` was removed). A shell command in that
     profile's terminal session still finds the binary.
   - Memory. Katie and Alice have their own `MemoryHigh` / `MemoryMax`. One
     systemd unit is one cgroup. Hermes has no per-profile memory key.
     `terminal.container_memory` applies to container backends, not this
     process. The shared `zz-multiplex-memory.conf` (`MemoryHigh=6G`,
     `MemoryMax=8G`) replaces those caps. One profile can use the whole cap.
     Do not copy a kid's smaller cap onto `hermes-gateway.service`.
   - Kanban. Katie and Alice set `HERMES_KANBAN_DISPATCH_IN_GATEWAY=false`.
     Wife and dad have `disable-kanban.conf`, which sets the same variable.
     The multiplexed process starts one dispatcher from the default profile's
     config (`_kanban_dispatcher_boot` in `gateway/kanban_watchers.py`). The
     env var is process-wide and is checked before config. Copying it onto
     `hermes-gateway.service` would disable dispatch for every profile,
     including default. `kanban.dispatch_in_gateway` in a named profile's
     `config.yaml` is not read at that boot. Do not set it. Retire the env
     var and `disable-kanban.conf` with the profile units. Leave default's
     dispatcher on. Leave `kanban.notify_in_gateway` at its default so the
     one process still delivers notifications.

2. Verify Matrix credentials before any unit is stopped. `sync` prints names
   and an action, not values. Do not pass `--apply`.

   ```bash
   hermes -p katie secrets bitwarden status
   hermes -p katie secrets bitwarden sync
   hermes -p alice secrets bitwarden status
   hermes -p alice secrets bitwarden sync
   ```

   Status must show Enabled, Token in env, the Hermes - Aoba project id, and
   that kid's key prefix with Strip prefix yes. The sync dry-run prints
   `Filter: key_prefix KATIE_MATRIX_` (or `ALICE_MATRIX_`). The Name column
   must list every `MATRIX_*` name `--renderer-keys` printed, with action
   `would export`. It must not list `KATIE_MATRIX_*`, `ALICE_MATRIX_*`, or
   `TELEGRAM_BOT_TOKEN`. Then confirm the adapter's credential gate, which is the same check as
   `check_matrix_requirements` without installing packages. The probe prints
   `present` or `missing` and exits non-zero when the homeserver or both
   login secrets are absent:

   ```bash
   docs/ops/rpi5-vanilla-migration.sh --matrix-probe katie
   docs/ops/rpi5-vanilla-migration.sh --matrix-probe alice
   ```

   Do not continue while either probe exits non-zero. The kid units are still
   running on `render_kid_matrix_env.py` until step 3, so a failed probe is
   fixed in Bitwarden or in that profile's `.env` token without a cutover.

3. Disable and stop the four profile units. Do not disable `hermes-gateway`.
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

4. Clear the shim on katie and alice only. They are the profiles that have
   `gateway.standalone: true`. Wife and dad do not; do not `config unset` a
   key they do not have. Do not set `gateway.standalone` on the default
   profile; it is ignored there (`profile_is_standalone`).

   ```bash
   hermes -p katie config unset gateway.standalone
   hermes -p alice config unset gateway.standalone
   ```

5. Fold preflight. Stop here if it exits non-zero. Do not set
   `gateway.multiplex_profiles` yet: explicit `true` skips this check at the
   next boot.

   ```bash
   docs/ops/rpi5-vanilla-migration.sh --preflight-fold
   ```

6. Move the default unit's drop-ins aside. `hermes gateway install` rewrites
   `hermes-gateway.service` when it differs from `generate_systemd_unit` and
   deletes only `20-replace.conf` (`refresh_systemd_unit_if_needed`). It does
   not touch the other files in `hermes-gateway.service.d/`. These four survive
   install and still apply at restart:

   | Drop-in | What it does |
   |---|---|
   | `zzz-main-v0217.conf` | forces the external venv's Python |
   | `zz-py314-runtime.conf` | forces the external venv's Python |
   | `92-canonical-runtime.conf` | forces the external venv's Python |
   | `memory-limits.conf` | `MemoryMax` 2G |

   A drop-in `ExecStart=` replaces the unit's `ExecStart`. After install, the
   unit file can point at the store launcher while these drop-ins still start
   the old venv. The 2G cap is under the measured ~3.1 GB RSS of the five
   gateways plus gbrain, so the multiplexed cgroup would be killed.

   ```bash
   docs/ops/rpi5-vanilla-migration.sh --sideline-dropins
   ```

   That moves the four files into `~/rpi5-unit-backup/gateway-dropins/` (it
   does not delete them) and writes `zz-multiplex-memory.conf`:

   ```ini
   [Service]
   MemoryHigh=6G
   MemoryMax=8G
   ```

   `MemoryHigh=6G` starts reclaim near twice the measured 3.1 GB set, so a
   normal multiplex plus gbrain children is not throttled. `MemoryMax=8G` is
   the hard stop. The host has 16 GB and about 9 GB free (about 3.9 GB of
   current use is outside this workload), so an 8 GB cap leaves about 4 GB for
   cache and spikes outside the cgroup. If gbrain is a child of this unit it
   counts against the cap. If it is its own unit, the gateway still has
   headroom. Do not put the 2G file back while five profiles share this cgroup.

   The command refuses when it finds more than one non-empty
   `hermes-gateway.service.d`, and when any of the four names is missing from
   both the live directory and the backup. An empty
   `/run/systemd/system/hermes-gateway.service.d` does not count: systemd
   creates that directory with no files, and it is not a second unit scope.
   A second run is a no-op once the backup holds the files.

   `systemctl set-property` writes
   `/etc/systemd/system.control/hermes-gateway.service.d/50-Memory*.conf`.
   That directory is a sibling of `/etc/systemd/system`, so a scan of the unit
   directory does not see it, and those files outrank unit drop-ins. The same
   command moves every `50-Memory*.conf` there into
   `~/rpi5-unit-backup/gateway-dropins/`. A host that has none is unchanged.
   Rollback puts them back when the backup has them, and does not require them.

   When the live directory is not writable, the script re-runs the helper
   under `sudo`. `$HOME` is expanded before `sudo`, so the backup is
   `~/rpi5-unit-backup/gateway-dropins` and not a path under `/root`. After
   the move it `chown`s that backup back to the invoking user. The new
   `zz-multiplex-memory.conf` stays root-owned. That is what a system unit
   drop-in is.

7. Leave `hermes-webui` alone. On this host it is the separate hermes-webui
   application. `ExecStart` is `hermex-webui-start.sh`. It is not
   `hermes dashboard`, and `hermes-dashboard.service` (port 9119,
   `hermes_cli/main_dashboard.py`) is a different program. Do not point this
   unit at `hermes dashboard`.

   If `98-py314-v0217.conf` sets `VIRTUAL_ENV`, `PYTHONPATH`, `PYTHONHOME`, or
   a path under `venvs/hermes-py314-v0.21.7`, retarget that interpreter to the
   committed PM interpreter the script printed
   (`~/.hermes/installs/5935b2ebc06c58fa/environments/<32-hex>/venv/bin/python`).
   `--preflight` prints `Interpreter drop-in: retarget` or `leave`. When it
   says `leave`, the start script is not using Hermes's env; do not edit the
   drop-in. Preflight ignores unit backup copies (`*.bak`, `*.old`,
   `*.dpkg-old`, `*.dpkg-dist`, `*.rpmnew`, `*.rpmsave`, and names ending in
   `~`). A `hermes-webui.service.bak` that still pins `VIRTUAL_ENV` is not a
   live unit. Do not restart the webui until the gateway restart below has
   succeeded.

   `hermex-webui-start.sh` imports `run_agent`. Once `install-stamp.json` says
   `updateMechanism: self`, that import re-execs into the store interpreter
   with `-I`, which drops the WebUI venv. The re-exec activates the managed
   environment before the WebUI script runs, so `fastapi` and `uvicorn` come
   from the committed environment. extra `web` is inside `all`, which this
   migration installs. Do not set `HERMES_DISABLE_LAZY_INSTALLS`. That variable
   is an internal PM switch; it is not how the WebUI finds its dependencies.

8. On the default profile, set the flag explicitly so the next boot does not
   depend on the retired false value:

   ```bash
   hermes config set gateway.multiplex_profiles true
   ```

   Do not set it to `false`. After this revision a false value is rewritten to
   `true` on the next gateway boot when nothing blocks multiplex.

9. Refresh only the default gateway unit, after `facts.json` exists and after
   the memory drop-in in step 6 is in place. Do not add
   `zz-profile-runtime-env.conf`. The renderer is retired:

   On this host the gateway unit is a system unit. `sudo` clears the
   environment, so `HERMES_HOME` has to be on the command, and the launcher
   has to be the absolute `.hermes/bin/hermes` from this checkout:

   ```bash
   docs/ops/rpi5-vanilla-migration.sh --install-system-unit
   ```

   That runs:

   ```bash
   stamp=$(mktemp)
   touch "$stamp"
   sudo env HERMES_HOME=/home/pi/.hermes \
     /home/pi/.hermes/hermes-agent/.hermes/bin/hermes \
     gateway install --system --run-as-user pi
   sudo find /home/pi/.hermes -user root -cnewer "$stamp" \
     -exec chown "$(id -u):$(id -g)" {} +
   rm -f "$stamp"
   ```

   The script uses `$HOME/.hermes` and `id -un`, which are `/home/pi/.hermes`
   and `pi` when the operator is `pi`. It does not also build the environment.
   The install runs as root and can leave new root-owned files under the
   user's Hermes home. The `find` chowns root-owned paths whose status changed
   during that install (`-cnewer` is ctime against the stamp's mtime).
   Root-owned files the install did not create or touch stay as they are.
   It does not chown the system unit drop-ins.

   This rewrites `hermes-gateway.service` and enables it. It does not restart a
   running process, and it does not remove `zz-multiplex-memory.conf` or
   `zz-profile-runtime-env.conf`. Do not run `hermes gateway install` under
   `-p wife` (or dad, katie, alice). That writes or enables the profile unit
   this migration just disabled.

   Then `sudo systemctl daemon-reload`. `gateway install` already reloads
   after a unit rewrite; reload again if the only edits were drop-ins.

10. Restart the default gateway, then the webui unit you already had (the one
   whose `ExecStart` contains `hermex-webui-start.sh`):

   ```bash
   hermes gateway restart
   systemctl --user restart <hermes-webui unit>.service
   ```

   `hermes gateway restart` is the official restart. It is the moment the
   default process leaves the external venv. Confirm `/proc/<pid>/exe` is the
   PM interpreter. If it is still the venv under `venvs/hermes-py314-v0.21.7`,
   a pinning drop-in is still loaded; stop and restore it to the backup before
   trying again.

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

Do this only after verification succeeds. Do not decide which directories are
orphans from a clock window.

Generation directories live under
`~/.hermes/installs/5935b2ebc06c58fa/environments/`. Names are `uuid4().hex`.
The new `facts.json` field `packages.venv.environment` is a path ending in
`environments/<32-hex>/venv`. That `<32-hex>` is the selected generation.
Every other directory there is an orphan. `cee636e2` is inside the
2026-10-15 12:22–12:41 window and is not an orphan when `facts.json` points
at it. A prefix guess (`cc8eed33`, `f098ffea`, or anything else in that
window) is not an identification.

```bash
docs/ops/rpi5-vanilla-migration.sh --orphans
```

That prints `referenced:` and one `orphan:` line per other directory. It does
not delete. If `facts.json` has no `packages.venv.environment`, it exits
non-zero and classifies nothing.

`hermes pm gc` calls `collect_generations` (`hermes_cli/runtime_state.py`). A
generation is removed only when all of these are true: it is not the selected
generation (the parent of the `environment` path in `facts.json`), it contains
`.lease-managed`, that marker is at least 86400 seconds old, no live lease
lock is held, and this process holds the runtime lock. Do not `rm -rf` them.

Do not run gc before 2026-10-16 12:41 Asia/Taipei (UTC+8). Markers written at
12:41 on the 15th are a day old at that instant, and not before. The script
refuses earlier, including when the machine clock is naive:

```bash
docs/ops/rpi5-vanilla-migration.sh --gc
```

`--gc` runs the gate, prints the orphan classification, then `hermes pm gc`.
After the cutoff, gc still keeps a marker younger than a day. Before and
after, confirm the `environment` path in `facts.json` is the `referenced:`
line and is not an `orphan:` line. `gc` also sweeps unreferenced tool-store
entries and install-state dirs of deleted checkouts
(`collect_orphan_install_states`). It will not remove `5935b2ebc06c58fa`
while this checkout is the live tree.

## Rollback

Rollback returns to the external venv and the five separate units. It does not
depend on `gateway.multiplex_profiles: false`. That value is ignored and gets
rewritten to `true`. `gateway.standalone: true` on wife, dad, katie, and
alice is what keeps those profiles off the host gateway after the units are
restored. Only katie and alice had the key before this cutover. Wife and dad
did not. Rollback sets it on all four so the default gateway does not also
serve the restored units.

Do not display `.env`, unit `Environment=` values, or snapshot contents while
you do this.

1. Stop the multiplexed gateway and the webui so they release tokens and ports.
   Stop the webui unit you already had. Do not change its `ExecStart`.

   ```bash
   hermes gateway stop
   systemctl --user stop <hermes-webui unit>.service
   ```

2. Put the shim on all four named profiles. Katie and alice are being restored
   to the value they had. Wife and dad did not have the key; they need it now
   because a false `gateway.multiplex_profiles` no longer keeps them off the
   default gateway:

   ```bash
   hermes -p wife config set gateway.standalone true
   hermes -p dad config set gateway.standalone true
   hermes -p katie config set gateway.standalone true
   hermes -p alice config set gateway.standalone true
   ```

3. Move the new `~/.hermes/installs/5935b2ebc06c58fa/facts.json` aside under a
   new name. Do not restore an older committed selection over it. With no
   runtime facts, the official launcher will not select the PM generation.
   Leave `~/.hermes/venvs/hermes-py314-v0.21.7` on disk. The restored unit
   files invoke it.

4. Restore the backed-up unit files from `~/rpi5-unit-backup/` over the copies
   `hermes gateway install` rewrote, including the main unit `ExecStart`.
   Restoring only a drop-in leaves the store-python launcher in place. The
   launcher ignores `PYTHONPATH` and does not run the external venv.

5. Put the four sideline'd drop-ins back and remove the ones this migration
   added:

   ```bash
   docs/ops/rpi5-vanilla-migration.sh --rollback
   ```

   That restores `zzz-main-v0217.conf`, `zz-py314-runtime.conf`,
   `92-canonical-runtime.conf`, and `memory-limits.conf` from
   `~/rpi5-unit-backup/gateway-dropins/`, and removes
   `zz-multiplex-memory.conf` and `zz-profile-runtime-env.conf`. When the
   backup also has `50-Memory*.conf`, it restores those into
   `/etc/systemd/system.control/hermes-gateway.service.d`. A host that never
   had them is unchanged; rollback does not require them. When the live
   directory is not writable, the restore runs under `sudo`. It then
   restores `install-stamp.json` from
   `install-stamp.json.external-backup` when that backup exists, and removes
   the `.hermes/bin/hermes` launcher the claim created. A missing stamp
   backup leaves the stamp unchanged. It then prints the rest of this list.
   It does not start or stop units. If the drop-in backup is missing one of
   the four files, it exits non-zero before it renames any drop-in, and it
   still restores the stamp backup when one exists. Keep the
   `~/rpi5-unit-backup/` copy from pre-flight step 4 so you can put a missing
   file back by hand without printing it.

6. If `98-py314-v0217.conf` was retargeted, restore it from the unit backup.
   Do not point hermes-webui at `hermes dashboard`. If the preflight said
   `leave`, there is nothing to restore.

7. Restore katie and alice `config.yaml` from the quick snapshot taken before
   the Bitwarden block and the `game-workshop` `env` edit. Restore each
   profile `.env` from that same snapshot, which removes a `BWS_ACCESS_TOKEN`
   this cutover added and puts the previous file back. The kid units'
   `ExecStartPre` (`render_kid_matrix_env.py`) loads Matrix credentials again
   once those units start. The snapshots contain secrets. Copy the files; do
   not cat them. Do not merge those credentials into `hermes-gateway.service`.

8. `systemctl --user daemon-reload` (and the system manager, if you restored
   system units).

9. Enable and start the four profile units, then the default gateway and the
   webui:

   ```bash
   systemctl --user enable --now hermes-gateway-wife.service
   systemctl --user enable --now hermes-gateway-dad.service
   systemctl --user enable --now hermes-gateway-katie.service
   systemctl --user enable --now hermes-gateway-alice.service
   hermes gateway start
   systemctl --user start <hermes-webui unit>.service
   ```

10. Restore vendor callbacks to the pre-multiplex URLs if you changed them.
11. Confirm each profile's own gateway is connected and
    `curl -fsS http://100.64.0.0:8642/health` succeeds on the process that
    owned that port before. Compare `/proc/<pid>/exe` with the external venv
    interpreter. Do not dump `/proc/<pid>/environ`.

Do not run `hermes gateway migrate --multiplex` in either direction. Apply
uninstalls the profile units. Disable and enable preserve them. Do not run
`hermes pm gc` as part of rollback.
