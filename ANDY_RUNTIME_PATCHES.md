# Andy Runtime Patch Ledger

This branch (`andy-runtime`) is the live Hermes runtime branch for Andy's Raspberry Pi gateway. It is based on current `main` plus local runtime patches that Andy needs before upstream merges or replaces them.

Do not assume a fix exists live just because it exists in a PR branch. Verify this checkout and the running service before restart.

## Integrated upstream baseline

- `31c08a9aad6e83ded5d0e55dc7d41b94a99f08a1` (2026-07-20 frozen rollout cutoff)
- Upstream Projects is canonical. The former explicit session-assignment and Desktop move-to-Project overlay was retired against this cutoff.
- Temporary overlays below were re-audited against this cutoff; idle-rendering PR #66160 remains external to upstream.

## Active runtime patches

- `fix(discord): isolate multiplex profile policy`
  - Keeps Discord authorization, mention, channel, thread, and reply policy adapter-local under profile multiplexing.
  - Disables legacy YAML-to-environment policy bridges in multiplex mode so one profile cannot inherit another profile's settings.

- `fix(desktop): close profile-generation handoff gaps`
  - Revalidates project RPC continuations, review reads, filesystem routing, branch/worktree consumers, and session startup against the initiating profile generation.
  - Defers composer draft cleanup until the profile-owned worktree session is committed, preventing stale or failed handoffs from eating the draft.

- `fix(discord): add smart auto-thread titles`
  - Adds deterministic Discord auto-thread title cleanup/summarization.
  - Runtime switch: `DISCORD_SMART_THREAD_TITLES` / `discord.smart_thread_titles`.

- `fix(discord): auto-thread free-response channels`
  - Keeps free-response channels eligible for auto-threading.
  - Critical live hunk: `skip_thread = bool(channel_keys & no_thread_channels)`.
  - Free-response status must not be part of `skip_thread`.

- `fix: retitle Discord auto threads after first exchange`
  - Renames Discord threads from the generated session title after the first response.
  - Uses adapter `rename_thread()` via title callback plumbing in `gateway/run.py`.

- `fix: preserve legacy auto-title callback alias`
  - Keeps `auto_title_session(..., on_title=...)` compatible while supporting `title_callback`.

- `Add Codex OAuth STT provider`
  - Allows STT through OpenAI Codex OAuth (`openai-codex`) without separate OpenAI API key.

- `fix(email): send HTML bodies as multipart alternative`
  - Sends HTML email content as `text/html` with a plain-text fallback instead of raw tags in plain text.

- `fix(gateway): release delivery claims without process start timestamps`
  - Uses SQLite's NULL-safe identity comparison so an unavailable process start time cannot strand a delivery obligation or consume its retry budget.

- `fix(cron): format Discord cron deliveries without tables`
  - Uses Discord-friendly headings and bullets for cron reports.
  - Converts simple Markdown tables to grouped bullet rows before Discord delivery.

- `feat(memory): add Memory Tree Lite helper modules`
  - Keeps deterministic build/search/attention/reconcile/privacy helpers repo-backed instead of script-only.

- `fix(line): preserve read-only/archive/prefix group gates`
  - Admits `LINE_ALLOWED_GROUPS | LINE_READ_ONLY_GROUPS | LINE_ARCHIVE_GROUPS` at adapter intake.
  - Archives `LINE_ARCHIVE_GROUPS`, short-circuits `LINE_READ_ONLY_GROUPS`, and requires `LINE_GROUP_PREFIXES` for `LINE_REQUIRE_PREFIX_GROUPS` before agent dispatch.
  - Teaches the generic gateway auth layer that LINE group chats use `LINE_ALLOWED_GROUPS` as a chat allowlist, not sender-only `LINE_ALLOWED_USERS`.

- `feat(matrix): auto-thread selected rooms`
  - Adds `matrix.auto_thread_rooms` / `MATRIX_AUTO_THREAD_ROOMS` so selected room root messages become native Matrix thread roots even while global `session_scope` remains `room`.
  - Existing Matrix threads and DM policy retain precedence; add a room to `free_response_rooms` too when every message should be handled without an `@mention`.
  - Retire this overlay once upstream Matrix supports an equivalent per-room thread policy.

- `feat(gateway): bind configured conversation scopes to upstream Projects`
  - Resolves profile-local room/channel bindings through upstream `projects.db` and persists the bound folder as the session cwd.
  - Upstream Projects remains the only membership authority: grouping is inferred from cwd and no parallel assignment table is written.
  - Configured-but-invalid bindings fail closed rather than running from a broader fallback directory.

