#!/usr/bin/env bash
# Normalize shared Hermes services onto one mutable source checkout and one
# stable SQLite-safe interpreter. This command only prepares the next start;
# it never recycles a running service.

set -euo pipefail

source_dir="/home/pi/.hermes/hermes-agent"
python_path="/home/pi/.hermes/hermes-agent/venv/bin/python"
write=false

while [ $# -gt 0 ]; do
  case "$1" in
    --source) source_dir="$2"; shift 2 ;;
    --python) python_path="$2"; shift 2 ;;
    --write) write=true; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

source_dir=$(realpath "$source_dir")
test -x "$python_path" || { echo "refusing: missing interpreter: $python_path" >&2; exit 2; }
python_path="$(cd "$(dirname "$python_path")" && pwd -P)/$(basename "$python_path")"
python_real=$(realpath -e "$python_path")

git -C "$source_dir" diff --quiet
git -C "$source_dir" diff --cached --quiet
test -z "$(git -C "$source_dir" status --porcelain)" || {
  echo "refusing: source checkout is dirty: $source_dir" >&2
  exit 2
}

sha=$(git -C "$source_dir" rev-parse HEAD)
case "$sha" in
  [0-9a-f][0-9a-f][0-9a-f][0-9a-f]*)
    test "${#sha}" -eq 40 || { echo "refusing: invalid source SHA" >&2; exit 2; }
    ;;
  *) echo "refusing: invalid source SHA" >&2; exit 2 ;;
esac

test -f "$source_dir/hermes_cli/web_dist/index.html" || {
  echo "refusing: dashboard bundle is missing from $source_dir/hermes_cli/web_dist" >&2
  exit 2
}

probe=$(
  env -u PYTHONPATH \
    PYTHONPATH="$source_dir" \
    PYTHONNOUSERSITE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    "$python_path" - <<'PY'
import json
import sqlite3
import hermes_cli

assert sqlite3.sqlite_version_info >= (3, 51, 3), sqlite3.sqlite_version
print(json.dumps({
    "sqlite": sqlite3.sqlite_version,
    "module": hermes_cli.__file__,
}))
PY
)

case "$probe" in
  *"$source_dir/hermes_cli/"*) ;;
  *) echo "refusing: Hermes import did not resolve from $source_dir: $probe" >&2; exit 2 ;;
esac

path="$(dirname "$python_path"):/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
stage=$(mktemp -d /tmp/hermes-runtime-normalize.XXXXXX)
trap 'rm -rf "$stage"' EXIT

emit_gateway() {
  unit="$1"
  home="$2"
  profile_args="$3"
  file="$stage/$unit.conf"
  {
    echo "[Service]"
    echo "Environment=\"PATH=$path\""
    echo "Environment=\"VIRTUAL_ENV=$(dirname "$(dirname "$python_path")")\""
    echo "Environment=\"PYTHONPATH=$source_dir\""
    echo "Environment=\"HERMES_HOME=$home\""
    echo "Environment=\"HERMES_RUNTIME_SHA=$sha\""
    echo "Environment=\"PYTHONDONTWRITEBYTECODE=1\""
    echo "Environment=\"PYTHONNOUSERSITE=1\""
    echo "ExecStart="
    echo "ExecStart=$python_path -m hermes_cli.main $profile_args gateway run"
  } >"$file"
}

emit_dashboard() {
  file="$stage/hermes-dashboard.service.conf"
  {
    echo "[Service]"
    echo "Environment=\"PATH=$path\""
    echo "Environment=\"VIRTUAL_ENV=$(dirname "$(dirname "$python_path")")\""
    echo "Environment=\"PYTHONPATH=$source_dir\""
    echo "Environment=\"HERMES_HOME=/home/pi/.hermes\""
    echo "Environment=\"HERMES_RUNTIME_SHA=$sha\""
    echo "Environment=\"PYTHONDONTWRITEBYTECODE=1\""
    echo "Environment=\"PYTHONNOUSERSITE=1\""
    echo "ExecStart="
    echo "ExecStart=/home/pi/.hermes/scripts/hermes-dashboard-backend.sh"
  } >"$file"
}

emit_gateway hermes-gateway.service /home/pi/.hermes ""
emit_gateway hermes-gateway-dad.service /home/pi/.hermes/profiles/dad "--profile dad"
emit_gateway hermes-gateway-wife.service /home/pi/.hermes/profiles/wife "--profile wife"
emit_dashboard

for unit in \
  hermes-gateway.service \
  hermes-gateway-dad.service \
  hermes-gateway-wife.service \
  hermes-dashboard.service; do
  echo "$unit source=$source_dir interpreter=$python_path resolved_interpreter=$python_real sha=$sha"
done
echo "probe=$probe"

if [ "$write" != true ]; then
  echo "dry-run only; pass --write to install the normalized definitions"
  exit 0
fi

retired_names='90-runtime-release.conf 91-upstream-source-sqlite3531.conf 91-upstream-first-canary.conf 91-sqlite3531-interpreter.conf 92-andy-runtime-current.conf 92-canonical-runtime.conf'
for unit in \
  hermes-gateway.service \
  hermes-gateway-dad.service \
  hermes-gateway-wife.service \
  hermes-dashboard.service; do
  dropin="/etc/systemd/system/$unit.d"
  archive="$dropin/retired-runtime-overrides-$stamp"
  sudo install -d -m 755 -o root -g root "$dropin" "$archive"
  for name in $retired_names; do
    if sudo test -f "$dropin/$name"; then
      sudo mv "$dropin/$name" "$archive/$name"
    fi
  done
  sudo install -m 644 -o root -g root \
    "$stage/$unit.conf" \
    "$dropin/92-canonical-runtime.conf"
done

sudo systemctl daemon-reload
if ! sudo systemd-analyze verify \
  hermes-gateway.service \
  hermes-gateway-dad.service \
  hermes-gateway-wife.service \
  hermes-dashboard.service; then
  echo "verification failed; restoring prior runtime definitions" >&2
  for unit in \
    hermes-gateway.service \
    hermes-gateway-dad.service \
    hermes-gateway-wife.service \
    hermes-dashboard.service; do
    dropin="/etc/systemd/system/$unit.d"
    archive="$dropin/retired-runtime-overrides-$stamp"
    sudo rm -f "$dropin/92-canonical-runtime.conf"
    for old in "$archive"/*; do
      test -e "$old" || continue
      sudo mv "$old" "$dropin/$(basename "$old")"
    done
  done
  sudo systemctl daemon-reload
  exit 1
fi

echo "installed normalized definitions; archived superseded runtime drop-ins with stamp $stamp"
echo "running processes are unchanged"