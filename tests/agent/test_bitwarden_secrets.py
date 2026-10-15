"""Hermetic tests for the Bitwarden Secrets Manager integration.

Secret/cache behavior stays offline. PM acquisition and executable invocation
are exercised by tests/pm/test_security_consumers.py.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest


# Make the worktree importable without depending on the installed wheel.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.secret_sources import bitwarden as bw


@pytest.fixture(autouse=True)
def _reset_caches():
    bw._reset_cache_for_tests()
    yield
    bw._reset_cache_for_tests()


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Point Hermes at an isolated home directory."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Some modules cache get_hermes_home; clear if needed.
    import hermes_constants
    if hasattr(hermes_constants, "_HERMES_HOME_CACHE"):
        hermes_constants._HERMES_HOME_CACHE = None  # type: ignore[attr-defined]
    return home


# ---------------------------------------------------------------------------
# fetch_bitwarden_secrets
# ---------------------------------------------------------------------------


def _fake_bws_payload(items):
    return json.dumps(items)














def test_fetch_server_url_sets_env(monkeypatch, tmp_path):
    """server_url must be plumbed into the subprocess as BWS_SERVER_URL."""
    fake_binary = tmp_path / "bws"
    fake_binary.write_text("")
    payload = _fake_bws_payload([{"key": "K", "value": "v"}])

    captured_env = {}

    def fake_run(cmd, **kwargs):
        captured_env.update(kwargs["env"])
        return mock.Mock(returncode=0, stdout=payload, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    bw.fetch_bitwarden_secrets(
        access_token="0.t",
        project_id="p",
        binary=fake_binary,
        use_cache=False,
        server_url="https://vault.bitwarden.eu",
    )
    assert captured_env.get("BWS_SERVER_URL") == "https://vault.bitwarden.eu"








# ---------------------------------------------------------------------------
# apply_bitwarden_secrets — the public entry point used by env_loader
# ---------------------------------------------------------------------------
















# ---------------------------------------------------------------------------
# env_loader integration
# ---------------------------------------------------------------------------




def test_env_loader_calls_bsm_when_enabled(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "secrets:\n"
        "  bitwarden:\n"
        "    enabled: true\n"
        "    project_id: 'proj-1'\n"
        "    access_token_env: 'BWS_ACCESS_TOKEN'\n"
        "    cache_ttl_seconds: 0\n"
        "    override_existing: false\n"
        "    auto_install: false\n"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "0.t")
    monkeypatch.delenv("MY_BSM_KEY", raising=False)

    called = {"n": 0}

    def fake_fetch(**kwargs):
        called["n"] += 1
        assert kwargs["project_id"] == "proj-1"
        return {"MY_BSM_KEY": "from-bsm"}, []

    monkeypatch.setattr(
        "agent.secret_sources.bitwarden.find_bws",
        lambda **_kw: Path("/fake/bws"),
    )
    monkeypatch.setattr(
        "agent.secret_sources.bitwarden.fetch_bitwarden_secrets",
        fake_fetch,
    )
    from agent.secret_sources import registry as reg_module

    reg_module._reset_registry_for_tests()

    from hermes_cli.env_loader import _apply_external_secret_sources
    _apply_external_secret_sources(home)

    assert called["n"] == 1
    assert os.environ.get("MY_BSM_KEY") == "from-bsm"


# ---------------------------------------------------------------------------
# Disk-persisted cache (cross-process — speeds up back-to-back CLI invocations)
# ---------------------------------------------------------------------------








def test_disk_cache_key_mismatch_triggers_refetch(monkeypatch, tmp_path):
    """Disk cache entry written by a different token/project is ignored."""
    home = tmp_path / ".hermes"
    home.mkdir()
    fake_binary = tmp_path / "bws"
    fake_binary.write_text("")
    payload = _fake_bws_payload([{"key": "K1", "value": "v1"}])

    call_count = {"n": 0}
    def fake_run(*a, **kw):
        call_count["n"] += 1
        return mock.Mock(returncode=0, stdout=payload, stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    bw._reset_cache_for_tests(home)

    # Write a cache entry for a DIFFERENT token/project pair
    cache_path = bw._disk_cache_path(home)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({
        "key": "deadbeef00000000|other-project|",
        "secrets": {"OTHER": "should-not-leak"},
        "fetched_at": time.time(),
    }))

    secrets, _ = bw.fetch_bitwarden_secrets(
        access_token="0.t", project_id="proj-1", binary=fake_binary,
        cache_ttl_seconds=300, home_path=home,
    )
    # We must NOT have used the foreign cache entry
    assert secrets == {"K1": "v1"}
    assert "OTHER" not in secrets
    assert call_count["n"] == 1






@pytest.mark.platforms("linux")
def test_encrypted_cache_writes_without_plaintext(monkeypatch, tmp_path):
    """Encrypted cache stores last-good secrets without raw values on disk."""
    home = tmp_path / ".hermes"
    home.mkdir()
    fake_binary = tmp_path / "bws"
    fake_binary.write_text("")
    payload = _fake_bws_payload([{"key": "K1", "value": "secret-value"}])

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: mock.Mock(returncode=0, stdout=payload, stderr=""),
    )
    bw._reset_cache_for_tests(home)
    # A successful encrypted write must remove a pre-existing legacy plaintext
    # cache from the migration path.
    legacy_key = (bw._token_fingerprint("0.t"), "proj-1", "")
    bw._DISK_CACHE.write(
        legacy_key,
        bw._CachedFetch(secrets={"K1": "legacy"}, fetched_at=time.time()),
        300,
        home,
    )
    assert bw._disk_cache_path(home).exists()

    secrets, warnings = bw.fetch_bitwarden_secrets(
        access_token="0.t", project_id="proj-1", binary=fake_binary,
        cache_ttl_seconds=0, encrypted_cache_enabled=True,
        encrypted_cache_max_stale_seconds=604800, home_path=home,
    )

    assert secrets == {"K1": "secret-value"}
    assert warnings == []
    assert not bw._disk_cache_path(home).exists()
    cache_path = bw._encrypted_disk_cache_path(home)
    assert cache_path.exists()
    mode = stat.S_IMODE(os.stat(cache_path).st_mode)
    assert mode == 0o600, f"expected 0o600, got 0o{mode:o}"
    text = cache_path.read_text()
    assert "secret-value" not in text
    assert "0.t" not in text
    payload_disk = json.loads(text)
    assert set(payload_disk.keys()) == {
        "version", "key", "salt", "nonce", "ciphertext",
    }
    assert not bw._disk_cache_path(home).exists()




def test_encrypted_cache_falls_back_on_network_error(monkeypatch, tmp_path):
    """A fresh-enough encrypted cache is used when BWS is unreachable."""
    home = tmp_path / ".hermes"
    home.mkdir()
    fake_binary = tmp_path / "bws"
    fake_binary.write_text("")
    calls = {"n": 0}

    def fake_run(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return mock.Mock(
                returncode=0,
                stdout=_fake_bws_payload([{"key": "K1", "value": "cached"}]),
                stderr="",
            )
        return mock.Mock(
            returncode=1,
            stdout="",
            stderr="Error: network is unreachable",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    bw._reset_cache_for_tests(home)

    first, _ = bw.fetch_bitwarden_secrets(
        access_token="0.t", project_id="proj-1", binary=fake_binary,
        cache_ttl_seconds=0, encrypted_cache_enabled=True,
        encrypted_cache_max_stale_seconds=604800, home_path=home,
    )
    assert first == {"K1": "cached"}
    bw._CACHE.clear()

    second, warnings = bw.fetch_bitwarden_secrets(
        access_token="0.t", project_id="proj-1", binary=fake_binary,
        cache_ttl_seconds=0, encrypted_cache_enabled=True,
        encrypted_cache_max_stale_seconds=604800, home_path=home,
    )
    assert second == {"K1": "cached"}
    assert calls["n"] == 2
    assert len(warnings) == 1
    assert "stale ENCRYPTED disk cache" in warnings[0]
    assert "bws live fetch failed" in warnings[0]






# ---------------------------------------------------------------------------
# Stale disk cache fallback when live bws fetch fails
# ---------------------------------------------------------------------------


def _seed_stale_disk_cache(home, *, secrets, age_seconds, project_id="proj-1",
                           access_token="0.t", server_url=""):
    """Populate the disk cache as if a successful fetch happened `age_seconds`
    ago. Writes the JSON payload directly (same shape the shared DiskCache
    reads/writes) rather than going through DiskCache.write, since that
    would honor cache_ttl_seconds and refuse to persist an already-"stale"
    entry — this needs to land on disk regardless of TTL."""
    cache_key = (
        bw._token_fingerprint(access_token), project_id, server_url,
    )
    cache_path = bw._disk_cache_path(home)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({
        "key": bw._cache_key_str(cache_key),
        "secrets": secrets,
        "fetched_at": time.time() - age_seconds,
    }))


def test_stale_disk_cache_returned_when_bws_fails(monkeypatch, tmp_path):
    """When bws fails and the disk cache is stale, return the stale secrets
    with a warning rather than raising."""
    home = tmp_path / ".hermes"
    home.mkdir()
    fake_binary = tmp_path / "bws"
    fake_binary.write_text("")
    bw._reset_cache_for_tests(home)

    # Seed a stale (older than TTL) disk cache from a previous successful fetch
    _seed_stale_disk_cache(home, secrets={"OPENAI_API_KEY": "sk-old"},
                           age_seconds=3600)

    # Now simulate a BWS network failure
    def fail_run(*a, **kw):
        return mock.Mock(returncode=1, stdout="",
                         stderr="Error: dns resolution failed")
    monkeypatch.setattr(subprocess, "run", fail_run)

    secrets, warnings = bw.fetch_bitwarden_secrets(
        access_token="0.t", project_id="proj-1", binary=fake_binary,
        cache_ttl_seconds=300, home_path=home,
    )
    assert secrets == {"OPENAI_API_KEY": "sk-old"}
    assert len(warnings) == 1
    assert "stale disk cache" in warnings[0]
    assert "dns resolution failed" in warnings[0]










def test_stale_fallback_skipped_on_auth_failure(monkeypatch, tmp_path):
    """An AUTH_FAILED bws error must raise, not serve stale secrets — a bad
    access token indicates a real credential problem the caller needs to
    see, not a transient outage worth papering over."""
    home = tmp_path / ".hermes"
    home.mkdir()
    fake_binary = tmp_path / "bws"
    fake_binary.write_text("")
    bw._reset_cache_for_tests(home)

    _seed_stale_disk_cache(home, secrets={"K1": "v1"}, age_seconds=3600)

    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: mock.Mock(returncode=1, stdout="",
                                   stderr="Error: unauthorized (401)"),
    )

    with pytest.raises(RuntimeError, match="unauthorized"):
        bw.fetch_bitwarden_secrets(
            access_token="0.t", project_id="proj-1", binary=fake_binary,
            cache_ttl_seconds=300, home_path=home,
        )


# ---------------------------------------------------------------------------
# key_prefix / strip_prefix — shared Hermes - Aoba project
# ---------------------------------------------------------------------------

# The 13 names a kid's Matrix adapter reads. Stored in the shared project as
# KATIE_<name> and ALICE_<name>. Values below are fixtures, not credentials.
_KID_MATRIX_NAMES = (
    "MATRIX_ACCESS_TOKEN",
    "MATRIX_ALLOWED_ROOMS",
    "MATRIX_ALLOWED_USERS",
    "MATRIX_AUTO_THREAD",
    "MATRIX_DEVICE_ID",
    "MATRIX_ENCRYPTION",
    "MATRIX_HOME_ROOM",
    "MATRIX_HOMESERVER",
    "MATRIX_PASSWORD",
    "MATRIX_REACTIONS",
    "MATRIX_RECOVERY_KEY",
    "MATRIX_REQUIRE_MENTION",
    "MATRIX_USER_ID",
)
_AOBA_PROJECT = "9d36a60d-2e7e-41c0-a90f-b47b0036e9c2"


def _matrix_adapter_accepts(names: set[str]) -> bool:
    """Credential half of check_matrix_requirements: homeserver plus a login."""
    return "MATRIX_HOMESERVER" in names and (
        "MATRIX_ACCESS_TOKEN" in names or "MATRIX_PASSWORD" in names
    )


def _shared_project_payload() -> str:
    items = []
    for kid in ("KATIE", "ALICE"):
        for name in _KID_MATRIX_NAMES:
            items.append({"key": f"{kid}_{name}", "value": f"v-{kid.lower()}-{name}"})
    items.append({"key": "TELEGRAM_BOT_TOKEN", "value": "v-telegram"})
    return json.dumps(items)


def _fetch_project(tmp_path, monkeypatch, payload: str, **cfg):
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "0.token")
    fake = tmp_path / "bws"
    fake.write_text("")
    monkeypatch.setattr(bw, "find_bws", lambda **_kw: fake)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: mock.Mock(returncode=0, stdout=payload, stderr=""),
    )
    bw._reset_cache_for_tests(tmp_path)
    return bw.BitwardenSource().fetch(
        {
            "enabled": True,
            "project_id": _AOBA_PROJECT,
            "access_token_env": "BWS_ACCESS_TOKEN",
            "auto_install": False,
            "cache_ttl_seconds": 0,
            **cfg,
        },
        tmp_path,
    )


def test_prefix_defaults_leave_the_whole_project_loaded():
    schema = bw.BitwardenSource().config_schema()
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    section = DEFAULT_CONFIG["secrets"]["bitwarden"]
    assert schema["key_prefix"]["default"] == ""
    assert schema["strip_prefix"]["default"] is False
    assert section["key_prefix"] == ""
    assert section["strip_prefix"] is False


@pytest.mark.parametrize(
    ("profile", "prefix"),
    [("katie", "KATIE_MATRIX_"), ("alice", "ALICE_MATRIX_")],
)
def test_kid_config_exports_exactly_her_matrix_names(tmp_path, monkeypatch, profile, prefix):
    result = _fetch_project(
        tmp_path, monkeypatch, _shared_project_payload(),
        key_prefix=prefix, strip_prefix=True,
    )
    assert result.ok
    assert set(result.secrets) == set(_KID_MATRIX_NAMES)
    assert len(result.secrets) == 13
    assert "TELEGRAM_BOT_TOKEN" not in result.secrets
    assert result.secrets["MATRIX_PASSWORD"] == f"v-{profile}-MATRIX_PASSWORD"
    assert result.secrets["MATRIX_HOMESERVER"] == f"v-{profile}-MATRIX_HOMESERVER"
    assert _matrix_adapter_accepts(set(result.secrets))
    assert not any(name.startswith(("KATIE_", "ALICE_")) for name in result.secrets)


def test_unprefixed_profile_keeps_prefixed_names_and_skips_matrix(tmp_path, monkeypatch):
    result = _fetch_project(tmp_path, monkeypatch, _shared_project_payload())
    names = set(result.secrets)
    expected = {f"{kid}_{name}" for kid in ("KATIE", "ALICE") for name in _KID_MATRIX_NAMES}
    expected.add("TELEGRAM_BOT_TOKEN")
    assert names == expected
    assert not any(name in names for name in _KID_MATRIX_NAMES)
    assert result.secrets["TELEGRAM_BOT_TOKEN"] == "v-telegram"
    assert result.secrets["KATIE_MATRIX_PASSWORD"] == "v-katie-MATRIX_PASSWORD"
    assert not _matrix_adapter_accepts(names)


def test_katie_prefix_example_strips_owner_and_drops_telegram(tmp_path, monkeypatch):
    """key_prefix KATIE_ exports KATIE_MATRIX_PASSWORD as MATRIX_PASSWORD."""
    payload = json.dumps([
        {"key": "KATIE_MATRIX_PASSWORD", "value": "v-katie-password"},
        {"key": "TELEGRAM_BOT_TOKEN", "value": "v-telegram"},
        {"key": "ALICE_MATRIX_PASSWORD", "value": "v-alice-password"},
    ])
    result = _fetch_project(
        tmp_path, monkeypatch, payload, key_prefix="KATIE_", strip_prefix=True,
    )
    assert result.secrets == {"MATRIX_PASSWORD": "v-katie-password"}
    assert "TELEGRAM_BOT_TOKEN" not in result.secrets


def test_prefix_without_strip_keeps_bitwarden_key_names(tmp_path, monkeypatch):
    result = _fetch_project(
        tmp_path, monkeypatch, _shared_project_payload(),
        key_prefix="KATIE_MATRIX_", strip_prefix=False,
    )
    assert set(result.secrets) == {f"KATIE_{name}" for name in _KID_MATRIX_NAMES}
    assert "TELEGRAM_BOT_TOKEN" not in result.secrets
    assert not _matrix_adapter_accepts(set(result.secrets))


def test_prefixed_cache_does_not_serve_the_unfiltered_project(tmp_path, monkeypatch):
    """A kid fetch must not land the rest of the project in cache for the next read."""
    home = tmp_path / ".hermes"
    home.mkdir()
    payload = _shared_project_payload()
    fake = tmp_path / "bws"
    fake.write_text("")
    calls = {"n": 0}

    def fake_run(*_a, **_kw):
        calls["n"] += 1
        return mock.Mock(returncode=0, stdout=payload, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    bw._reset_cache_for_tests(home)
    common = dict(access_token="0.t", project_id=_AOBA_PROJECT, binary=fake,
                  cache_ttl_seconds=300, home_path=home)
    filtered, _ = bw.fetch_bitwarden_secrets(
        **common, key_prefix="KATIE_MATRIX_", strip_prefix=True,
    )
    assert set(filtered) == set(_KID_MATRIX_NAMES)
    cached = bw._disk_cache_path(home).read_text(encoding="utf-8")
    assert "v-telegram" not in cached
    assert "v-alice-" not in cached
    assert "TELEGRAM_BOT_TOKEN" not in cached

    full, _ = bw.fetch_bitwarden_secrets(**common)
    assert "TELEGRAM_BOT_TOKEN" in full
    assert "KATIE_MATRIX_HOMESERVER" in full
    assert "MATRIX_HOMESERVER" not in full
    assert calls["n"] == 2


def test_sync_dry_run_shows_the_filter(monkeypatch, tmp_path, capsys):
    payload = _shared_project_payload()
    fake = tmp_path / "bws"
    fake.write_text("")
    monkeypatch.setattr(bw, "find_bws", lambda **_kw: fake)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: mock.Mock(returncode=0, stdout=payload, stderr=""),
    )
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "0.token")
    for name in (*_KID_MATRIX_NAMES, "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        "hermes_cli.secrets_cli.load_config",
        lambda: {"secrets": {"bitwarden": {
            "enabled": True,
            "project_id": _AOBA_PROJECT,
            "access_token_env": "BWS_ACCESS_TOKEN",
            "key_prefix": "KATIE_MATRIX_",
            "strip_prefix": True,
        }}},
    )
    from hermes_cli.secrets_cli import cmd_sync

    code = cmd_sync(argparse.Namespace(apply=False))
    captured = capsys.readouterr()
    assert code == 0
    assert "Filter: key_prefix KATIE_MATRIX_" in captured.out
    assert captured.out.count("would export") == 13
    for name in _KID_MATRIX_NAMES:
        assert name in captured.out
    assert "TELEGRAM_BOT_TOKEN" not in captured.out
    assert "KATIE_MATRIX_HOMESERVER" not in captured.out
    assert "ALICE_MATRIX_" not in captured.out
    assert "v-telegram" not in captured.out
    assert "v-katie-" not in captured.out




