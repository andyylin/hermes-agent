"""Cross-package version invariants for a Hermes release checkout."""

import json
import re
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_runtime_and_desktop_versions_match():
    """Python metadata and the packaged Desktop bundle share one release version."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    runtime_init = (REPO_ROOT / "hermes_cli" / "__init__.py").read_text(encoding="utf-8")
    desktop_package = json.loads(
        (REPO_ROOT / "apps" / "desktop" / "package.json").read_text(encoding="utf-8")
    )
    package_lock = json.loads((REPO_ROOT / "package-lock.json").read_text(encoding="utf-8"))

    runtime_match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', runtime_init, re.MULTILINE)
    assert runtime_match is not None, "hermes_cli/__init__.py must declare __version__"

    versions = {
        "pyproject.toml": pyproject["project"]["version"],
        "hermes_cli/__init__.py": runtime_match.group(1),
        "apps/desktop/package.json": desktop_package["version"],
        "package-lock.json apps/desktop": package_lock["packages"]["apps/desktop"]["version"],
    }
    assert len(set(versions.values())) == 1, f"Hermes release versions drifted: {versions}"