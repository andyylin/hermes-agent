# Memory Tree — core seams (not implemented in plugin)

The user plugin at `plugin-export/memory-tree/` covers everything the existing
plugin APIs support today: the `memory_tree` model tool, `hermes memory-tree`
CLI, and operator workflows (build/search/status/attention/privacy/reconcile).

These behaviors still require **core or overlay** wiring if you want them
without an explicit model tool call:

## 1. Auto prompt-context injection (`memory_tree.mode: auto`)

`memory_tree.enabled: true` with `memory_tree.mode: auto` was designed to
inject retrieved context into the user turn automatically. Hermes has **no
plugin hook** that can prepend bounded pack context to each turn without
breaking per-conversation prompt caching. The plugin reports
`context.auto_injection` in `hermes memory-tree status` but does not mutate
prompt state.

**Workaround:** keep `mode: manual` (Andy default) and call the `memory_tree`
tool or `hermes memory-tree search` on demand.

## 2. Built-in `memory_tree` toolset in `toolsets.py`

Core no longer ships `memory_tree` in `_HERMES_CORE_TOOLS` or
`CONFIGURABLE_TOOLSETS`. After install, enable the plugin toolset via
`plugins.enabled` and `hermes tools` (plugin toolsets appear under the 🔌
entries from `get_plugin_toolsets()`).

## 3. Cron / automation that assumed in-tree modules

Jobs that imported `agent.memory_tree_*` or `hermes_cli.memory_tree` must use
the installed plugin path (`hermes memory-tree build`, etc.) or import
`hermes_plugins.memory_tree` after the plugin is on disk. No core import shim
is provided on this branch.

## 4. `memory_tree` config section

The plugin reads the existing top-level `memory_tree:` keys from
`config.yaml` (`enabled`, `mode`, `packs`, `max_results`, …). No new core
`DEFAULT_CONFIG` entry or `plugins.entries` schema was added — enable the
plugin with `plugins.enabled: [memory-tree]` (or `plugins.entries`).
