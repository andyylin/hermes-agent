# Andy Runtime Patch Queue

`andy-runtime` is a deliberately small, linear patch queue rebased on current
`upstream/main`. It is the only supported custom runtime branch.

Frozen upstream cutoff: `930be347f1e3efefae0aa2f98219b3d126abf0ce`
(2026-08-21, latest origin/main at composition time). Exact-SHA CI and review evidence must name this object.

## Retained behavior

1. **Discord free-response auto-threading** — free-response controls mention
   gating, not thread creation; `discord.no_thread_channels` remains the
   explicit opt-out.
2. **Discord cron formatting** — wrapped Discord reports use readable headings
   and table-to-bullet conversion, while mixed email/Discord fan-out is rendered
   independently and unwrapped output remains literal.
3. **Bitwarden plaintext-cache purge** — encrypted-cache mode removes obsolete
   plaintext cache data before validation or fetch. The encrypted AES-GCM
   network-failure-only fallback remains upstream-owned.
4. **Discord thread retention** — Hermes-created threads default to one-week
   auto-archive, configurable with `discord.thread_auto_archive_minutes`.
5. **LINE group collection policy** — supports allowed, read-only, archived, and
   prefix-required groups. Read-only messages are archived before dispatch is
   stopped; archive groups can still dispatch; prefix-required groups strip an
   approved prefix before dispatch.
6. **Email notifications** — standalone email sends multipart HTML plus plain
   fallback; cron jobs can use `email_subject_template` and
   `email_thread_key` for dated subjects and stable RFC threading.
   `hermes send` exposes the same threading metadata.
7. **Cron delivery integrity** — pre-agent exits close their session store;
   attachment fallback retries only confirmed failures, and an already
   in-flight timeout is not retried because that could duplicate delivery.

## Explicitly retired

Do not revive Discord copy-fence isolation, Matrix additions, Projects/session
DB authority, shared-process profile multiplexing, Buzz overlays, Desktop
bundles, custom no-live-FTS state policy, or obsolete Discord auto-title code.
Smart Discord auto-titling and encrypted Bitwarden fallback are already
upstream.

## Two update lanes

Use the **routine lane** when the patch queue rebases cleanly and the upstream
delta does not change dependency manifests, database/schema behavior,
authentication, profile isolation, systemd topology, native/generated assets,
or other security-sensitive runtime contracts.

1. Run `scripts/maintenance/refresh_andy_runtime.sh --push` from a clean
   checkout.
2. Run focused tests/import probes for the upstream and overlay surfaces that
   actually changed. Broad GitHub CI is optional in this lane, not ceremonial
   tribute demanded by every refresh.
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
the patch queue, and publishes to Andy's fork only when `--push` is requested.
Rebase conflicts stop fail-closed with the backup intact and automatically
promote the work to the exceptional lane.

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
