#!/usr/bin/env bash
# Commit the rpi5 dependency environment with official Hermes commands.
#
# The default mode commits the PM environment only. It does not edit
# config.yaml, systemd units, or profile homes. --sideline-dropins moves
# gateway drop-in files and nothing else. --rollback restores those drop-ins
# and, when a claim backup exists, the external install stamp. No mode prints
# credential values.
#
# Idempotent. A missing runtime facts.json is gated by a throwaway
# `python -m pm.build_env` (no --record-selection, so it cannot write
# installs/<key>/facts.json) plus import checks. Only after those imports pass
# does `hermes pm install` build a fresh generation; that command's own
# `environment.check()` (uv pip check) runs inside Venv.apply, and facts.json is
# recorded only after apply returns (pm/packages.py, pm/install.py).
#
# Usage:
#   docs/ops/rpi5-vanilla-migration.sh                 # build + commit if needed
#   docs/ops/rpi5-vanilla-migration.sh --check         # exit 0 only when committed imports pass
#   docs/ops/rpi5-vanilla-migration.sh --preflight     # unit inventory + dry-run; abort on blockers
#   docs/ops/rpi5-vanilla-migration.sh --preflight-fold
#       # after gateway.standalone is unset on katie and alice only
#   docs/ops/rpi5-vanilla-migration.sh --config-edits
#       # kids share Hermes - Aoba (9d36a60d-2e7e-41c0-a90f-b47b0036e9c2)
#       # key_prefix KATIE_MATRIX_ / ALICE_MATRIX_, strip_prefix true -> MATRIX_*
#       # copy dad's BWS_ACCESS_TOKEN into each kid .env; do not print it
#   docs/ops/rpi5-vanilla-migration.sh --renderer-keys /path/to/render_kid_matrix_env.py
#   docs/ops/rpi5-vanilla-migration.sh --matrix-probe katie
#   docs/ops/rpi5-vanilla-migration.sh --alice-mcp-env          # print paths, do not write
#   docs/ops/rpi5-vanilla-migration.sh --alice-mcp-env --write  # write alice config.yaml
#   docs/ops/rpi5-vanilla-migration.sh --sideline-dropins
#   docs/ops/rpi5-vanilla-migration.sh --orphans
#   docs/ops/rpi5-vanilla-migration.sh --gc            # refused before 2026-10-16 12:41 Taipei
#   docs/ops/rpi5-vanilla-migration.sh --rollback      # restore sideline'd drop-ins and the external stamp; print the rest
#
# --preflight greps hermes/hermex units for EnvironmentFile, ExecStartPre, and
# Environment=, and runs `hermes gateway migrate --multiplex --dry-run`. The
# dry-run exits 0 when blockers are present; the helper aborts anyway. Neither
# mode prints Environment= values or command arguments. The early pass aborts
# if wife or dad are "left alone" (they are not standalone) or if only one
# profile is visible. Katie and alice left alone is expected on that pass.
# --preflight-fold aborts while wife, dad, katie, or alice are still "left alone".
#
# Exit codes:
#   0  committed interpreter imports the required modules, or the selected mode succeeded
#   1  facts.json absent (--check), the pre-commit import gate failed, a preflight
#      blocker, or an orphan/drop-in classification failure
#   2  facts.json exists but imports failed (refusing to rebuild), or more than one
#      hermes-gateway.service.d was found
#   3  wrong checkout (PR 46/47 still present, or install key is not this Pi's)
#   4  driver interpreter or official launcher missing, or unknown arguments
set -euo pipefail

CHECKOUT="${HERMES_CHECKOUT:-${HOME}/.hermes/hermes-agent}"
EXPECTED_KEY="${HERMES_INSTALL_KEY:-5935b2ebc06c58fa}"
MODE="apply"
case "${1:-}" in
  --check) MODE="check" ;;
  --preflight) MODE="preflight" ;;
  --preflight-fold) MODE="preflight-fold" ;;
  --sideline-dropins) MODE="sideline-dropins" ;;
  --orphans) MODE="orphans" ;;
  --gc) MODE="gc" ;;
  --rollback) MODE="rollback" ;;
  --config-edits) MODE="config-edits" ;;
  --renderer-keys) MODE="renderer-keys" ;;
  --matrix-probe) MODE="matrix-probe" ;;
  --alice-mcp-env) MODE="alice-mcp-env" ;;
  "") MODE="apply" ;;
  *)
    echo "usage: $0 [--check | --preflight | --preflight-fold | --config-edits | --renderer-keys PATH | --matrix-probe PROFILE | --alice-mcp-env [--write] | --sideline-dropins | --orphans | --gc | --rollback]" >&2
    exit 4
    ;;
