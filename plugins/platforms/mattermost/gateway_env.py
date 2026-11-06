"""Mattermost env -> gateway config bridge (``PlatformEntry.env_overrides``)."""

from gateway.config import Platform
from gateway.platform_env import EnvCred, home_step

ENV_ENABLE_CREDENTIALS = ("MATTERMOST_TOKEN",)

_MATTERMOST_CRED = EnvCred(
    Platform.MATTERMOST, ("MATTERMOST_TOKEN",), token="MATTERMOST_TOKEN",
    warn_missing=("MATTERMOST_URL", "MATTERMOST_TOKEN set but MATTERMOST_URL is missing"),
    fixed=(("url", "MATTERMOST_URL"),),
)


def _mattermost_env_overrides(config) -> None:
    """Auto-enable only when this profile owns the token (default, .env, or vault alias)."""
    from plugins.platforms.mattermost.token_scope import profile_owns_mattermost_token
    if not profile_owns_mattermost_token():
        return
    _MATTERMOST_CRED(config)


ENV_OVERRIDES = (
    _mattermost_env_overrides,
    home_step(Platform.MATTERMOST, "MATTERMOST_HOME_CHANNEL"),
)

# Authorization env names (``PlatformEntry`` fields). Core reads them from this file when the
# live entry lacks them: a user plugin overriding this platform, safe mode, the plugin disabled.
AUTHZ = {
    "allowed_users_env": "MATTERMOST_ALLOWED_USERS",
    "allow_all_env": "MATTERMOST_ALLOW_ALL_USERS",
    "allowlist_suppresses_pairing": True,
}
