"""Bitwarden Secrets Manager (`bws` CLI) integration.

Pulls API keys from BSM at startup so they need not live in ``~/.hermes/.env``.
``bws`` is auto-installed into the PM tool store (one pinned version,
SHA-256-verified against the published checksum). The one bootstrap secret is
the access token in ``.env``; every other key can live in BSM. One
``bws secret list <project_id>`` call per fetch, cached in-process and on disk
for ``cache_ttl_seconds``. Failures NEVER block startup. Subprocess-driven on
purpose: one cross-platform binary beats the ``bitwarden-sdk-secrets`` Rust wheel.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import shutil
import time
from multiprocessing import current_process
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from agent.secret_sources._cache import (
    CachedFetch as _CachedFetch, SecretCache, atomic_write_json, entry_from_payload,
    fingerprint as _token_fingerprint, resolve_cache_home,
)
from agent.secret_sources.base import (
    ErrorKind, FetchResult, SecretSource, classify_cli_error, coerce_float,
    is_valid_env_name as _is_valid_env_name, get_source_environment, run_cli, source_child_env,
)

logger = logging.getLogger(__name__)

_BWS_RUN_TIMEOUT = 30
DEFAULT_BWS_CACHE_TTL_SECONDS = 600.0
_BWS_RETRY_BACKOFF = (1.0, 2.0, 4.0)
_BWS_MAX_RETRIES = 3
_BWS_RETRY_SLEEP_CAP_SECONDS = 10.0
_TRY_AGAIN_IN_RE = re.compile(r"try again in\s+(\d+(?:\.\d+)?)\s*s", re.IGNORECASE)
_STALE_FALLBACK_KINDS = frozenset({
    ErrorKind.NETWORK, ErrorKind.TIMEOUT, ErrorKind.RATE_LIMITED,
})

# <hermes_home>/cache/bws_cache.json holds only secret VALUES (never the access
# token); kept out of .env so users editing .env don't commit BSM-sourced secrets.
# (fingerprint, project_id, server_url) plus, when key_prefix is set,
# (key_prefix, "1"|"0" strip flag). The unprefixed key stays 3-wide so an
# existing disk entry still hits. A prefixed fetch must not share that slot:
# the file holds values, and a kid profile must not cache the rest of the project.
_CacheKey = tuple[str, ...]
_DISK_CACHE_BASENAME = "bws_cache.json"
_ENCRYPTED_CACHE_BASENAME = "bws_cache.enc.json"
_ENCRYPTED_CACHE_VERSION = 1
_ENCRYPTED_CACHE_INFO = b"hermes-bws-encrypted-cache-v1"


def _cache_key_str(cache_key: _CacheKey) -> str:
    return "|".join(cache_key)


_STORE: SecretCache[_CacheKey] = SecretCache(_DISK_CACHE_BASENAME, key_serializer=_cache_key_str)
# Test seams: L1 dict, L2 DiskCache, and its path.
_CACHE = _STORE.memory
_DISK_CACHE = _STORE.disk
_disk_cache_path = _DISK_CACHE.path


def _purge_plaintext_disk_cache(home_path: Optional[Path] = None) -> None:
    """Remove the legacy plaintext cache or fail closed."""
    path = _disk_cache_path(home_path)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError(
            f"Encrypted Bitwarden cache could not remove legacy plaintext cache: {path}"
        ) from exc


def _encrypted_disk_cache_path(home_path: Optional[Path] = None) -> Path:
    return resolve_cache_home(home_path) / "cache" / _ENCRYPTED_CACHE_BASENAME


# First matching rule wins. The BSM identity endpoint rejects a revoked /
# expired machine-account token with an OAuth-style
# `[400 Bad Request] {"error":"invalid_client"}`, hence those AUTH tokens.
_BWS_ERROR_RULES = (
    (ErrorKind.TIMEOUT, ("timed out",)),
    (ErrorKind.BINARY_MISSING, ("binary not available", "failed to invoke")),
    (ErrorKind.AUTH_FAILED, ("unauthorized", "invalid token", "access token", "401", "403",
                             "invalid_client", "invalid_grant", "400 bad request")),
    (ErrorKind.NETWORK, ("network", "connection", "resolve", "download", "dns")),
)
_RATE_LIMIT_STATUS_RE = re.compile(
    r"(?:\[429\b|\b429\s+too\s+many|\b429\b[^\d]{0,40}too many requests)",
    re.IGNORECASE,
)


def _is_bws_rate_limited_message(message: str) -> bool:
    """True for HTTP 429 throttling — never bare ``429`` inside UUIDs or request ids."""
    lowered = message.lower()
    if "too many requests" in lowered or "slow down" in lowered:
        return True
    return _RATE_LIMIT_STATUS_RE.search(message) is not None


def _classify_bws_error(message: str) -> ErrorKind:
    kind = classify_cli_error(message, _BWS_ERROR_RULES)
    if kind is not ErrorKind.INTERNAL:
        return kind
    if _is_bws_rate_limited_message(message):
        return ErrorKind.RATE_LIMITED
    return ErrorKind.INTERNAL


def _bws_caller_label() -> str:
    """Process / unit name for optional bws debug logging (no secret values)."""
    name = current_process().name
    profile = os.environ.get("HERMES_PROFILE", "").strip()
    if profile:
        return f"{name} profile={profile}"
    return name


def _bws_retry_delay_seconds(retry_index: int, err_message: str) -> float:
    """Seconds to wait before retry ``retry_index`` (0-based), honoring BWS hints."""
    step = _BWS_RETRY_BACKOFF[min(retry_index, len(_BWS_RETRY_BACKOFF) - 1)]
    hinted = _TRY_AGAIN_IN_RE.search(err_message)
    base = max(float(hinted.group(1)), step) if hinted else step
    return base + random.uniform(0.0, 0.25 * base)


def _stale_cache_fallback(
    *,
    exc: RuntimeError,
    use_cache: bool,
    cache_key: _CacheKey,
    encrypted_cache_enabled: bool,
    encrypted_cache_max_stale_seconds: float,
    cache_ttl_seconds: float,
    home_path: Optional[Path],
    read_encrypted: Callable[[float], Optional[_CachedFetch]],
) -> Optional[tuple[dict[str, str], list[str]]]:
    """Serve last-good cache for transport/rate-limit failures; None when unavailable."""
    if not use_cache or _classify_bws_error(str(exc)) not in _STALE_FALLBACK_KINDS:
        return None
    stale = label = None
    if encrypted_cache_enabled:
        stale = read_encrypted(encrypted_cache_max_stale_seconds)
        label = "stale ENCRYPTED disk cache"
    elif cache_ttl_seconds > 0:
        stale = _STORE.disk.read(cache_key, float("inf"), home_path)
        label = "stale disk cache"
    if stale is None:
        return None
    age = max(0.0, time.time() - stale.fetched_at)
    _STORE.memory[cache_key] = stale
    return stale.secrets, [
        f"bws live fetch failed ({exc}); falling back to {label} ({int(age)}s old)"
    ]


# --- Binary discovery + lazy install ----------------------------------------


def find_bws(*, install_if_missing: bool = False) -> Optional[Path]:
    """External tools do not require PM platform support; acquire only on a miss."""
    if system := shutil.which("bws"):
        return Path(system)
    import pm

    selected = pm.installed_package("bws")
    if selected is not None:
        return selected.binary
    if install_if_missing:
        try:
            pm.ensure("bws")
            return pm.installed_package("bws").binary
        except Exception as exc:
            logger.warning("bws auto-install failed: %s", exc)
    return None


def install_bws(*, force: bool = False) -> Path:
    """Explicit setup/repair; PM re-verifies even a warm entry (including force)."""
    import pm

    pm.ensure("bws", explicit=True)
    return pm.installed_package("bws").binary


# --- Encrypted last-good cache (opt-in) -------------------------------------


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _derive_encrypted_cache_key(access_token: str, salt: bytes) -> bytes:
    """HKDF the local cache key from the bootstrap BWS token. cryptography is imported
    lazily: eagerly mapping ``_rust.pyd`` on Windows blocks the updater replacing it."""
    # Keep the native cryptography extension lazy. Most CLI commands import this module while building
    # argparse, even though only encrypted-cache reads/writes need it. See #73381.
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                info=_ENCRYPTED_CACHE_INFO).derive(access_token.encode("utf-8"))


def _write_encrypted_disk_cache(*, cache_key: _CacheKey, access_token: str, entry: _CachedFetch,
                                home_path: Optional[Path] = None) -> None:
    """Persist an AES-GCM encrypted last-good entry atomically (best-effort). The raw
    token only derives the key; a successful write removes the legacy plaintext cache."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        salt = os.urandom(16)
        nonce = os.urandom(12)
        serialized_key = _cache_key_str(cache_key)
        key = _derive_encrypted_cache_key(access_token, salt)
        plaintext = json.dumps(
            {"secrets": entry.secrets, "fetched_at": entry.fetched_at}, separators=(",", ":"),
        ).encode("utf-8")
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, serialized_key.encode("utf-8"))
        payload = {"version": _ENCRYPTED_CACHE_VERSION, "key": serialized_key,
                   "salt": _b64e(salt), "nonce": _b64e(nonce), "ciphertext": _b64e(ciphertext)}
        atomic_write_json(_encrypted_disk_cache_path(home_path), payload)
        _STORE.disk.clear(home_path)
    except Exception:
        return
    # Cache persistence is best-effort; plaintext exclusion is not.
    _purge_plaintext_disk_cache(home_path)


