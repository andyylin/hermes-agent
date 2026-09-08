"""Memory Tree Lite model tool (on-call retrieval only)."""
from __future__ import annotations

import json
from typing import Any


def _memory_tree_config_enabled() -> bool:
    try:
        from hermes_cli.config import load_config_readonly

        data = load_config_readonly()
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    section = data.get("memory_tree")
    if not isinstance(section, dict):
        return False
    return bool(section.get("enabled", False))


def check_memory_tree_requirements() -> bool:
    """Expose the tool only when ``memory_tree.enabled`` is true in config."""
    return _memory_tree_config_enabled()


MEMORY_TREE_SCHEMA = {
    "name": "memory_tree",
    "description": (
        "On-call retrieval from generated Hermes Memory Tree Lite packs. "
        "Use this for current assistant-owned automation/runtime state, active-work ledger context, "
        "or bounded context-preview from generated local packs. This tool does NOT auto-inject memory; "
        "it only returns results when called."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "search", "context-preview"],
                "description": "Operation to perform. status reports build/context config; search returns matches; context-preview returns a bounded context block.",
                "default": "search",
            },
            "query": {
                "type": "string",
                "description": "Search query. Required for search and context-preview.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of search results/context records.",
                "default": 3,
            },
            "chars": {
                "type": "integer",
                "description": "Maximum snippet/context chars per record.",
                "default": 800,
            },
            "packs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional pack names or Markdown paths. Defaults to configured packs, usually recent.",
            },
        },
        "required": [],
    },
}


def memory_tree_tool(args: dict[str, Any] | None = None, **_: Any) -> str:
    if not _memory_tree_config_enabled():
        return json.dumps(
            {
                "success": False,
                "error": "memory_tree.enabled is false in config.yaml; enable it to use Memory Tree retrieval",
                "auto_injected": False,
            }
        )

    from .cli import memory_tree_context_preview, memory_tree_search, memory_tree_status

    args = args or {}
    action = str(args.get("action") or "search")
    query = str(args.get("query") or "").strip()
    limit = int(args.get("limit") or 3)
    chars = int(args.get("chars") or 800)
    packs = args.get("packs")

    if action == "status":
        return json.dumps(
            {
                "success": True,
                "auto_injected": False,
                "status": json.loads(memory_tree_status(json_mode=True)),
            }
        )
    if action == "search":
        if not query:
            return json.dumps(
                {
                    "success": False,
                    "error": "query is required for memory_tree search",
                    "auto_injected": False,
                }
            )
        return json.dumps(
            {
                "success": True,
                "auto_injected": False,
                "result": json.loads(
                    memory_tree_search(query, packs=packs, limit=limit, chars=chars, json_mode=True)
                ),
            }
        )
    if action == "context-preview":
        if not query:
            return json.dumps(
                {
                    "success": False,
                    "error": "query is required for memory_tree context-preview",
                    "auto_injected": False,
                }
            )
        payload = json.loads(
            memory_tree_context_preview(query, packs=packs, limit=limit, chars=chars, json_mode=True)
        )
        return json.dumps(
            {
                "success": True,
                "auto_injected": False,
                "context": payload["context"],
                "result": payload,
            }
        )
    return json.dumps(
        {
            "success": False,
            "error": f"unknown memory_tree action: {action}",
            "auto_injected": False,
        }
    )
