# Sibling configs stamped 52 while the code is v0.22.5

The failed deploy of `bdc36e808` migrated sibling profiles (wife, dad, alice,
katie, group-assistant, and the others) from schema 51 to 52, then the host
was rolled back to `771fd573` (v0.22.5). This note is analysis of that
checkout. It does not change a host.

## Can v0.22.5 read a schema-52 config?

Yes. `771fd573` does not reject a stamp newer than its own latest (51).
`check_config_version()` returns the on-disk number, and `load_config()`
deep-merges `DEFAULT_CONFIG` over the file. A higher `_config_version` is
not a startup failure.

What it cannot recover by itself is `browser.backend`. Schema 52 deletes
that key (`migrate_to_52` pops it). v0.22.5's default is `browser.backend:
""`: Browser Use when that CLI (or `uvx`) is available, otherwise the
built-in browser tools. `off` on v0.22.5 meant "force the built-in tools".
A sibling that had `backend: off`, `browser-use`, or any other explicit
value now behaves as that default, because the key is gone and the
deep-merge fills `""`. The same is true of `browser.extension_control`,
whose v0.22.5 default is `{enabled: false, developer_mode: false}`. `.env`
was not rewritten; `AGENT_BROWSER_*` lines are still there, and any copies
the migration wrote under `browser.*` are keys v0.22.5 already knows.

## What a reverse 52-to-51 migration takes

The migrated file does not contain the deleted values. Setting
`_config_version: 51` is not enough to restore `browser.backend`: the value
was removed, not transformed. A reverse step would have to invent it.

Safe recovery, per sibling home, while the code is still v0.22.5:

1. Do not start a v0.23 gateway against that home, and do not run
   `hermes config migrate` there. Either one will treat 52 as current and
   leave the deleted key gone.
2. If `config.yaml.bak-<UTC stamp>` sits next to `config.yaml`, that copy
   is the pre-migration file (the boot migrator writes it; the fleet
   updater does not). Restore it over `config.yaml`. Confirm
   `_config_version: 51` and that `browser.backend` is the value you expect.
3. If there is no backup, the file is still readable. To put the stamp back
   to 51, set `_config_version: 51` and put `browser.backend` back only if
   you still have the old value (the migration's own note is
   `removed browser.backend: <value>`, in the gateway log for that home).
   Do not guess the value. Without it, leave the key absent: v0.22.5 will
   use the default `""` until someone sets it on purpose.
4. `browser.extension_control` is the same shape. The default is
   `{enabled: false, developer_mode: false}`. Restore it only from a backup
   or a note you trust.

After the file is schema 51 again, a later deploy of code that contains the
targeted-profile migrator will migrate that one home, write a new backup,
and will not walk the other profiles.
