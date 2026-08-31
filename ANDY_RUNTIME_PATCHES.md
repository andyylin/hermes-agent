# Andy Runtime Patch Queue

`andy-runtime` / live `runtime-deploy` is a deliberately small, linear patch
queue rebased on current `upstream/main`. It is the only supported custom
runtime branch.

This file is the retained/retired **authority**, not a commit-count souvenir.
Score overlays as `KEEP` / `UPSTREAM-NOW` / `DROP` against current official
`origin/main`. Do not replay `DROP` or `UPSTREAM-NOW` behavior on refresh.

Frozen upstream cutoff: `29112bef099274229cadff79cdff7bf7b99c4b77`
(`v2026.8.31` / Hermes 0.21.0). Exact-SHA CI and review evidence must name
this object.

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
2. **Discord free-response auto-threading** — free-response controls mention
   gating, not thread creation; `discord.no_thread_channels` remains the
   explicit opt-out. Official still does
   `skip_thread = no_thread OR is_free_channel`.
3. **Discord thread retention** — Hermes-created threads default to one-week
   auto-archive, configurable with `discord.thread_auto_archive_minutes`.
   Official create-thread default is 1440 minutes.
4. **Discord cron formatting** — wrapped Discord reports use readable headings
   and table-to-bullet conversion **inside** the per-target loop. Mixed
   email/Discord fan-out is rendered independently; unwrapped output stays
   literal. Official has no `_markdown_tables_to_bullets` helper.
5. **Email notifications** — standalone email sends multipart HTML plus plain
   fallback; cron jobs can use `email_subject_template` and `email_thread_key`
   for dated subjects and stable RFC threading. Inbound sessions isolate by
   RFC thread. `hermes send` exposes the same threading metadata. Official
   outbound is plain-text only.
6. **Cron delivery integrity** — pre-agent exits close / defer the session
   store; attachment fallback retries only confirmed failures; an already
   in-flight timeout is not retried because that could duplicate delivery.
7. **Bitwarden plaintext-cache purge** — encrypted-cache mode removes obsolete
   plaintext cache data fail-closed before validation, read, or fetch. The
   encrypted AES-GCM network-failure-only fallback is already upstream-owned.
8. **Memory Tree manual retrieval** — local subsystem (`memory_tree` tool,
   CLI, build/privacy/attention/reconcile). Official `origin/main` has none of
   these files. Keep while default config has `memory_tree.enabled: true`.
   This is the fattest unique KEEP; retire only with an explicit replacement
   (session_search + skills) decision.
9. **House runtime pin/refresh scripts** — `scripts/maintenance/refresh_andy_runtime.sh`
   and `scripts/maintenance/pin-shared-hermes-runtime.sh`. Ops, not product.
   They keep source SHA and the SQLite-safe interpreter separate.

## UPSTREAM-NOW — do not keep as a fork reason

Official already owns the equivalent or stronger contract:

- Encrypted Bitwarden outage fallback (not the plaintext purge above).
- Smart Discord auto-titling.
- Authz `_platform_gate_env` fail-closed on multiplex scoped allowlist misses
  (issue #72348).
- Telegram `_scoped_gate_env` and Signal scoped allowlist reads.
- Discord native thread rename via `edit(name=...)`.

## DROP — retired, reverted, or not load-bearing here

Do not revive:

- Shared-process profile multiplexing and the later isolate-policy stack
  (Discord mention/multiplex family, remaining gateway multiplex seams,
  LINE-as-multiplex-only scoping, cron `adapters_by_profile` fail-closed).
  Separate systemd units already isolate wife/dad/default.
- `Format cron deliveries per medium` and its immediate revert. Net zero.
  The earlier Discord table-to-bullet KEEP stays; do not replay this pair.
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
