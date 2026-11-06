"""Load a live ``_config_version`` 51 config on a candidate tree.

``HERMES_HOME`` must already point at the scratch home. The candidate has to
migrate that stamp forward to its own latest schema without dropping unrelated
user keys. A stamp newer than the candidate is a failure: this tree cannot
represent the live file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import hermes_yaml as yaml

from hermes_cli.config import DEFAULT_CONFIG, migrate_config


def _fixture(live_version: int) -> str:
    return (
        f"_config_version: {live_version}\n"
        "model:\n"
        "  default: live-model\n"
        "  provider: openrouter\n"
        "browser:\n"
        "  backend: browser-use\n"
        "  cloud_provider: local\n"
        "cron:\n"
        "  max_parallel_jobs: 2\n"
        "display:\n"
        "  skin: mono\n"
    )


def _read(path: Path) -> dict:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--live-version", type=int, required=True)
    args = parser.parse_args(argv)
    home = args.home.resolve()
    if Path(os.environ.get("HERMES_HOME", "")).resolve() != home:
        print(json.dumps({"ok": False, "error": "HERMES_HOME does not match --home"}))
        return 1
    path = home / "config.yaml"
    path.write_text(_fixture(args.live_version), encoding="utf-8")
    latest = int(DEFAULT_CONFIG["_config_version"])
    failures: list[str] = []
    if latest < args.live_version:
        failures.append(f"candidate schema {latest} is older than live {args.live_version}")
    results = migrate_config(interactive=False, quiet=True)
    warnings = [str(item) for item in results.get("warnings") or []]
    if any("support floor" in item.lower() or "failed" in item.lower() for item in warnings):
        failures.append("migration warnings: " + "; ".join(warnings))
    raw = _read(path)
    if raw.get("_config_version") != latest:
        failures.append(f"on-disk stamp {raw.get('_config_version')} != latest {latest}")
    model = raw.get("model") if isinstance(raw.get("model"), dict) else {}
    if model.get("default") != "live-model" or model.get("provider") != "openrouter":
        failures.append(f"model keys changed: {model}")
    cron = raw.get("cron") if isinstance(raw.get("cron"), dict) else {}
    if cron.get("max_parallel_jobs") != 2:
        failures.append(f"cron settings changed: {cron}")
    display = raw.get("display") if isinstance(raw.get("display"), dict) else {}
    if display.get("skin") != "mono":
        failures.append(f"display.skin changed: {display}")
    browser = raw.get("browser") if isinstance(raw.get("browser"), dict) else {}
    if latest > args.live_version and "backend" in browser:
        failures.append("forward migration left browser.backend in place")
    if browser.get("cloud_provider") != "local":
        failures.append(f"browser.cloud_provider changed: {browser}")
    settled = path.read_bytes()
    again = migrate_config(interactive=False, quiet=True)
    if again.get("config_added"):
        failures.append(f"second migrate rewrote config: {again.get('config_added')}")
    if path.read_bytes() != settled:
        failures.append("second migrate changed config.yaml bytes")
    payload = {
        "ok": not failures,
        "failures": failures,
        "latest": latest,
        "on_disk": raw.get("_config_version"),
        "warnings": warnings,
        "backend_present": "backend" in browser,
    }
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
