# Andy Runtime Patch Queue

`andy-runtime` / live `runtime-deploy` is a deliberately small, linear patch
queue rebased on a frozen official tag. It is the only supported custom
runtime branch.

This file is the retained/retired **authority**, not a commit-count souvenir.
Score overlays as `KEEP` / `UPSTREAM-NOW` / `DROP` against the frozen tag
below. Do not score against newer untagged `main` or `rc.` / `abandoned-rc.`
tags. Do not replay `DROP` or `UPSTREAM-NOW` behavior on refresh.

Frozen upstream cutoff: `818c13be1dc4fd28987e1e881a9408224afd4535`
(`v0.21.6`). Exact-SHA CI and review evidence must name this object. The
previous cutoff was `f97608f178d1ffeca59860195ab7da295f7c8e5f`
(`v2026.9.24` / Hermes 0.21.5).

This tag declares Python 3.14 as the only supported runtime (`pyproject.toml`
marks every core dependency `python_version >= '3.14'`; `uv.lock` resolves
only for `>=3.14`). `hermes_bootstrap` calls
`hermes_cli.venv_sync.prepare_launch`. A stampless `.git` checkout at
`$HERMES_HOME/hermes-agent` is adopted by
`hermes_cli.post_update.step_adopt_blessed_checkout` with
`updateMechanism=self`, and the next launch can build a PM environment and
`execv` into the store Python. No KEEP file edits `hermes_bootstrap.py`,
`venv_sync.py`, or `post_update.py`. KEEP 6's pin script still
`ExecStart`s the shared venv via `python -m hermes_cli.main`, and that entry
imports `hermes_bootstrap`. `HERMES_DISABLE_LAZY_INSTALLS=1` makes
`prepare_launch` return before adoption and before `execv`. With no PM
install state, `pm.environments.activate_dependencies` then keeps a real venv
(`sys.prefix != sys.base_prefix`). The variable does not undo a stamp or a
committed PM generation written by an earlier unguarded launch.

Cron memory: take official `skip_memory=False` from this tag. Do not replay
`fc9cbc87` (skip MEMORY.md in scheduled jobs). Per-job toolset denylist still
wins.

Live topology fact: Default / Dad / Wife are **separate systemd gateways**,
not `gateway.multiplex_profiles`. Shared-process multiplexing stays retired.

## KEEP — missing or weaker upstream, and still used

1. **LINE group collection policy** — allowed, read-only, archived, and
   prefix-required groups. Read-only messages are archived before dispatch is
   stopped; archive groups can still dispatch; prefix-required groups strip an
   approved prefix before dispatch. Official LINE still has allowlists only.
2. **Email notifications** — standalone email sends multipart HTML plus plain
   fallback; cron jobs can use `email_subject_template` and `email_thread_key`
   for dated subjects and stable RFC threading. Inbound sessions isolate by
   RFC thread. `hermes send` exposes the same threading metadata. Official
   outbound is plain-text only.
3. **Cron delivery integrity** — pre-agent exits close / defer the session
   store; attachment fallback retries only confirmed failures; an already
   in-flight timeout is not retried because that could duplicate delivery.
4. **Bitwarden plaintext-cache purge** — encrypted-cache mode removes obsolete
   plaintext cache data fail-closed before validation, read, or fetch
   (`_purge_plaintext_disk_cache` from `fetch_bitwarden_secrets`,
   `BitwardenSource.fetch`, and `_write_encrypted_disk_cache`). The
   encrypted AES-GCM network-failure-only fallback is already upstream-owned.
   v0.21.6 removed the pre-decomposition `apply_bitwarden_secrets`
   PLUGIN-COMPAT shim; do not put that shim back.
5. **Memory Tree manual retrieval** — peeled to the user plugin at
   `plugin-export/memory-tree/` on branch `feat/memory-tree-user-plugin-20260905`.
   Copy to `~/.hermes/plugins/memory-tree` and enable via `plugins.enabled`.
   Core overlay files (`agent/memory_tree_*`, `hermes_cli/memory_tree.py`,
   `tools/memory_tree_tool.py`) are removed from this tree; keep
   `memory_tree.enabled: true` in config for Andy's runtime semantics once the
   plugin is installed.
6. **House runtime pin/refresh scripts** — `scripts/maintenance/refresh_andy_runtime.sh`
   and `scripts/maintenance/pin-shared-hermes-runtime.sh`. Ops, not product.
   They keep source SHA and the SQLite-safe interpreter separate.
7. **Mattermost `delete_message`** — official `cleanup_progress` only honors
   adapters that override `delete_message` (Telegram/Discord). Joi Mattermost
   posts thinking/tool bubbles, then deletes them after a successful final
   reply. Nemo PD One already has this; keep the DELETE + closed-session retry.
   Follow-ons still missing upstream, carried with this item:
   CRT typing sends `parent_id` (`_typing_parent_id_from_metadata`); a child
   agent or background process wakes the channel thread (`_ROUTING_KEYS` and
   `watcher_chat_type`); MEDIA images, voice, video, and documents stay in
   the CRT thread (`reply_to` on `_deliver_media_attachments` /
   `send_multiple_images`). Ephemeral progress was tried and then removed on
   this queue; interim progress stays persistent and returns post ids.

