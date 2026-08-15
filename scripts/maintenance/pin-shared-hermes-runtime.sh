#!/usr/bin/env bash
# Pin Default/Dad/Wife gateways AND Dashboard to one immutable release.
# Those units share SQLite state and must use the same source + 3.53.1 interpreter.
#
# Usage:
#   pin-shared-hermes-runtime.sh --sha <40-hex>
#   pin-shared-hermes-runtime.sh --sha <40-hex> --write
#   pin-shared-hermes-runtime.sh --sha <40-hex> --write --restart-dashboard
#
# Dry-run prints drop-in paths. Never restarts gateways.

set -euo pipefail

sha=""
rel=""
write=false
restart_dashboard=false
while [ $# -gt 0 ]; do
  case "$1" in
    --sha) sha="$2"; shift 2 ;;
    --rel-dir) rel="$2"; shift 2 ;;
    --write) write=true ;;
    --restart-dashboard) restart_dashboard=true ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

case "$sha" in
  [0-9a-f][0-9a-f][0-9a-f][0-9a-f]*)
    [ "${#sha}" -eq 40 ] || { echo "refusing: --sha must be 40 hex chars" >&2; exit 2; }
    ;;
  *) echo "refusing: --sha must be 40 hex chars" >&2; exit 2 ;;
esac

if [ -z "$rel" ]; then
  rel="/home/pi/.hermes/releases/andy-runtime-${sha}-sqlite3531"
fi
py="${rel}/.venv/bin/python"
if [ ! -x "$py" ]; then
  echo "refusing: missing $py" >&2
  exit 2
fi
if [ ! -f "${rel}/hermes_cli/web_dist/index.html" ]; then
  echo "refusing: ${rel}/hermes_cli/web_dist/index.html missing (copy a built dashboard bundle before pin)" >&2
  exit 2
fi

path="${rel}/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

emit() {
  unit="$1"
  home="$2"
  start="$3"
  dest="/etc/systemd/system/${unit}.d/92-andy-runtime-current.conf"
  if [ "$write" != true ]; then
    echo "would write $dest"
    return
  fi
  umask 022
  tmp=$(mktemp /tmp/hermes-92.XXXXXX)
  {
    echo "[Service]"
    echo "Environment=\"PATH=${path}\""
    echo "Environment=\"PYTHONPATH=${rel}\""
    echo "Environment=\"HERMES_HOME=${home}\""
    echo "Environment=\"HERMES_RUNTIME_SHA=${sha}\""
    echo "Environment=\"PYTHONDONTWRITEBYTECODE=1\""
    echo "ExecStart="
    echo "ExecStart=${start}"
  } >"$tmp"
  sudo install -m 644 -o root -g root "$tmp" "$dest"
  rm -f "$tmp"
  echo "wrote $dest"
}

emit hermes-gateway.service /home/pi/.hermes \
  "${py} -m hermes_cli.main gateway run"
emit hermes-gateway-dad.service /home/pi/.hermes/profiles/dad \
  "${py} -m hermes_cli.main --profile dad gateway run"
emit hermes-gateway-wife.service /home/pi/.hermes/profiles/wife \
  "${py} -m hermes_cli.main --profile wife gateway run"
emit hermes-dashboard.service /home/pi/.hermes \
  "${py} -m hermes_cli.main dashboard --host 127.0.0.1 --port 9119 --no-open --skip-build"

if [ "$write" = true ]; then
  sudo systemctl daemon-reload
  echo "daemon-reloaded"
fi

if [ "$restart_dashboard" = true ]; then
  if [ "$write" != true ]; then
    echo "refusing: --restart-dashboard requires --write" >&2
    exit 2
  fi
  sudo systemctl try-restart hermes-dashboard.service
  echo "try-restarted hermes-dashboard.service"
fi
