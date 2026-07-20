"""Bind messaging conversation scopes to first-class Hermes Projects.

The user-facing configuration lives under ``gateway.workspace_bindings``. A
binding is resolved from the stable platform conversation ID (a Matrix room ID
for the first consumer), never from a mutable display name. Configured but
invalid bindings fail closed instead of silently dropping into the gateway's
global working directory.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping, Optional

from hermes_cli import projects_db as pdb


class WorkspaceBindingError(ValueError):
    """A configured workspace binding cannot be resolved safely."""


@dataclass(frozen=True)
class WorkspaceBinding:
    platform: str
    conversation_id: str
    project_id: str
    project_slug: str
    project_name: str
    cwd: str
    allowed_folders: tuple[str, ...]


def _platform_name(source: Any) -> str:
    platform = getattr(source, "platform", "")
    value = getattr(platform, "value", platform)
    return str(value or "").strip().lower()


def _binding_map(config: Mapping[str, Any], platform: str) -> Mapping[str, Any]:
    if not isinstance(config, Mapping):
        raise WorkspaceBindingError("gateway configuration is not a mapping")
    if "gateway" not in config:
        return {}
    gateway = config.get("gateway")
    if not isinstance(gateway, Mapping):
        raise WorkspaceBindingError("gateway configuration is not a mapping")
    if "workspace_bindings" not in gateway:
        return {}
    bindings = gateway.get("workspace_bindings")
    if not isinstance(bindings, Mapping):
        raise WorkspaceBindingError("gateway.workspace_bindings is not a mapping")
    if platform not in bindings:
        return {}
    platform_bindings = bindings.get(platform)
    if isinstance(platform_bindings, str):
        try:
            platform_bindings = json.loads(platform_bindings)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WorkspaceBindingError(
                f"workspace bindings for {platform} are not valid JSON"
            ) from exc
    if not isinstance(platform_bindings, Mapping):
        raise WorkspaceBindingError(
            f"workspace bindings for {platform} are not a mapping"
        )
    return platform_bindings


def _project_reference(raw: Any) -> str:
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, Mapping):
        return str(raw.get("project") or "").strip()
    return ""


def resolve_workspace_binding(
    config: Mapping[str, Any],
    source: Any,
    *,
    projects_db_path: Optional[Path] = None,
) -> Optional[WorkspaceBinding]:
    """Resolve a configured conversation to a live Project and primary folder.

    Returns ``None`` when the platform/conversation has no binding. Once a
    conversation appears in configuration, malformed entries, missing or
    archived Projects, and missing primary folders raise
    :class:`WorkspaceBindingError`; callers must not fall back to a broader cwd.
    """

    platform = _platform_name(source)
    if not platform:
        return None

    conversation_id = str(getattr(source, "chat_id", "") or "").strip()
    platform_bindings = _binding_map(config, platform)
    if conversation_id not in platform_bindings:
        return None
    raw = platform_bindings[conversation_id]

    project_ref = _project_reference(raw)
    if not project_ref:
        raise WorkspaceBindingError(
            f"workspace binding for {platform}:{conversation_id} has no project"
        )

    with pdb.connect_closing(projects_db_path) as conn:
        project = pdb.get_project(conn, project_ref)
    if project is None or project.archived:
        raise WorkspaceBindingError(
            f"workspace binding project {project_ref!r} is missing or archived"
        )

    primary = str(project.primary_path or "").strip()
    if not primary:
        raise WorkspaceBindingError(
            f"workspace binding project {project.slug!r} has no primary folder"
        )
    primary_path = Path(primary).expanduser().resolve()
    if not primary_path.is_dir():
        raise WorkspaceBindingError(
            f"workspace binding primary folder does not exist: {primary_path}"
        )

    allowed: list[str] = []
    for folder in project.folders:
        candidate = Path(folder.path).expanduser().resolve()
        if candidate.is_dir():
            value = str(candidate)
            if value not in allowed:
                allowed.append(value)
    if str(primary_path) not in allowed:
        allowed.insert(0, str(primary_path))

    return WorkspaceBinding(
        platform=platform,
        conversation_id=conversation_id,
        project_id=project.id,
        project_slug=project.slug,
        project_name=project.name,
        cwd=str(primary_path),
        allowed_folders=tuple(allowed),
    )
