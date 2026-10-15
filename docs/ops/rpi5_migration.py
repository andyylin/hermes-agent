"""Decisions for the rpi5 vanilla migration.

The shell driver ``rpi5-vanilla-migration.sh`` calls this. Output is names,
paths, and plan lines. ``Environment=`` values and command arguments are not
printed: a unit file on this host holds Matrix tokens and bot tokens.

This module does not talk to systemd or Bitwarden. It classifies text and
moves drop-in files the caller points at. It can also claim an external
checkout as PM-managed: back up ``install-stamp.json``, rewrite it with
``write_source_stamp`` (the stamp a fresh self-managed checkout publishes),
publish ``.hermes/bin/hermes``, and restore that backup on rollback.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePath

# Asia/Taipei is UTC+8 all year. A fixed offset avoids a tzdata lookup on the
# host that is about to restart its only gateway.
TAIPEI = timezone(timedelta(hours=8))
# Lease markers from the 2026-10-15 12:41 window are a day old at this instant.
# ``hermes pm gc`` also requires the marker itself to be >= 86400s old.
GC_NOT_BEFORE = datetime(2026, 10, 16, 12, 41, tzinfo=TAIPEI)

# Drop-ins on hermes-gateway.service that pin the external venv's Python.
# ``hermes gateway install`` rewrites the unit and removes only 20-replace.conf.
PINNING_DROPINS = (
    "zzz-main-v0217.conf",
    "zz-py314-runtime.conf",
    "92-canonical-runtime.conf",
)
MEMORY_DROPIN = "memory-limits.conf"
NEW_MEMORY_DROPIN = "zz-multiplex-memory.conf"
RUNTIME_DROPIN = "zz-profile-runtime-env.conf"
GATEWAY_UNIT = "hermes-gateway.service"
# systemd set-property writes these under system.control (or user.control).
# They outrank unit drop-ins, so a 2G MemoryMax there survives the sideline.
_CONTROL_DIR_NAMES = frozenset({"system.control", "user.control"})
_UNIT_BACKUP_SUFFIXES = (
    ".bak",
    ".old",
    ".dpkg-old",
    ".dpkg-dist",
    ".rpmnew",
    ".rpmsave",
)
# python-olm probes clang++. This Pi has gcc/g++ and no clang++.
COMPILER_ENV = (("CC", "gcc"), ("CXX", "g++"))
WEBUI_MANAGED_ENV = (
    "hermes-webui imports run_agent. A self-managed stamp re-execs that process "
    "into the store interpreter. The re-exec activates the managed environment "
    "before the WebUI script runs. extra web (fastapi, uvicorn) is inside all, "
    "which this migration installs. Do not set HERMES_DISABLE_LAZY_INSTALLS."
)
FOLD_PROFILES = ("wife", "dad", "katie", "alice")
# Live host: only the kids opt out. Wife and dad are already in the multiplex plan.
STANDALONE_PROFILES = ("katie", "alice")
ALREADY_SERVED_PROFILES = ("wife", "dad")
ALICE_MCP_SERVER = "game-workshop"
ALICE_MCP_ENV_KEYS = ("ALICE_GAMES_ROOT", "ALICE_PROFILE_ROOT")
# Recorded live value. The writer reads the unit and refuses a different games
# root. ALICE_PROFILE_ROOT is never a constant.
ALICE_GAMES_ROOT_EXPECTED = "/srv/alice-portal/games"
_KANBAN_ENV = "HERMES_KANBAN_DISPATCH_IN_GATEWAY"
_KANBAN_OFF = {"0", "false", "no", "off"}
_MATRIX_NAME = re.compile(r"""["'](MATRIX_[A-Z0-9_]+)["']""")
_ENV_WRITE = re.compile(r"""os\.environ\s*\[\s*["']([A-Z][A-Z0-9_]+)["']\s*\]\s*=""")
_BARE_MATRIX_ASSIGN = re.compile(r"""(?m)^(?:export\s+)?(MATRIX_[A-Z0-9_]+)\s*=""")

# Measured 2026-10-15 on this Pi: five gateway processes plus gbrain ~3.1 GiB
# RSS, 16 GiB RAM, ~9 GiB free. MemoryHigh starts reclaim near twice the
# measured set. MemoryMax is the hard stop and leaves 8 GiB for the rest of
# the machine. The installed 2G cap is under the measured set.
MEMORY_HIGH = "6G"
MEMORY_MAX = "8G"

_INTERPRETERS = {"python", "python3", "bash", "sh", "env"}
_PIN_MARKERS = ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "hermes-py314", "/venvs/")


def memory_dropin_text() -> str:
    """Unit drop-in that replaces the 2G cap for the multiplexed cgroup."""
    return f"[Service]\nMemoryHigh={MEMORY_HIGH}\nMemoryMax={MEMORY_MAX}\n"


def gc_permitted(now: datetime) -> bool:
    """True only at or after 2026-10-16 12:41 Taipei.

    A naive datetime is refused: the host's local zone is not assumed.
    """
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        return False
    return now.astimezone(TAIPEI) >= GC_NOT_BEFORE


def referenced_generation(facts_text: str) -> str:
    """Generation directory named by ``packages.venv.environment``.

    Orphans are every other directory under ``environments/``. A timestamp
    window is not an input: ``cee636e2`` sits in the 12:22–12:41 window and
    stays when this path points at it.
    """
    data = json.loads(facts_text)
    packages = data.get("packages") if isinstance(data, dict) else None
    venv = packages.get("venv") if isinstance(packages, dict) else None
    environment = venv.get("environment") if isinstance(venv, dict) else None
    if not isinstance(environment, str) or not environment:
        raise ValueError("facts.json has no packages.venv.environment")
    parts = PurePath(environment).parts
    if "environments" not in parts:
        raise ValueError("environment path has no environments segment")
    index = parts.index("environments")
    if index + 1 >= len(parts):
        raise ValueError("environment path has no generation name")
    return parts[index + 1]


def orphan_generations(names: list[str], referenced: str) -> tuple[str, ...]:
    """Directory names that are not the generation ``facts.json`` selects."""
    if not referenced:
        raise ValueError("refusing to classify orphans without a referenced generation")
    return tuple(sorted(name for name in names if name != referenced))


def _systemd_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    buf: list[str] = []
    quoted = False
    for char in value.strip():
        if char == '"':
            quoted = not quoted
            continue
        if char.isspace() and not quoted:
            if buf:
                tokens.append("".join(buf))
                buf = []
            continue
        buf.append(char)
    if buf:
        tokens.append("".join(buf))
    return tokens


def program_label(value: str) -> str:
    """Executable of an ``ExecStart`` / ``ExecStartPre``, without arguments.

    ``python script.py --token …`` keeps the interpreter and the script.
    Later tokens, including ``NAME=value`` assignments to ``env``, are dropped.
    """
    tokens = _systemd_tokens(value)
    if not tokens:
        return ""
    if tokens[0].startswith("-") and len(tokens[0]) > 1:
        tokens[0] = tokens[0][1:]
    exe = tokens[0]
    base = PurePath(exe).name
    if base not in _INTERPRETERS and not base.startswith("python"):
        return exe
    rest = [
        token for token in tokens[1:] if not token.startswith("-") and "=" not in token
    ]
    if base == "env" and len(rest) >= 2 and PurePath(rest[0]).name.startswith("python"):
        return f"{exe} {rest[0]} {rest[1]}"
    if rest:
        return f"{exe} {rest[0]}"
    return exe


def environment_names(value: str) -> tuple[str, ...]:
    """Names from an ``Environment=`` value. The characters after ``=`` are not returned."""
    names: list[str] = []
    for token in _systemd_tokens(value):
        key, sep, _rest = token.partition("=")
        if sep and key and key.replace("_", "").isalnum() and not key[0].isdigit():
            names.append(key)
    return tuple(names)


def _logical_lines(text: str) -> list[str]:
    lines: list[str] = []
    buf = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not buf and (not stripped or stripped[0] in "#;"):
            continue
        buf = f"{buf} {stripped}".strip() if buf else stripped
        if buf.endswith("\\"):
            buf = buf[:-1].rstrip()
            continue
        lines.append(buf)
        buf = ""
    if buf:
        lines.append(buf)
    return lines


