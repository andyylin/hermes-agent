# Memory Tree user plugin export

Standalone Hermes plugin package peeled from the Andy runtime overlay on branch
`feat/memory-tree-user-plugin-20260905`. Copy this directory into your profile's
plugin path so Memory Tree survives `hermes-agent` updates without keeping fork
patches in core.

## Install

```bash
mkdir -p ~/.hermes/plugins
cp -a plugin-export/memory-tree ~/.hermes/plugins/memory-tree
```

For a named profile, use `~/.hermes/profiles/<name>/plugins/memory-tree` instead
(same layout; `get_hermes_home()` resolves paths).

## Enable

1. Add the plugin to config (exact-path edit to your `config.yaml`):

```yaml
plugins:
  enabled:
    - memory-tree
```

2. Keep Memory Tree behavior flags under the existing section (Andy runtime
   uses `enabled: true`):

```yaml
memory_tree:
  enabled: true
  mode: manual
  packs:
    - recent
```

3. Enable the `memory_tree` toolset for your platforms via `hermes tools`
   (plugin toolsets show as 🔌 entries once the plugin is loaded).

4. Restart Hermes processes that cache tool schemas (CLI session, gateway) when
   you are ready — **not required for validating this export in the worktree**.

## What the plugin provides

- Model tool: `memory_tree` (`status`, `search`, `context-preview`)
- CLI: `hermes memory-tree` with subcommands `status`, `build`, `search`,
  `context-preview`, `attention`, `reconcile`, `privacy`
- Flags: `--json`, `--report`, `--quiet` (via argparse defaults), Han/Bopomofo
  search tokenization preserved in `memory_tree_lite.py`
- Data under `$HERMES_HOME/data/memory-tree-lite/` (profile-aware via
  `get_hermes_home()`)

## What still requires overlay / core

See [SEAMS.md](./SEAMS.md) — mainly automatic turn injection when
`memory_tree.mode: auto` (no cache-safe plugin hook today).

## Verify (Pi-safe, from repo worktree)

```bash
cd /path/to/hermes-agent-worktree
python -m compileall plugin-export/memory-tree
python -m pytest tests/plugins/memory_tree tests/hermes_cli/test_tools_config.py -q -o addopts=
git diff --check
```

## Package layout

```
plugin-export/memory-tree/
  plugin.yaml
  __init__.py          # register(ctx): tool + CLI
  cli.py               # hermes memory-tree implementation
  tool.py              # memory_tree model tool
  memory_tree_lite.py  # search/build primitives
  memory_tree_build.py
  memory_tree_attention.py
  memory_tree_privacy.py
  memory_tree_reconcile.py
```

Tests live at `tests/plugins/memory_tree/` (import the export package from
`plugin-export/memory-tree/`).