## UPSTREAM-NOW — do not keep as a fork reason

Official already owns the equivalent or stronger contract:

- Encrypted Bitwarden outage fallback (not the plaintext purge above).
- Smart Discord auto-titling.
- Authz `_platform_gate_env` fail-closed on multiplex scoped allowlist misses
  (issue #72348).
- Telegram `_scoped_gate_env` and Signal scoped allowlist reads.
- Discord native thread rename via `edit(name=...)`.
- Dashboard password login behind `X-Forwarded-Prefix` (old fork commit
  `1dd23c9958`, never on GitHub). `hermes_cli/dashboard_auth/login_page.py`
  `render_login_html` / `_apply_proxy_prefix` sets `<main data-prefix>`, and
  `_PASSWORD_FORM_SCRIPT` posts to `prefix + '/auth/password-login'`.
  `hermes_cli/dashboard_auth/routes.py` `_prefix()` reads
  `prefix_from_request`, and `login_page` passes `prefix=_prefix(request)`
  into `render_login_html`. Do not re-add the fork patch.
- Mattermost DM top-level thread roots. Already in this tag via
  `5a0e0d35b9` (`fix(mattermost): preserve thread-local delivery hygiene`):
  `MattermostAdapter._last_post_failure_is_broken_thread_root`. Not a
  separate overlay.

## DROP — retired, reverted, or not load-bearing here

Do not revive:

- Discord free-response auto-threading overlay (Andy asked to drop 2026-09-07).
  Official: `skip_thread = no_thread OR is_free_channel`.
- Discord one-week thread retention / `discord.thread_auto_archive_minutes`
  (Andy asked to drop 2026-09-07). Official hardcodes 1440 minutes.
- Discord cron headings + `_markdown_tables_to_bullets` (Andy asked to drop
  2026-09-07). Official uses the shared `Cronjob Response` wrapper only.
- Shared-process profile multiplexing and the later isolate-policy stack
  (Discord mention/multiplex family, remaining gateway multiplex seams,
  LINE-as-multiplex-only scoping, cron `adapters_by_profile` fail-closed).
  Separate systemd units already isolate wife/dad/default.
- `Format cron deliveries per medium` and its immediate revert. Net zero.
  Do not replay this pair.
- Discord copy-fence isolation.
- Matrix additions.
- Projects / session-DB authority overlays.
- Buzz overlays.
- Custom Desktop bundles / desktop version lockstep as a product overlay.
- Custom no-live-FTS state policy.
- Obsolete Discord auto-title code.
- Cutoff-pin-only docs commits, contributor-email mapping hitchhikers,
  overlay CI contract churn, cold turn-lease test tweaks, and obsolete
  xfail retirement. Maintenance residue, not user-visible behavior.

## Two update lanes

Use the **routine lane** when the KEEP patch queue rebases cleanly and the
upstream delta does not change dependency manifests, database/schema behavior,
authentication, profile isolation, systemd topology, native/generated assets,
or other security-sensitive runtime contracts.

1. Run `scripts/maintenance/refresh_andy_runtime.sh --push` from a clean
   checkout.
2. Run focused tests/import probes for the upstream and KEEP overlay surfaces
   that actually changed. Broad GitHub CI is optional in this lane.
3. Fast-forward the canonical source checkout to the published runtime head.
4. Run `scripts/maintenance/pin-shared-hermes-runtime.sh --write` to refresh
   the exact source SHA in systemd without changing the SQLite-safe
   interpreter.
5. With separate just-in-time approval, activate Dad → Wife → Default, then
   Dashboard, verifying each before advancing.

Use the **exceptional lane** when there is a rebase conflict or any dependency,
database/schema, authentication, profile-isolation, systemd/runtime,
native/generated-asset, security-sensitive, or broad core change. That lane
uses an isolated candidate, exact-SHA hosted CI, semantic review where it earns
its keep, rollback evidence, and the same staged activation order. Do not let
exceptional ceremony leak back into routine updates.

## Refresh procedure

From a clean `andy-runtime` checkout:

```bash
scripts/maintenance/refresh_andy_runtime.sh --push
```

The script fetches `upstream/main`, creates a timestamped backup branch, rebases
the KEEP patch queue, and publishes to Andy's fork only when `--push` is
requested. Rebase conflicts stop fail-closed with the backup intact and
automatically promote the work to the exceptional lane.

The normalized production topology keeps source and interpreter separate:
Default, Dad, Wife, and Dashboard import the canonical mutable checkout while
all four use the stable SQLite-safe interpreter. Default gateway and Dashboard
both open `/home/pi/.hermes/state.db`, so they must still use the same source
SHA and SQLite build. Prepare their next-start definitions with:

```bash
/home/pi/.hermes/scripts/pin-shared-hermes-runtime.sh --write
```

That writes one `92-canonical-runtime.conf` per service and archives obsolete
release/canary pins without recycling a process. Activate only after the lane's
checks pass, and never recycle a healthy profile without explicit approval.