@dataclass(frozen=True)
class UnitFacts:
    name: str
    environment_files: tuple[str, ...]
    exec_start_pre: tuple[str, ...]
    environment_names: tuple[str, ...]
    exec_programs: tuple[str, ...]
    interpreter_pin: bool
    source_files: tuple[str, ...] = ()
    memory_high: tuple[str, ...] = ()
    memory_max: tuple[str, ...] = ()
    profile_bins: tuple[str, ...] = ()
    kanban_dispatch_off: bool = False


def _empty_facts(name: str) -> UnitFacts:
    return UnitFacts(name, (), (), (), (), False)


def _append_environment_file(bucket: dict[str, list[str]], value: str) -> None:
    path = value.strip().removeprefix("-")
    if path:
        bucket["files"].append(path)


def _append_program(field: str, bucket: dict[str, list[str]], value: str) -> None:
    label = program_label(value)
    if label:
        bucket[field].append(label)


def _append_exec_start_pre(bucket: dict[str, list[str]], value: str) -> None:
    _append_program("pre", bucket, value)


def _profile_bin_entries(raw: str) -> list[str]:
    """PATH entries that are a profile's own bin. The rest of PATH is dropped."""
    entries: list[str] = []
    for entry in raw.split(":"):
        if "/profiles/" in entry and entry.rstrip("/").endswith("/bin"):
            entries.append(entry)
    return entries


def _append_environment(bucket: dict[str, list[str]], value: str) -> None:
    bucket["names"].extend(environment_names(value))
    for token in _systemd_tokens(value):
        name, eq, raw = token.partition("=")
        if not eq:
            continue
        if name == "PATH":
            bucket["bins"].extend(_profile_bin_entries(raw))
        elif name == _KANBAN_ENV and raw.strip().lower() in _KANBAN_OFF:
            bucket["kanban"].append("off")


def _append_memory(field: str, bucket: dict[str, list[str]], value: str) -> None:
    text = value.strip()
    if text:
        bucket[field].append(text)


def _append_memory_high(bucket: dict[str, list[str]], value: str) -> None:
    _append_memory("memory_high", bucket, value)


def _append_memory_max(bucket: dict[str, list[str]], value: str) -> None:
    _append_memory("memory_max", bucket, value)


def _append_exec_start(bucket: dict[str, list[str]], value: str) -> None:
    _append_program("programs", bucket, value)


# Directive name → handler. An if/elif ladder on the directive is rejected by
# the code-health ratchet (HX011).
_UNIT_DIRECTIVES = {
    "EnvironmentFile": _append_environment_file,
    "ExecStartPre": _append_exec_start_pre,
    "Environment": _append_environment,
    "ExecStart": _append_exec_start,
    "MemoryHigh": _append_memory_high,
    "MemoryMax": _append_memory_max,
}


def parse_unit_text(name: str, text: str, source_name: str = "") -> UnitFacts:
    """Pull runtime directives out of one unit or drop-in.

    Credential values are discarded. Memory limits, profile ``bin`` PATH
    entries, and a kanban-off flag are kept because they are not secrets.
    """
    bucket: dict[str, list[str]] = {
        "files": [],
        "pre": [],
        "names": [],
        "programs": [],
        "memory_high": [],
        "memory_max": [],
        "bins": [],
        "kanban": [],
    }
    for line in _logical_lines(text):
        key, sep, value = line.partition("=")
        if not sep:
            continue
        handler = _UNIT_DIRECTIVES.get(key.strip())
        if handler is not None:
            handler(bucket, value)
    kanban_off = bool(bucket["kanban"]) or source_name == "disable-kanban.conf"
    sources = (source_name,) if source_name else ()
    return UnitFacts(
        name,
        tuple(bucket["files"]),
        tuple(bucket["pre"]),
        tuple(bucket["names"]),
        tuple(bucket["programs"]),
        any(marker in text for marker in _PIN_MARKERS),
        sources,
        tuple(bucket["memory_high"]),
        tuple(bucket["memory_max"]),
        tuple(bucket["bins"]),
        kanban_off,
    )


def owning_unit_name(path: Path) -> str:
    parent = path.parent.name
    if parent.endswith(".service.d"):
        return parent[: -len(".d")]
    return path.name


def _merge_facts(name: str, parts: list[UnitFacts]) -> UnitFacts:
    files: list[str] = []
    pre: list[str] = []
    env_names: list[str] = []
    programs: list[str] = []
    sources: list[str] = []
    memory_high: list[str] = []
    memory_max: list[str] = []
    bins: list[str] = []
    pin = False
    kanban_off = False
    for part in parts:
        files.extend(part.environment_files)
        pre.extend(part.exec_start_pre)
        env_names.extend(part.environment_names)
        programs.extend(part.exec_programs)
        sources.extend(part.source_files)
        memory_high.extend(part.memory_high)
        memory_max.extend(part.memory_max)
        bins.extend(part.profile_bins)
        pin = pin or part.interpreter_pin
        kanban_off = kanban_off or part.kanban_dispatch_off
    return UnitFacts(
        name,
        tuple(files),
        tuple(pre),
        tuple(env_names),
        tuple(programs),
        pin,
        tuple(sources),
        tuple(memory_high),
        tuple(memory_max),
        tuple(bins),
        kanban_off,
    )


def iter_unit_files(roots: list[Path]) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        try:
            if not root.is_dir():
                continue
            paths = list(root.rglob("*"))
        except OSError:
            print(f"skip unreadable root: {root}", file=sys.stderr)
            continue
        for path in paths:
            if not path.is_file() or _is_unit_backup(path):
                continue
            blob = f"{path.parent.name}/{path.name}"
            if "hermes" in blob or "hermex" in blob:
                found.append(path)
    return sorted(found)


def scan_units(files: list[tuple[str, ...]]) -> list[UnitFacts]:
    """``files`` is ``(owning unit name, text)`` or ``(..., source filename)``."""
    grouped: dict[str, list[UnitFacts]] = {}
    order: list[str] = []
    for item in files:
        name, text = item[0], item[1]
        source = item[2] if len(item) > 2 else ""
        if name not in grouped:
            order.append(name)
            grouped[name] = []
        grouped[name].append(parse_unit_text(name, text, source))
    return [_merge_facts(name, grouped[name]) for name in order]