- `fix(state): retire unstable trigram FTS writes`
  - Removes the disabled `messages_fts_trigram` table and its synchronization triggers so it cannot continue corrupting `state.db` behind the fallback path.
  - Keeps base FTS5 enabled and uses the existing `LIKE` fallback for CJK/substring search.
  - Repair procedure: take a coherent SQLite online backup, copy canonical rows logically, rebuild ordinary indexes and base FTS, and do not recreate trigram.

- `fix(matrix): preserve password-auth reconnect eligibility`
  - Treats `MATRIX_PASSWORD` and `matrix.password` as valid Matrix credentials when no access token is configured.
  - Prevents a temporary Synapse startup outage from permanently removing password-auth Matrix from the gateway reconnect queue.

- `fix(desktop): preserve unsafe config integers`
  - Serializes large config identifiers without JavaScript precision loss so Discord/channel/account IDs are not silently rounded by Desktop edits.

- `fix(desktop): discover runtime plugins from the local Desktop Hermes home`
  - Resolves the on-disk plugin directory through Desktop-local profile IPC instead of backend-reported status.
  - Prevents a Mac Desktop connected to the Pi from reading or opening the Pi's plugin directory.
  - Fails closed when the Desktop bridge does not provide a non-empty local Hermes home.

- `fix(desktop): isolate upstream Projects state by live profile`
  - Captures the concrete profile generation and gateway for every asynchronous Projects RPC so stale A results and writes cannot publish through B.
  - Binds repo discovery, folder-picker browsing, and post-create `IDEA.md` writes to the initiating profile's filesystem transport.
  - Settles replaced/unmounted remote folder pickers and generation-binds both generic and explicit picker requests, including rapid A→B→A swaps.
  - Carries profile plus generation ownership through worktree creation, branch switching, and new-session handoff, rejecting stale `config.get`, draft clearing, and composer routing.
  - Binds project-drill-in worktree probes to the initiating profile generation so stale lanes cannot repaint after a swap.
  - Binds coding-status and review results to both cwd and profile, and refuses Git operations while the active profile and foreground connection disagree.
  - Clears filesystem-bound worktree/project order, collapse, and dismissal state on profile swap until those persisted keys are profile-namespaced.
  - Resets profile-bound Projects caches, optimistic state, dialogs, tombstones, and loading flags on a live profile swap.
  - Keys repo-scan completion and persisted Project view scope by profile.
  - Retire this overlay once upstream Projects provides equivalent A→B→A isolation and regression coverage.

- `fix(desktop): stop idle renderer animation loops` *(temporary pending upstream)*
  - Source: `andyylin/hermes-agent:fix/desktop-idle-rendering` / upstream PR #66160, preserving Ho Lim's original commits from PR #61084.
  - Replaces permanent pet/roam/terminal RAF loops with bounded scheduling while preserving unfocused and occluded transcript streaming.
  - Retire this overlay once PR #66160 (or an equivalent upstream implementation) lands and the runtime has been reconciled to that upstream version.

## Custom Desktop distribution

Install or repair the canonical macOS/Linux Desktop build with:

```bash
curl -fsSL https://raw.githubusercontent.com/andyylin/hermes-agent/andy-runtime/scripts/install-andy-desktop.sh | bash
```

The bootstrap pins the checkout and in-app updater to `origin/andy-runtime`, builds the native Desktop app, and installs `hermes-custom-update`. It deliberately does not copy credentials or remote-backend authentication. Re-running it is idempotent, but it refuses to proceed over tracked source changes.

## Retired runtime patches

- Custom explicit Project assignments, explicit `No Project`, assign/unassign RPCs, Desktop drag/move controls, assignment-aware colors, and assignment overrides were removed on 2026-07-20.
- Upstream Projects now owns Project persistence, discovery, grouping, session colors, Desktop navigation, tools, and Kanban integration.
- Existing `project_session_assignments` rows are migration input only. The legacy table is preserved in database backups but is not read or written by the runtime.

## Operating rule

Before restarting live Hermes gateway after upstream updates:

1. Confirm branch is `andy-runtime`.
2. Confirm the Discord adapter has `skip_thread = bool(channel_keys & no_thread_channels)`.
3. Confirm LINE group allowlist handling remains upstream in `plugins/platforms/line/adapter.py`.
4. Run focused gateway/title/STT/cron tests.
5. Run upstream Projects/database compatibility tests, conversation-scope workspace-binding tests, Desktop typecheck, and focused Project UI tests.
6. Restart the gateway and inspect logs for reconnect/errors.

If upstream `main` later contains any item above, trim the duplicate local patch and update this ledger.
