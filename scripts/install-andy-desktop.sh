#!/usr/bin/env bash
# One-line bootstrap for Andy's canonical Hermes Desktop runtime.
# Usage: curl -fsSL https://raw.githubusercontent.com/andyylin/hermes-agent/andy-runtime/scripts/install-andy-desktop.sh | bash
set -Eeuo pipefail

REPO_URL="https://github.com/andyylin/hermes-agent.git"
BRANCH="andy-runtime"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
export HERMES_HOME
INSTALL_DIR="$HERMES_HOME/hermes-agent"
LAUNCH=true
CHECKOUT_ONLY=false
DRY_RUN=false

log() { printf '\033[1;34m→\033[0m %s\n' "$*"; }
ok() { printf '\033[1;32m✓\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }
run() {
    printf '+ '
    printf '%q ' "$@"
    printf '\n'
    if [ "$DRY_RUN" = false ]; then "$@"; fi
}

usage() {
    cat <<'EOF'
Install Andy's canonical Hermes Desktop runtime.

Options:
  --no-launch          Build but do not launch Desktop
  --checkout-only     Prepare the custom Git checkout, then stop
  --dry-run           Print intended commands without changing anything
  --install-dir PATH  Override the source checkout path
  --repo-url URL      Override the Git repository (testing/recovery)
  --branch NAME       Override the runtime branch (testing/recovery)
  -h, --help          Show this help
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --no-launch) LAUNCH=false; shift ;;
        --checkout-only) CHECKOUT_ONLY=true; shift ;;
        --dry-run) DRY_RUN=true; shift ;;
        --install-dir) [ "$#" -ge 2 ] || die "--install-dir requires a path"; INSTALL_DIR="$2"; shift 2 ;;
        --repo-url) [ "$#" -ge 2 ] || die "--repo-url requires a URL"; REPO_URL="$2"; shift 2 ;;
        --branch) [ "$#" -ge 2 ] || die "--branch requires a name"; BRANCH="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "Unknown option: $1" ;;
    esac
done

case "$(uname -s)" in
    Darwin|Linux) ;;
    *) die "This bootstrap supports macOS and Linux. Use the Windows PowerShell installer on Windows." ;;
esac

command -v git >/dev/null 2>&1 || die "Git is required. Install Git, then rerun this command."

if [ "$DRY_RUN" = true ]; then
    log "Dry run for $REPO_URL ($BRANCH) at $INSTALL_DIR"
fi

if [ ! -e "$INSTALL_DIR" ]; then
    run mkdir -p "$(dirname "$INSTALL_DIR")"
    run git clone --single-branch --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
elif [ ! -d "$INSTALL_DIR/.git" ]; then
    die "$INSTALL_DIR exists but is not a Git checkout; move it aside and rerun"
else
    cd "$INSTALL_DIR"
    tracked_changes="$(git status --porcelain --untracked-files=no 2>/dev/null || true)"
    [ -z "$tracked_changes" ] || die "Tracked source changes exist in $INSTALL_DIR; commit or stash them before installing"

    current_origin="$(git remote get-url origin 2>/dev/null || true)"
    if [ -n "$current_origin" ] && [ "$current_origin" != "$REPO_URL" ]; then
        current_upstream="$(git remote get-url upstream 2>/dev/null || true)"
        if [ -n "$current_upstream" ] && [ "$current_upstream" != "$current_origin" ]; then
            backup_remote="upstream-before-andy-bootstrap"
            suffix=2
            while git remote get-url "$backup_remote" >/dev/null 2>&1; do
                backup_remote="upstream-before-andy-bootstrap-$suffix"
                suffix=$((suffix + 1))
            done
            run git remote rename upstream "$backup_remote"
        fi
        if git remote get-url upstream >/dev/null 2>&1; then
            run git remote set-url upstream "$current_origin"
        else
            run git remote add upstream "$current_origin"
        fi
        run git remote set-url origin "$REPO_URL"
    elif [ -z "$current_origin" ]; then
        run git remote add origin "$REPO_URL"
    fi

    run git config --replace-all remote.origin.fetch "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"
    run git fetch --prune origin
    if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
        run git switch "$BRANCH"
    else
        run git switch --create "$BRANCH" --track "origin/$BRANCH"
    fi
    run git branch --set-upstream-to="origin/$BRANCH" "$BRANCH"
    run git merge --ff-only "origin/$BRANCH"
