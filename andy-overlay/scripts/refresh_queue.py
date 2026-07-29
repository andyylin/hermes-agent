#!/usr/bin/env python3
"""Regenerate the Andy overlay patch queue from one validated candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def run(repo: Path, *args: str, env: dict[str, str] | None = None, text: bool = True):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=text,
        env=env,
    ).stdout


def commit(repo: Path, value: str) -> str:
    return run(repo, "rev-parse", "--verify", f"{value}^{{commit}}").strip()


def payload_tree(repo: Path, candidate: str, control_path: str) -> str:
    fd, index_name = tempfile.mkstemp(prefix="andy-overlay-index-")
    os.close(fd)
    os.unlink(index_name)
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = index_name
    try:
        run(repo, "read-tree", candidate, env=env)
        controlled = run(
            repo,
            "ls-tree",
            "-r",
            "--name-only",
            candidate,
            "--",
            control_path,
            env=env,
        ).splitlines()
        if controlled:
            run(repo, "update-index", "--force-remove", "--", *controlled, env=env)
        return run(repo, "write-tree", env=env).strip()
    finally:
        Path(index_name).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--candidate", required=True)
    args = parser.parse_args()

    repo = args.repo.resolve()
    manifest_path = args.manifest.resolve()
    manifest = json.loads(manifest_path.read_text())
    control_path = manifest["control_path"].rstrip("/")
    base = commit(repo, args.base)
    candidate = commit(repo, args.candidate)
    subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", base, candidate],
        check=True,
    )

    changed = set(
        run(repo, "diff", "--name-only", f"{base}..{candidate}").splitlines()
    )
    changed = {p for p in changed if p != control_path and not p.startswith(control_path + "/")}

    declared: list[str] = []
    for group in manifest["groups"]:
        declared.extend(group["paths"])
    duplicates = sorted({p for p in declared if declared.count(p) > 1})
    if duplicates:
        raise SystemExit(f"duplicate classified paths: {duplicates}")
    missing = sorted(changed - set(declared))
    stale = sorted(set(declared) - changed)
    if missing or stale:
        raise SystemExit(
            "classification mismatch\n"
            f"unclassified changed paths: {missing}\n"
            f"declared but unchanged paths: {stale}"
        )

    patches_dir = manifest_path.parent / "patches"
    patches_dir.mkdir(parents=True, exist_ok=True)
    expected_names = {f"{g['name']}.patch" for g in manifest["groups"]}
    for old in patches_dir.glob("*.patch"):
        if old.name not in expected_names:
            old.unlink()

    series: list[str] = []
    for group in manifest["groups"]:
        name = f"{group['name']}.patch"
        patch = run(
            repo,
            "diff",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            f"{base}..{candidate}",
            "--",
            *group["paths"],
            text=False,
        )
        if not patch:
            raise SystemExit(f"empty patch group: {group['name']}")
        (patches_dir / name).write_bytes(patch)
        group["patch"] = f"patches/{name}"
        group["sha256"] = hashlib.sha256(patch).hexdigest()
        series.append(group["patch"])

    manifest["frozen_upstream"] = {"sha": base}
    manifest["source_candidate"] = {
        "sha": candidate,
        "tree": run(repo, "rev-parse", f"{candidate}^{{tree}}").strip(),
    }
    manifest["expected_payload_tree"] = payload_tree(repo, candidate, control_path)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    (manifest_path.parent / "series").write_text("\n".join(series) + "\n")
    print(
        json.dumps(
            {
                "base": base,
                "candidate": candidate,
                "payload_tree": manifest["expected_payload_tree"],
                "patches": len(series),
                "paths": len(changed),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