def scan_roots(roots: list[Path]) -> list[UnitFacts]:
    grouped: dict[str, list[UnitFacts]] = {}
    order: list[str] = []
    for path in iter_unit_files(roots):
        name = owning_unit_name(path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            print(f"skip unreadable unit: {path}", file=sys.stderr)
            continue
        if name not in grouped:
            order.append(name)
            grouped[name] = []
        grouped[name].append(parse_unit_text(name, text, path.name))
    return [_merge_facts(name, grouped[name]) for name in order]


def format_unit(facts: UnitFacts) -> str:
    """One unit, directives only. Callers must not pass raw file text here."""
    files = ", ".join(facts.environment_files) or "(none)"
    pre = ", ".join(facts.exec_start_pre) or "(none)"
    names = ", ".join(facts.environment_names) or "(none)"
    programs = ", ".join(facts.exec_programs) or "(none)"
    pin = "yes" if facts.interpreter_pin else "no"
    return (
        f"unit: {facts.name}\n"
        f"  EnvironmentFile: {files}\n"
        f"  ExecStartPre: {pre}\n"
        f"  Environment: {names}\n"
        f"  ExecStart: {programs}\n"
        f"  interpreter-pin: {pin}"
    )


def gateway_profile(unit_name: str) -> str | None:
    stem = unit_name.removesuffix(".service")
    if stem == "hermes-gateway":
        return "default"
    prefix = "hermes-gateway-"
    if stem.startswith(prefix) and stem != prefix:
        return stem[len(prefix) :]
    return None


def dotenv_destination(profile: str) -> str:
    """The file ``build_profile_secret_scope`` reads for arbitrary keys."""
    if profile == "default":
        return ".env"
    return f"profiles/{profile}/.env"


def _shared_unit_ban() -> str:
    return (
        f"Do not add this profile's EnvironmentFile or Environment= to {GATEWAY_UNIT}."
    )


def _parent_credential_plan(profile: str, facts: UnitFacts | None) -> str:
    """Wife and dad already hydrate tokens from their own Bitwarden source."""
    dest = dotenv_destination(profile)
    if facts is None:
        return (
            f"{profile}: no hermes-gateway-{profile} unit in the scan. "
            f"Bot tokens stay in secrets.bitwarden in this profile's config.yaml, "
            f"with BWS_ACCESS_TOKEN in {dest}. Do not render a unit env file into "
            f"{dest}. {_shared_unit_ban()}"
        )
    extra = ""
    if facts.environment_names:
        extra += " Unit Environment names: " + ", ".join(facts.environment_names) + "."
    if (
        not facts.environment_files
        and not facts.exec_start_pre
        and not facts.environment_names
    ):
        program = ", ".join(facts.exec_programs) or "(none)"
        extra += f" ExecStart program {program} is not a credential source."
    return (
        f"{profile}: bot tokens stay in secrets.bitwarden already in this profile's "
        f"config.yaml, with BWS_ACCESS_TOKEN in {dest}. Do not render unit files "
        f"into {dest}.{extra} {_shared_unit_ban()}"
    )


def _kid_credential_plan(profile: str, facts: UnitFacts | None) -> str:
    """Matrix credentials follow wife and dad: Bitwarden source, not a merged .env."""
    dest = dotenv_destination(profile)
    renderer = ""
    if facts and any(
        "render_kid_matrix_env.py" in item for item in facts.exec_start_pre
    ):
        renderer = (
            " The unit ExecStartPre render_kid_matrix_env.py is retired and is not "
            f"copied onto {GATEWAY_UNIT}."
        )
    alice = ""
    if profile == "alice":
        alice = (
            " ALICE_GAMES_ROOT and ALICE_PROFILE_ROOT go in "
            f"mcp_servers.{ALICE_MCP_SERVER}.env, read from the unit. They are not "
            ".env keys: _build_safe_env does not pass plain .env values to MCP servers."
        )
    return (
        f"{profile}: move Matrix credentials to secrets.bitwarden in this profile's "
        "config.yaml (enabled, access_token_env BWS_ACCESS_TOKEN, project_id "
        f"{AOBA_PROJECT_ID} Hermes - Aoba, key_prefix {profile.upper()}_MATRIX_, "
        "strip_prefix true so those keys export as MATRIX_*). Copy dad's "
        f"BWS_ACCESS_TOKEN into {dest}. Do not print the token. Do not merge the "
        f"rendered env file into {dest}."
        f"{renderer}{alice} {_shared_unit_ban()}"
    )


# Shared BSM project "Hermes - Aoba". Kids filter with key_prefix; dad does not.
AOBA_PROJECT_ID = "9d36a60d-2e7e-41c0-a90f-b47b0036e9c2"


_PROFILE_PLANS = {
    "wife": _parent_credential_plan,
    "dad": _parent_credential_plan,
    "katie": _kid_credential_plan,
    "alice": _kid_credential_plan,
}


def credential_plan(units: list[UnitFacts]) -> tuple[str, ...]:
    """How each folded profile gets its own secrets. No values."""
    by_profile: dict[str, UnitFacts] = {}
    for unit in units:
        profile = gateway_profile(unit.name)
        if profile and profile != "default":
            by_profile[profile] = unit
    lines = [
        "default: its Environment= and EnvironmentFile= stay the launch process "
        "environment. Do not append wife, dad, katie, or alice credentials to it. "
        "Managed /etc/hermes/.env is merged into every profile and is not a "
        "per-profile store.",
    ]
    for profile in FOLD_PROFILES:
        lines.append(_PROFILE_PLANS[profile](profile, by_profile.get(profile)))
    return tuple(lines)


def carryover_lines(units: list[UnitFacts]) -> tuple[str, ...]:
    """Per-unit PATH, memory, and kanban settings the shared unit must not copy."""
    by_profile: dict[str, UnitFacts] = {}
    for unit in units:
        profile = gateway_profile(unit.name)
        if profile in FOLD_PROFILES:
            by_profile[profile] = unit
    lines: list[str] = []
    for profile in FOLD_PROFILES:
        facts = by_profile.get(profile)
        if facts is None:
            continue
        if facts.memory_high or facts.memory_max:
            high = ", ".join(facts.memory_high) or "(none)"
            maximum = ", ".join(facts.memory_max) or "(none)"
            lines.append(
                f"{profile}: unit MemoryHigh={high} MemoryMax={maximum} is dropped. "
                "No per-profile memory key."
            )
        if facts.profile_bins:
            lines.append(
                f"{profile}: unit PATH includes {', '.join(facts.profile_bins)}. "
                "The terminal tool appends that profile's bin on its own."
            )
        if facts.kanban_dispatch_off or "disable-kanban.conf" in facts.source_files:
            lines.append(
                f"{profile}: kanban dispatch is off on this unit "
                f"({_KANBAN_ENV} or disable-kanban.conf). "
                f"Do not copy that onto {GATEWAY_UNIT}."
            )
    lines.append(
        f"Shared cgroup only: {NEW_MEMORY_DROPIN} sets MemoryHigh={MEMORY_HIGH} "
        f"MemoryMax={MEMORY_MAX}. Hermes has no per-profile memory key. One profile "
        "can use the whole cap. Do not copy a smaller per-unit MemoryMax onto "
        f"{GATEWAY_UNIT}."
    )
    lines.append(
        "Terminal PATH: tools/environments/local.py _managed_runtime_path_entries "
        "appends <profile home>/bin when that directory exists, for the served "
        "profile. No config.yaml PATH key. Do not put a profile bin on the shared "
        "unit PATH. MCP stdio env is _build_safe_env and does not receive that "
        "directory. Core no longer invokes tirith."
    )
    lines.append(
        "Kanban: one dispatcher starts from the default profile config "
        "(gateway/kanban_watchers.py _kanban_dispatcher_boot). "
        f"{_KANBAN_ENV} on the shared unit would disable it for every profile. "
        "kanban.dispatch_in_gateway on wife, dad, katie, or alice is not read. "
        "Retire disable-kanban.conf and the kids' env var with their units. "
        "Leave default's dispatcher on. notify_in_gateway stays at its default "
        "so delivery still runs."
    )
    return tuple(lines)


def webui_lines(units: list[UnitFacts]) -> tuple[str, ...]:
    """hermes-webui stays on its own start script. Never recommend ``hermes dashboard``."""
    lines: list[str] = []
    for unit in units:
        blob = " ".join(unit.exec_programs) + " " + unit.name
        if (
            "hermex-webui-start.sh" not in blob
            and "webui" not in unit.name
            and "hermex" not in unit.name
        ):
            continue
        if "hermex-webui-start.sh" in blob:
            pin = "retarget" if unit.interpreter_pin else "leave"
            lines.append(
                "hermes-webui stays on hermex-webui-start.sh. "
                f"Interpreter drop-in: {pin}. Do not replace ExecStart with hermes dashboard. "
                f"{WEBUI_MANAGED_ENV}"
            )
        elif unit.interpreter_pin:
            lines.append(
                f"{unit.name}: interpreter drop-in should be retargeted to the PM "
                "interpreter, or left if the start script does not use Hermes."
            )
    return tuple(lines)


def format_inventory(units: list[UnitFacts]) -> str:
    blocks = [format_unit(unit) for unit in units]
    blocks.extend(credential_plan(units))
    blocks.extend(carryover_lines(units))
    blocks.extend(webui_lines(units))
    return "\n".join(blocks) + ("\n" if blocks else "")


@dataclass(frozen=True)
class DryRunFindings:
    blockers: tuple[str, ...]
    left_alone: tuple[str, ...]
    only_one_profile: bool
    command_failed: bool


def _redact(line: str) -> str:
    """Drop assignment values that look like credentials. Profile names stay."""
    kept: list[str] = []
    for token in line.split():
        key, sep, _value = token.partition("=")
        if sep and key.lower().rstrip(":") in {
            "token",
            "secret",
            "password",
            "api_key",
            "auth",
            "webhook_url",
        }:
            kept.append(f"{key}=[redacted]")
        else:
            kept.append(token)
    return " ".join(kept)


def parse_dry_run(text: str, *, command_exit: int = 0) -> DryRunFindings:
    """Read ``hermes gateway migrate --multiplex --dry-run`` text.

    That command returns 0 when blockers are present (``cmd_migrate`` returns
    before any service action). Callers must use this parse, not the exit code
    alone.
    """
    blockers: list[str] = []
    left_alone: list[str] = []
    in_blockers = False
    for line in text.splitlines():
        stripped = line.strip()
        if "Blockers" in line and ("✗" in line or "⚠" in line):
            in_blockers = True
            continue
        if in_blockers:
            if stripped.startswith(("•", "-", "·")):
                blockers.append(_redact(stripped))
                continue
            if stripped:
                in_blockers = False
        if "left alone:" in line:
            tail = line.split("left alone:", 1)[1]
            left_alone = [part.strip() for part in tail.split(",") if part.strip()]
    return DryRunFindings(
        blockers=tuple(blockers),
        left_alone=tuple(left_alone),
        only_one_profile="Only one profile exists" in text,
        command_failed=command_exit != 0,
    )


def evaluate_preflight(
    findings: DryRunFindings,
    *,
    require_fold: bool,
) -> tuple[bool, tuple[str, ...]]:
    """``(ok, reasons)``. ``require_fold`` is the cutover pass, after standalone is unset."""
    reasons: list[str] = []
    if findings.command_failed:
        reasons.append("migrate --dry-run exited non-zero")
    if findings.blockers:
        reasons.append("blockers:")
        reasons.extend(findings.blockers)
    if require_fold:
        still = [name for name in FOLD_PROFILES if name in findings.left_alone]
        if still:
            reasons.append(
                "still standalone (dry-run did not check them): " + ", ".join(still)
            )
        if findings.only_one_profile:
            reasons.append(
                "dry-run sees only one profile; unset gateway.standalone on "
                "katie and alice first"
            )
    else:
        unexpected = [
            name for name in ALREADY_SERVED_PROFILES if name in findings.left_alone
        ]
        if unexpected:
            reasons.append(
                "wife and dad are not standalone; dry-run left them alone: "
                + ", ".join(unexpected)
            )
        if findings.only_one_profile:
            reasons.append(
                "dry-run sees only one profile; wife and dad are not standalone "
                "and should already be in the plan"
            )
    return (not reasons, tuple(reasons))


def read_named_environment(text: str, names: tuple[str, ...]) -> dict[str, str]:
    """Values for ``names`` only. Every other ``Environment=`` token is dropped."""
    wanted = set(names)
    found: dict[str, str] = {}
    for line in _logical_lines(text):
        key, sep, value = line.partition("=")
        if not sep or key.strip() != "Environment":
            continue
        for token in _systemd_tokens(value):
            name, eq, raw = token.partition("=")
            if eq and name in wanted:
                found[name] = raw
    return found


def require_alice_games_root(env: dict[str, str], expected: str) -> None:
    """Refuse a write when the unit's games root is not the recorded live path."""
    actual = env.get("ALICE_GAMES_ROOT")
    if actual != expected:
        raise ValueError(
            "ALICE_GAMES_ROOT from the unit does not match the recorded live path "
            + expected
        )


def renderer_env_names(script_text: str) -> tuple[str, ...]:
    """Env names ``render_kid_matrix_env.py`` writes. Values are not returned.

    BSM secret names are these strings. ``BWS_ACCESS_TOKEN`` is the bootstrap
    in the profile ``.env``, not a project secret Hermes will apply.
    """
    names = set(_MATRIX_NAME.findall(script_text))
    names.update(_ENV_WRITE.findall(script_text))
    names.update(_BARE_MATRIX_ASSIGN.findall(script_text))
    names.discard("BWS_ACCESS_TOKEN")
    return tuple(sorted(names))


def matrix_adapter_sees_credentials(present: set[str]) -> bool:
    """Credential half of ``check_matrix_requirements``, without installing deps.

    The adapter accepts a homeserver plus either an access token or a password.
    """
    has_login = "MATRIX_ACCESS_TOKEN" in present or "MATRIX_PASSWORD" in present
    return has_login and "MATRIX_HOMESERVER" in present


def matrix_probe_lines(present: set[str]) -> tuple[str, ...]:
    """Presence only. Callers must not pass values into the printed lines."""
    names = ("MATRIX_HOMESERVER", "MATRIX_ACCESS_TOKEN", "MATRIX_PASSWORD")
    lines = [f"{name}: {'present' if name in present else 'missing'}" for name in names]
    verdict = "yes" if matrix_adapter_sees_credentials(present) else "no"
    lines.append(f"matrix adapter would accept credentials: {verdict}")
    return tuple(lines)


def load_profile_secret_names(home: Path) -> set[str]:
    """Non-empty secret names for ``home``. Values stay in the scope."""
    from agent.secret_scope import build_profile_secret_scope
    from hermes_cli.env_loader import hydrate_profile_secret_sources

    hydrate_profile_secret_sources(home)
    scope = build_profile_secret_scope(home)
    return {key for key, value in scope.items() if str(value).strip()}


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _yaml_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped[0] in "#-" or stripped.startswith("- "):
        return None
    name, sep, _rest = stripped.partition(":")
    if not sep or not name or " " in name:
        return None
    return name


def _block_end(lines: list[str], start: int) -> int:
    base = _indent_of(lines[start])
    end = start + 1
    while end < len(lines):
        if lines[end].strip() and _indent_of(lines[end]) <= base:
            break
        end += 1
    return end


def _direct_child(lines: list[str], parent: int, key: str) -> int | None:
    end = _block_end(lines, parent)
    indents = [
        _indent_of(lines[index])
        for index in range(parent + 1, end)
        if lines[index].strip()
    ]
    if not indents:
        return None
    direct = min(indents)
    for index in range(parent + 1, end):
        if (
            lines[index].strip()
            and _indent_of(lines[index]) == direct
            and _yaml_key(lines[index]) == key
        ):
            return index
    return None


def _is_yaml_block(line: str) -> bool:
    """True when the key's value is a nested block, not a flow mapping."""
    stripped = line.split("#", 1)[0].strip()
    _name, sep, rest = stripped.partition(":")
    if not sep:
        return False
    rest = rest.strip()
    if not rest:
        return True
    return rest[0] in {">", "|"}


def _yaml_quote(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("refusing a multi-line environment value")
    return json.dumps(value)


def patch_mcp_server_env(text: str, server: str, env: dict[str, str]) -> str:
    """Set ``mcp_servers.<server>.env`` keys. Every other line stays.

    Raises when the server block is missing or is a one-line flow mapping.
    """
    lines = text.splitlines()
    mcp_at = next(
        (
            index
            for index, line in enumerate(lines)
            if _indent_of(line) == 0 and _yaml_key(line) == "mcp_servers"
        ),
        None,
    )
    if mcp_at is None:
        raise ValueError("config.yaml has no mcp_servers block")
    if not _is_yaml_block(lines[mcp_at]):
        raise ValueError("mcp_servers is not a block")
    server_at = _direct_child(lines, mcp_at, server)
    if server_at is None:
        raise ValueError(f"mcp_servers has no {server} block")
    if not _is_yaml_block(lines[server_at]):
        raise ValueError(f"{server} is not a block")
    env_at = _direct_child(lines, server_at, "env")
    if env_at is None:
        lines = _insert_env_block(lines, server_at, env)
    else:
        if not _is_yaml_block(lines[env_at]):
            raise ValueError(f"{server}.env is not a block")
        lines = _upsert_env_keys(lines, env_at, env)
    trailer = "\n" if text.endswith("\n") or not text else ""
    return "\n".join(lines) + trailer


def _insert_env_block(
    lines: list[str], server_at: int, env: dict[str, str]
) -> list[str]:
    end = _block_end(lines, server_at)
    indents = [
        _indent_of(lines[index])
        for index in range(server_at + 1, end)
        if lines[index].strip()
    ]
    child = min(indents) if indents else _indent_of(lines[server_at]) + 2
    key_indent = " " * (child + 2)
    env_indent = " " * child
    block = [f"{env_indent}env:"]
    block.extend(f"{key_indent}{key}: {_yaml_quote(env[key])}" for key in env)
    return lines[: server_at + 1] + block + lines[server_at + 1 :]


def _upsert_env_keys(lines: list[str], env_at: int, env: dict[str, str]) -> list[str]:
    end = _block_end(lines, env_at)
    indents = [
        _indent_of(lines[index])
        for index in range(env_at + 1, end)
        if lines[index].strip()
    ]
    child = min(indents) if indents else _indent_of(lines[env_at]) + 2
    indent = " " * child
    updated = list(lines)
    seen: set[str] = set()
    for index in range(env_at + 1, end):
        key = _yaml_key(updated[index])
        if key in env and _indent_of(updated[index]) == child:
            updated[index] = f"{indent}{key}: {_yaml_quote(env[key])}"
            seen.add(key)
    missing = [key for key in env if key not in seen]
    insertion = [f"{indent}{key}: {_yaml_quote(env[key])}" for key in missing]
    return updated[:end] + insertion + updated[end:]


def profile_unit_text(roots: list[Path], profile: str) -> str:
    """Concatenated text of one profile's unit and drop-ins. Not for printing."""
    unit = f"hermes-gateway-{profile}.service"
    chunks: list[str] = []
    for path in iter_unit_files(roots):
        owner = owning_unit_name(path)
        if owner != unit:
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            print(f"skip unreadable unit: {path}", file=sys.stderr)
    if not chunks:
        raise FileNotFoundError(f"no {unit} under the given roots")
    return "\n".join(chunks)


CONFIG_EDITS = (
    "default config.yaml: set gateway.multiplex_profiles: true",
    "default config.yaml: set platforms.sms.enabled: false when SMS blocks the fold; otherwise leave platforms.sms.enabled",
    "default config.yaml: leave kanban.dispatch_in_gateway unset. This process is the only dispatcher.",
    "wife config.yaml: no change. gateway.standalone is not set. secrets.bitwarden stays as it is.",
    "dad config.yaml: no change. gateway.standalone is not set. secrets.bitwarden stays as it is.",
    "katie config.yaml: remove gateway.standalone",
    "katie config.yaml: secrets.bitwarden.enabled: true",
    "katie config.yaml: secrets.bitwarden.access_token_env: BWS_ACCESS_TOKEN",
    "katie config.yaml: secrets.bitwarden.project_id: 9d36a60d-2e7e-41c0-a90f-b47b0036e9c2 (Hermes - Aoba, the project dad already uses)",
    "katie config.yaml: secrets.bitwarden.key_prefix: KATIE_MATRIX_",
    "katie config.yaml: secrets.bitwarden.strip_prefix: true",
    "katie config.yaml: secrets.bitwarden.server_url: the value wife or dad already use, or empty for US Cloud",
    "katie .env: BWS_ACCESS_TOKEN only, copied from dad's profile .env. Do not write MATRIX_* values into this file. Do not print the token.",
    "alice config.yaml: remove gateway.standalone",
    "alice config.yaml: secrets.bitwarden.enabled: true",
    "alice config.yaml: secrets.bitwarden.access_token_env: BWS_ACCESS_TOKEN",
    "alice config.yaml: secrets.bitwarden.project_id: 9d36a60d-2e7e-41c0-a90f-b47b0036e9c2 (Hermes - Aoba, the project dad already uses)",
    "alice config.yaml: secrets.bitwarden.key_prefix: ALICE_MATRIX_",
    "alice config.yaml: secrets.bitwarden.strip_prefix: true",
    "alice config.yaml: secrets.bitwarden.server_url: the value wife or dad already use, or empty for US Cloud",
    "alice .env: BWS_ACCESS_TOKEN only, copied from dad's profile .env. Do not write MATRIX_* values into this file. Do not print the token.",
    "alice config.yaml: mcp_servers.game-workshop.env.ALICE_GAMES_ROOT read from her unit (recorded live path /srv/alice-portal/games)",
    "alice config.yaml: mcp_servers.game-workshop.env.ALICE_PROFILE_ROOT read from her unit. Do not hardcode the profile root.",
    "katie config.yaml: do not set kanban.dispatch_in_gateway. The multiplexed dispatcher does not read it.",
    "alice config.yaml: do not set kanban.dispatch_in_gateway. The multiplexed dispatcher does not read it.",
    "wife config.yaml: do not set kanban.dispatch_in_gateway. Retire disable-kanban.conf with the profile unit.",
    "dad config.yaml: do not set kanban.dispatch_in_gateway. Retire disable-kanban.conf with the profile unit.",
    "no profile config.yaml: no PATH key. The terminal tool appends <profile home>/bin on its own.",
    "no profile config.yaml: no memory key. The shared unit MemoryHigh=6G MemoryMax=8G replaces per-unit caps.",
)


def _is_unit_backup(path: Path) -> bool:
    """Editor and package-manager copies are not live units."""
    name = path.name
    return name.endswith(_UNIT_BACKUP_SUFFIXES) or name.endswith("~")


def _is_control_memory(name: str) -> bool:
    return name.startswith("50-Memory") and name.endswith(".conf")


def _dropin_dir_counts(path: Path) -> bool:
    """A live unit drop-in directory.

    An empty ``/run/systemd/system/hermes-gateway.service.d`` is not a second
    scope. ``system.control`` / ``user.control`` hold ``set-property`` overrides
    and are sidelined separately, not as another unit directory.
    """
    if _CONTROL_DIR_NAMES.intersection(path.parts):
        return False
    try:
        entries = list(path.iterdir())
    except OSError:
        return True
    return bool(entries)


def locate_dropin_dirs(roots: list[Path], unit: str = GATEWAY_UNIT) -> list[Path]:
    name = f"{unit}.d"
    found: list[Path] = []
    for root in roots:
        try:
            if not root.is_dir():
                continue
            matches = [
                path
                for path in root.rglob(name)
                if path.is_dir() and _dropin_dir_counts(path)
            ]
        except OSError:
            print(f"skip unreadable root: {root}", file=sys.stderr)
            continue
        found.extend(matches)
    return sorted(found)


def control_memory_files(control_dir: Path) -> list[Path]:
    if not control_dir.is_dir():
        return []
    return sorted(
        path
        for path in control_dir.iterdir()
        if path.is_file() and _is_control_memory(path.name)
    )


def _backup_has_control_memory(backup_dir: Path | None) -> bool:
    if backup_dir is None or not backup_dir.is_dir():
        return False
    return any(
        path.is_file() and _is_control_memory(path.name) for path in backup_dir.iterdir()
    )


def _can_write_dir(path: Path) -> bool:
    if path.exists():
        return os.access(path, os.W_OK)
    parent = path.parent
    return parent.exists() and os.access(parent, os.W_OK)


def _control_has_memory(control_dir: Path) -> bool:
    try:
        return bool(control_memory_files(control_dir))
    except OSError:
        return True


def sideline_privilege(
    dropin_dir: Path,
    control_dir: Path | None = None,
    backup_dir: Path | None = None,
) -> str:
    """``root`` when sideline or rollback has to rewrite a directory this user cannot.

    An empty ``system.control`` directory does not force root. ``50-Memory*.conf``
    in that directory, or the same names sitting in the backup, does.
    """
    if dropin_dir.exists() and not os.access(dropin_dir, os.W_OK):
        return "root"
    if control_dir is None:
        return "user"
    restoring = _backup_has_control_memory(backup_dir) and not _can_write_dir(control_dir)
    moving = control_dir.is_dir() and _control_has_memory(control_dir) and not os.access(
        control_dir, os.W_OK
    )
    if restoring or moving:
        return "root"
    return "user"


def compiler_assignments() -> tuple[str, ...]:
    return tuple(f"{key}={value}" for key, value in COMPILER_ENV)


def system_gateway_install_command(
    home: Path, user: str, launcher: Path
) -> tuple[str, ...]:
    """``sudo`` clears the environment, so HERMES_HOME is on the env prefix."""
    return (
        "sudo",
        "env",
        f"HERMES_HOME={home}",
        str(launcher),
        "gateway",
        "install",
        "--system",
        "--run-as-user",
        user,
    )


def chown_root_owned_command(home: Path, uid: int, gid: int) -> tuple[str, ...]:
    """Give back only the files the system install created as root."""
    return (
        "sudo",
        "find",
        str(home),
        "-user",
        "root",
        "-exec",
        "chown",
        f"{uid}:{gid}",
        "{}",
        "+",
    )


def _require_dropin(dropin_dir: Path, backup_dir: Path, name: str) -> str | None:
    """Move ``name`` into the backup. None when it is already only in the backup."""
    src = dropin_dir / name
    dest = backup_dir / name
    if src.is_file():
        if dest.exists():
            raise FileExistsError(f"backup already has {name}")
        shutil.move(str(src), str(dest))
        return name
    if dest.is_file():
        return None
    raise FileNotFoundError(f"{name} is not in {dropin_dir} or {backup_dir}")


def _sideline_control_memory(
    control_dir: Path | None, backup_dir: Path
) -> list[str]:
    if control_dir is None:
        return []
    moved: list[str] = []
    for path in control_memory_files(control_dir):
        relocated = _require_dropin(control_dir, backup_dir, path.name)
        if relocated:
            moved.append(relocated)
    return moved


def sideline_dropins(
    dropin_dir: Path, backup_dir: Path, control_dir: Path | None = None
) -> tuple[str, ...]:
    """Move the Python-pinning drop-ins and the 2G memory cap aside, then write the new cap.

    Missing pinning files are an error: ``gateway install`` will not remove them,
    and a restart would keep the external venv. A second run is a no-op when the
    backup already holds the files and the new cap is in place. ``50-Memory*.conf``
    under ``control_dir`` is moved into the same backup when that directory exists.
    A host with no set-property overrides is unchanged.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    dropin_dir.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for name in (*PINNING_DROPINS, MEMORY_DROPIN):
        relocated = _require_dropin(dropin_dir, backup_dir, name)
        if relocated:
            moved.append(relocated)
    moved.extend(_sideline_control_memory(control_dir, backup_dir))
    (dropin_dir / NEW_MEMORY_DROPIN).write_text(memory_dropin_text(), encoding="utf-8")
    return tuple(moved)


def _restore_control_memory(
    control_dir: Path | None, backup_dir: Path
) -> list[str]:
    """Put ``50-Memory*.conf`` back. Absence is success: not every host has them."""
    if control_dir is None or not _backup_has_control_memory(backup_dir):
        return []
    names = sorted(
        path.name
        for path in backup_dir.iterdir()
        if path.is_file() and _is_control_memory(path.name)
    )
    control_dir.mkdir(parents=True, exist_ok=True)
    actions: list[str] = []
    for name in names:
        dest = control_dir / name
        if dest.exists():
            dest.unlink()
        shutil.move(str(backup_dir / name), str(dest))
        actions.append(f"restored {name}")
    return actions


def rollback_dropins(
    dropin_dir: Path, backup_dir: Path, control_dir: Path | None = None
) -> tuple[str, ...]:
    """Put the sideline backup back and remove drop-ins this migration added.

    Refuses before any rename when the backup is missing a sideline'd pinning
    or ``memory-limits.conf`` file, so a partial backup cannot leave the live
    directory half-restored. ``50-Memory*.conf`` is restored when present and
    is not required.
    """
    if not backup_dir.is_dir():
        raise FileNotFoundError(f"drop-in backup missing: {backup_dir}")
    missing = [
        name
        for name in (*PINNING_DROPINS, MEMORY_DROPIN)
        if not (backup_dir / name).is_file()
    ]
    if missing:
        raise FileNotFoundError("backup is missing " + ", ".join(missing))
    actions: list[str] = []
    dropin_dir.mkdir(parents=True, exist_ok=True)
    for name in (NEW_MEMORY_DROPIN, RUNTIME_DROPIN):
        path = dropin_dir / name
        if path.is_file():
            path.unlink()
            actions.append(f"removed {name}")
    for name in (*PINNING_DROPINS, MEMORY_DROPIN):
        dest = dropin_dir / name
        if dest.exists():
            dest.unlink()
        shutil.move(str(backup_dir / name), str(dest))
        actions.append(f"restored {name}")
    actions.extend(_restore_control_memory(control_dir, backup_dir))
    return tuple(actions)


class ClaimError(Exception):
    """An external checkout was not claimed. A raised claim leaves the stamp as it was."""


STAMP_NAME = "install-stamp.json"
STAMP_BACKUP_NAME = "install-stamp.json.external-backup"
RECEIPT_NAME = ".hermes-bootstrap-complete"
RECEIPT_BACKUP_NAME = ".hermes-bootstrap-complete.external-backup"
CLAIM_RECORD_NAME = "install-stamp.external-claim.json"
_LAUNCHER_NAMES = ("hermes", "hermes-acp")


def claim_external_checkout(root: Path) -> str:
    """Re-stamp an ``external`` git checkout as PM-managed and publish its launcher.

    ``publish_launchers`` leaves ``updateMechanism: external`` alone, so
    ``pm install python`` can succeed and still omit ``.hermes/bin/hermes``.
    This copies the stamp, writes the stamp ``write_source_stamp`` publishes
    for a fresh self-managed checkout, then calls ``publish_launchers`` the
    same way ``pm install python`` does (``create=False``). A failed publish
    puts the backup back. A stamp that is not ``external`` is left unchanged.
    """
    root = Path(root)
    if not _is_git_checkout(root):
        raise ClaimError(
            f"refusing to claim {root}: a PM-managed stamp is written only for a git checkout"
        )
    stamp_path = root / STAMP_NAME
    try:
        original = stamp_path.read_bytes()
    except OSError as exc:
        raise ClaimError(f"cannot read {STAMP_NAME}: {exc}") from exc
    mechanism = _stamp_mechanism(original)
    if mechanism == "self" and _launcher_ready(root):
        return f"already managed: {_launcher_path(root)}"
    if mechanism != "external":
        return f"not claimed: updateMechanism is {mechanism or 'absent'}"
    if not _store_python_ready(root):
        raise ClaimError(
            "managed interpreter is not installed; run "
            "`python -m hermes_cli.main pm install python` and re-run"
        )
    backup = root / STAMP_BACKUP_NAME
    _backup_once(backup, original)
    receipt_backup = _backup_receipt(root)
    created = _absent_launchers(root)
    _write_claim_record(root, created)
    try:
        _publish_managed_stamp(root)
        _publish_launcher(root)
        if not _launcher_ready(root):
            raise ClaimError(f"official launcher missing: {_launcher_path(root)}")
    except ClaimError:
        _abort_claim(root, backup, receipt_backup, created)
        raise
    except Exception as exc:  # health: allow BLE001 -- publish_launchers raises InstallError and other pm errors; restore the external stamp
        _abort_claim(root, backup, receipt_backup, created)
        raise ClaimError(f"launcher publish failed: {exc}") from exc
    return f"claimed launcher: {_launcher_path(root)}\nstamp backup: {backup}"


def restore_external_stamp(root: Path) -> str:
    """Put the external stamp backup back and remove launchers this claim created.

    A checkout that was never claimed is unchanged. Drop-in rollback can call
    this every time.
    """
    root = Path(root)
    backup = root / STAMP_BACKUP_NAME
    record_path = root / CLAIM_RECORD_NAME
    receipt_backup = root / RECEIPT_BACKUP_NAME
    if not backup.is_file() and not record_path.is_file() and not receipt_backup.is_file():
        return "no external stamp backup; stamp unchanged"
    lines: list[str] = []
    created = _created_from_record(record_path)
    if backup.is_file():
        _replace_bytes(root / STAMP_NAME, backup.read_bytes())
        lines.append(f"restored {STAMP_NAME} from {STAMP_BACKUP_NAME}")
    if receipt_backup.is_file():
        _replace_bytes(root / RECEIPT_NAME, receipt_backup.read_bytes())
        lines.append(f"restored {RECEIPT_NAME}")
    for name in created:
        if _unlink_launcher(root, name):
            lines.append(f"removed .hermes/bin/{name}")
    backup.unlink(missing_ok=True)
    receipt_backup.unlink(missing_ok=True)
    record_path.unlink(missing_ok=True)
    if not lines:
        return "no external stamp backup; stamp unchanged"
    return "\n".join(lines)


def _is_git_checkout(root: Path) -> bool:
    return (root / ".git").exists() and (root / "pyproject.toml").is_file()


def _stamp_mechanism(payload: bytes) -> str:
    try:
        data = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise ClaimError(f"{STAMP_NAME} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ClaimError(f"{STAMP_NAME} is not an object")
    mechanism = data.get("updateMechanism")
    return mechanism if isinstance(mechanism, str) else ""


def _store_python_ready(root: Path) -> bool:
    from hermes_cli._launchers import resolve_store_python

    return resolve_store_python(root) is not None


def _publish_managed_stamp(root: Path) -> None:
    from hermes_cli.source_stamp import write_source_stamp

    written = write_source_stamp(root)
    if not written or written.get("updateMechanism") != "self":
        raise ClaimError("could not write a managed install stamp")


def _publish_launcher(root: Path) -> None:
    from hermes_cli.venv_sync import publish_launchers

    # create=False matches `hermes pm install python`: publish the checkout
    # launcher, and do not add a new PATH entry.
    publish_launchers(root, create=False)


def _launcher_path(root: Path) -> Path:
    return root / ".hermes" / "bin" / "hermes"


def _launcher_ready(root: Path) -> bool:
    path = _launcher_path(root)
    return path.is_file() and os.access(path, os.X_OK)


def _absent_launchers(root: Path) -> tuple[str, ...]:
    bin_dir = root / ".hermes" / "bin"
    return tuple(name for name in _LAUNCHER_NAMES if not (bin_dir / name).exists())


def _backup_receipt(root: Path) -> Path | None:
    src = root / RECEIPT_NAME
    if not src.is_file():
        return None
    dest = root / RECEIPT_BACKUP_NAME
    _backup_once(dest, src.read_bytes())
    return dest


def _backup_once(dest: Path, payload: bytes) -> None:
    if dest.is_file() and dest.read_bytes() == payload:
        return
    try:
        fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if dest.is_file() and dest.read_bytes() == payload:
            return
        raise ClaimError(
            f"refusing to overwrite {dest.name}; it does not match the current file"
        ) from None
    try:
        _write_all(fd, payload)
    except (OSError, ClaimError):
        os.close(fd)
        with contextlib.suppress(OSError):
            dest.unlink()
        raise
    os.close(fd)


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise ClaimError("could not write the stamp backup")
        view = view[written:]


def _write_claim_record(root: Path, created: tuple[str, ...]) -> None:
    payload = json.dumps({"created": list(created)}, indent=2) + "\n"
    _replace_bytes(root / CLAIM_RECORD_NAME, payload.encode("utf-8"))


def _replace_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        _write_all(fd, payload)
        os.close(fd)
        fd = -1
        os.replace(tmp_name, path)
    finally:
        if fd >= 0:
            os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)


def _abort_claim(
    root: Path,
    backup: Path,
    receipt_backup: Path | None,
    created: tuple[str, ...],
) -> None:
    """Return the checkout to the bytes from before the claim. Keep the backup if restore fails."""
    try:
        _replace_bytes(root / STAMP_NAME, backup.read_bytes())
    except OSError:
        return
    if receipt_backup is not None and receipt_backup.is_file():
        try:
            _replace_bytes(root / RECEIPT_NAME, receipt_backup.read_bytes())
        except OSError:
            return
        with contextlib.suppress(OSError):
            receipt_backup.unlink()
    for name in created:
        with contextlib.suppress(OSError):
            _unlink_launcher(root, name)
    with contextlib.suppress(OSError):
        backup.unlink()
    with contextlib.suppress(OSError):
        (root / CLAIM_RECORD_NAME).unlink()


def _unlink_launcher(root: Path, name: str) -> bool:
    if name not in _LAUNCHER_NAMES:
        return False
    path = root / ".hermes" / "bin" / name
    if path.is_dir():
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _created_from_record(path: Path) -> tuple[str, ...]:
    if not path.is_file():
        return ()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    created = data.get("created") if isinstance(data, dict) else None
    if not isinstance(created, list):
        return ()
    return tuple(name for name in created if isinstance(name, str) and name in _LAUNCHER_NAMES)


ROLLBACK_CHECKLIST = (
    "Stop the multiplexed gateway with `hermes gateway stop`, then stop the "
    "hermes-webui unit. Leave its ExecStart on hermex-webui-start.sh.",
    "Set gateway.standalone: true on wife, dad, katie, and alice. Only katie and "
    "alice had that key. Wife and dad did not; setting it on rollback stops the "
    "default gateway from also serving the restored units. Do not set "
    "gateway.multiplex_profiles to false; that value is ignored and rewritten to true.",
    "Move the new installs/5935b2ebc06c58fa/facts.json aside under a new name. "
    "Do not restore an older selection over it. Leave venvs/hermes-py314-v0.21.7 "
    "on disk; the restored units invoke it.",
    "Restore the backed-up unit files over the copies `hermes gateway install` "
    "rewrote, including the main unit ExecStart. Restoring only a drop-in leaves "
    "the store-python launcher in place.",
    "This command restores zzz-main-v0217.conf, zz-py314-runtime.conf, "
    "92-canonical-runtime.conf, and memory-limits.conf from "
    "~/rpi5-unit-backup/gateway-dropins, and removes zz-multiplex-memory.conf "
    "and zz-profile-runtime-env.conf. It also restores 50-Memory*.conf into "
    "/etc/systemd/system.control/hermes-gateway.service.d when the backup has "
    "them. A host that never had those files is unchanged.",
    "Restore install-stamp.json from install-stamp.json.external-backup when "
    "that file exists, and remove the .hermes/bin/hermes launcher this claim "
    "created. A missing backup leaves the stamp unchanged.",
    "If 98-py314-v0217.conf was retargeted, restore that file from the unit backup. "
    "Do not point hermes-webui at `hermes dashboard`.",
    "Restore katie and alice config.yaml, and each profile .env, from the quick "
    "snapshot taken before the Bitwarden and game-workshop edits. Those snapshots "
    "contain secrets; do not display them. Do not merge credentials into the "
    "shared unit environment.",
    "daemon-reload the manager that owns the units.",
    "enable --now hermes-gateway-wife, hermes-gateway-dad, hermes-gateway-katie, "
    "and hermes-gateway-alice, then start hermes-gateway and the webui unit.",
    "Restore vendor callbacks to the pre-multiplex URLs if they were changed.",
    "Confirm each profile's own gateway is connected and the process image is "
    "the external venv interpreter. Compare /proc/<pid>/exe only.",
    "Do not run `hermes gateway migrate --multiplex`. Apply uninstalls the "
    "profile units.",
    "Do not run `hermes pm gc` as part of rollback.",
)


def _cmd_classify(args: argparse.Namespace) -> int:
    text = sys.stdin.read()
    findings = parse_dry_run(text, command_exit=args.command_exit)
    ok, reasons = evaluate_preflight(findings, require_fold=args.require_fold)
    if findings.left_alone:
        print("standalone left alone: " + ", ".join(findings.left_alone))
    if findings.only_one_profile:
        print("dry-run: only one profile is in the plan")
    if ok:
        print("preflight: no blockers")
        return 0
    print("preflight: abort")
    for reason in reasons:
        print(reason)
    return 1


def _cmd_inventory(args: argparse.Namespace) -> int:
    units = scan_roots([Path(root) for root in args.root])
    if not units:
        print("no hermes unit files under the given roots", file=sys.stderr)
        return 1
    sys.stdout.write(format_inventory(units))
    return 0


def _cmd_locate(args: argparse.Namespace) -> int:
    found = locate_dropin_dirs([Path(root) for root in args.root])
    if len(found) != 1:
        print(f"expected one {GATEWAY_UNIT}.d, found {len(found)}", file=sys.stderr)
        for path in found:
            print(path, file=sys.stderr)
        return 2
    print(found[0])
    return 0


def _optional_path(value: str | None) -> Path | None:
    if not value:
        return None
    return Path(value)


def _cmd_sideline(args: argparse.Namespace) -> int:
    try:
        moved = sideline_dropins(
            Path(args.dropin_dir),
            Path(args.backup),
            _optional_path(args.control_dir),
        )
    except (FileNotFoundError, FileExistsError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for name in moved:
        print(f"moved {name}")
    print(f"wrote {NEW_MEMORY_DROPIN} MemoryHigh={MEMORY_HIGH} MemoryMax={MEMORY_MAX}")
    return 0


def _cmd_rollback(args: argparse.Namespace) -> int:
    try:
        actions = rollback_dropins(
            Path(args.dropin_dir),
            Path(args.backup),
            _optional_path(args.control_dir),
        )
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for action in actions:
        print(action)
    for step in ROLLBACK_CHECKLIST:
        print(f"rollback: {step}")
    return 0


def _cmd_orphans(args: argparse.Namespace) -> int:
    facts_path = Path(args.facts)
    env_root = Path(args.environments)
    try:
        referenced = referenced_generation(facts_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"cannot classify orphans: {exc}", file=sys.stderr)
        return 1
    if not env_root.is_dir():
        print(f"environments directory missing: {env_root}", file=sys.stderr)
        return 1
    names = sorted(path.name for path in env_root.iterdir() if path.is_dir())
    print(f"referenced: {referenced}")
    for name in orphan_generations(names, referenced):
        print(f"orphan: {name}")
    return 0


def _cmd_alice_mcp(args: argparse.Namespace) -> int:
    try:
        text = profile_unit_text([Path(root) for root in args.root], "alice")
        env = read_named_environment(text, ALICE_MCP_ENV_KEYS)
        missing = [name for name in ALICE_MCP_ENV_KEYS if name not in env]
        if missing:
            print("missing from alice unit: " + ", ".join(missing), file=sys.stderr)
            return 1
        require_alice_games_root(env, args.expect_games_root)
        config_path = Path(args.config)
        original = config_path.read_text(encoding="utf-8")
        patched = patch_mcp_server_env(original, ALICE_MCP_SERVER, env)
    except (OSError, ValueError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for name in ALICE_MCP_ENV_KEYS:
        print(f"{name}={env[name]}")
    if args.dry_run:
        print("dry-run: config not written")
        return 0
    config_path.write_text(patched, encoding="utf-8")
    print(f"wrote mcp_servers.{ALICE_MCP_SERVER}.env into {config_path}")
    return 0


def _cmd_renderer_keys(args: argparse.Namespace) -> int:
    try:
        text = Path(args.path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    names = renderer_env_names(text)
    if not names:
        print("no MATRIX_* names found", file=sys.stderr)
        return 1
    for name in names:
        print(name)
    return 0


def _cmd_matrix_probe(args: argparse.Namespace) -> int:
    try:
        present = load_profile_secret_names(Path(args.home))
    except Exception:  # health: allow BLE001 -- do not print the error; a Bitwarden failure can contain the token
        print("matrix probe failed", file=sys.stderr)
        return 1
    for line in matrix_probe_lines(present):
        print(line)
    if matrix_adapter_sees_credentials(present):
        return 0
    return 1


def _cmd_config_edits(_args: argparse.Namespace) -> int:
    for line in CONFIG_EDITS:
        print(line)
    return 0


def _cmd_gc_gate(_args: argparse.Namespace) -> int:
    now = datetime.now(TAIPEI)
    if gc_permitted(now):
        print("gc permitted")
        return 0
    print(
        "gc refused until 2026-10-16 12:41 +0800; "
        "identify orphans from facts.json, do not delete by mtime",
        file=sys.stderr,
    )
    return 1


def _cmd_privilege(args: argparse.Namespace) -> int:
    print(
        sideline_privilege(
            Path(args.dropin_dir),
            _optional_path(args.control_dir),
            _optional_path(args.backup),
        )
    )
    return 0


def _print_argv(argv: tuple[str, ...]) -> int:
    for token in argv:
        print(token)
    return 0


def _cmd_compiler(_args: argparse.Namespace) -> int:
    for line in compiler_assignments():
        print(line)
    return 0


def _cmd_system_install(args: argparse.Namespace) -> int:
    return _print_argv(
        system_gateway_install_command(
            Path(args.home), args.user, Path(args.launcher)
        )
    )


def _cmd_chown_root(args: argparse.Namespace) -> int:
    return _print_argv(
        chown_root_owned_command(Path(args.home), args.uid, args.gid)
    )


def _cmd_claim(args: argparse.Namespace) -> int:
    try:
        print(claim_external_checkout(Path(args.checkout)))
    except ClaimError as exc:
        print(str(exc), file=sys.stderr)
        return 4
    return 0


def _cmd_restore_stamp(args: argparse.Namespace) -> int:
    try:
        print(restore_external_stamp(Path(args.checkout)))
    except (ClaimError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 4
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rpi5_migration")
    sub = parser.add_subparsers(dest="cmd", required=True)

    classify = sub.add_parser("classify-dry-run")
    classify.add_argument("--command-exit", type=int, default=0)
    classify.add_argument("--require-fold", action="store_true")
    classify.set_defaults(func=_cmd_classify)

    inventory = sub.add_parser("inventory")
    inventory.add_argument("--root", action="append", required=True)
    inventory.set_defaults(func=_cmd_inventory)

    locate = sub.add_parser("locate-dropins")
    locate.add_argument("--root", action="append", required=True)
    locate.set_defaults(func=_cmd_locate)

    sideline = sub.add_parser("sideline-dropins")
    sideline.add_argument("--dropin-dir", required=True)
    sideline.add_argument("--backup", required=True)
    sideline.add_argument("--control-dir", default=None)
    sideline.set_defaults(func=_cmd_sideline)

    rollback = sub.add_parser("rollback-dropins")
    rollback.add_argument("--dropin-dir", required=True)
    rollback.add_argument("--backup", required=True)
    rollback.add_argument("--control-dir", default=None)
    rollback.set_defaults(func=_cmd_rollback)

    privilege = sub.add_parser("sideline-privilege")
    privilege.add_argument("--dropin-dir", required=True)
    privilege.add_argument("--control-dir", default=None)
    privilege.add_argument("--backup", default=None)
    privilege.set_defaults(func=_cmd_privilege)

    compiler = sub.add_parser("compiler-env")
    compiler.set_defaults(func=_cmd_compiler)

    system_install = sub.add_parser("system-gateway-install")
    system_install.add_argument("--home", required=True)
    system_install.add_argument("--user", required=True)
    system_install.add_argument("--launcher", required=True)
    system_install.set_defaults(func=_cmd_system_install)

    chown_root = sub.add_parser("chown-root-owned")
    chown_root.add_argument("--home", required=True)
    chown_root.add_argument("--uid", type=int, required=True)
    chown_root.add_argument("--gid", type=int, required=True)
    chown_root.set_defaults(func=_cmd_chown_root)

    orphans = sub.add_parser("orphans")
    orphans.add_argument("--facts", required=True)
    orphans.add_argument("--environments", required=True)
    orphans.set_defaults(func=_cmd_orphans)

    gate = sub.add_parser("gc-gate")
    gate.set_defaults(func=_cmd_gc_gate)

    alice = sub.add_parser("alice-mcp-env")
    alice.add_argument("--root", action="append", required=True)
    alice.add_argument("--config", required=True)
    alice.add_argument("--dry-run", action="store_true")
    alice.add_argument("--expect-games-root", default=ALICE_GAMES_ROOT_EXPECTED)
    alice.set_defaults(func=_cmd_alice_mcp)

    renderer = sub.add_parser("renderer-keys")
    renderer.add_argument("--path", required=True)
    renderer.set_defaults(func=_cmd_renderer_keys)

    probe = sub.add_parser("matrix-probe")
    probe.add_argument("--home", required=True)
    probe.set_defaults(func=_cmd_matrix_probe)

    edits = sub.add_parser("config-edits")
    edits.set_defaults(func=_cmd_config_edits)

    claim = sub.add_parser("claim-managed")
    claim.add_argument("--checkout", required=True)
    claim.set_defaults(func=_cmd_claim)

    restore_stamp = sub.add_parser("restore-external-stamp")
    restore_stamp.add_argument("--checkout", required=True)
    restore_stamp.set_defaults(func=_cmd_restore_stamp)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