def _read_encrypted_disk_cache(*, cache_key: _CacheKey, access_token: str, max_age_seconds: float,
                               home_path: Optional[Path] = None) -> Optional[_CachedFetch]:
    """Decrypted encrypted-cache entry if it matches ``cache_key`` and is in-window."""
    if max_age_seconds <= 0:
        return None
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        payload = json.loads(_encrypted_disk_cache_path(home_path).read_text(encoding="utf-8-sig"))
        serialized_key = _cache_key_str(cache_key)
        if (not isinstance(payload, dict)
                or payload.get("version") != _ENCRYPTED_CACHE_VERSION
                or payload.get("key") != serialized_key):
            return None
        salt, nonce, ciphertext = (base64.b64decode(str(payload.get(k, "")).encode("ascii"), validate=True)
                                   for k in ("salt", "nonce", "ciphertext"))
        key = _derive_encrypted_cache_key(access_token, salt)
        entry = entry_from_payload(json.loads(
            AESGCM(key).decrypt(nonce, ciphertext, serialized_key.encode("utf-8")).decode("utf-8")
        ))
        if entry is None:
            return None
        entry_age = time.time() - entry.fetched_at
        return None if entry_age < 0 or entry_age > max_age_seconds else entry
    except Exception:
        return None


# --- Key prefix -------------------------------------------------------------


