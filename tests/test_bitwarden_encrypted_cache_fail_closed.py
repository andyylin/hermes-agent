"""Encrypted Bitwarden cache must fail closed across fetch failures."""

from unittest import mock

import pytest

from agent.secret_sources import bitwarden as bw


def test_encrypted_mode_removes_plaintext_before_failed_fetch(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    fake_binary = tmp_path / "bws"
    fake_binary.write_text("")
    bw._reset_cache_for_tests(home)
    plaintext_path = bw._disk_cache_path(home)
    plaintext_path.parent.mkdir(parents=True, exist_ok=True)
    plaintext_path.write_text('{"secrets":{"OLD":"plaintext-secret"}}')

    monkeypatch.setattr(
        bw.subprocess,
        "run",
        lambda *a, **kw: mock.Mock(
            returncode=1,
            stdout="",
            stderr="Error: network is unreachable",
        ),
    )

    with pytest.raises(RuntimeError):
        bw.fetch_bitwarden_secrets(
            access_token="0.t",
            project_id="proj-1",
            binary=fake_binary,
            cache_ttl_seconds=0,
            encrypted_cache_enabled=True,
            encrypted_cache_max_stale_seconds=0,
            home_path=home,
        )

    assert not plaintext_path.exists()
