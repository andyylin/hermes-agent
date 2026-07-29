#!/usr/bin/env python3
"""Build an upstream-first Hermes candidate by replaying the Andy patch queue."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def git(repo: Path, *args: str, capture: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=capture,
        text=True,
    )
    return result.stdout if capture else ""


def resolve(repo: Path, value: str) -> str:
    return git(repo, "rev-parse", "--verify", f"{value}^{{commit}}").strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True, help="repository containing refs and the control plane")
    parser.add_argument("--upstream", required=True, help="upstream commit/tag to start from")
    parser.add_argument("--control-ref", default="HEAD", help="ref containing andy-overlay/")
    parser.add_argument("--output", type=Path, required=True, help="new worktree path; must not exist")
    parser.add_argument("--expect-control-tree", action="store_true", help="require final tree to equal control-ref tree")
    args = parser.parse_args()

    repo = args.repo.resolve()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"refusing existing output path: {output}")
    upstream = resolve(repo, args.upstream)
    control_ref = resolve(repo, args.control_ref)
    manifest_raw = git(repo, "show", f"{control_ref}:andy-overlay/manifest.json")
    manifest = json.loads(manifest_raw)
    control_path = manifest["control_path"].rstrip("/")
    if git(repo, "ls-tree", "--name-only", upstream, "--", control_path).strip():
        raise SystemExit(f"upstream unexpectedly owns {control_path}; classify before replay")

    user_name = git(repo, "config", "user.name").strip()
    user_email = git(repo, "config", "user.email").strip()
    if not user_name or not user_email:
        raise SystemExit("git user.name and user.email are required for transparent replay commits")

    git(repo, "worktree", "add", "--detach", str(output), upstream, capture=False)
    patch_reports: list[dict[str, str]] = []
    report: dict[str, object] = {
        "upstream": upstream,
        "control_ref": control_ref,
        "output": str(output),
        "patches": patch_reports,
    }
    try:
        for group in manifest["groups"]:
            patch_rel = group["patch"]
            patch_bytes = subprocess.run(
                ["git", "-C", str(repo), "show", f"{control_ref}:andy-overlay/{patch_rel}"],
                check=True,
                capture_output=True,
            ).stdout
            actual = hashlib.sha256(patch_bytes).hexdigest()
            if actual != group["sha256"]:
                raise RuntimeError(f"patch checksum mismatch: {patch_rel}")
            patch_path = output / ".git-andy-overlay-current.patch"
            patch_path.write_bytes(patch_bytes)
            try:
                subprocess.run(
                    ["git", "-C", str(output), "apply", "--index", "--3way", str(patch_path)],
                    check=True,
                )
            finally:
                patch_path.unlink(missing_ok=True)
            git(output, "commit", "-m", group["subject"], capture=False)
            patch_reports.append({"name": group["name"], "commit": resolve(output, "HEAD")})

        payload_tree = git(output, "rev-parse", "HEAD^{tree}").strip()
        report["payload_tree"] = payload_tree
        if upstream == manifest["frozen_upstream"]["sha"] and payload_tree != manifest["expected_payload_tree"]:
            raise RuntimeError(
                f"frozen-base payload tree mismatch: {payload_tree} != {manifest['expected_payload_tree']}"
            )

        git(output, "checkout", control_ref, "--", control_path, capture=False)
        git(output, "commit", "-m", "chore(runtime): add Andy overlay replay control plane", capture=False)
        final_commit = resolve(output, "HEAD")
        final_tree = git(output, "rev-parse", "HEAD^{tree}").strip()
        report["candidate_commit"] = final_commit
        report["candidate_tree"] = final_tree
        report["clean"] = not bool(git(output, "status", "--porcelain").strip())
        if not report["clean"]:
            raise RuntimeError("candidate worktree is dirty after replay")
        if args.expect_control_tree:
            expected = git(repo, "rev-parse", f"{control_ref}^{{tree}}").strip()
            report["expected_control_tree"] = expected
            if final_tree != expected:
                raise RuntimeError(f"control-tree mismatch: {final_tree} != {expected}")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(f"REPLAY BLOCKED: {exc}", file=sys.stderr)
        print(f"diagnostic worktree retained at {output}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
