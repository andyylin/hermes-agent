# Andy Retained Runtime Overlay Manifest

## Purpose and authority boundary

This branch reconstructs Andy's retained Hermes runtime behavior on current upstream without restoring the retired custom Projects or shared-process profile authority.

Public classification is deliberately limited to:

- **PORT** — retained behavior that current upstream does not provide equivalently.
- **UPSTREAM** — current upstream provides the invariant, including stronger superseding implementations.
- **DROP** — unsafe, obsolete, reconciliation-only, fixture-only, or retired shared-process machinery.
- **PROJECT-EXCLUDED** — historical code whose authority or purpose belongs to the retired custom Projects stack.

Upstream is authoritative for Projects. Existing state databases are migration and rollback evidence, not permission to fork Project ownership.

## Frozen composition

- Actual live runtime ancestry: `01ffcd6529a7d626990eafff0784a2ad09a16560`
- Prior published source candidate: `b40724ace24392c9b82cfd6dd046cda5d24fb050`
- Classified retained-overlay parent: `56e6855edd4abcef1ffcefc3d5336a8a078cf5fa`
- Frozen latest upstream: `533d633ab9d18ce0f05bc44d4077576b69dc58e8`
- Linear candidate branch: `integrate/andy-runtime-upstream-linear-20260729T020809Z`
- Candidate worktree: `/home/pi/.hermes/worktrees/integrate/andy-runtime-upstream-linear-20260729T020809Z`
- Candidate lease: `/home/pi/.hermes/recovery/runtime-update-owner-20260729T020809Z.json`
- Rollback ref: `backup/andy-runtime-pre-upstream-20260729T020809Z`

This cycle linearly merges the frozen cutoff into the prior published source candidate, whose ancestry already contains the live-derived runtime and classified retained-overlay tree. Overlap is resolved toward current upstream architecture while preserving the explicit PORT invariants below. The final candidate SHA is recorded externally in exact-SHA GitHub CI evidence because a commit cannot truthfully contain its own hash.

## PORT

### Messaging and gateway

- **Discord free-response auto-threading**
  - Free-response controls mention gating; `DISCORD_NO_THREAD_CHANNELS` remains the explicit inline-response override.
  - Preserved from the minimal live ancestry with focused gateway tests.

- **Discord copy-fence isolation**
  - Exact marker: `<!-- hermes:copy -->` immediately before the fenced block.
  - Only marked fences are isolated; ordinary explanatory fences stay in surrounding prose.
  - Oversized fences become attachments and forum chunks remain in one thread.
  - Candidate commits: `ff71822bc`, `b0900bddd`, `cf97e0261`, `52843b6e6`.

- **Discord one-week thread auto-archive**
  - Candidate commit: `4500e7a17`.

- **Legacy auto-title callback alias**
  - `auto_title_session(..., on_title=...)` remains compatible while current `title_callback`, runtime validation, atomic persistence, and deduplicated persisted titles remain authoritative.
  - Candidate commits: `6daafd367`, `b3476521b`.

- **LINE read-only/archive/prefix group gates**
  - Read-only groups archive without agent dispatch.
  - Archive groups retain collection while dispatching eligible messages.
  - Prefix-required groups dispatch only after stripping an approved prefix.
  - Group authorization uses the chat allowlist and profile scope remains authoritative.
  - Preserved from the minimal live ancestry.

- **Matrix selected-room native threading**
  - Selected room root messages become Matrix thread roots without globally changing room session scope.
  - Candidate commit: `266aff72a`.

- **Matrix DM auto-thread YAML bridge**
  - Candidate commit: `540d5a17d`.

- **Matrix password-auth reconnect eligibility**
  - Password-only Matrix remains reconnectable after transient startup failure.
  - Candidate commit: `5a2a1266b`.

- **Matrix silent progress notices**
  - Progress updates can be sent/edited without noisy push notifications while retaining thread context.
  - Candidate commit: `17308d956`.

- **Lifecycle delivery-readiness enforcement**
  - A readiness helper returning false blocks the protected delivery side effect rather than being decorative.
  - Candidate commit: `cb7fd1037`.

- **Profile-scoped runtime state paths**
  - State paths are resolved lazily from the active profile/home rather than captured at import time.
  - Candidate commit: `2f4bba7ea`.

### Desktop and distribution

- **Large configuration identifier preservation**
  - Desktop serialization does not round unsafe JavaScript integers such as channel/account identifiers.
  - Candidate commit: `31b3e3514`.