fi

if [ "$DRY_RUN" = true ]; then
    if [ "$CHECKOUT_ONLY" = true ]; then exit 0; fi
    log "Would run the custom installer, build Desktop, pin the update channel, and install hermes-custom-update"
    exit 0
fi

cd "$INSTALL_DIR"
head_sha="$(git rev-parse HEAD)"
remote_sha="$(git rev-parse "origin/$BRANCH")"
[ "$head_sha" = "$remote_sha" ] || die "Checkout does not match origin/$BRANCH"

if [ "$CHECKOUT_ONLY" = true ]; then
    ok "Custom checkout ready: $BRANCH@$head_sha"
    exit 0
fi

log "Installing Hermes dependencies from $BRANCH"
bash "$INSTALL_DIR/scripts/install.sh" --branch "$BRANCH" --dir "$INSTALL_DIR" --skip-setup

export PATH="$HERMES_HOME/node/bin:$HOME/.local/bin:$PATH"
HERMES_BIN="$INSTALL_DIR/venv/bin/hermes"
[ -x "$HERMES_BIN" ] || die "Hermes executable was not created at $HERMES_BIN"

log "Pinning Desktop updates to $BRANCH"
case "$(uname -s)" in
    Darwin) UPDATE_DIR="$HOME/Library/Application Support/Hermes" ;;
    Linux) UPDATE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/Hermes" ;;
esac
mkdir -p "$UPDATE_DIR"
"$INSTALL_DIR/venv/bin/python" - "$UPDATE_DIR/updates.json" "$BRANCH" <<'PY'
import json, os, sys, tempfile
path, branch = sys.argv[1:3]
try:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        data = {}
except (FileNotFoundError, json.JSONDecodeError, OSError):
    data = {}
data["branch"] = branch
fd, temporary = tempfile.mkstemp(prefix="updates.", suffix=".tmp", dir=os.path.dirname(path))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY

log "Installing guarded update command"
mkdir -p "$HOME/.local/bin"
update_command="$HOME/.local/bin/hermes-custom-update"
update_command_tmp="$update_command.tmp.$$"
{
    printf '%s\n' '#!/usr/bin/env bash' 'set -e'
    printf 'exec %q --repo %q --hermes-home %q "$@"\n' \
        "$INSTALL_DIR/scripts/andy-desktop-update.sh" "$INSTALL_DIR" "$HERMES_HOME"
} > "$update_command_tmp"
chmod 0755 "$update_command_tmp"
mv "$update_command_tmp" "$update_command"

log "Stopping a running source-built Desktop before replacing its bundle"
desktop_binaries() {
    case "$(uname -s)" in
        Darwin)
            printf '%s\n' \
                "$INSTALL_DIR/apps/desktop/release/mac-arm64/Hermes.app/Contents/MacOS/Hermes" \
                "$INSTALL_DIR/apps/desktop/release/mac-x64/Hermes.app/Contents/MacOS/Hermes" \
                "$INSTALL_DIR/apps/desktop/release/mac/Hermes.app/Contents/MacOS/Hermes"
            ;;
        Linux)
            for binary in "$INSTALL_DIR"/apps/desktop/release/*/[Hh]ermes; do
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
    die "Hermes Desktop did not quit cleanly; refusing to overwrite the running bundle"
fi

log "Building native Hermes Desktop"
if [ "$LAUNCH" = true ]; then
    "$HERMES_BIN" desktop --force-build
else
    "$HERMES_BIN" desktop --force-build --build-only
fi

ok "Hermes Desktop installed from $BRANCH@$head_sha"
ok "Future updates: use the in-app Update button or run hermes-custom-update"
