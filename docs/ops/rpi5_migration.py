"""Decisions for the rpi5 vanilla migration.

The shell driver ``rpi5-vanilla-migration.sh`` calls this. Output is names,
paths, and plan lines. ``Environment=`` values and command arguments are not
printed: a unit file on this host holds Matrix tokens and bot tokens.

This module does not talk to systemd or Bitwarden. It classifies text and
moves drop-in files the caller points at.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
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
FOLD_PROFILES = ("wife", "dad", "katie", "alice")

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


def _append_environment(bucket: dict[str, list[str]], value: str) -> None:
    bucket["names"].extend(environment_names(value))


def _append_exec_start(bucket: dict[str, list[str]], value: str) -> None:
    _append_program("programs", bucket, value)


# Directive name → handler. An if/elif ladder on the directive is rejected by
# the code-health ratchet (HX011).
_UNIT_DIRECTIVES = {
    "EnvironmentFile": _append_environment_file,
    "ExecStartPre": _append_exec_start_pre,
    "Environment": _append_environment,
    "ExecStart": _append_exec_start,
}


def parse_unit_text(name: str, text: str) -> UnitFacts:
    """Pull runtime directives out of one unit or drop-in. Values are discarded."""
    bucket: dict[str, list[str]] = {
        "files": [],
        "pre": [],
        "names": [],
        "programs": [],
    }
    for line in _logical_lines(text):
        key, sep, value = line.partition("=")
        if not sep:
            continue
        handler = _UNIT_DIRECTIVES.get(key.strip())
        if handler is not None:
            handler(bucket, value)
    return UnitFacts(
        name,
        tuple(bucket["files"]),
        tuple(bucket["pre"]),
        tuple(bucket["names"]),
        tuple(bucket["programs"]),
        any(marker in text for marker in _PIN_MARKERS),
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
    pin = False
    for part in parts:
        files.extend(part.environment_files)
        pre.extend(part.exec_start_pre)
        env_names.extend(part.environment_names)
        programs.extend(part.exec_programs)
        pin = pin or part.interpreter_pin
    return UnitFacts(
        name, tuple(files), tuple(pre), tuple(env_names), tuple(programs), pin
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
            if not path.is_file():
                continue
            blob = f"{path.parent.name}/{path.name}"
            if "hermes" in blob or "hermex" in blob:
                found.append(path)
    return sorted(found)


def scan_units(files: list[tuple[str, str]]) -> list[UnitFacts]:
    """``files`` is ``(owning unit name, text)`` in the order they should merge."""
    grouped: dict[str, list[UnitFacts]] = {}
    order: list[str] = []
    for name, text in files:
        if name not in grouped:
            order.append(name)
            grouped[name] = []
        grouped[name].append(parse_unit_text(name, text))
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
        grouped[name].append(parse_unit_text(name, text))
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


def _profile_action(profile: str, facts: UnitFacts | None) -> str:
    dest = dotenv_destination(profile)
    shared = (
        f"Do not add this profile's EnvironmentFile or Environment= to {GATEWAY_UNIT}."
    )
    if facts is None:
        return (
            f"{profile}: no hermes-gateway-{profile} unit in the scan. "
            f"Check the other systemd scope. Destination remains {dest}. {shared}"
        )
    sources: list[str] = []
    if facts.exec_start_pre:
        sources.append("ExecStartPre " + ", ".join(facts.exec_start_pre))
    if facts.environment_files:
        sources.append("EnvironmentFile " + ", ".join(facts.environment_files))
    if facts.environment_names:
        sources.append("Environment names " + ", ".join(facts.environment_names))
    if not sources:
        program = ", ".join(facts.exec_programs) or "(none)"
        return (
            f"{profile}: unit has no EnvironmentFile, ExecStartPre, or Environment=. "
            f"Read ExecStart program {program} on the host for a file it loads. "
            f"Write that into {dest} from an ExecStartPre drop-in on {GATEWAY_UNIT}. "
            f"{shared}"
        )
    return (
        f"{profile}: source is {'; '.join(sources)}. "
        f"Render those values into {dest} (mode 0600, merge, do not truncate) "
        f"from an ExecStartPre drop-in ({RUNTIME_DROPIN}) on {GATEWAY_UNIT}. "
        f"The scope loads that file via build_profile_secret_scope. {shared}"
    )


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
        lines.append(_profile_action(profile, by_profile.get(profile)))
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
                f"Interpreter drop-in: {pin}. Do not replace ExecStart with hermes dashboard."
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
                "wife, dad, katie, and alice first"
            )
    return (not reasons, tuple(reasons))


def locate_dropin_dirs(roots: list[Path], unit: str = GATEWAY_UNIT) -> list[Path]:
    name = f"{unit}.d"
    found: list[Path] = []
    for root in roots:
        try:
            if not root.is_dir():
                continue
            matches = [path for path in root.rglob(name) if path.is_dir()]
        except OSError:
            print(f"skip unreadable root: {root}", file=sys.stderr)
            continue
        found.extend(matches)
    return sorted(found)


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


def sideline_dropins(dropin_dir: Path, backup_dir: Path) -> tuple[str, ...]:
    """Move the Python-pinning drop-ins and the 2G memory cap aside, then write the new cap.

    Missing pinning files are an error: ``gateway install`` will not remove them,
    and a restart would keep the external venv. A second run is a no-op when the
    backup already holds the files and the new cap is in place.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    dropin_dir.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for name in (*PINNING_DROPINS, MEMORY_DROPIN):
        relocated = _require_dropin(dropin_dir, backup_dir, name)
        if relocated:
            moved.append(relocated)
    (dropin_dir / NEW_MEMORY_DROPIN).write_text(memory_dropin_text(), encoding="utf-8")
    return tuple(moved)