esac

if [[ ! -d "$CHECKOUT/pm" || ! -f "$CHECKOUT/pyproject.toml" ]]; then
  echo "checkout is not a Hermes tree: ${CHECKOUT}" >&2
  exit 3
fi
if [[ -f "$CHECKOUT/pm/deployment_guard.py" ]]; then
  echo "refusing: ${CHECKOUT} still contains pm/deployment_guard.py (PR 46). Check out the vanilla revision first." >&2
  exit 3
fi
if grep -q "CONFIG_OPT_OUT_REASON" "$CHECKOUT/hermes_cli/gateway_multiplex_mode.py"; then
  echo "refusing: ${CHECKOUT} still honors multiplex_profiles false (PR 47). Check out the vanilla revision first." >&2
  exit 3
fi

# The external venv must not be the selected interpreter for child PM commands.
unset VIRTUAL_ENV PYTHONHOME
stripped=""
IFS=':' read -r -a _path_parts <<< "${PATH}"
for part in "${_path_parts[@]}"; do
  case "$part" in
    "${HOME}/.hermes/venvs/"*) ;;
    *) stripped="${stripped:+${stripped}:}${part}" ;;
  esac
done
PATH="$stripped"
export PATH

pick_driver() {
  if [[ -n "${HERMES_DRIVER_PYTHON:-}" ]]; then
    if [[ ! -x "${HERMES_DRIVER_PYTHON}" ]]; then
      echo "HERMES_DRIVER_PYTHON is not executable" >&2
      exit 4
    fi
    printf '%s\n' "${HERMES_DRIVER_PYTHON}"
    return
  fi
  local cand
  for cand in /usr/bin/python3 /usr/local/bin/python3; do
    if [[ -x "$cand" ]] && "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
      printf '%s\n' "$cand"
      return
    fi
  done
  # Driver only. pm.build_env / hermes pm install use PM's pinned toolchain
  # (pm/environment.py managed_environment), not this interpreter, for the venv.
  cand="${HOME}/.hermes/venvs/hermes-py314-v0.21.7/bin/python"
  if [[ -x "$cand" ]]; then
    printf '%s\n' "$cand"
    return
  fi
  echo "no Python >= 3.11 available to drive PM" >&2
  exit 4
}

DRIVER="$(pick_driver)"
export PYTHONPATH="$CHECKOUT"
cd "$CHECKOUT"

KEY="$("$DRIVER" -c 'from pathlib import Path; from pm.environments import install_key; print(install_key(Path(".").resolve()))')"
if [[ "$KEY" != "$EXPECTED_KEY" ]]; then
  echo "refusing: checkout install key is ${KEY}, expected ${EXPECTED_KEY}. Set HERMES_CHECKOUT to ~/.hermes/hermes-agent." >&2
  exit 3
fi

HELPER="${CHECKOUT}/docs/ops/rpi5_migration.py"
BACKUP_DROPINS="${HOME}/rpi5-unit-backup/gateway-dropins"
FACTS="${HOME}/.hermes/installs/${EXPECTED_KEY}/facts.json"
ENVIRONMENTS="${HOME}/.hermes/installs/${EXPECTED_KEY}/environments"

unit_roots() {
  local xdg="${XDG_CONFIG_HOME:-${HOME}/.config}"
  printf '%s\n' \
    "${xdg}/systemd/user" \
    "${xdg}/systemd/system" \
    /etc/systemd/user \
    /etc/systemd/system \
    /usr/lib/systemd/user \
    /usr/lib/systemd/system \
    /lib/systemd/system \
    /run/systemd/system \
    /run/systemd/user
}

helper() {
  "$DRIVER" "$HELPER" "$@"
}

