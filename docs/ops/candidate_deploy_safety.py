"""Focused deployment-safety checks for one exceptional-lane candidate SHA.

``ANDY_RUNTIME_PATCHES.md`` leaves broad GitHub CI optional on the routine
lane and refuses unattended deploys that touch dependency manifests. The
rpi5 workflow posts to the host on every push to ``main``, so this runner
stays local. It does not deploy, activate a profile, or change
``needs_manual``. Andy authorized these checks for the v0.23.0 candidate.

A ledger freeze that differs from the PATCH-SCORE target is reported as a
human flag. The check passes only when the score still cites the ledger
freeze verbatim. Advancing the freeze is an activation decision.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

_OPS = Path(__file__).resolve().parent
_REPO = _OPS.parents[1]
for _path in (_REPO, _OPS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import rpi5_deploy_engine as eng
from pm.environments import ACTIVATION_INPUTS

UPSTREAM = "https://github.com/NousResearch/hermes-agent.git"
MARKER = "python_version >= '3.14'"
LIVE_CONFIG_VERSION = 51
_FREEZE_RE = re.compile(
    r"Frozen upstream cutoff: `([0-9a-f]{40})`\s*\n\(`(v\d+\.\d+\.\d+)`\)")
_SCORE_HEAD_RE = re.compile(
    r"^# Andy runtime patch score — (v\d+\.\d+\.\d+) \(`([0-9a-f]{7,40})`\)",
    re.MULTILINE)
_SCORE_PEEL_RE = re.compile(r"Scored against peeled tag \*\*(v\d+\.\d+\.\d+)\*\*")
_SCORE_CITE_RE = re.compile(
    r"frozen cutoff `([0-9a-f]{7,40})` / (v\d+\.\d+\.\d+)")
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")


@dataclass(frozen=True)
class Freeze:
    sha: str
    version: str


@dataclass(frozen=True)
class Score:
    version: str
    sha_prefix: str
    peel_version: str
    cited_prefix: str
    cited_version: str


def _result(area: str, check: str, status: str, summary: str, **detail) -> dict:
    return {
        "area": area, "check": check, "status": status,
        "summary": summary, "detail": detail,
    }


def git(repo: Path, *args: str, check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        timeout=timeout, check=check)


def parse_ledger_freeze(text: str) -> Freeze:
    match = _FREEZE_RE.search(text)
    if match is None:
        raise ValueError("ledger has no frozen upstream cutoff")
    return Freeze(match.group(1), match.group(2))


def parse_score(text: str) -> Score:
    head = _SCORE_HEAD_RE.search(text)
    peel = _SCORE_PEEL_RE.search(text)
    cited = _SCORE_CITE_RE.search(text)
    if head is None or peel is None or cited is None:
        raise ValueError("PATCH-SCORE is missing its target or ledger citation")
    return Score(head.group(1), head.group(2), peel.group(1), cited.group(1), cited.group(2))


def citation_ok(freeze: Freeze, score: Score) -> bool:
    return (
        freeze.sha.startswith(score.cited_prefix)
        and len(score.cited_prefix) >= 7
        and freeze.version == score.cited_version
        and score.version == score.peel_version
    )


def marker_gaps(pyproject: str) -> list[str]:
    data = tomllib.loads(pyproject)
    gaps = []
    for dep in data["project"]["dependencies"]:
        if MARKER not in dep:
            gaps.append(dep.split(";", 1)[0].strip())
    return gaps


def interpreter_satisfies_core_markers(version: tuple[int, ...]) -> bool:
    """Core dependencies are marker-gated. Below 3.14 an install looks empty."""
    return version >= (3, 14)


def schema_version(source: str) -> int:
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        named = any(isinstance(target, ast.Name) and target.id == "DEFAULT_CONFIG"
                    for target in node.targets)
        if not named or not isinstance(node.value, ast.Dict):
            continue
        for key, item in zip(node.value.keys, node.value.values):
            if isinstance(key, ast.Constant) and key.value == "_config_version":
                if isinstance(item, ast.Constant) and isinstance(item.value, int):
                    return item.value
    raise ValueError("DEFAULT_CONFIG has no integer _config_version")


def duplicate_names(source: str) -> list[str]:
    tree = ast.parse(source)
    found = _duplicate_functions(tree.body, "")
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            found.extend(_duplicate_functions(node.body, f"{node.name}."))
    return found


def _duplicate_functions(nodes, prefix: str) -> list[str]:
    lines: dict[str, list[int]] = {}
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines.setdefault(node.name, []).append(node.lineno)
    return [f"{prefix}{name}@{rows}" for name, rows in lines.items() if len(rows) > 1]


def scored_paths(score_text: str, names: list[str]) -> list[str]:
    """Python and shell paths named in backticks, including ``*`` globs."""
    tokens = [item for item in _BACKTICK_RE.findall(score_text)
              if item.endswith((".py", ".sh"))]
    chosen = []
    for token in tokens:
        if "*" in token:
            chosen.extend(name for name in names if Path(name).match(token))
        elif token in names:
            chosen.append(token)
    return sorted(set(chosen))


def manual_gate(paths: list[str]) -> dict:
    plan = eng.classify(paths)
    hits = [path for path in paths if path in eng.DEPENDENCY_INPUTS]
    gate_holds = set(eng.DEPENDENCY_INPUTS) == set(ACTIVATION_INPUTS)
    if not gate_holds or (hits and plan.kind != "manual"):
        return _result(
            "rollback", "manual-gate", "fail",
            "a dependency manifest change is not held in the manual lane",
            kind=plan.kind, hits=hits, gate=list(eng.DEPENDENCY_INPUTS))
    return _result(
        "rollback", "manual-gate", "pass",
        "dependency inputs stay on the unattended-deploy manual gate",
        kind=plan.kind, hits=hits)


def _blob(repo: Path, sha: str, path: str) -> str:
    return git(repo, "show", f"{sha}:{path}").stdout


def _resolve(repo: Path, prefix: str) -> str:
    done = git(repo, "rev-parse", "--verify", f"{prefix}^{{commit}}")
    return done.stdout.strip()


def _ancestor(repo: Path, older: str, newer: str) -> bool:
    return git(repo, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def _peel_tag(repo: Path, version: str) -> str:
    local = git(repo, "rev-parse", "--verify", f"{version}^{{commit}}", check=False)
    if local.returncode == 0:
        return local.stdout.strip()
    ref = f"refs/deploy-safety/{version}"
    fetched = git(
        repo, "fetch", "--no-tags", UPSTREAM, f"refs/tags/{version}:{ref}",
        check=False, timeout=90)
    if fetched.returncode != 0:
        raise RuntimeError(fetched.stderr.strip() or f"could not fetch {version}")
    try:
        return git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()
    finally:
        git(repo, "update-ref", "-d", ref, check=False)


def _names_at(repo: Path, sha: str) -> list[str]:
    return [line for line in git(repo, "ls-tree", "-r", "--name-only", sha).stdout.splitlines() if line]


def phase_ledger(repo: Path, sha: str) -> list[dict]:
    ledger_text = _blob(repo, sha, "ANDY_RUNTIME_PATCHES.md")
    score_text = _blob(repo, sha, "PATCH-SCORE.md")
    freeze = parse_ledger_freeze(ledger_text)
    score = parse_score(score_text)
    findings = []
    if citation_ok(freeze, score):
        findings.append(_result(
            "ledger", "citation", "pass",
            "PATCH-SCORE cites the ledger freeze and its own peel tag",
            freeze=freeze.sha, freeze_version=freeze.version,
            score_prefix=score.sha_prefix, score_version=score.version))
    else:
        findings.append(_result(
            "ledger", "citation", "fail",
            "PATCH-SCORE does not cite the ledger freeze verbatim",
            freeze=freeze.sha, freeze_version=freeze.version,
            cited_prefix=score.cited_prefix, cited_version=score.cited_version,
            score_version=score.version, peel_version=score.peel_version))
    score_sha = _resolve(repo, score.sha_prefix)
    findings.extend(_tag_findings(repo, freeze, score, score_sha))
    findings.append(_divergence(freeze, score_sha))
    findings.append(_marker_finding(repo, ledger_text, freeze.sha))
    findings.append(_ancestry_finding(repo, sha, freeze.sha, score_sha))
    return findings


def _tag_findings(repo: Path, freeze: Freeze, score: Score, score_sha: str) -> list[dict]:
    try:
        freeze_tag = _peel_tag(repo, freeze.version)
        score_tag = _peel_tag(repo, score.version)
    except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
        return [_result(
            "ledger", "release-tags", "fail",
            "release tags could not be peeled", error=str(exc))]
    ok = freeze_tag == freeze.sha and score_tag == score_sha
    status = "pass" if ok else "fail"
    summary = "tags peel to the ledger freeze and the score target" if ok else (
        "a documented SHA is not the tag it names")
    return [_result(
        "ledger", "release-tags", status, summary,
        freeze_tag=freeze_tag, score_tag=score_tag, score_sha=score_sha)]


def _divergence(freeze: Freeze, score_sha: str) -> dict:
    if freeze.sha == score_sha:
        return _result(
            "ledger", "freeze-versus-score", "pass",
            "ledger freeze and PATCH-SCORE target are the same commit",
            sha=freeze.sha)
    return _result(
        "ledger", "freeze-versus-score", "flag",
        "ledger freeze and PATCH-SCORE target differ; left for a human",
        freeze=freeze.sha, freeze_version=freeze.version, score=score_sha,
        action="do not advance the frozen cutoff until activation")


def _marker_finding(repo: Path, ledger_text: str, freeze_sha: str) -> dict:
    gaps = marker_gaps(_blob(repo, freeze_sha, "pyproject.toml"))
    claimed = MARKER in ledger_text
    if claimed and not gaps:
        return _result(
            "ledger", "python-markers", "pass",
            "frozen tag core dependencies carry the ledger's 3.14 marker")
    return _result(
        "ledger", "python-markers", "fail",
        "ledger marker claim does not match the frozen tag",
        claimed=claimed, gaps=gaps)


def _ancestry_finding(repo: Path, sha: str, freeze_sha: str, score_sha: str) -> dict:
    freeze_in = _ancestor(repo, freeze_sha, sha)
    score_in = _ancestor(repo, score_sha, sha)
    if not freeze_in:
        return _result(
            "ledger", "ancestry", "fail",
            "candidate does not contain the ledger freeze", freeze=freeze_sha)
    if score_in:
        return _result(
            "ledger", "ancestry", "pass",
            "candidate contains the ledger freeze and the score target")
    return _result(
        "ledger", "ancestry", "flag",
        "score target is not an ancestor because the squash has one parent",
        score=score_sha,
        action="use the tree, not merge-base, to prove the peeled tag")


def _missing_scored_py(score_text: str, names: list[str]) -> list[str]:
    missing = []
    known = set(names)
    for token in _BACKTICK_RE.findall(score_text):
        if not token.endswith(".py"):
            continue
        if "*" in token:
            if not any(Path(name).match(token) for name in names):
                missing.append(token)
        elif token not in known:
            missing.append(token)
    return missing


def phase_duplicates(repo: Path, sha: str) -> list[dict]:
    score_text = _blob(repo, sha, "PATCH-SCORE.md")
    names = _names_at(repo, sha)
    duplicates = []
    missing = _missing_scored_py(score_text, names)
    for path in scored_paths(score_text, names):
        if not path.endswith(".py"):
            continue
        duplicates.extend(f"{path}:{item}" for item in duplicate_names(_blob(repo, sha, path)))
    if missing or duplicates:
        return [_result(
            "keep", "duplicate-defs", "fail",
            "a scored Python file defines the same function twice or is missing",
            duplicates=duplicates, missing=missing)]
    return [_result(
        "keep", "duplicate-defs", "pass",
        "scored Python files define each function once")]


def phase_scripts(repo: Path, root: Path) -> list[dict]:
    failures = []
    for path in sorted(root.glob("scripts/maintenance/*.sh")):
        syntax = subprocess.run(
            ["bash", "-n", str(path)], capture_output=True, text=True, check=False)
        if syntax.returncode != 0:
            failures.append(f"{path.name}:bash -n:{syntax.stderr.strip()}")
            continue
        failures.extend(_script_behavior(repo, path))
    status = "fail" if failures else "pass"
    summary = "maintenance scripts stay fail-closed without --write or --push" if not failures else (
        "a maintenance script did not fail closed")
    return [_result("keep", "maintenance-scripts", status, summary, failures=failures)]


def _script_behavior(repo: Path, path: Path) -> list[str]:
    if path.name == "refresh_andy_runtime.sh":
        done = subprocess.run(
            ["bash", str(path)], cwd=repo, capture_output=True, text=True, check=False)
        if done.returncode != 2 or "expected branch andy-runtime" not in done.stderr:
            return [f"refresh exit {done.returncode}: {done.stderr.strip()[:300]}"]
        return []
    if path.name != "pin-shared-hermes-runtime.sh":
        return []
    done = subprocess.run(
        ["bash", str(path), "--source", str(repo)], cwd=repo,
        capture_output=True, text=True, check=False)
    if "installed normalized definitions" in done.stdout:
        return ["pin installed unit definitions without --write"]
    return []


def phase_rollback(repo: Path, sha: str, freeze_sha: str) -> list[dict]:
    paths = [line for line in git(
        repo, "diff", "--name-only", "--no-renames", freeze_sha, sha).stdout.splitlines() if line]
    findings = [manual_gate(paths)]
    parents = [line for line in git(repo, "rev-parse", f"{sha}^@").stdout.splitlines() if line]
    if parents and _ancestor(repo, parents[0], sha):
        findings.append(_result(
            "rollback", "fast-forward-parent", "pass",
            "candidate fast-forwards from its first parent, the rollback SHA",
            parent=parents[0]))
    else:
        findings.append(_result(
            "rollback", "fast-forward-parent", "fail",
            "candidate has no first parent to roll back to", parents=parents))
    if not _ancestor(repo, freeze_sha, sha):
        findings.append(_result(
            "rollback", "freeze-ancestor", "fail",
            "frozen tag is not an ancestor, so a later manual fast-forward cannot start there",
            freeze=freeze_sha))
    else:
        findings.append(_result(
            "rollback", "freeze-ancestor", "pass",
            "frozen tag is an ancestor of the candidate"))
    return findings


def _python_version(python: Path) -> tuple[int, int, int]:
    done = subprocess.run(
        [str(python), "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
        capture_output=True, text=True, check=False)
    raw = done.stdout.strip().split(".")
    if done.returncode != 0 or len(raw) != 3:
        raise RuntimeError(done.stderr.strip() or "could not read the interpreter version")
    major, minor, micro = (int(item) for item in raw)
    return (major, minor, micro)


def _lock_id(root: Path) -> str:
    digest = hashlib.sha256()
    for name in ("pyproject.toml", "uv.lock", "pm/lock.json"):
        digest.update((root / name).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _isolated_env(home: Path, runtime: Path, root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(home.parent)
    env["HERMES_HOME"] = str(home)
    env["HERMES_RUNTIME_DIR"] = str(runtime)
    env["PYTHONPATH"] = str(root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    return env


def _venv_python(venv: Path) -> Path:
    return venv / "bin" / "python"


def phase_install(root: Path, scratch: Path, python: Path) -> tuple[list[dict], Path | None]:
    version = _python_version(python)
    if not interpreter_satisfies_core_markers(version):
        return [_result(
            "preflight", "scratch-install", "fail",
            "interpreter is below 3.14, so core markers would install nothing",
            python=str(python), version=".".join(str(part) for part in version))], None
    (scratch / "install-home").mkdir(parents=True, exist_ok=True)
    venv = scratch / "venv"
    stamp = scratch / "lock-id"
    lock_id = _lock_id(root)
    runtime = scratch / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    if stamp.is_file() and stamp.read_text(encoding="utf-8") == lock_id and _venv_python(venv).is_file():
        return [_result(
            "preflight", "scratch-install", "pass",
            "reused an isolated environment built from this lock",
            python=str(_venv_python(venv)), lock=lock_id)], _venv_python(venv)
    env = _isolated_env(scratch / "install-home", runtime, root)
    env["UV_CACHE_DIR"] = str(scratch / "uv-cache")
    done = subprocess.run(
        [sys.executable, "-m", "pm.build_env", "--source", str(root), "--out", str(venv),
         "--python", str(python), "--cache", str(scratch / "uv-cache"),
         "--extra", "messaging", "--extra", "mcp", "--group", "dev",
         "--no-install-project"],
        cwd=root, env=env, capture_output=True, text=True, check=False, timeout=1800)
    if done.returncode != 0 or not _venv_python(venv).is_file():
        tail = (done.stderr or done.stdout)[-2000:]
        return [_result(
            "preflight", "scratch-install", "fail",
            "isolated pm.build_env failed", exit=done.returncode, tail=tail)], None
    stamp.write_text(lock_id, encoding="utf-8")
    return [_result(
        "preflight", "scratch-install", "pass",
        "isolated environment installed from the candidate lock",
        python=str(_venv_python(venv)), lock=lock_id,
        version=".".join(str(part) for part in version))], _venv_python(venv)


def _run_json(python: Path, argv: list[str], env: dict[str, str]) -> tuple[int, dict, str]:
    done = subprocess.run(
        [str(python), *argv], capture_output=True, text=True, env=env, check=False, timeout=600)
    line = next((item for item in reversed(done.stdout.splitlines()) if item.startswith("{")), "")
    try:
        payload = json.loads(line) if line else {}
    except json.JSONDecodeError:
        payload = {}
    return done.returncode, payload, (done.stdout + done.stderr)[-2000:]


def phase_runtime(root: Path, venv_python: Path | None, scratch: Path, live_version: int,
                  freeze_schema: int) -> list[dict]:
    if venv_python is None:
        reason = "scratch install failed"
        return [
            _result("preflight", "import-probe", "fail", "probe did not run", reason=reason),
            _result("keep", "smoke", "fail", "KEEP probes did not run", reason=reason),
            _result("config", "live-schema", "fail", "config migration did not run", reason=reason),
        ]
    runtime = scratch / "runtime"
    probe_home = _fresh_home(scratch / "probe-root")
    smoke_home = _fresh_home(scratch / "smoke-root")
    config_home = _fresh_home(scratch / "config-root")
    findings = [_probe(root, venv_python, probe_home, _isolated_env(probe_home, runtime, root))]
    findings.append(_smoke(root, venv_python, smoke_home, _isolated_env(smoke_home, runtime, root)))
    findings.append(_config(
        root, venv_python, config_home, _isolated_env(config_home, runtime, root),
        live_version, freeze_schema))
    return findings


def _fresh_home(root: Path) -> Path:
    home = root / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    return home


def _probe(root: Path, python: Path, home: Path, env: dict[str, str]) -> dict:
    code, payload, tail = _run_json(
        python, [str(root / "docs" / "ops" / "rpi5_deploy_probe.py"),
                 "--checkout", str(root), "preflight", "--home", str(home)], env)
    ok = code == 0 and payload.get("ok") is True
    return _result(
        "preflight", "import-probe", "pass" if ok else "fail",
        "probe preflight imported aiohttp, httpx2, and mcp" if ok else "probe preflight failed",
        payload=payload, tail=tail)


def _smoke(root: Path, python: Path, home: Path, env: dict[str, str]) -> dict:
    code, payload, tail = _run_json(
        python, [str(_OPS / "candidate_keep_smoke.py"), "--home", str(home)], env)
    ok = code == 0 and payload.get("ok") is True
    return _result(
        "keep", "smoke", "pass" if ok else "fail",
        "KEEP import and behavior probes passed" if ok else "KEEP probes failed",
        failed=payload.get("failed"), tail=tail)


def _config(root: Path, python: Path, home: Path, env: dict[str, str],
            live_version: int, freeze_schema: int) -> dict:
    code, payload, tail = _run_json(
        python, [str(_OPS / "candidate_config_compat.py"), "--home", str(home),
                 "--live-version", str(live_version)], env)
    ok = code == 0 and payload.get("ok") is True
    findings_status = "pass" if ok else "fail"
    summary = "live config migrates forward without dropping user keys" if ok else (
        "live config is not compatible with this candidate")
    result = _result(
        "config", "live-schema", findings_status, summary,
        live_version=live_version, payload=payload, tail=tail)
    return result if not ok else _with_schema_flag(result, payload, freeze_schema, live_version)


def _with_schema_flag(result: dict, payload: dict, freeze_schema: int, live_version: int) -> dict:
    latest = payload.get("latest")
    if isinstance(latest, int) and latest > freeze_schema:
        result["schema_rollback"] = _result(
            "config", "schema-rollback", "flag",
            "candidate stamps a newer schema than the frozen tag; rollback does not rewrite it down",
            live_version=live_version, freeze_schema=freeze_schema, candidate_schema=latest,
            backend_removed=payload.get("backend_present") is False)
    return result


def _materialize(repo: Path, sha: str, scratch: Path) -> tuple[Path, bool]:
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    dirty = bool(git(repo, "status", "--porcelain").stdout.strip())
    if sha == head and not dirty:
        return repo, False
    dest = scratch / "worktree"
    if dest.exists():
        git(repo, "worktree", "remove", "--force", str(dest), check=False)
    git(repo, "worktree", "add", "--detach", str(dest), sha)
    return dest, True


def run(repo: Path, sha: str, scratch: Path, python: Path, live_version: int) -> dict:
    sha = _resolve(repo, sha)
    scratch.mkdir(parents=True, exist_ok=True)
    root, temporary = _materialize(repo, sha, scratch)
    try:
        findings = phase_ledger(repo, sha)
        findings.extend(phase_duplicates(repo, sha))
        findings.extend(phase_scripts(repo, root))
        freeze_sha = parse_ledger_freeze(_blob(repo, sha, "ANDY_RUNTIME_PATCHES.md")).sha
        findings.extend(phase_rollback(repo, sha, freeze_sha))
        freeze_schema = schema_version(_blob(repo, freeze_sha, "hermes_cli/config_defaults.py"))
        installed, venv_python = phase_install(root, scratch, python)
        findings.extend(installed)
        runtime = phase_runtime(root, venv_python, scratch, live_version, freeze_schema)
        findings.extend(_flatten_runtime(runtime))
    finally:
        if temporary:
            git(repo, "worktree", "remove", "--force", str(scratch / "worktree"), check=False)
    return {"sha": sha, "findings": findings}


def _flatten_runtime(items: list[dict]) -> list[dict]:
    flat = []
    for item in items:
        extra = item.pop("schema_rollback", None)
        flat.append(item)
        if extra:
            flat.append(extra)
    return flat


def _print_summary(report: dict) -> None:
    for item in report["findings"]:
        print(f"{item['status'].upper():4} {item['area']}:{item['check']} {item['summary']}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--sha", default="HEAD")
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--live-config-version", type=int, default=LIVE_CONFIG_VERSION)
    args = parser.parse_args(argv)
    report = run(args.repo.resolve(), args.sha, args.scratch.resolve(),
                 args.python, args.live_config_version)
    print(json.dumps(report, indent=2, sort_keys=True), flush=True)
    _print_summary(report)
    return 1 if any(item["status"] == "fail" for item in report["findings"]) else 0


if __name__ == "__main__":
    sys.exit(main())