def rollback_dropins(dropin_dir: Path, backup_dir: Path) -> tuple[str, ...]:
    """Put the sideline backup back and remove drop-ins this migration added.

    Refuses before any rename when the backup is missing a sideline'd file, so
    a partial backup cannot leave the live directory half-restored.
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
    return tuple(actions)


ROLLBACK_CHECKLIST = (
    "Stop the multiplexed gateway with `hermes gateway stop`, then stop the "
    "hermes-webui unit. Leave its ExecStart on hermex-webui-start.sh.",
    "Set gateway.standalone: true on wife, dad, katie, and alice. They already "
    "had that key; this puts the shim back. Do not set gateway.multiplex_profiles "
    "to false; that value is ignored and rewritten to true.",
    "Move the new installs/5935b2ebc06c58fa/facts.json aside under a new name. "
    "Do not restore an older selection over it. Leave venvs/hermes-py314-v0.21.7 "
    "on disk; the restored units invoke it.",
    "Restore the backed-up unit files over the copies `hermes gateway install` "
    "rewrote, including the main unit ExecStart. Restoring only a drop-in leaves "
    "the store-python launcher in place.",
    "This command restores zzz-main-v0217.conf, zz-py314-runtime.conf, "
    "92-canonical-runtime.conf, and memory-limits.conf from "
    "~/rpi5-unit-backup/gateway-dropins, and removes zz-multiplex-memory.conf "
    "and zz-profile-runtime-env.conf.",
    "If 98-py314-v0217.conf was retargeted, restore that file from the unit backup. "
    "Do not point hermes-webui at `hermes dashboard`.",
    "Restore each profile .env from the quick snapshot taken before the render "
    "wrote into it. Those snapshots contain secrets; do not display them. "
    "Do not merge credentials into the shared unit environment.",
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


def _cmd_sideline(args: argparse.Namespace) -> int:
    try:
        moved = sideline_dropins(Path(args.dropin_dir), Path(args.backup))
    except (FileNotFoundError, FileExistsError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for name in moved:
        print(f"moved {name}")
    print(f"wrote {NEW_MEMORY_DROPIN} MemoryHigh={MEMORY_HIGH} MemoryMax={MEMORY_MAX}")
    return 0


def _cmd_rollback(args: argparse.Namespace) -> int:
    try:
        actions = rollback_dropins(Path(args.dropin_dir), Path(args.backup))
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
    sideline.set_defaults(func=_cmd_sideline)

    rollback = sub.add_parser("rollback-dropins")
    rollback.add_argument("--dropin-dir", required=True)
    rollback.add_argument("--backup", required=True)
    rollback.set_defaults(func=_cmd_rollback)

    orphans = sub.add_parser("orphans")
    orphans.add_argument("--facts", required=True)
    orphans.add_argument("--environments", required=True)
    orphans.set_defaults(func=_cmd_orphans)

    gate = sub.add_parser("gc-gate")
    gate.set_defaults(func=_cmd_gc_gate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