def prefix_settings(cfg: object) -> tuple[str, bool]:
    """``(key_prefix, strip_prefix)`` from a ``secrets.bitwarden`` mapping.

    Missing or non-string ``key_prefix`` is ``""`` (no filter). ``strip_prefix``
    is true only for a real boolean true or the strings true/yes/on/1, so the
    YAML string ``"false"`` does not turn stripping on.
    """
    if not isinstance(cfg, dict):
        return "", False
    raw = cfg.get("key_prefix", "")
    prefix = raw.strip() if isinstance(raw, str) else ""
    flag = cfg.get("strip_prefix", False)
    if isinstance(flag, str):
        strip = flag.strip().lower() in {"1", "true", "yes", "on"}
    else:
        strip = flag is True
    return prefix, strip


def owner_strip_span(key_prefix: str) -> str:
    """Characters ``strip_prefix`` removes from a key that matched ``key_prefix``.

    A one-segment prefix is removed whole (``KATIE_`` → ``KATIE_MATRIX_PASSWORD``
    exports as ``MATRIX_PASSWORD``). A longer prefix keeps its final segment, so
    ``KATIE_MATRIX_`` exports that same key as ``MATRIX_PASSWORD`` rather than
    ``PASSWORD``. The filter can still be the full ``KATIE_MATRIX_`` and leave
    ``TELEGRAM_BOT_TOKEN`` and ``ALICE_MATRIX_*`` unloaded.
    """
    if not key_prefix:
        return ""
    body = key_prefix.removesuffix("_")
    owner, sep, _kept = body.rpartition("_")
    if not owner:
        return key_prefix
    return owner + sep


