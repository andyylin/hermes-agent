# Andy Minimal Runtime Overlay

## Baseline and boundary

- Canonical source: `NousResearch/hermes-agent`
- Frozen upstream cutoff: `477c08b44766ace8b890faa72bf82ecbcf2b3ba8`
- Reconstruction branch: `reconstruct/andy-runtime-minimal-20260721T010524Z`
- Historical inclusion boundary: only intentional runtime behavior committed before custom Projects work began at `2026-07-15T23:21:11+08:00` was considered for the first reconstruction.

Upstream is the product. This branch carries only a small, explicit behavioral overlay for Andy's working style. It must not fork Projects, profile ownership, multi-window state, database authority, or shared-process multiplex architecture.

## Ported behaviors

### Discord auto-threading in free-response channels

- Reconstruction commits: `1c9fd9434`, `6ead40403`, `91f11a279`
- Historical sources: `494db21ec690d765e60828554a9fa8f9a18518f1`, `fdd9d09a7111cbb55a6817b80bcedde49bb903b8`
- Contract: free-response controls mention gating; it does not disable automatic thread creation. `DISCORD_NO_THREAD_CHANNELS` remains the explicit inline-response override.

### LINE private collection gates

- Reconstruction commits: `47d38e49c`, `86b106688`
- Historical sources: `b067c99151b3038b3a5516ec553204fa62562257`, `0444a8c2f72be8a9d4b1d16737cdc7081dfe9ad3`
- Contract: read-only groups archive without agent dispatch; archive groups retain collection while dispatching eligible messages; prefix-required groups dispatch only after stripping an approved prefix; group authorization uses the chat allowlist.

### Standalone HTML email delivery

- Reconstruction commit: `4fb88eeba`
- Historical source: `5de0025a1647a9c2fc5bd8fb9cb74dabbd9ce8a9`
- Contract: standalone SMTP delivery sends `multipart/alternative` with readable plain text and HTML instead of exposing raw tags as `text/plain`.

### Discord-friendly cron formatting

- Reconstruction commits: `019524ab5`, `24435d77a`
- Historical source: `0e7c69634fff5cbbf3d0b1e0688231f3847e6179`
- Contract: Discord cron reports use headings and bullets; simple Markdown tables are converted to grouped bullet rows. Formatting is selected independently per delivery target, so mixed Discord/email fan-out preserves each platform's native rendering.

## Rejected after exact-SHA review

### Codex OAuth speech-to-text

Historical source `c857b505cf04edf7d6b4aba23957ac7ea4ecb5dc` was replayed and then reverted by `08347b85f`. The implementation forwarded a ChatGPT/Codex OAuth bearer to the public OpenAI audio endpoint and allowed a configurable base URL. That credential path was neither proven compatible nor acceptably constrained, so STT remains on supported local, Groq, direct OpenAI, Mistral, xAI, or ElevenLabs backends.

## Skipped because pinned upstream already provides the behavior

### Discord smart thread titles and first-exchange retitling

Pinned upstream already:

- strips raw Discord mention markers from provisional auto-thread names;
- marks only Hermes-created threads as eligible for later renaming;
- carries the provisional title through `SessionSource`;
- renames the thread from the generated session title after the first exchange;
- enforces Discord-safe title length and preserves human-created/renamed threads.

Historical sources retained only as evidence:

- `7c9db1072469652f393d9369147d1c4ae2bf3241`
- `691af1aac839bd4e3ce7a8bf9b37441de229d0ae`

## Explicitly deferred from the first reconstruction

- All custom Projects assignment, movement, metadata, conversation-binding, database, and UI behavior.
- All Desktop Projects profile-generation, preview ownership, worktree, review, picker, and multi-window isolation overlays.
- Shared-process profile multiplex hardening, adapter registries, profile pairing stores, global-environment isolation refactors, and sibling-platform authority audits.
- Matrix room auto-threading and password reconnect patches added after the Projects boundary.
- Desktop config-integer, local-plugin-home, idle-rendering, clarify-timeout, and custom bootstrap patches added after the Projects boundary.
- Session workspace metadata RPC additions.
- Trigram FTS retirement, state-database repair code, delivery-claim changes, and Memory Tree helper modules added after the Projects boundary.

Deferred does not mean deleted. These remain available from the forensic bundle and backup ref. A deferred behavior returns only through a separate, user-approved need, reproduced against current upstream and implemented as a narrow patch.

## Architectural rules

- Use upstream Projects unchanged and as the only Project authority.
- Prefer dad, wife, and default as separate gateway processes; do not carry custom shared-process multiplex architecture merely because historical branches contain it.
- Do not merge another upstream cutoff during this reconstruction.
- One behavior per commit, with focused regression coverage.
- No opportunistic sibling refactors.
- Existing state databases are operational migration input, never justification for permanent forked code.

## Preserved evidence

- Abandoned integration backup ref: `backup/abandoned-full-integration-20260721T010524Z`
- Abandoned integration HEAD: `2352725a2be9e46f4be4a93e7d232d16da75dd82`
- Protected live runtime HEAD: `d4bd74064896f639e608d1bf1f7db968f6e0a90d`
- Complete Git bundle: `/home/pi/.hermes/backups/hermes-minimal-reconstruction-20260721T010524Z/abandoned-full-integration.bundle`

No live service, database, or runtime branch was changed while creating this reconstruction lane.
