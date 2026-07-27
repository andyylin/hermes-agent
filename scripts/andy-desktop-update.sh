#!/usr/bin/env bash
# Safely update and rebuild Andy's canonical Hermes Desktop runtime.
set -Eeuo pipefail

REMOTE="origin"
BRANCH="andy-runtime"
HERMES_HOME_VALUE="${HERMES_HOME:-$HOME/.hermes}"
REPO=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --repo)
            [ "$#" -ge 2 ] || { printf '%s\n' '--repo requires a path' >&2; exit 2; }
            REPO="$2"
            shift 2
            ;;
        --hermes-home)
            [ "$#" -ge 2 ] || { printf '%s\n' '--hermes-home requires a path' >&2; exit 2; }
            HERMES_HOME_VALUE="$2"
            shift 2
            ;;
        *)
            printf 'Unknown option: %s\n' "$1" >&2
            exit 2
            ;;
    esac
done

HERMES_HOME="$HERMES_HOME_VALUE"
export HERMES_HOME
REPO="${REPO:-$HERMES_HOME/hermes-agent}"
export PATH="$HERMES_HOME/node/bin:$HOME/.local/bin:$PATH"

cd "$REPO"

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    printf 'Refusing to update: tracked changes exist in %s\n' "$REPO" >&2
    git status --short
    exit 2
fi

expected="https://github.com/andyylin/hermes-agent.git"
actual="$(git remote get-url "$REMOTE" 2>/dev/null || true)"
if [ "$actual" != "$expected" ]; then
    printf "Refusing to update: origin is '%s', expected '%s'\n" "$actual" "$expected" >&2
    exit 3
fi

git config --replace-all remote.origin.fetch "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"
git fetch --prune "$REMOTE"
git switch "$BRANCH"
git branch --set-upstream-to="$REMOTE/$BRANCH" "$BRANCH" >/dev/null
git merge --ff-only "$REMOTE/$BRANCH"

case "$(uname -s)" in
    Darwin|Linux) ;;
    *) printf 'Unsupported operating system\n' >&2; exit 4 ;;
esac

desktop_binaries() {
    case "$(uname -s)" in
        Darwin)
            printf '%s\n' \
                "$REPO/apps/desktop/release/mac-arm64/Hermes.app/Contents/MacOS/Hermes" \
                "$REPO/apps/desktop/release/mac-x64/Hermes.app/Contents/MacOS/Hermes" \
                "$REPO/apps/desktop/release/mac/Hermes.app/Contents/MacOS/Hermes"
            ;;
        Linux)
            for binary in "$REPO"/apps/desktop/release/*/[Hh]ermes; do
                [ -e "$binary" ] && printf '%s\n' "$binary"
            done
            ;;
    esac
}
running_desktop_pids() {
    while IFS= read -r binary; do
        ps -ww -axo pid=,command= | awk -v target="$binary" '
            {
                pid = $1
                sub(/^[[:space:]]*[0-9]+[[:space:]]+/, "", $0)
                if ($0 == target) print pid
            }
        '
    done < <(desktop_binaries)
}
desktop_pids="$(running_desktop_pids)"
if [ -n "$desktop_pids" ]; then
    # Exact executable-path matching keeps another Hermes checkout untouched.
    while IFS= read -r pid; do kill -TERM "$pid"; done <<< "$desktop_pids"
fi

for _ in {1..15}; do
    [ -n "$(running_desktop_pids)" ] || break
    sleep 1
done
if [ -n "$(running_desktop_pids)" ]; then
    printf 'Hermes Desktop did not quit cleanly; refusing to overwrite the running bundle\n' >&2
    exit 5
fi

./venv/bin/hermes desktop --force-build --build-only
./venv/bin/hermes desktop --skip-build

printf 'Hermes custom Desktop rebuilt and launched.\n'
printf 'Branch: %s\n' "$(git branch --show-current)"
printf 'Commit: %s\n' "$(git rev-parse HEAD)"