def select_prefixed_secrets(
    secrets: dict[str, str], key_prefix: str, strip_prefix: bool,
) -> tuple[dict[str, str], list[str]]:
    """Keep keys that start with ``key_prefix`` and optionally rename them.

    An empty prefix returns ``secrets`` unchanged. ``strip_prefix`` without a
    prefix is a no-op plus a warning, so the default config does not rename.
    """
    prefix = key_prefix.strip() if isinstance(key_prefix, str) else ""
    if not prefix:
        if strip_prefix:
            return secrets, ["strip_prefix is set but key_prefix is empty; names are unchanged"]
        return secrets, []
    span = owner_strip_span(prefix) if strip_prefix else ""
    selected: dict[str, str] = {}
    warnings: list[str] = []
    matched = False
    for key in sorted(secrets):
        if not key.startswith(prefix):
            continue
        matched = True
        export = key[len(span):] if span and key.startswith(span) else key
        if not _is_valid_env_name(export):
            warnings.append(f"Skipping secret {key!r}: {export!r} is not a valid env-var name")
            continue
        if export in selected:
            warnings.append(f"Skipping secret {key!r}: export name {export} already taken")
            continue
        selected[export] = secrets[key]
    if not matched:
        warnings.append(f"key_prefix {prefix!r} matched no secrets")
    return selected, warnings


# --- Secret fetch -----------------------------------------------------------


def _cache_key(
    access_token: str, project_id: str, server_url: str, key_prefix: str, strip_prefix: bool,
) -> _CacheKey:
    base = (_token_fingerprint(access_token), project_id, server_url or "")
    if not key_prefix:
        return base
    return (*base, key_prefix, "1" if strip_prefix else "0")


