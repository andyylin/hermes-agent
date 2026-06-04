"""CLI and callable helpers for Memory Tree Lite.

Memory Tree is deliberately on-call: these helpers read generated packs and
scanner outputs only when explicitly invoked. They do not mutate prompt state or
auto-inject context.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from hermes_constants import get_hermes_home


DEFAULT_PACKS = ("recent",)


def _home() -> Path:
    return get_hermes_home()


def _data_dir(home: Path | None = None) -> Path:
    return (home or _home()) / "data" / "memory-tree-lite"


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _load_config(home: Path | None = None) -> dict[str, Any]:
    path = (home or _home()) / "config.yaml"
    try:
        import yaml  # type: ignore
    except Exception:
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _memory_tree_config(home: Path | None = None) -> dict[str, Any]:
    config = _load_config(home).get("memory_tree")
    if not isinstance(config, dict):
        config = {}
    return {
        "enabled": bool(config.get("enabled", False)),
        "mode": str(config.get("mode", "manual")),
        "max_results": int(config.get("max_results", 3) or 3),
        "max_chars": int(config.get("max_chars", 1200) or 1200),
        "snippet_chars": int(config.get("snippet_chars", 360) or 360),
        "packs": list(config.get("packs") or DEFAULT_PACKS),
        "allowed_sources": list(config.get("allowed_sources") or []),
    }


def _state(home: Path | None = None) -> dict[str, Any]:
    data = _load_json(_data_dir(home) / "state.json", {})
    return data if isinstance(data, dict) else {}


def _pack_path(name_or_path: str, *, home: Path | None = None, state: dict[str, Any] | None = None) -> Path:
    raw = str(name_or_path)
    path = Path(raw).expanduser()
    if path.is_absolute() or raw.endswith(".md") or "/" in raw:
        return path
    outputs = (state or _state(home)).get("outputs")
    if isinstance(outputs, dict) and raw in outputs:
        return Path(str(outputs[raw])).expanduser()
    if raw == "recent":
        return _data_dir(home) / "recent.md"
    if raw == "index":
        return _data_dir(home) / "index.md"
    if raw == "daily":
        outputs = (state or _state(home)).get("outputs")
        if isinstance(outputs, dict) and "daily" in outputs:
            return Path(str(outputs["daily"])).expanduser()
    return _data_dir(home) / f"{raw}.md"


def resolve_pack_paths(packs: Iterable[str] | None = None, *, home: Path | None = None) -> list[Path]:
    state = _state(home)
    config = _memory_tree_config(home)
    selected = list(packs or config.get("packs") or DEFAULT_PACKS)
    return [_pack_path(pack, home=home, state=state) for pack in selected]


def _pack_status(path: Path) -> dict[str, Any]:
    exists = path.exists()
    return {
        "path": str(path),
        "exists": exists,
        "bytes": path.stat().st_size if exists else 0,
    }


def memory_tree_status(*, json_mode: bool = False, verbose: bool = False, home: Path | None = None) -> str:
    state = _state(home)
    config = _memory_tree_config(home)
    packs = resolve_pack_paths(config.get("packs"), home=home)
    raw_counts = state.get("counts")
    counts: dict[str, Any] = raw_counts if isinstance(raw_counts, dict) else {}
    payload = {
        "schema": "memory-tree-status-v1",
        "updated_at": state.get("updated_at"),
        "context": {
            "enabled": config["enabled"],
            "mode": config["mode"],
            "auto_injection": bool(config["enabled"] and config["mode"] == "auto"),
            "note": "Memory Tree is on-call only unless memory_tree.enabled=true and mode=auto.",
        },
        "build": {
            "records_total": counts.get("records_total", 0),
            "sessions": counts.get("sessions", 0),
            "active_work": counts.get("active_work", 0),
            "cron_outputs": counts.get("cron_outputs", 0),
        },
        "packs": [_pack_status(path) for path in packs],
        "scanner_state": {
            "attention": str(_data_dir(home) / "attention-state.json"),
            "reconcile": str(_data_dir(home) / "reconcile-state.json"),
            "privacy": str(_data_dir(home) / "privacy-state.json"),
        },
    }
    if json_mode:
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)

    lines = ["Memory Tree Lite status"]
    lines.append(f"context: enabled={payload['context']['enabled']} mode={payload['context']['mode']} auto_injection={payload['context']['auto_injection']}")
    lines.append(f"updated_at: {payload['updated_at'] or 'never'}")
    lines.append(
        "records: total={records_total} sessions={sessions} active_work={active_work} cron_outputs={cron_outputs}".format(
            **payload["build"]
        )
    )
    lines.append("packs:")
    for pack in payload["packs"]:
        lines.append(f"- {pack['path']} ({'present' if pack['exists'] else 'missing'}, {pack['bytes']} bytes)")
    if verbose:
        lines.append("scanner_state:")
        for name, path in payload["scanner_state"].items():
            lines.append(f"- {name}: {path}")
    return "\n".join(lines)


def _result_to_dict(result: Any) -> dict[str, Any]:
    return {
        "score": result.score,
        "pack_path": str(result.pack_path),
        "source_type": result.source_type,
        "source_id": result.source_id,
        "title": result.title,
        "timestamp": result.timestamp,
        "snippet": result.snippet,
        "metadata": result.metadata,
    }


def _search_results(query: str, *, packs: Iterable[str] | None = None, limit: int = 5, chars: int = 500) -> list[Any]:
    from agent.memory_tree_lite import search_memory_packs

    return search_memory_packs(resolve_pack_paths(packs), query, limit=limit, max_snippet_chars=chars)


def memory_tree_search(
    query: str,
    *,
    packs: Iterable[str] | None = None,
    limit: int = 5,
    chars: int = 500,
    json_mode: bool = False,
) -> str:
    results = _search_results(query, packs=packs, limit=limit, chars=chars)
    if json_mode:
        return json.dumps(
            {"schema": "memory-tree-search-v1", "query": query, "results": [_result_to_dict(r) for r in results]},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    if not results:
        return "Memory Tree search: no matches"
    lines = [f"Memory Tree search: {len(results)} match(es) for {query!r}"]
    for idx, result in enumerate(results, start=1):
        lines.extend(
            [
                f"{idx}. {result.title}",
                f"   source: {result.source_type} / {result.source_id}",
                f"   score: {result.score}",
                f"   snippet: {result.snippet}",
            ]
        )
    return "\n".join(lines)


def memory_tree_context_preview(
    query: str,
    *,
    packs: Iterable[str] | None = None,
    limit: int = 3,
    chars: int = 1200,
    json_mode: bool = False,
) -> str:
    results = _search_results(query, packs=packs, limit=limit, chars=chars)
    block_lines = [
        "[Memory Tree context preview — on-call retrieval only; not auto-injected]",
        f"query: {query}",
    ]
    for idx, result in enumerate(results, start=1):
        block_lines.extend(
            [
                f"\n{idx}. {result.title}",
                f"source: {result.source_type} / {result.source_id}",
                f"pack: {result.pack_path}",
                "content:",
                result.snippet,
            ]
        )
    context = "\n".join(block_lines)
    if json_mode:
        return json.dumps(
            {
                "schema": "memory-tree-context-preview-v1",
                "query": query,
                "auto_injected": False,
                "context": context,
                "results": [_result_to_dict(r) for r in results],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    return context


def cmd_memory_tree(args: Any) -> int:
    action = getattr(args, "memory_tree_command", None) or "status"
    if action == "status":
        print(memory_tree_status(json_mode=getattr(args, "json", False), verbose=getattr(args, "verbose", False)))
        return 0
    if action == "build":
        from agent.memory_tree_build import BuildOptions, build_memory_tree_packs, format_build_report

        state = build_memory_tree_packs(
            BuildOptions(
                session_limit=getattr(args, "session_limit", 40),
                cron_limit=getattr(args, "cron_limit", 30),
                ledger_limit=getattr(args, "ledger_limit", 80),
                max_record_chars=getattr(args, "max_record_chars", 4000),
                include_tools=getattr(args, "include_tools", False),
            )
        )
        if getattr(args, "json", False):
            print(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))
        elif getattr(args, "report", False):
            print(format_build_report(state))
        return 0
    if action == "search":
        print(
            memory_tree_search(
                getattr(args, "query"),
                packs=getattr(args, "packs", None),
                limit=getattr(args, "limit", 5),
                chars=getattr(args, "chars", 500),
                json_mode=getattr(args, "json", False),
            )
        )
        return 0
    if action == "context-preview":
        print(
            memory_tree_context_preview(
                getattr(args, "query"),
                packs=getattr(args, "packs", None),
                limit=getattr(args, "limit", 3),
                chars=getattr(args, "chars", 1200),
                json_mode=getattr(args, "json", False),
            )
        )
        return 0
    if action == "attention":
        from agent.memory_tree_attention import format_attention_json, format_attention_report, scan_attention

        items = scan_attention(stale_days=getattr(args, "stale_days", 7), include_stale=getattr(args, "include_stale", False))
        print(format_attention_json(items, max_chars=getattr(args, "chars", 4000)) if getattr(args, "json", False) else format_attention_report(items, max_chars=getattr(args, "chars", 4000)))
        return 0
    if action == "reconcile":
        from agent.memory_tree_reconcile import format_reconcile_json, format_reconcile_text, reconcile_active_work

        report = reconcile_active_work()
        print(format_reconcile_json(report, max_chars=getattr(args, "chars", 4000)) if getattr(args, "json", False) else format_reconcile_text(report, max_chars=getattr(args, "chars", 4000)))
        return 0
    if action == "privacy":
        from agent.memory_tree_privacy import format_privacy_json, format_privacy_text, scan_memory_tree_privacy

        report = scan_memory_tree_privacy(max_snippet_chars=getattr(args, "snippet_chars", 160))
        print(format_privacy_json(report, max_chars=getattr(args, "chars", 4000)) if getattr(args, "json", False) else format_privacy_text(report, max_chars=getattr(args, "chars", 4000)))
        return 0
    raise SystemExit(f"Unknown memory-tree command: {action}")


def add_memory_tree_parser(subparsers: Any) -> Any:
    parser = subparsers.add_parser(
        "memory-tree",
        help="Inspect/search generated Memory Tree Lite packs without auto-injection",
        description="Build, inspect, search, and preview Memory Tree Lite context on demand.",
    )
    sub = parser.add_subparsers(dest="memory_tree_command")

    status = sub.add_parser("status", help="Show pack/build/config status")
    status.add_argument("--json", action="store_true", help="Emit JSON")
    status.add_argument("--verbose", action="store_true", help="Show scanner state paths")

    build = sub.add_parser("build", help="Build packs from local Hermes state")
    build.add_argument("--session-limit", type=int, default=40)
    build.add_argument("--cron-limit", type=int, default=30)
    build.add_argument("--ledger-limit", type=int, default=80)
    build.add_argument("--max-record-chars", type=int, default=4000)
    build.add_argument("--include-tools", action="store_true")
    build.add_argument("--report", action="store_true", help="Print compact report")
    build.add_argument("--json", action="store_true", help="Emit build state JSON")

    search = sub.add_parser("search", help="Search generated packs")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--chars", type=int, default=500)
    search.add_argument("--pack", dest="packs", action="append", help="Pack name or path; repeatable")
    search.add_argument("--json", action="store_true")

    preview = sub.add_parser("context-preview", help="Preview a bounded context block for a query")
    preview.add_argument("query")
    preview.add_argument("--limit", type=int, default=3)
    preview.add_argument("--chars", type=int, default=1200)
    preview.add_argument("--pack", dest="packs", action="append", help="Pack name or path; repeatable")
    preview.add_argument("--json", action="store_true")

    attention = sub.add_parser("attention", help="Run attention scan")
    attention.add_argument("--stale-days", type=int, default=7)
    attention.add_argument("--chars", type=int, default=4000)
    attention.add_argument("--include-stale", action="store_true", help="Include low-priority stale active-work ledger review items")
    attention.add_argument("--json", action="store_true")

    reconcile = sub.add_parser("reconcile", help="Reconcile active-work source handles")
    reconcile.add_argument("--chars", type=int, default=4000)
    reconcile.add_argument("--json", action="store_true")

    privacy = sub.add_parser("privacy", help="Scan packs for credential-shaped leaks")
    privacy.add_argument("--chars", type=int, default=4000)
    privacy.add_argument("--snippet-chars", type=int, default=160)
    privacy.add_argument("--json", action="store_true")

    parser.set_defaults(func=cmd_memory_tree)
    return parser
