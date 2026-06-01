"""On-call Memory Tree Lite retrieval tool.

This exposes generated Memory Tree packs to the agent only when explicitly
called. It never auto-injects context into prompts.
"""
from __future__ import annotations

import json
from typing import Any


def check_memory_tree_requirements() -> bool:
    return True


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


def memory_tree_tool(args: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    args = args or {}
    action = str(args.get("action") or "search")
    query = str(args.get("query") or "").strip()
    limit = int(args.get("limit") or 3)
    chars = int(args.get("chars") or 800)
    packs = args.get("packs")

    from hermes_cli.memory_tree import memory_tree_context_preview, memory_tree_search, memory_tree_status

    if action == "status":
        return {
            "success": True,
            "auto_injected": False,
            "status": json.loads(memory_tree_status(json_mode=True)),
        }
    if action == "search":
        if not query:
            return {"success": False, "error": "query is required for memory_tree search", "auto_injected": False}
        return {
            "success": True,
            "auto_injected": False,
            "result": json.loads(
                memory_tree_search(query, packs=packs, limit=limit, chars=chars, json_mode=True)
            ),
        }
    if action == "context-preview":
        if not query:
            return {"success": False, "error": "query is required for memory_tree context-preview", "auto_injected": False}
        payload = json.loads(
            memory_tree_context_preview(query, packs=packs, limit=limit, chars=chars, json_mode=True)
        )
        return {
            "success": True,
            "auto_injected": False,
            "context": payload["context"],
            "result": payload,
        }
    return {"success": False, "error": f"unknown memory_tree action: {action}", "auto_injected": False}


from tools.registry import registry

registry.register(
    name="memory_tree",
    toolset="memory_tree",
    schema=MEMORY_TREE_SCHEMA,
    handler=lambda args, **kw: memory_tree_tool(args, **kw),
    check_fn=check_memory_tree_requirements,
    emoji="🌳",
)
