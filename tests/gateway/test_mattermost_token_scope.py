"""Mattermost token is default-profile-owned; a second login is refused.

Regression for the rpi5/Joi incident: a parked named profile with no Mattermost
config pulled Default's Bitwarden ``MATTERMOST_TOKEN`` and double-replied as
the same bot. Tests use fake tokens only.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.mattermost.adapter import MattermostAdapter, _url_and_token, validate_mattermost_config
from plugins.platforms.mattermost.token_scope import owned_mattermost_token, profile_owns_mattermost_token


@pytest.fixture
def profile_homes(tmp_path, monkeypatch):
    """Default ``~/.hermes`` plus a named profile home; Path.home is the tmp root."""
    root = tmp_path / ".hermes"
    root.mkdir()
    named = root / "profiles" / "utility-agent"
    named.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return root, named


def _point_home(monkeypatch, home: Path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(home))
    from agent.secret_scope import invalidate_env_file_cache
    invalidate_env_file_cache(home / ".env")


class TestOwnedMattermostToken:
    def test_named_profile_without_own_token_cannot_resolve_shared_token(
        self, profile_homes, monkeypatch,
    ):
        _default, named = profile_homes
        (named / ".env").write_text("OPENAI_API_KEY=not-a-mm-token\n", encoding="utf-8")
        _point_home(monkeypatch, named)
        monkeypatch.setenv("MATTERMOST_TOKEN", "shared-default-token")
        monkeypatch.setenv("MATTERMOST_URL", "https://mm.example.com")

        assert owned_mattermost_token(named) == ""
        assert profile_owns_mattermost_token(named) is False
        _url, token = _url_and_token(PlatformConfig(enabled=True, extra={}))
        assert token == ""
        assert validate_mattermost_config(PlatformConfig(enabled=True, extra={"url": "https://mm.example.com"})) is False

    def test_default_profile_still_resolves_shared_token(self, profile_homes, monkeypatch):
        default, _named = profile_homes
        _point_home(monkeypatch, default)
        monkeypatch.setenv("MATTERMOST_TOKEN", "shared-default-token")
        monkeypatch.setenv("MATTERMOST_URL", "https://mm.example.com")

        assert owned_mattermost_token(default) == "shared-default-token"
        assert profile_owns_mattermost_token(default) is True
        _url, token = _url_and_token(PlatformConfig(enabled=True, extra={}))
        assert token == "shared-default-token"
        assert validate_mattermost_config(
            PlatformConfig(enabled=True, extra={"url": "https://mm.example.com"}),
        ) is True

    def test_named_profile_own_env_token_resolves(self, profile_homes, monkeypatch):
        _default, named = profile_homes
        (named / ".env").write_text("MATTERMOST_TOKEN=utility-own-token\n", encoding="utf-8")
        _point_home(monkeypatch, named)
        monkeypatch.setenv("MATTERMOST_TOKEN", "shared-default-token")

        assert owned_mattermost_token(named) == "utility-own-token"
        _url, token = _url_and_token(PlatformConfig(enabled=True, extra={}))
        assert token == "utility-own-token"

    def test_named_profile_ignores_leaked_config_token(self, profile_homes, monkeypatch):
        _default, named = profile_homes
        (named / ".env").write_text("", encoding="utf-8")
        _point_home(monkeypatch, named)

        _url, token = _url_and_token(PlatformConfig(enabled=True, token="shared-default-token", extra={}))
        assert token == ""

    def test_named_profile_suffixed_vault_token_resolves(self, profile_homes, monkeypatch):
        _default, named = profile_homes
        (named / ".env").write_text("", encoding="utf-8")
        _point_home(monkeypatch, named)
        from hermes_cli.env_loader import reset_secret_source_cache
        from hermes_cli import env_loader
        reset_secret_source_cache(named)
        env_loader._SECRET_SOURCE_VALUES_BY_HOME[str(named.resolve())] = {
            "MATTERMOST_TOKEN_UTILITY_AGENT": "utility-aliased-token",
        }
        try:
            assert owned_mattermost_token(named) == "utility-aliased-token"
        finally:
            reset_secret_source_cache(named)


class TestMattermostTokenLock:
    def _adapter(self) -> MattermostAdapter:
        return MattermostAdapter(PlatformConfig(
            enabled=True, token="test-token", extra={"url": "https://mm.example.com"},
        ))

    @pytest.mark.asyncio
    async def test_connect_refuses_when_token_lock_held(self):
        adapter = self._adapter()
        seen = []

        def _refuse(scope, identity, desc):
            seen.append((scope, identity, desc))
            return False

        adapter._acquire_platform_lock = _refuse
        assert await adapter.connect() is False
        assert seen == [("mattermost-bot-token", "test-token", "Mattermost bot token")]
        assert adapter._session is None

    @pytest.mark.asyncio
    async def test_connect_releases_lock_when_auth_fails(self, monkeypatch):
        adapter = self._adapter()
        released = []
        adapter._acquire_platform_lock = lambda *a, **k: True
        adapter._release_platform_lock = lambda: released.append(True)
        adapter._api_get = AsyncMock(return_value={})

        class _Sess:
            closed = False

            async def close(self):
                self.closed = True

        import aiohttp
        monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: _Sess())
        assert await adapter.connect() is False
        assert released == [True]

    @pytest.mark.asyncio
    async def test_disconnect_releases_token_lock(self):
        adapter = self._adapter()
        released = []
        adapter._release_platform_lock = lambda: released.append(True)
        adapter._session = None
        adapter._ws = None
        await adapter.disconnect()
        assert released == [True]