def fetch_bitwarden_secrets(
    *, access_token: str, project_id: str, binary: Optional[Path] = None,
    cache_ttl_seconds: float = DEFAULT_BWS_CACHE_TTL_SECONDS, use_cache: bool = True,
    server_url: str = "", home_path: Optional[Path] = None,
    encrypted_cache_enabled: bool = False, encrypted_cache_max_stale_seconds: float = 0,
    key_prefix: str = "", strip_prefix: bool = False, log_bws_calls: bool = False,
) -> tuple[dict[str, str], list[str]]:
    """Pull the secrets for ``project_id`` from BSM → ``(secrets, warnings)``.

    ``key_prefix`` empty (the default) returns every valid env-name in the
    project. A non-empty prefix keeps only keys that start with it.
    ``strip_prefix`` renames those keys via :func:`owner_strip_span` before
    they are cached or returned, so a shared project can export ``MATRIX_*``
    for one profile and leave the raw ``KATIE_MATRIX_*`` names for another.
    The cache key includes the prefix, so a filtered entry is never served
    as the unfiltered project.

    ``server_url``: region / self-hosted instance (empty = US Cloud). With
    ``encrypted_cache_enabled`` fresh entries are written AES-GCM encrypted and a
    last-good entry may be served after NETWORK/TIMEOUT failures for up to
    ``encrypted_cache_max_stale_seconds`` — independent of the fresh TTL, so
    ``cache_ttl_seconds: 0`` can coexist with a break-glass offline cache.
    ``log_bws_calls``: DEBUG-log each ``bws`` subprocess with caller/process name (no values).
    Raises ``RuntimeError`` on fatal conditions (missing binary, auth failure,
    unparseable output); env_loader catches, the setup wizard lets it propagate.
    """
    if encrypted_cache_enabled:
        _purge_plaintext_disk_cache(home_path)
    if not access_token:
        raise RuntimeError("Bitwarden access token is empty")
    if not project_id:
        raise RuntimeError("Bitwarden project_id is empty")

    prefix = key_prefix.strip() if isinstance(key_prefix, str) else ""
    cache_key = _cache_key(access_token, project_id, server_url, prefix, bool(strip_prefix))

    def _read_encrypted(max_age: float) -> Optional[_CachedFetch]:
        return _read_encrypted_disk_cache(cache_key=cache_key, access_token=access_token,
                                          max_age_seconds=max_age, home_path=home_path)

    if use_cache and cache_ttl_seconds > 0:
        # L2 (~5ms) vs ~380ms for `bws secret list`.
        cached = _STORE.lookup(
            cache_key, cache_ttl_seconds, home_path,
            read_disk=(lambda: _read_encrypted(cache_ttl_seconds)) if encrypted_cache_enabled else None,
        )
        if cached is not None:
            return cached.secrets, []

    bws = binary or find_bws(install_if_missing=True)
    if bws is None:
        raise RuntimeError("bws binary not available — auto-install failed and `bws` is "
                           "not on PATH.  Install manually from "
                           "https://github.com/bitwarden/sdk-sm/releases or re-run "
                           "`hermes secrets bitwarden setup`.")

    try:
        secrets, warnings = _run_bws_list_with_retries(
            bws, access_token, project_id, server_url, log_bws_calls=log_bws_calls,
        )
    except RuntimeError as exc:
        # Stale fallback ONLY for transport/rate-limit failures — never AUTH_FAILED /
        # INTERNAL, where old secrets would mask a real problem (without it a fleet
        # sharing one project all stops on a network blip). With the encrypted cache on
        # it is the ONLY fallback (at-rest payload must never be plaintext); else the
        # plain DiskCache is read with ttl=inf, but only when the real TTL > 0.
        fallback = _stale_cache_fallback(
            exc=exc, use_cache=use_cache, cache_key=cache_key,
            encrypted_cache_enabled=encrypted_cache_enabled,
            encrypted_cache_max_stale_seconds=encrypted_cache_max_stale_seconds,
            cache_ttl_seconds=cache_ttl_seconds, home_path=home_path,
            read_encrypted=_read_encrypted,
        )
        if fallback is not None:
            return fallback
        raise
    secrets, prefix_warnings = select_prefixed_secrets(secrets, prefix, bool(strip_prefix))
    warnings.extend(prefix_warnings)
    entry = _CachedFetch(secrets=secrets, fetched_at=time.time())
    if use_cache:
        if cache_ttl_seconds > 0:
            _STORE.memory[cache_key] = entry
        if encrypted_cache_enabled:  # storage policy; max_stale only gates outage reads
            _write_encrypted_disk_cache(cache_key=cache_key, access_token=access_token,
                                        entry=entry, home_path=home_path)
        else:
            _STORE.disk.write(cache_key, entry, cache_ttl_seconds, home_path)
    return secrets, warnings


def _summarize_bws_stderr(raw: str) -> str:
    """Reduce a bws (color-eyre) error dump to its numbered cause lines joined with
    ``; `` (dropping ``Location:``/``Backtrace`` on); raw text if unrecognized."""
    text = raw.replace("\x1b", "").strip()
    causes: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("Location:", "Backtrace omitted", "Run with ")):
            break
        if stripped not in ("", "Error:") and (cause := re.sub(r"^\d+:\s*", "", stripped)):
            causes.append(cause)
    return "; ".join(causes) if causes else text


def _run_bws_list_with_retries(
    bws: Path, access_token: str, project_id: str, server_url: str = "",
    *, log_bws_calls: bool = False,
) -> tuple[dict[str, str], list[str]]:
    """Invoke ``bws secret list``, retrying rate limits with capped backoff."""
    last_exc: Optional[RuntimeError] = None
    slept = 0.0
    for attempt in range(_BWS_MAX_RETRIES + 1):
        try:
            return _run_bws_list(
                bws, access_token, project_id, server_url, log_bws_calls=log_bws_calls,
            )
        except RuntimeError as exc:
            last_exc = exc
            if (_classify_bws_error(str(exc)) != ErrorKind.RATE_LIMITED
                    or attempt >= _BWS_MAX_RETRIES):
                raise
            delay = _bws_retry_delay_seconds(attempt, str(exc))
            remaining = _BWS_RETRY_SLEEP_CAP_SECONDS - slept
            if remaining <= 0:
                raise
            delay = min(delay, remaining)
            time.sleep(delay)
            slept += delay
    assert last_exc is not None
    raise last_exc