- **Configured clarification timeout**
  - Desktop/TUI clarification waits honor the configured runtime timeout.
  - Candidate commit: `9b4f7e0fb`.

- **Idle renderer suspension and bounded animation**
  - Pet, roaming, terminal overlay, and decorative work suspend or use bounded scheduling while inactive without suppressing transcript streaming.
  - Candidate commits: `41475de07`, `f33e0579d`, `62f66a2ad`, `81b8f58e7`.

- **Desktop-local plugin root**
  - Plugin discovery uses Electron's machine-local Hermes home, not a remote gateway's reported home.
  - Missing local home fails closed.
  - Candidate commits: `49893c810`, `5af6b8e74`.

- **Canonical custom Desktop bootstrap/update scripts**
  - Installation and updates remain branch-pinned and refuse tracked source changes.
  - Candidate commit: `f56b18f3e`.

### Secrets, state, and memory

- **Encrypted Bitwarden last-good cache**
  - AES-GCM encrypted fallback cache.
  - Stale fallback only for network/timeout failures.
  - Authentication failures never use stale credentials.
  - Rotation/test cleanup clears in-memory, plaintext, and encrypted caches.
  - Candidate commit: `30c76cb6e`.

- **Memory Tree Lite operational modules**
  - Repo-backed build, search, attention, reconciliation, privacy, CLI, and tool surfaces.
  - Credential-shaped pack text is scrubbed.
  - Verified repairs and resolved statuses suppress false alerts.
  - Pruned cron output is tolerated.
  - Candidate commits: `1049850bf`, `5522c3ca3`, `f9ffb6ef8`, `29f61855c`, `8e2700528`, `26ed6c299`, `e95e62f07`.

- **Retired live FTS write/rebuild surface**
  - Canonical LIKE search remains authoritative.
  - Healthy opens avoid retired FTS DDL/rebuild/write-retry behavior.
  - Gateway transcript failures use the bounded canonical dirty queue and never classify generic SQLite corruption as a derived-index repair opportunity.
  - Preserved from the protected minimal live ancestry.

### Sessions, cron, and email

- **Session workspace metadata in list RPC**
  - Session listing exposes generic workspace metadata without assigning or moving Projects.
  - Candidate commit: `3b29b941d`.

- **Per-target cron rendering**
  - Discord receives heading/bullet formatting while email retains rich HTML/Markdown rendering during mixed fan-out.
  - Preserved from minimal live ancestry and reconciled in `583022e94`.

- **Operator-invoked deterministic cron-definition export**
  - `cron definitions export` writes a stable, diffable representation on explicit request; it is not an automatic scheduler write path.
  - Implemented by `cron/definitions_export.py`, `cron/jobs.py:export_definitions_file`, and `hermes_cli/cron.py`.

- **Dormant cron repair-gate detectors**
  - Warning/error classification helpers remain for compatibility, but no scheduler call site suppresses delivery through them.
  - `cron.route_alerts_through_repair_gate` must stay disabled unless an enabled, tested repair job and fail-open delivery path are deployed together.

- **Email HTML and notification rendering**
  - Multipart alternative output with readable plain fallback.
  - Safe Markdown-ish HTML rendering.
  - Allowlist sanitization for raw HTML fragments; executable tags, event
    handlers, unsafe URL schemes, and remote-loading style attributes are removed.
  - Dated subject templates and explicit `hermes send` subjects.
  - Stable recurring-email thread anchors.
  - Copyable `HERMES-NOTIFY` references for notification diagnosis.
  - Candidate commits: `468b20894`, `b9134408f`, `0d9f132e6`, `0e4bd9458`, `b5d92d27c`, `8113b5c50`, `5609b80a6`, `583022e94`.

## UPSTREAM

- **Projects authority and repository discovery**
  - Frozen upstream cutoff `533d633ab` owns Project records, repository discovery, Project RPCs, and Desktop Project UI behavior.
  - Candidate-only diff against the frozen cutoff must contain no retired custom Project authority.

- **Discord smart provisional titles and first-exchange semantic retitling**
  - Current upstream strips mention noise, tracks Hermes-created threads, carries provisional titles, renames from generated session titles, and preserves human-created/renamed threads.
  - Historical smart-title and retitle implementations are not replayed.

- **Durable delivery ledger foundation and unavailable-platform retry protection**
  - Upstream claims only platforms connected on the current boot, so an unavailable platform does not consume a retry attempt.
  - This stronger pre-claim filter supersedes the older post-claim release helper.

