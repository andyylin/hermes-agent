"""Which profile may use MATTERMOST_TOKEN.

Canonical ``MATTERMOST_TOKEN`` from a shared Bitwarden project is default-only.
A named profile must set its own ``.env`` value or a ``MATTERMOST_TOKEN_<PROFILE>``
vault key (aliased by the secret orchestrator). Host scripts that read the
unsuffixed name keep working under the default profile.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

MATTERMOST_TOKEN = "MATTERMOST_TOKEN"


def named_profile_home(home: Optional[Path] = None) -> bool:
    """True when *home* is ``…/profiles/<name>`` (not the default ``~/.hermes``)."""
    from hermes_constants import get_hermes_home

    resolved = Path(home) if home is not None else Path(get_hermes_home())
    return resolved.parent.name == "profiles" and bool(resolved.name)


def profile_token_alias(home: Path) -> str:
    return f"{MATTERMOST_TOKEN}_{Path(home).name.replace('-', '_').upper()}"


def owned_mattermost_token(home: Optional[Path] = None) -> str:
    """Token this profile configured itself — never another profile's shared vault value."""
    from agent.secret_scope import load_env_file
    from hermes_constants import get_hermes_home

    resolved = Path(home) if home is not None else Path(get_hermes_home())
    if not named_profile_home(resolved):
        from gateway.platforms._shared import get_scoped_secret
        return (get_scoped_secret(MATTERMOST_TOKEN, "") or "").strip()
    env_token = (load_env_file(resolved / ".env").get(MATTERMOST_TOKEN) or "").strip()
    if env_token:
        return env_token
    from hermes_cli.env_loader import get_secret_source_values
    external = get_secret_source_values(resolved)
    return (external.get(profile_token_alias(resolved)) or "").strip()


def profile_owns_mattermost_token(home: Optional[Path] = None) -> bool:
    """Default profile: yes. Named profile: only when it has its own token."""
    if not named_profile_home(home):
        return True
    return bool(owned_mattermost_token(home))