def _run_bws_list(
    bws: Path, access_token: str, project_id: str, server_url: str = "",
    *, log_bws_calls: bool = False,
) -> tuple[dict[str, str], list[str]]:
    if log_bws_calls:
        logger.debug(
            "bws secret list caller=%s pid=%s project_id=%s",
            _bws_caller_label(), os.getpid(), project_id,
        )
    cmd = [str(bws), "secret", "list", project_id, "--output", "json"]
    # The bws child intentionally receives the access token; a profile-local
    # fetch must not inherit sibling credentials (source_child_env).
    env = source_child_env()
    env["BWS_ACCESS_TOKEN"] = access_token
    env.setdefault("NO_COLOR", "1")
    if server_url:  # empty keeps whatever BWS_SERVER_URL the shell already had
        env["BWS_SERVER_URL"] = server_url

    proc = run_cli(cmd, env=env, timeout=_BWS_RUN_TIMEOUT, label="bws",
                   timeout_message=f"bws timed out after {_BWS_RUN_TIMEOUT}s fetching secrets")

    if proc.returncode != 0:
        err = _summarize_bws_stderr(proc.stderr or proc.stdout or "")
        raise RuntimeError(f"bws exited {proc.returncode}: {err[:200]}")

    raw = proc.stdout.strip()
    if not raw:
        return {}, ["bws returned no output (empty project?)"]
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"bws returned non-JSON output: {exc}") from exc
    if not isinstance(payload, list):
        raise RuntimeError(f"bws returned unexpected shape: {type(payload).__name__}")

    secrets: dict[str, str] = {}
    warnings: list[str] = []
    for item in payload:
        key, value = (item.get("key"), item.get("value")) if isinstance(item, dict) else (None, None)
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if _is_valid_env_name(key):
            secrets[key] = value
        else:
            warnings.append(f"Skipping secret {key!r}: not a valid env-var name")
    return secrets, warnings


