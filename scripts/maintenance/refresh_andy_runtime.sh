#!/usr/bin/env bash
set -euo pipefail

# Rebase Andy's small runtime patch queue onto current upstream/main.
# Usage: scripts/maintenance/refresh_andy_runtime.sh [--push] [--no-tests]

push=false
run_tests=true
for arg in "$@"; do
  case "$arg" in
    --push) push=true ;;
    --no-tests) run_tests=false ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

repo=$(git rev-parse --show-toplevel)
cd "$repo"
branch=$(git branch --show-current)
if [[ "$branch" != "andy-runtime" ]]; then
  echo "refusing: expected branch andy-runtime, found ${branch:-detached HEAD}" >&2
  exit 2
fi
if [[ -n $(git status --porcelain) ]]; then
  echo "refusing: worktree is not clean" >&2
  exit 2
fi

git fetch upstream main --prune
old_head=$(git rev-parse HEAD)
upstream_head=$(git rev-parse upstream/main)
if git merge-base --is-ancestor "$upstream_head" HEAD; then
  echo "andy-runtime already contains upstream/main ($upstream_head)"
else
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  backup="backup/andy-runtime-before-refresh-$stamp"
  git branch "$backup" "$old_head"
  echo "backup: $backup -> $old_head"
  if ! git rebase upstream/main; then
    echo "rebase stopped on a conflict; resolve and continue, or run: git rebase --abort" >&2
    exit 1
  fi
fi

if $run_tests; then
  python_bin=python3
  if [[ -x "$repo/.venv/bin/python" ]]; then
    python_bin="$repo/.venv/bin/python"
  fi
  "$python_bin" -m pytest -q \
    tests/gateway/test_discord_free_response.py \
    tests/gateway/test_discord_slash_commands.py \
    tests/tools/test_discord_tool.py \
    tests/test_bitwarden_encrypted_cache_fail_closed.py \
    tests/gateway/test_line_group_authorization.py \
    tests/gateway/test_line_group_policies.py \
    tests/cron/test_discord_delivery_format.py \
    tests/gateway/test_email_custom_notifications.py
fi

new_head=$(git rev-parse HEAD)
echo "andy-runtime refreshed: $old_head -> $new_head"
if $push; then
  git push --force-with-lease andy andy-runtime
  echo "pushed andy/andy-runtime"
else
  echo "not pushed; publish with: git push --force-with-lease andy andy-runtime"
fi
