# Andy upstream-first overlay queue

This directory replaces historical merge archaeology with a small, explicit replay queue.

## Contract

- Start every refresh from one frozen canonical upstream SHA or release tag.
- Apply the ordered patches in `series`.
- Every payload path is classified exactly once in `manifest.json`.
- A new or removed payload path makes queue refresh fail closed until it is classified deliberately.
- Patches describe the final validated behavior delta, not the tangled commit history that produced it.
- Source integration ends after exact-SHA CI and semantic review. Venv construction, database work, canaries, service restarts, and activation remain separate approvals.
- Never run broad tests, builds, dependency installation, or repository-wide type/lint checks on the Raspberry Pi deployment host.

## Build a candidate on a newer upstream

Run from any clean clone/worktree containing the committed control plane:

```bash
python3 andy-overlay/scripts/build_candidate.py \
  --repo "$PWD" \
  --upstream <frozen-upstream-sha-or-tag> \
  --control-ref HEAD \
  --output /tmp/hermes-andy-candidate
```

The builder:

1. creates a new detached worktree at the selected upstream commit;
2. verifies every patch checksum;
3. applies each behavioral slice with Git three-way support;
4. commits each slice separately using the operator's configured Git identity;
5. copies this control plane into the result;
6. prints a machine-readable replay report;
7. leaves the diagnostic worktree intact if any patch conflicts.

A conflict is a review request, not permission to take the old file wholesale.

## Refresh the queue after resolving a new upstream

After the replay candidate has been repaired, committed, and validated:

```bash
python3 andy-overlay/scripts/refresh_queue.py \
  --repo "$PWD" \
  --manifest "$PWD/andy-overlay/manifest.json" \
  --base <new-frozen-upstream-sha> \
  --candidate <validated-candidate-sha>
```

If the candidate adds or removes payload paths, edit the explicit group path lists first. The refresher refuses unclassified drift and regenerates checksummed binary-capable patches plus `series`.

Then rebuild once from the same frozen upstream with:

```bash
python3 andy-overlay/scripts/build_candidate.py \
  --repo "$PWD" \
  --upstream <new-frozen-upstream-sha> \
  --control-ref <commit-containing-refreshed-queue> \
  --output /tmp/hermes-andy-replay-proof \
  --expect-control-tree
```

`--expect-control-tree` proves that a clean replay reproduces the committed candidate tree exactly. Only that immutable replayed SHA goes to GitHub CI.

## Current queue

The initial queue is derived from:

- frozen upstream: `533d633ab9d18ce0f05bc44d4077576b69dc58e8`
- validated source candidate: `da4d73940b8e0b9f8a74d0e98fb4ac12d20ba598`
- expected payload tree: `d134b90ba35f8501481f7be6cd4807d030415162`

Slices:

1. metadata and overlay contracts;
2. profile-scoped secrets;
3. canonical state and no-live-FTS;
4. messaging and authorization;
5. cron and email delivery;
6. private memory tree;
7. Desktop profile bridge/bootstrap;
8. retained runtime compatibility.