- **Matrix encrypted transport foundation**
  - Upstream remains authoritative for Matrix E2EE, device/session identity, and core thread transport; the PORT entries above add only Andy-specific policy/reconnect/notice behavior.

- **Current Desktop Projects, profile, preview, picker, Git/worktree, and multi-window architecture**
  - Upstream implementations remain byte-authoritative unless listed explicitly under PORT outside Project ownership.

- **Normal Codex inference/account usage**
  - Ordinary `chatgpt.com/backend-api/codex` support is legitimate inference/account functionality and is not the rejected STT capability.

## PROJECT-EXCLUDED

The following historical authorities are deliberately absent:

- explicit session-to-Project assignment and unassignment;
- explicit `No Project` authority;
- session move/drag lifecycle and cwd re-anchoring owned by the old custom stack;
- conversation-scope-to-Project binding;
- custom Project membership tables or databases;
- custom move-session RPCs;
- historical Project-specific profile-generation, preview, picker, review, worktree, and multi-window isolation machinery whose purpose was protecting the retired authority.

Generic workspace metadata is retained because it does not assign, move, or own Projects.

## DROP

- **Codex OAuth speech-to-text**
  - Historical source: `c857b505cf04edf7d6b4aba23957ac7ea4ecb5dc`.
  - The implementation forwarded a ChatGPT/Codex bearer credential to an insufficiently proven transcription endpoint and allowed an inadequately constrained base URL.
  - It was reverted in the minimal ancestry and must not return without independent token-audience and endpoint proof.

- **Retired shared-process multiplex authority**
  - Custom global adapter registries, parallel profile stores, profile pairing authority, broad global-environment rewrites, and machinery whose only purpose was supporting the old Projects/profile architecture.
  - Retained platform code consumes current upstream isolation primitives instead.

- **Historical reconciliation-only material**
  - Merge-resolution commits, stale ledgers, fixture-only repairs with no surviving behavior, dead helper scaffolding, malformed snapshots, and duplicated predecessor implementations.

## Latest-upstream policy divergence

The custom runtime intentionally keeps live SQLite FTS retired. Latest upstream's
new CJK tokenizer, live FTS migration, external-content FTS, and FTS read-probe
tests are therefore marked skipped in this fork with explicit reasons. They are
replaced by active coverage for canonical LIKE search, no-FTS fresh/open/migration
behavior, stale FTS object and metadata retirement, nonblocking WAL read paths,
PASSIVE checkpointing, connection cleanup, canonical b-tree REINDEX repair, and
recovered-database verification without derived indexes. This is an intentional
safety policy, not a temporary CI waiver.

## Verification contract

The retained-overlay parent previously passed independent review and carried regression coverage for profile-scoped authorization, SMTP scope, Matrix sync, Desktop-local plugin discovery, encrypted Bitwarden fallback, raw-HTML sanitization, cron scope lifetime, and sibling policy paths. That evidence explains the retained contracts but does **not** transfer to this new composition SHA.

The linear candidate must receive fresh exact-SHA GitHub CI and fresh semantic review. Broad Python/Desktop tests, typechecks, dependency installs, and production builds are prohibited on the Pi; GitHub Actions is the authoritative proving surface. Local work is limited to read-only manifest/source audits, `git diff --check`, tiny syntax/compile checks for directly changed files, ARM64/runtime-specific smoke checks, database work, and staged rollout verification.

Before promotion, the exact final SHA must satisfy all of the following:

1. Candidate worktree clean; no active merge/cherry-pick and no conflict markers.
2. Both protected live commit and final upstream cutoff are ancestors.
3. Candidate-only diff against final upstream contains no Project-owned files or custom Project authority symbols.
4. Capability-specific Codex-STT source/config/test/dependency search is empty while normal Codex inference remains intact.
5. GitHub CI runs the focused and broad Python overlay suites at the exact candidate SHA.
6. GitHub CI runs Desktop focused tests, repository-wide typecheck, and production build at the exact candidate SHA.
7. GitHub CI runs the broad hermetic regression/security/dependency gates without weakening or rewriting workflows.
8. Fresh independent semantic reviews inspect the immutable exact SHA; prior-SHA reviews are context only.
9. Production service PID/start time/restart count and deployed release remain unchanged until explicit activation approval.

## Production boundary

This manifest does not authorize deployment or restart. Source promotion, release creation, Desktop rebuild/install, plugin activation, and each gateway restart are separate operations requiring their own verified handoff and approval.
