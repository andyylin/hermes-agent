"""Keep plugin sync from replacing an externally managed interpreter.

``hermes plugins install``, ``enable``, and ``disable`` publish through
``sync_venv``. A plugin with no Python declaration must not build an
environment. A process that is not already on a PM-built interpreter, and
has no committed ``facts.json``, must not create one unless the operator
opts in. A generation PM does commit keeps every extra for a platform that
is enabled in config, and is discarded when that generation would drop one.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

PLATFORM_EXTRAS = {
    "telegram": "telegram",
    "discord": "discord",
    "matrix": "matrix",
    "slack": "slack",
    "feishu": "feishu",
    "dingtalk": "dingtalk",
    "wecom": "wecom",
    "wecom_callback": "wecom",
    "teams": "teams",
    "google_chat": "google-chat",
    "google-chat": "google-chat",
}

EXTERNAL_COMMIT_CAUSE = (
    "this process is running an externally managed interpreter and no dependency "
    "environment is committed"
)
EXTERNAL_COMMIT_REMEDY = (
    "leave the current interpreter in place, or opt in with "
    "plugins.commit_dependency_environment: true or --commit-environment"
)


def _extra_key(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _pm_built_tree(prefix: Path) -> bool:
    """True for an interpreter PM staged: a generation, the test env, or the PM runtime."""
    parts = set(prefix.parts)
    return "installs" in parts and bool(parts & {"environments", "test-environment", "pm-runtime"})


def externally_managed_runtime(project_root: Path) -> bool:
    """True when this process is not on a PM-built interpreter and nothing is committed.

    A recorded ``facts.json`` means PM already owns the selection. A prefix inside
    a dependency generation, the PM test environment, the PM runtime, or the PM
    store is an interpreter PM built. A user venv such as ``~/.hermes/venvs/...``
    and a system interpreter are not.
    """
    from pm.environments import install_state_dir, runtime_facts_path, store_root

    root = Path(project_root)
    if runtime_facts_path(root).is_file():
        return False
    managed = [install_state_dir(root) / "environments", store_root(root)]
    prefixes = [Path(value) for value in (sys.prefix, sys.base_prefix) if value]
    if any(_pm_built_tree(prefix) for prefix in prefixes):
        return False
    return not any(_under(prefix, place) for prefix in prefixes for place in managed)


def commit_environment_opted_in() -> bool:
    """``plugins.commit_dependency_environment: true`` in the active home. Anything else is off."""
    from hermes_constants import get_hermes_home
    from pm.plugins_state import read_home_selection

    try:
        config = read_home_selection(get_hermes_home()) or {}
    except (OSError, ValueError):
        return False
    plugins = config.get("plugins")
    if not isinstance(plugins, dict):
        return False
    return plugins.get("commit_dependency_environment") is True


def deployment_platform_extras(project_root: Path) -> list[str]:
    """Extras for platforms enabled in any home that shares this dependency environment.

    Only extras the project declares, and only extras this platform can install.
    An anchor that happens to be importable in this process does not count: the
    gate is the platform marker, not the interpreter plugin sync is about to leave.
    """
    from pm.extras import extra_supported
    from pm.features import declared_extras
    from pm.plugins_state import dependency_homes, read_home_selection

    root = Path(project_root)
    if not (root / "pyproject.toml").is_file():
        return []
    declared = {_extra_key(name): name for name in declared_extras(root)}
    wanted: list[str] = []
    seen: set[str] = set()
    homes = dependency_homes()
    for index, home in enumerate(homes):
        try:
            config = read_home_selection(home) or {}
        except ValueError:
            # Member discovery logs and skips a broken secondary profile. The
            # active home still fails closed so its platforms cannot be dropped.
            if index == 0:
                raise
            continue
        platforms = config.get("platforms")
        if not isinstance(platforms, dict):
            continue
        for name, section in platforms.items():
            if not isinstance(section, dict) or section.get("enabled") is not True:
                continue
            extra = PLATFORM_EXTRAS.get(str(name))
            if extra is None:
                continue
            key = _extra_key(extra)
            canonical = declared.get(key)
            if canonical is None or key in seen:
                continue
            if not extra_supported(canonical, importable=lambda _anchor: False):
                continue
            seen.add(key)
            wanted.append(canonical)
    return sorted(wanted)


def union_platform_extras(enabled: list[str], *, project: Path, frozen, recorded: list[str]) -> list[str]:
    """Add enabled-platform extras, or refuse when a frozen bundle cannot carry them."""
    from pm.package import InstallError

    required = deployment_platform_extras(project)
    if not required:
        return list(enabled)
    if frozen is not None:
        allowed = {_extra_key(name) for name in (*frozen, *recorded)}
        dropped = [name for name in required if _extra_key(name) not in allowed]
        if dropped:
            raise InstallError(
                "venv",
                "refusing to commit a dependency environment that drops "
                + ", ".join(dropped)
                + " (enabled platforms are outside the frozen feature set)",
                "set security.allow_lazy_installs true, or record those extras before freezing the bundle",
            )
    return sorted(set(enabled) | set(required))


def _member_paths(paths) -> set[Path]:
    if isinstance(paths, dict):
        paths = paths.values()
    return {Path(path).resolve() for path in paths}


def _staged_kind(change) -> str:
    from pm.plugin_declarations import read_python_declaration

    staged = read_python_declaration(change.staged).is_member
    previous = change.target.exists() and read_python_declaration(change.target).is_member
    if staged and previous:
        return "replace"
    if staged:
        return "add"
    if previous:
        return "remove"
    return "none"


def _selection_kind(change) -> str:
    from pm.publication import candidate_members

    current = _member_paths(candidate_members())
    proposed = _member_paths(change.members)
    added, removed = proposed - current, current - proposed
    if added and removed:
        return "replace"
    if added:
        return "add"
    if removed:
        return "remove"
    return "none"


def _config_names(document: dict, key: str) -> set[str]:
    plugins = document.get("plugins") if isinstance(document, dict) else None
    names = plugins.get(key) if isinstance(plugins, dict) else None
    if not isinstance(names, list):
        return set()
    return {name for name in names if isinstance(name, str)}


def _touched_names(change) -> set[str] | None:
    """Names whose enabled/disabled membership changed, or None when the lists cannot be read."""
    from hermes_yaml import YAMLError, safe_load

    try:
        before = safe_load(change.previous.decode("utf-8-sig")) if change.previous else {}
        after = safe_load(change.proposed.decode("utf-8-sig"))
    except (OSError, UnicodeError, YAMLError, ValueError):
        return None
    if not isinstance(before, dict):
        before = {}
    if not isinstance(after, dict):
        return None
    return (
        (_config_names(before, "enabled") ^ _config_names(after, "enabled"))
        | (_config_names(before, "disabled") ^ _config_names(after, "disabled"))
    )


def _plugin_dirs(home: Path, name: str) -> list[Path]:
    from pm.paths import repo_root

    leaf = name.split("/")[-1]
    dirs = [home / "plugins" / name, home / "plugins" / leaf, repo_root() / "plugins" / name,
            repo_root() / "plugins" / leaf]
    return dirs


def _is_nonmember_plugin(home: Path, name: str) -> bool:
    """True only when a directory for *name* exists and declares no Python dependencies.

    A name that does not resolve stays on the sync path: there is no declaration
    to trust, and a dependency build is where publication still detects a
    concurrent config edit.
    """
    from pm.plugin_declarations import read_python_declaration

    found = False
    for path in _plugin_dirs(home, name):
        if not path.is_dir():
            continue
        found = True
        try:
            if read_python_declaration(path).is_member:
                return False
        except (OSError, ValueError):
            return False
    return found


def _nonmember_toggle(change) -> bool:
    """True when a selection only toggles plugins that declare no Python dependencies."""
    from pm.publication import PluginSelection

    if not isinstance(change, PluginSelection):
        return False
    touched = _touched_names(change)
    if not touched:
        return False
    return all(_is_nonmember_plugin(change.home, name) for name in touched)


def plugin_sync_action(change, *, project_root: Path, commit_environment: bool,
                       externally_managed: bool | None = None) -> str:
    """``publish`` (no environment), ``refuse`` (change nothing), or ``sync``.

    ``externally_managed`` is the calling process. Plugin commands run the
    sync in the PM worker, whose own prefix is a PM runtime, so the worker
    must use the classification the client recorded rather than its own.
    """
    from pm.publication import PluginSelection, StagedPlugin

    if isinstance(change, StagedPlugin):
        kind = _staged_kind(change)
    elif isinstance(change, PluginSelection):
        kind = _selection_kind(change)
    else:
        return "sync"
    if kind == "none" and (isinstance(change, StagedPlugin) or _nonmember_toggle(change)):
        return "publish"
    if externally_managed is None:
        externally_managed = externally_managed_runtime(project_root)
    external = externally_managed
    opted = commit_environment or commit_environment_opted_in()
    if external and not opted:
        if kind in ("add", "replace"):
            return "refuse"
        return "publish"
    return "sync"


def _discard_uncommitted_generation(environment: Path, project: Path) -> None:
    from pm.environments import install_state_dir

    env = Path(environment).resolve()
    generations = (install_state_dir(project) / "environments").resolve()
    generation = env.parent
    if generation.parent.resolve() != generations or generation == generations or not generation.is_dir():
        return
    shutil.rmtree(generation, ignore_errors=True)


def _module_present(python_exe: Path, venv: Path, module: str) -> bool:
    import subprocess

    from pm.features import FeatureProbeError

    probe = (
        "import contextlib, importlib.util, os, site, sys, sysconfig\n"
        "base, module = sys.argv[1], sys.argv[2]\n"
        "paths = sysconfig.get_paths(vars={'base': base, 'platbase': base})\n"
        "sites = dict.fromkeys(paths[key] for key in ('purelib', 'platlib'))\n"
        "with contextlib.redirect_stdout(sys.stderr):\n"
        "    for path in sites:\n"
        "        if os.path.isdir(path):\n"
        "            site.addsitedir(path)\n"
        "try:\n"
        "    print('1' if importlib.util.find_spec(module) is not None else '0')\n"
        "except (ImportError, ValueError):\n"
        "    print('0')\n"
    )
    try:
        child = subprocess.run(
            [str(python_exe), "-B", "-I", "-S", "-c", probe, str(Path(venv).resolve()), module],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = (getattr(exc, "stderr", None) or str(exc)).strip()
        raise FeatureProbeError(f"feature inventory failed on {python_exe}: {detail}") from exc
    return child.stdout.strip() == "1"


def _missing_platform_dependencies(environment: Path, project: Path, enabled: list[str]) -> list[str]:
    from pm.environments import venv_python
    from pm.features import installed_extras

    enabled_keys = {_extra_key(name) for name in enabled}
    wanted = [extra for extra in deployment_platform_extras(project) if _extra_key(extra) in enabled_keys]
    if not wanted:
        return []
    present = {_extra_key(name) for name in installed_extras(project, environment, python_exe=venv_python(environment))}
    missing = [
        "matrix (mautrix[encryption])" if _extra_key(extra) == "matrix" else extra
        for extra in wanted if _extra_key(extra) not in present
    ]
    if any(_extra_key(extra) == "matrix" for extra in wanted) and "matrix" not in {item.split()[0] for item in missing}:
        if not _module_present(venv_python(environment), environment, "olm"):
            missing.append("matrix (mautrix[encryption] / olm)")
    return missing


def assert_committed_platform_dependencies(environment: Path, project: Path, enabled: list[str]) -> None:
    """Refuse to record *environment* when an enabled platform's dependencies are absent."""
    from pm.features import FeatureProbeError
    from pm.package import InstallError

    try:
        missing = _missing_platform_dependencies(environment, project, enabled)
    except FeatureProbeError as exc:
        _discard_uncommitted_generation(environment, project)
        raise InstallError(
            "venv",
            "refusing to commit a dependency environment that drops platform dependencies "
            f"({exc})",
            "install the enabled platform extras before committing a dependency environment",
        ) from exc
    if not missing:
        return
    _discard_uncommitted_generation(environment, project)
    raise InstallError(
        "venv",
        "refusing to commit a dependency environment that drops " + ", ".join(missing),
        "install the enabled platform extras before committing a dependency environment",
    )
