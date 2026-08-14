# Andy Runtime Patch Queue

`andy-runtime` is a deliberately small, linear patch queue rebased on current
`upstream/main`. It is the only supported custom runtime branch.

Frozen upstream cutoff: `0a8765a236ed1d253ef18ecc856cbe881fa3e52a`
(2026-08-15). Exact-SHA CI and review evidence must name this object.

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
7. **Cron delivery integrity** — pre-agent exits close their session store;
   attachment fallback retries only confirmed failures, and an already
   in-flight timeout is not retried because that could duplicate delivery.

## Explicitly retired

Do not revive Discord copy-fence isolation, Matrix additions, Projects/session
DB authority, shared-process profile multiplexing, Buzz overlays, Desktop
bundles, custom no-live-FTS state policy, or obsolete Discord auto-title code.
Smart Discord auto-titling and encrypted Bitwarden fallback are already
upstream.

## Refresh after upstream updates

From a clean `andy-runtime` checkout:

```bash
scripts/maintenance/refresh_andy_runtime.sh --push
```

The script fetches `upstream/main`, creates a timestamped backup branch, rebases
the patch queue, and publishes to Andy's fork only when `--push` is requested.
Pi-side tests are opt-in; broad verification belongs to exact-SHA GitHub CI.
Rebase conflicts stop fail-closed with the backup intact.

After hosted CI is green, deploy the exact immutable SHA using the established
Dad → Wife → Default staged gateway rollout. Never build a broad release on the
Pi, and never restart a healthy profile without explicit approval.
