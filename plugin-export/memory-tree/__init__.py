"""Memory Tree Lite user plugin — on-call local pack retrieval and maintenance."""

from __future__ import annotations

from .cli import cmd_memory_tree, register_cli
from .tool import MEMORY_TREE_SCHEMA, check_memory_tree_requirements, memory_tree_tool


def register(ctx) -> None:
    ctx.register_tool(
        name="memory_tree",
        toolset="memory_tree",
        schema=MEMORY_TREE_SCHEMA,
        handler=lambda args, **kw: memory_tree_tool(args, **kw),
        check_fn=check_memory_tree_requirements,
        emoji="🌳",
    )
    ctx.register_cli_command(
        name="memory-tree",
        help="Inspect/search generated Memory Tree Lite packs without auto-injection",
        setup_fn=register_cli,
        handler_fn=cmd_memory_tree,
        description="Build, inspect, search, and preview Memory Tree Lite context on demand.",
    )