class BitwardenSource(SecretSource):
    """Bitwarden Secrets Manager as a registered **bulk** source.

    With no ``key_prefix`` it injects every secret in the project, so explicit
    mapped bindings outrank it. ``key_prefix`` narrows that set before apply;
    ``strip_prefix`` renames the matches (see :func:`owner_strip_span`).
    """

    name = "bitwarden"
    label = "Bitwarden Secrets Manager"
    shape = "bulk"
    scheme = "bws"
    token_env_key = "access_token_env"
    default_token_env = "BWS_ACCESS_TOKEN"
    # override_existing defaults True: the point of BSM is centralized rotation
    # — a stale .env line must not have the final say.
    override_existing_default = True
    _AUTH_HINT = (
        "Run `hermes secrets bitwarden token` to paste a fresh access "
        "token (create one in the Bitwarden web app: Secrets Manager → "
        "Machine accounts → Access tokens).  Wrong region?  Re-run "
        "`hermes secrets bitwarden setup` and pick EU/self-hosted."
    )
    remediation_hints = {ErrorKind.AUTH_FAILED: _AUTH_HINT, ErrorKind.AUTH_EXPIRED: _AUTH_HINT}

    def config_schema(self) -> dict:
        return {
            "enabled": {"description": "Master switch", "default": False},
            "access_token_env": {"description": "Env var holding the machine-account access token",
                                 "default": "BWS_ACCESS_TOKEN"},
            "project_id": {"description": "BSM project UUID", "default": ""},
            "key_prefix": {
                "description": "Load only secret keys that start with this prefix. Empty loads the whole project.",
                "default": "",
            },
            "strip_prefix": {
                "description": (
                    "When key_prefix is set, drop its leading owner segment from exported names "
                    "(KATIE_ or KATIE_MATRIX_ both export KATIE_MATRIX_PASSWORD as MATRIX_PASSWORD)."
                ),
                "default": False,
            },
            "cache_ttl_seconds": {
                "description": "Fresh disk+memory cache TTL; 0 disables fresh-cache reuse",
                "default": int(DEFAULT_BWS_CACHE_TTL_SECONDS),
            },
            "log_bws_calls": {
                "description": "DEBUG-log each bws subprocess (caller/process name only)",
                "default": False,
            },
            "encrypted_cache": {"description": "Encrypted last-good cache for network/timeout fallback",
                                "default": {"enabled": False, "max_stale_seconds": 0}},
            "override_existing": {"description": "BSM values overwrite .env/shell values", "default": True},
            "auto_install": {"description": "Auto-download the pinned bws binary", "default": True},
            "server_url": {"description": "Region / self-hosted endpoint (empty = US Cloud)", "default": ""},
        }

    def fetch(self, cfg: dict, home_path: Path) -> FetchResult:
        cfg = cfg if isinstance(cfg, dict) else {}
        result = FetchResult()

        access_token_env = self.token_env(cfg)
        access_token = get_source_environment().get(access_token_env, "").strip()
        if not access_token:
            return result.fail(f"secrets.bitwarden.enabled is true but {access_token_env} is "
                               "not set.  Run `hermes secrets bitwarden setup`.", ErrorKind.NOT_CONFIGURED)
        project_id = str(cfg.get("project_id") or "")
        key_prefix, strip_prefix = prefix_settings(cfg)
        if not project_id:
            return result.fail("secrets.bitwarden.project_id is empty.  Run `hermes secrets bitwarden setup`.",
                               ErrorKind.NOT_CONFIGURED)
        encrypted_cfg = cfg.get("encrypted_cache")
        encrypted_cfg = encrypted_cfg if isinstance(encrypted_cfg, dict) else {}
        if bool(encrypted_cfg.get("enabled", False)):
            _purge_plaintext_disk_cache(home_path)

        binary = find_bws(install_if_missing=bool(cfg.get("auto_install", True)))
        result.binary_path = binary
        if binary is None:
            return result.fail("bws binary not available and auto-install is disabled.  "
                               "Run `hermes secrets bitwarden setup` to install.", ErrorKind.BINARY_MISSING)

        try:
            secrets, warnings = fetch_bitwarden_secrets(
                access_token=access_token, project_id=project_id, binary=binary,
                cache_ttl_seconds=coerce_float(
                    cfg.get("cache_ttl_seconds", DEFAULT_BWS_CACHE_TTL_SECONDS),
                    DEFAULT_BWS_CACHE_TTL_SECONDS,
                ),
                server_url=str(cfg.get("server_url", "") or "").strip(), home_path=home_path,
                encrypted_cache_enabled=bool(encrypted_cfg.get("enabled", False)),
                encrypted_cache_max_stale_seconds=coerce_float(encrypted_cfg.get("max_stale_seconds", 0), 0.0),
                key_prefix=key_prefix, strip_prefix=strip_prefix,
                log_bws_calls=bool(cfg.get("log_bws_calls", False)),
            )
        except RuntimeError as exc:
            result.fail(str(exc), _classify_bws_error(str(exc)))
            if result.error_kind == ErrorKind.AUTH_FAILED:  # say what the raw OAuth reject means first
                result.error = ("Bitwarden rejected the machine-account access token "
                                f"({access_token_env}) — it was likely revoked, expired, "
                                f"or belongs to another region.  ({result.error})")
            return result

        result.secrets = secrets
        result.warnings.extend(warnings)
        return result


def clear_caches(home_path: Optional[Path] = None) -> None:
    """Drop in-process AND disk caches (plaintext and encrypted), e.g. after a token rotation."""
    _STORE.clear(home_path)
    try:
        _encrypted_disk_cache_path(home_path).unlink()
    except (FileNotFoundError, OSError):
        pass


_reset_cache_for_tests = clear_caches
