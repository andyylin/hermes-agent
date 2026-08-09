"""Encrypted Bitwarden cache must purge legacy plaintext fail-closed."""

from unittest import mock

import pytest

from agent.secret_sources import bitwarden as bw
from agent.secret_sources.base import ErrorKind


def _write_plaintext_cache(home):
    path = bw._disk_cache_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"secrets":{"OLD":"plaintext-secret"}}', encoding="utf-8")
    return path


def test_encrypted_mode_removes_plaintext_before_failed_fetch(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    fake_binary = tmp_path / "bws"
    fake_binary.write_text("", encoding="utf-8")
    bw._reset_cache_for_tests(home)
    plaintext_path = _write_plaintext_cache(home)

    monkeypatch.setattr(
        bw.subprocess,
        "run",
        lambda *args, **kwargs: mock.Mock(
            returncode=1,
            stdout="",
            stderr="Error: network is unreachable",
        ),
    )

    with pytest.raises(RuntimeError):
        bw.fetch_bitwarden_secrets(
            access_token="0.fake-token",
            project_id="project",
            binary=fake_binary,
            cache_ttl_seconds=0,
            encrypted_cache_enabled=True,
            encrypted_cache_max_stale_seconds=0,
            home_path=home,
        )

    assert not plaintext_path.exists()


def test_direct_fetch_purges_plaintext_before_credential_validation(tmp_path):
    plaintext_path = _write_plaintext_cache(tmp_path)

    with pytest.raises(RuntimeError, match="access token is empty"):
        bw.fetch_bitwarden_secrets(
            access_token="",
            project_id="project",
            home_path=tmp_path,
            encrypted_cache_enabled=True,
        )

    assert not plaintext_path.exists()


def test_apply_purges_plaintext_before_binary_discovery_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "0.fake-token")
    plaintext_path = _write_plaintext_cache(tmp_path)
    monkeypatch.setattr(bw, "find_bws", lambda **_kwargs: None)

    result = bw.apply_bitwarden_secrets(
        enabled=True,
        project_id="project",
        auto_install=False,
        encrypted_cache_enabled=True,
        home_path=tmp_path,
    )

    assert not result.ok
    assert result.error is not None
    assert "binary not available" in result.error
    assert not plaintext_path.exists()


def test_source_purges_plaintext_before_binary_discovery_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "0.fake-token")
    plaintext_path = _write_plaintext_cache(tmp_path)
    monkeypatch.setattr(bw, "find_bws", lambda **_kwargs: None)

    result = bw.BitwardenSource().fetch(
        {
            "enabled": True,
            "project_id": "project",
            "auto_install": False,
            "encrypted_cache": {"enabled": True},
        },
        tmp_path,
    )

    assert result.error_kind is ErrorKind.BINARY_MISSING
    assert not plaintext_path.exists()


def test_post_write_plaintext_purge_failure_is_not_swallowed(monkeypatch, tmp_path):
    entry = bw._CachedFetch(secrets={"TOKEN": "secret"}, fetched_at=1.0)
    monkeypatch.setattr(
        bw,
        "_purge_plaintext_disk_cache",
        mock.Mock(side_effect=RuntimeError("plaintext purge blocked")),
    )

    with pytest.raises(RuntimeError, match="plaintext purge blocked"):
        bw._write_encrypted_disk_cache(
            cache_key=("fingerprint", "project", ""),
            access_token="0.fake-token",
            entry=entry,
            home_path=tmp_path,
        )


def test_encrypted_fallback_repurges_plaintext_recreated_during_fetch(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    fake_binary = tmp_path / "bws"
    fake_binary.write_text("", encoding="utf-8")
    token = "0.fake-token"
    project = "project"
    cache_key = (bw._token_fingerprint(token), project, "")
    bw._reset_cache_for_tests(home)
    bw._write_encrypted_disk_cache(
        cache_key=cache_key,
        access_token=token,
        entry=bw._CachedFetch(secrets={"TOKEN": "encrypted"}, fetched_at=bw.time.time()),
        home_path=home,
    )
    plaintext_path = bw._disk_cache_path(home)

    def fail_after_legacy_reappears(*_args, **_kwargs):
        _write_plaintext_cache(home)
        raise RuntimeError("NETWORK: network is unreachable")

    monkeypatch.setattr(bw, "_run_bws_list", fail_after_legacy_reappears)

    secrets, warnings = bw.fetch_bitwarden_secrets(
        access_token=token,
        project_id=project,
        binary=fake_binary,
        cache_ttl_seconds=0,
        encrypted_cache_enabled=True,
        encrypted_cache_max_stale_seconds=3600,
        home_path=home,
    )

    assert secrets == {"TOKEN": "encrypted"}
    assert warnings
    assert not plaintext_path.exists()


def test_failed_encrypted_write_repurges_plaintext_recreated_mid_write(monkeypatch, tmp_path):
    entry = bw._CachedFetch(secrets={"TOKEN": "secret"}, fetched_at=1.0)
    plaintext_path = bw._disk_cache_path(tmp_path)

    def fail_replace(*_args, **_kwargs):
        _write_plaintext_cache(tmp_path)
        raise OSError("simulated encrypted cache replace failure")

    monkeypatch.setattr(bw.os, "replace", fail_replace)

    bw._write_encrypted_disk_cache(
        cache_key=("fingerprint", "project", ""),
        access_token="0.fake-token",
        entry=entry,
        home_path=tmp_path,
    )

    assert not plaintext_path.exists()
