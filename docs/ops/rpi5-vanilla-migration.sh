#!/usr/bin/env bash
# Commit the rpi5 dependency environment with official Hermes commands.
#
# Scope: the PM environment only. This script does not edit config.yaml, systemd
# units, or profile homes, and it does not print credential values.
#
# Idempotent. A missing runtime facts.json is gated by a throwaway
# `python -m pm.build_env` (no --record-selection, so it cannot write
# installs/<key>/facts.json) plus import checks. Only after those imports pass
# does `hermes pm install` build a fresh generation; that command's own
# `environment.check()` (uv pip check) runs inside Venv.apply, and facts.json is
# recorded only after apply returns (pm/packages.py, pm/install.py).
#
# Usage:
#   docs/ops/rpi5-vanilla-migration.sh           # build + commit if needed
#   docs/ops/rpi5-vanilla-migration.sh --check   # exit 0 only when committed imports pass
#
# Exit codes:
#   0  committed interpreter imports the required modules
#   1  facts.json absent (--check), or the pre-commit import gate failed
#   2  facts.json exists but imports failed; refusing to rebuild
#   3  wrong checkout (PR 46/47 still present, or install key is not this Pi's)
#   4  driver interpreter or official launcher missing
set -euo pipefail

CHECKOUT="${HERMES_CHECKOUT:-${HOME}/.hermes/hermes-agent}"
EXPECTED_KEY="${HERMES_INSTALL_KEY:-5935b2ebc06c58fa}"
MODE="apply"
if [[ "${1:-}" == "--check" ]]; then
  MODE="check"
elif [[ $# -gt 0 ]]; then
  echo "usage: $0 [--check]" >&2
  exit 4
fi

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
