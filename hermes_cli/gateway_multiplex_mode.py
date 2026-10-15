"""Boot-time verdict for ``gateway.multiplex_profiles`` (the default is on; an explicit false opts out).

``GatewayConfig.from_dict`` leaves the flag ``None`` when neither config.yaml nor
``GATEWAY_MULTIPLEX_PROFILES`` set it. Turning the default on must not make a default gateway
double-bind a fleet that still runs per-profile gateways (two pollers on one bot token, port
fights), so the implicit default is a *request*: the gateway runs the same preflight
``hermes gateway migrate --multiplex`` runs and multiplexes only when the fold would have been
safe. An explicit ``true`` is never second-guessed.

An explicit ``false`` is a real opt-out. It stays standalone even when every other profile's
gateway is down — naming those processes as the reason made the setting look irrelevant, and
the process then took the whole fleet the moment they stopped. The log names the setting that
won (config.yaml, or ``GATEWAY_MULTIPLEX_PROFILES`` when the env override is false).

An unset flag that the preflight refuses is logged, never fatal: the gateway comes up standalone
and the log names the blocker plus ``hermes gateway migrate --multiplex``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SINGLE_PROFILE_REASON = "only one profile exists (nothing to multiplex)"
STANDALONE_PROFILE_REASON = "this profile is standalone (gateway.standalone: true); it serves only itself"

#: ``gateway.standalone: true`` is a TEMPORARY backwards-compatibility shim, not a supported topology.
#: It exists so fleets that lost per-profile gateways in the multiplex-only switch keep working while
#: the remaining multiplexing gaps (per-profile stop/restart, WhatsApp bridge/relay on secondaries,
#: dashboard scoping) are closed; it is removed once they are. Every surface that names the key
#: prints this so nobody builds on it.
STANDALONE_DEPRECATION_NOTICE = (
    "gateway.standalone is a temporary compatibility shim while multiplexing gaps are fixed; "
    "it will be removed once they are — plan to fold this profile with `hermes gateway migrate --multiplex`."
)

#: ``gateway.multiplex_profiles: false`` pins this process to its own profile. The boot log uses
#: one of these two sentences so the reason is the setting that won, not a live secondary gateway.
CONFIG_OPT_OUT_REASON = "gateway.multiplex_profiles is false in config.yaml"
ENV_OPT_OUT_REASON = "GATEWAY_MULTIPLEX_PROFILES is false and overrides config.yaml"

#: Marker an older build left after rewriting ``false`` to ``true``. ``hermes update`` still prints
#: that notice once when the marker is already on disk; a boot that honors ``false`` does not write it.
REWRITTEN_MARKER_NAME = ".multiplex_opt_out_rewritten"


def explicit_multiplex_flag(default_home: Path) -> Optional[bool]:
    """The operator's explicit choice for the DEFAULT profile's gateway: a recognized
    ``GATEWAY_MULTIPLEX_PROFILES``, else ``gateway.multiplex_profiles`` (or the top-level alias) as
    written in its config.yaml; ``None`` when neither is set. Raw read on purpose: the callers are
    other processes (``hermes -p X ...`` has X's config loaded) asking about the default's file."""
    from gateway.config import _bool_token, _env_multiplex_profiles_override
    env = _env_multiplex_profiles_override()
    if env is not None:
        return env
    cfg_path = Path(default_home) / "config.yaml"
    if not cfg_path.exists():
        return None
    from hermes_cli.config import read_user_config_raw
    cfg = read_user_config_raw(cfg_path) or {}
    gateway_section = cfg.get("gateway") if isinstance(cfg.get("gateway"), dict) else {}
    value = cfg.get("multiplex_profiles")
    if value is None:
        value = gateway_section.get("multiplex_profiles")
    if value is None:
        return None
    if isinstance(value, str):
        parsed = _bool_token(value)
        return True if parsed is None else parsed
    return bool(value)


def default_gateway_multiplexes(default_home: Optional[Path] = None) -> bool:
    """Does the default profile's gateway serve every profile? For CLI/dashboard processes: the LIVE
    gateway's ``served_profiles`` record when one runs (it settled the unset default itself), else
    the explicit flag, else False — an unset flag is decided by the gateway at boot, never guessed
    here.

    An explicit ``false`` reports standalone. Answering True from it made every CLI surface
    contradict a gateway that stays on its own profile (see :func:`resolve_multiplex_mode`).
    """
    from hermes_constants import get_default_hermes_root
    from hermes_cli.gateway_multiplex_served import recorded_served_profiles
    root = Path(default_home) if default_home is not None else get_default_hermes_root()
    recorded = recorded_served_profiles(root)
    if recorded is not None:
        return bool(recorded)
    return explicit_multiplex_flag(root) is True


@dataclass(frozen=True)
class MultiplexDecision:
    enabled: bool
    # "config" (config.yaml / env override — explicit true, or explicit false with ``reason``),
    # "default" (implicit default applied),
    # "guard" (implicit default refused; ``reason`` names the blocker).
    source: str
    reason: str = ""


def _standalone_launcher() -> bool:
    from hermes_constants import get_hermes_home, profile_name_for_home
    from hermes_cli.profiles import profile_is_standalone

    home = get_hermes_home()
    return profile_name_for_home(home) not in (None, "default") and profile_is_standalone(home)


def standalone_launcher_decision(config) -> Optional[MultiplexDecision]:
    """The per-profile opt-out also binds callers supplying an explicit GatewayConfig."""
    if not _standalone_launcher():
        return None
    config.multiplex_profiles = False
    return MultiplexDecision(False, "guard", STANDALONE_PROFILE_REASON)


def implicit_multiplex_blocker() -> Optional[str]:
    """Why THIS process must not multiplex right now, or None when it may.

    Mirrors what makes ``hermes gateway migrate --multiplex`` refuse or leave a per-profile gateway
    in place: hosts whose per-profile gateways the preflight cannot see (s6 slots) stay standalone;
    a secondary that still runs its own gateway (live process or installed service) or a preflight
    blocker (duplicate bot credential, port binder without a ``/p/<profile>/`` ingress) keeps this
    gateway standalone.

    These blockers apply only to an UNSET flag. An explicit ``false`` never reaches here: it is
    not a transient condition, and treating it as one made the boot log name a live secondary
    gateway instead of the setting (see :func:`resolve_multiplex_mode`).

    The launching profile's IDENTITY is deliberately not a blocker: multiplex-only means "the one
    host process", whichever profile started it. Gating on ``active == 'default'`` made a host
    whose only gateway runs under a named profile permanently standalone — and every lifecycle
    verb built on "the default's multiplexer" blind to the process actually serving the host.
    """
    from hermes_cli.profiles import profiles_to_serve
    if _standalone_launcher():
        return STANDALONE_PROFILE_REASON
    # Cheap and first: a single-profile install has nothing to multiplex, and the fail-closed secret
    # scope the multiplexer arms buys it nothing. (Also keeps every embedded/test runner off the
    # service-manager probes below.) Create a second profile and restart to start serving it.
    # Parking is reversible without a host restart, so keep the reconcile watcher alive.
    if len(profiles_to_serve(multiplex=True, include_parked=True)) < 2:
        return SINGLE_PROFILE_REASON
    from hermes_cli.gateway_migrate import MIGRATE_COMMAND, _host_supports_migration, build_migration_plan
    host_reason = _host_supports_migration()
    if host_reason:
        return host_reason
    plan = build_migration_plan()
    if plan.standalone_secondaries:
        owned = ", ".join(
            f"'{p.name}' ({'pid ' + str(p.pid) if p.pid else p.service_label()})"
            for p in plan.standalone_secondaries)
        return f"profile(s) {owned} still run their own gateway; fold them with `{MIGRATE_COMMAND}`"
    if plan.blocked:
        return "; ".join(plan.blockers)
    return None


def _default_profile_home() -> Path:
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root()


def persist_resolved_default(decision: MultiplexDecision, default_home: Optional[Path] = None) -> bool:
    """Write ``gateway.multiplex_profiles: true`` into the DEFAULT profile's config.yaml so an UNSET
    key that resolved on reads as the gateway behaves ("left unset" was read as "off"). An explicit
    ``false`` never reaches here: the runtime stayed standalone and the file must keep saying false.
    Comment-preserving writer, once, and NEVER on a guard refusal (the file must not say true while
    the runtime is standalone). Returns True on a write."""
    if not decision.enabled or decision.source == "guard":
        return False
    default_home = Path(default_home) if default_home is not None else _default_profile_home()
    cfg_path = default_home / "config.yaml"
    try:
        from hermes_cli.config import read_user_config_raw
        cfg = read_user_config_raw(cfg_path) or {} if cfg_path.exists() else {}
        section = cfg.get("gateway") if isinstance(cfg.get("gateway"), dict) else {}
        in_file = cfg.get("multiplex_profiles", section.get("multiplex_profiles"))
        if in_file is True:
            return False
        from hermes_cli.gateway_migrate import _write_multiplex_flag
        _write_multiplex_flag(default_home, True)
    except Exception:
        logger.debug("could not persist gateway.multiplex_profiles: true", exc_info=True)
        return False
    logger.info("Wrote gateway.multiplex_profiles: true to %s (was %s).", cfg_path,
                "unset" if in_file is None else repr(in_file))
    return True


def retired_opt_out_notice_lines() -> list[str]:
    """The one-time boxed notice for a rewritten ``false`` (same box as the guard warning)."""
    return _box(["⚠ gateway.multiplex_profiles: false is retired and was rewritten to true;",
                 "one gateway per host serves every profile.",
                 "A per-profile gateway is `gateway.standalone: true` in that profile's config",
                 "(temporary shim) or `--force`."])


def consume_rewritten_notice(default_home: Optional[Path] = None) -> list[str]:
    """``hermes update``'s summary: print the rewrite notice ONCE more, then clear the marker."""
    default_home = Path(default_home) if default_home is not None else _default_profile_home()
    marker = default_home / REWRITTEN_MARKER_NAME
    if not marker.exists():
        return []
    try:
        marker.unlink()
    except OSError:
        return []
    return retired_opt_out_notice_lines()


def _box(body: list[str]) -> list[str]:
    width = max(len(line) for line in body) + 2
    return ["┌" + "─" * width + "┐",
            *[f"│ {line.ljust(width - 1)}│" for line in body],
            "└" + "─" * width + "┘"]


def _explicit_opt_out_reason() -> str:
    """The setting that pinned ``false``: the env override when it won, else config.yaml.

    ``GatewayConfig.multiplex_profiles`` is already the merged bool. Re-read the env here so the
    boot log can name which one the operator set, including when a live secondary gateway would
    otherwise have been the only sentence in the log.
    """
    from gateway.config import _env_multiplex_profiles_override
    if _env_multiplex_profiles_override() is False:
        return ENV_OPT_OUT_REASON
    return CONFIG_OPT_OUT_REASON


def resolve_multiplex_mode(config) -> MultiplexDecision:
    """Settle ``config.multiplex_profiles`` for one gateway boot; the config is updated in place.

    An explicit ``false`` (config.yaml, or ``GATEWAY_MULTIPLEX_PROFILES`` when that override won)
    stays standalone before any preflight. The "another profile still runs its own gateway" guard
    is only for an UNSET flag: it is transient, and using it as the reason for an explicit false
    made the process take every profile the moment those gateways were down.

    An unset flag still refuses to multiplex while a real blocker holds (an s6 container, a
    secondary that still owns a gateway, a duplicate bot credential). An explicit ``true`` skips
    the preflight.
    """
    current = getattr(config, "multiplex_profiles", None)
    standalone = standalone_launcher_decision(config)
    if standalone is not None:
        return standalone
    if current is False:
        config.multiplex_profiles = False
        return MultiplexDecision(False, "config", _explicit_opt_out_reason())
    if current:
        return MultiplexDecision(True, "config")
    try:
        blocker = implicit_multiplex_blocker()
    except Exception as exc:  # a broken preflight must not take the gateway down with it
        logger.warning("Multiplex preflight failed; starting standalone: %s", exc, exc_info=True)
        blocker = f"preflight failed ({exc})"
    if blocker:
        decision = MultiplexDecision(False, "guard", blocker)
    else:
        decision = MultiplexDecision(True, "default", "gateway.multiplex_profiles unset; default applies")
    config.multiplex_profiles = decision.enabled
    return decision


def record_multiplex_decision(decision: MultiplexDecision) -> None:
    """Persist a guard refusal into ``gateway_state.json`` so `hermes gateway status` can show why this
    gateway serves one profile while the default says multiplex; any other verdict clears the field."""
    try:
        from gateway.status import write_runtime_status
        write_runtime_status(multiplex_standalone_reason=decision.reason if decision.source == "guard" else None)
    except Exception:
        logger.debug("could not record the multiplex decision", exc_info=True)


def log_multiplex_decision(decision: MultiplexDecision) -> None:
    record_multiplex_decision(decision)
    if decision.source == "config" and not decision.enabled:
        logger.warning(
            "This gateway stays standalone: %s. It serves only the launching profile.",
            decision.reason)
    elif decision.source == "guard" and decision.reason == SINGLE_PROFILE_REASON:
        logger.info("Single-profile install: gateway.multiplex_profiles unset, serving the default profile only.")
    elif decision.source == "guard":
        logger.warning(
            "This gateway stays standalone: %s. It serves only the launching profile.",
            decision.reason)
        for line in standalone_warning_lines(decision):
            print(line)
    elif decision.source == "default":
        logger.info("Serving every profile on this host (gateway.multiplex_profiles unset; default on).")
        persist_resolved_default(decision)


def unserved_profiles() -> list[str]:
    """Named profiles a standalone gateway leaves without a bot (the whole point of the warning)."""
    from hermes_constants import get_hermes_home, profile_name_for_home
    from hermes_cli.profiles import profiles_to_serve
    me = profile_name_for_home(get_hermes_home()) or "default"
    return [name for name, _home in profiles_to_serve(multiplex=True, include_parked=True) if name != me]


def standalone_warning_lines(decision: MultiplexDecision, unserved: Optional[list[str]] = None) -> list[str]:
    """The boxed warning a multi-profile host prints when a guard keeps its gateway standalone.

    Empty for anything but a guard refusal on a host with other profiles to serve: a single-profile
    install has nothing unserved, so there is nothing to shout about. The same box appears at
    gateway start, in the ``hermes update`` summary and (as text) in the dashboard banner.
    """
    if decision.source != "guard" or decision.reason == SINGLE_PROFILE_REASON:
        return []
    if unserved is None:
        try:
            unserved = unserved_profiles()
        except Exception:
            unserved = []
    if not unserved:
        return []
    from hermes_cli.gateway_migrate import MIGRATE_COMMAND
    body = [
        "⚠ This gateway is STANDALONE: it serves only its own profile.",
        "Profiles NOT served (their bots stay silent): " + ", ".join(unserved),
        f"Why: {decision.reason}",
        f"Fix: {MIGRATE_COMMAND}",
    ]
    return _box(body)


def recorded_standalone_warning_lines() -> list[str]:
    """Same box, rebuilt from the live gateway's ``gateway_state.json`` for processes that did not
    make the decision (``hermes update``'s summary, ``hermes gateway status``)."""
    try:
        from gateway.status import read_runtime_status, runtime_status_pid_is_live
        record = read_runtime_status() or {}
        if record.get("gateway_state") in (None, "stopped", "startup_failed"):
            return []
        # Liveness is the state + a live PID, never the heartbeat: a paused or wedged gateway is
        # still the standalone process the box warns about (#120991).
        if not runtime_status_pid_is_live(record):
            return []
        reason = record.get("multiplex_standalone_reason")
    except Exception:
        return []
    if not reason:
        return []
    return standalone_warning_lines(MultiplexDecision(False, "guard", str(reason)))
