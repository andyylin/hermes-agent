# Andy runtime patch score — v0.23.0 (`2bd378ed`)

Scored against peeled tag **v0.23.0** on branch `candidate/rpi5-v0.23.0`. Ledger authority:
`ANDY_RUNTIME_PATCHES.md` at fork `main` `771fd573` (frozen cutoff `bf0203a` / v0.22.5).

Verdicts: **KEEP** (ported on this branch), **UPSTREAM** (official v0.23.0 already has equivalent),
**CONFLICT** (not ported cleanly; left out).

| Patch | Verdict | Reason | Files touched |
| --- | --- | --- | --- |
| 1. LINE group collection policy | KEEP | v0.23.0 LINE plugin still allowlist-only; ported read-only archive, archive-only groups, and prefix-required dispatch. | `plugins/platforms/line/adapter.py` |
| 2. Email notifications | KEEP | Multipart HTML/plain, RFC thread anchors, cron `email_subject_template` / `email_thread_key`, and `send_message` subject metadata ported; inbound threading unchanged (already header-driven). | `plugins/platforms/email/adapter.py`, `cron/scheduler_delivery.py`, `tools/send_message_tool.py` |
| 3. Cron delivery integrity | UPSTREAM | Pre-agent SessionDB deferral (#96290), in-flight timeout duplicate guard, and attachment skip-on-timeout match v0.23.0 `cron/scheduler_delivery.py` / `cron/scheduler.py`. | _(none — no overlay)_ |
| 4. Bitwarden plaintext-cache purge | KEEP | Encrypted stale fallback exists upstream; fail-closed `_purge_plaintext_disk_cache` before encrypted reads/writes was still missing. | `agent/secret_sources/bitwarden.py` |
| 5. Memory Tree manual retrieval | KEEP (plugin) | Core overlay retired per ledger; install user plugin from `plugin-export/memory-tree` — no core files on this branch. | _(none — external plugin)_ |
| 6. House runtime pin/refresh scripts | KEEP | Ops scripts unchanged in role; copied onto v0.23.0 tree. | `scripts/maintenance/refresh_andy_runtime.sh`, `scripts/maintenance/pin-shared-hermes-runtime.sh` |
| 7. Mattermost `delete_message` + follow-ons | KEEP | delete progress posts, CRT typing `parent_id`, thread-safe roots, media `reply_to`, per-profile token scope (separate gateways), platform token lock — not in v0.23.0. | `plugins/platforms/mattermost/adapter.py`, `plugins/platforms/mattermost/gateway_env.py`, `plugins/platforms/mattermost/token_scope.py` |
| 8. New-skill description Not exclusion | KEEP | Create-only word-boundary `not` inside the 60-char index window; edits/patches unchanged. | `tools/skill_manager_tool.py` |

## Not ported (CONFLICT / out of scope)

| Item | Verdict | Reason |
| --- | --- | --- |
| Cron origin → stored session delivery (`cron/session_delivery.py`) | CONFLICT | Fork overlay depends on `gateway/session_message_events.py`, absent on v0.23.0; session-delivery hunks were dropped from `cron/scheduler_delivery.py` so email/cron subject KEEP stands alone. |
| Fork BWS 429 retry / rate-limit stale fallback | UPSTREAM / fork-only | Ledger separates encrypted outage fallback (upstream) from plaintext purge (KEEP 4); full fork BWS retry stack not replayed. |
| UPSTREAM-NOW / DROP ledger entries | UPSTREAM / DROP | Per `ANDY_RUNTIME_PATCHES.md` — not replayed on refresh. |

## Topology note

Default / Dad / Wife remain **separate systemd gateway processes**; Mattermost `token_scope` keeps named profiles from inheriting the default bot token (no shared-process multiplex collapse).
