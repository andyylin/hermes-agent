# Andy Runtime Patch Ledger

This branch (`andy-runtime`) is the live Hermes runtime branch for Andy's Raspberry Pi gateway. It is based on current `main` plus local runtime patches that Andy needs before upstream merges or replaces them.

Do not assume a fix exists live just because it exists in a PR branch. Verify this checkout and the running service before restart.

## Active runtime patches

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

- `fix(cron): format Discord cron deliveries without tables`
  - Uses Discord-friendly headings and bullets for cron reports.
  - Converts simple Markdown tables to grouped bullet rows before Discord delivery.

- `feat(memory): add Memory Tree Lite helper modules`
  - Keeps deterministic build/search/attention/reconcile/privacy helpers repo-backed instead of script-only.

- `fix(line): preserve read-only/archive/prefix group gates`
  - Admits `LINE_ALLOWED_GROUPS | LINE_READ_ONLY_GROUPS | LINE_ARCHIVE_GROUPS` at adapter intake.
  - Archives `LINE_ARCHIVE_GROUPS`, short-circuits `LINE_READ_ONLY_GROUPS`, and requires `LINE_GROUP_PREFIXES` for `LINE_REQUIRE_PREFIX_GROUPS` before agent dispatch.
  - Teaches the generic gateway auth layer that LINE group chats use `LINE_ALLOWED_GROUPS` as a chat allowlist, not sender-only `LINE_ALLOWED_USERS`.

## Operating rule

Before restarting live Hermes gateway after upstream updates:

1. Confirm branch is `andy-runtime`.
2. Confirm the Discord adapter has `skip_thread = bool(channel_keys & no_thread_channels)`.
3. Confirm LINE group allowlist handling remains upstream in `plugins/platforms/line/adapter.py`.
4. Run focused gateway/title/STT/cron tests.
5. Restart the gateway and inspect logs for reconnect/errors.

If upstream `main` later contains any item above, trim the duplicate local patch and update this ledger.