# publish_launchers leaves updateMechanism external alone, so `pm install python`
# can succeed while this file is still missing. claim-managed backs the stamp
# up, rewrites it with write_source_stamp, publishes the launcher, and verifies
# it. A stamp that is not external is left unchanged; the check below still
# exits 4 when the launcher is absent.
ensure_official_launcher() {
  local launcher="${CHECKOUT}/.hermes/bin/hermes"
  if [[ -x "$launcher" ]]; then
    return 0
  fi
  helper claim-managed --checkout "$CHECKOUT"
}

# One hermes-gateway.service.d. Two scopes means the operator has to pass the
# directory by running the helper; guessing would sideline the wrong unit.
locate_gateway_dropins() {
  local args=() root
  while IFS= read -r root; do
    args+=(--root "$root")
  done < <(unit_roots)
  helper locate-dropins "${args[@]}"
}

run_inventory() {
  local args=() root
  while IFS= read -r root; do
    args+=(--root "$root")
  done < <(unit_roots)
  helper inventory "${args[@]}"
}

# Dry-run output stays in a 0600 file and is not echoed. The helper prints
# blocker lines with assignment values redacted.
run_dry_run_classify() {
  local capture class code fold=()
  if [[ "$1" == "fold" ]]; then
    fold=(--require-fold)
  fi
  capture="$(mktemp)"
  chmod 600 "$capture"
  set +e
  "$DRIVER" -m hermes_cli.main gateway migrate --multiplex --dry-run >"$capture" 2>&1
  code=$?
  if [[ ${#fold[@]} -gt 0 ]]; then
    helper classify-dry-run --command-exit "$code" "${fold[@]}" <"$capture"
  else
    helper classify-dry-run --command-exit "$code" <"$capture"
  fi
  class=$?
  set -e
  rm -f "$capture"
  return "$class"
}

if [[ "$MODE" == "config-edits" ]]; then
  helper config-edits
  exit $?
fi

if [[ "$MODE" == "renderer-keys" ]]; then
  if [[ -z "${2:-}" ]]; then
    echo "usage: $0 --renderer-keys /path/to/render_kid_matrix_env.py" >&2
    exit 4
  fi
  helper renderer-keys --path "$2"
  exit $?
fi

if [[ "$MODE" == "matrix-probe" ]]; then
  if [[ -z "${2:-}" ]]; then
    echo "usage: $0 --matrix-probe <profile>" >&2
    exit 4
  fi
  if [[ "$2" == "default" ]]; then
    probe_home="${HOME}/.hermes"
  else
    probe_home="${HOME}/.hermes/profiles/$2"
  fi
  helper matrix-probe --home "$probe_home"
  exit $?
fi

if [[ "$MODE" == "alice-mcp-env" ]]; then
  args=()
  while IFS= read -r root; do
    args+=(--root "$root")
  done < <(unit_roots)
  alice_config="${HOME}/.hermes/profiles/alice/config.yaml"
  if [[ "${2:-}" == "--write" ]]; then
    helper alice-mcp-env "${args[@]}" --config "$alice_config"
  else
    helper alice-mcp-env "${args[@]}" --config "$alice_config" --dry-run
  fi
  exit $?
fi

if [[ "$MODE" == "preflight" || "$MODE" == "preflight-fold" ]]; then
  run_inventory || exit $?
  if [[ "$MODE" == "preflight-fold" ]]; then
    run_dry_run_classify fold || exit $?
  else
    run_dry_run_classify early || exit $?
  fi
  exit 0
fi

if [[ "$MODE" == "sideline-dropins" ]]; then
  dropin_dir="$(locate_gateway_dropins)" || exit $?
  helper sideline-dropins --dropin-dir "$dropin_dir" --backup "$BACKUP_DROPINS"
  exit $?
fi

if [[ "$MODE" == "orphans" ]]; then
  helper orphans --facts "$FACTS" --environments "$ENVIRONMENTS"
  exit $?
fi

if [[ "$MODE" == "gc" ]]; then
  helper gc-gate
  helper orphans --facts "$FACTS" --environments "$ENVIRONMENTS"
  ensure_official_launcher
  LAUNCHER="${CHECKOUT}/.hermes/bin/hermes"
  if [[ ! -x "$LAUNCHER" ]]; then
    echo "official launcher missing: ${LAUNCHER}" >&2
    exit 4
  fi
  "$LAUNCHER" pm gc
  exit $?
fi

if [[ "$MODE" == "rollback" ]]; then
  dropin_dir="$(locate_gateway_dropins)" || exit $?
  set +e
  helper rollback-dropins --dropin-dir "$dropin_dir" --backup "$BACKUP_DROPINS"
  dropin_rc=$?
  helper restore-external-stamp --checkout "$CHECKOUT"
  stamp_rc=$?
  set -e
  if [[ "$dropin_rc" -ne 0 ]]; then
    exit "$dropin_rc"
  fi
  exit "$stamp_rc"
fi

# Prints three lines: status, interpreter, extras. status is ok | absent | broken.
read_selection() {
  "$DRIVER" -c '
import json, sys
from pathlib import Path
from pm.environments import committed_venv, runtime_facts_path, venv_python
from pm.paths import repo_root
root = repo_root()
facts = runtime_facts_path(root)
if not facts.is_file():
    print("absent")
    print("-")
    print("-")
    sys.exit(0)
try:
    venv = committed_venv(root)
except RuntimeError:
    print("broken")
    print("-")
    print("-")
    sys.exit(0)
if venv is None:
    print("broken")
    print("-")
    print("-")
    sys.exit(0)
py = venv_python(venv)
data = json.loads(facts.read_text(encoding="utf-8-sig"))
enabled = ((data.get("packages") or {}).get("venv") or {}).get("enabled") or []
print("ok")
print(py)
print(",".join(str(item) for item in enabled))
'
}

import_check() {
  local py="$1"
  env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV "$py" -c '
import importlib, sys
mods = (
    "telegram", "discord", "aiohttp", "mautrix", "olm",
    "aiosqlite", "asyncpg", "aiohttp_socks", "fastapi", "mcp", "markdown",
    "imaplib", "smtplib",
)
bad = 0
for name in mods:
    try:
        importlib.import_module(name)
    except Exception as exc:
        print(f"{name}:{type(exc).__name__}", file=sys.stderr)
        bad = 1
raise SystemExit(bad)
'
}

mapfile -t selection < <(read_selection)
status="${selection[0]:-broken}"
selected_py="${selection[1]:--}"
extras="${selection[2]:--}"

if [[ "$status" == "ok" ]]; then
  if import_check "$selected_py"; then
    echo "committed interpreter imports ok"
    echo "extras: ${extras}"
    echo "interpreter: ${selected_py}"
    exit 0
  fi
  echo "facts.json is committed but required imports failed; not rebuilding" >&2
  exit 2
fi

if [[ "$status" == "broken" ]]; then
  echo "runtime facts.json is present but the recorded environment is unusable; not rebuilding" >&2
  exit 2
fi

if [[ "$MODE" == "check" ]]; then
  echo "runtime facts.json is absent" >&2
  exit 1
fi

ensure_official_launcher
LAUNCHER="${CHECKOUT}/.hermes/bin/hermes"
if [[ ! -x "$LAUNCHER" ]]; then
  echo "official launcher missing: ${LAUNCHER}" >&2
  exit 4
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# No --record-selection: that flag writes the tool-store facts, not the runtime
# selection, and this throwaway must not commit anything.
build_out="$("$DRIVER" -m pm.build_env \
  --source "$CHECKOUT" \
  --out "${tmp}/venv" \
  --extra all \
  --extra telegram \
  --extra discord \
  --extra matrix)"
throwaway="$(printf '%s\n' "$build_out" | tail -n 1)"
if [[ ! -x "$throwaway" ]]; then
  echo "throwaway build did not produce an interpreter" >&2
  exit 1
fi
if ! import_check "$throwaway"; then
  echo "pre-commit import gate failed; facts.json was not written" >&2
  exit 1
fi

rm -rf "$tmp"
trap - EXIT

# Official commit. sync_venv(extras, explicit=True) unions these extras; passing
# them without "all" would record only the named extras on a fresh install.
"$LAUNCHER" pm install --extra all --extra telegram --extra discord --extra matrix

mapfile -t selection < <(read_selection)
status="${selection[0]:-broken}"
selected_py="${selection[1]:--}"
extras="${selection[2]:--}"
if [[ "$status" != "ok" ]]; then
  echo "hermes pm install returned without a committed environment" >&2
  exit 1
fi
if ! import_check "$selected_py"; then
  echo "committed interpreter failed the import re-check" >&2
  exit 2
fi
echo "committed interpreter imports ok"
echo "extras: ${extras}"
echo "interpreter: ${selected_py}"
