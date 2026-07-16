"""Tests for the projects.* JSON-RPC methods on the tui_gateway server."""

from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

import tui_gateway.server as server


@pytest.fixture(autouse=True)
def _isolate_server_runtime_state():
    """Keep the gateway's module caches bound to each test's HERMES_HOME."""
    if server._db is not None:
        server._db.close()
    server._db = None
    server._db_error = None
    server._sessions.clear()
    yield
    if server._db is not None:
        server._db.close()
    server._db = None
    server._db_error = None
    server._sessions.clear()


def _call(method, params=None):
    handler = server._methods[method]
    resp = handler(1, params or {})
    assert "error" not in resp, resp.get("error")
    return resp["result"]


def test_methods_registered():
    for m in (
        "projects.list",
        "projects.create",
        "projects.create_managed",
        "projects.get",
        "projects.update",
        "projects.add_folder",
        "projects.remove_folder",
        "projects.set_primary",
        "projects.archive",
        "projects.set_active",
        "projects.assign_session",
        "projects.unassign_session",
        "projects.for_cwd",
    ):
        assert m in server._methods


def test_create_managed_project_owns_a_profile_folder(tmp_path, monkeypatch):
    managed_root = tmp_path / "profile" / "projects"
    monkeypatch.setattr(server, "_managed_projects_root", lambda: managed_root)

    result = _call("projects.create_managed", {"name": "My First Project", "use": True})
    project = result["project"]
    expected = managed_root / "my-first-project"

    assert expected.is_dir()
    assert project["slug"] == "my-first-project"
    assert project["primary_path"] == str(expected)
    assert project["folders"][0]["path"] == str(expected)


def test_create_managed_project_uses_a_collision_safe_slug(tmp_path, monkeypatch):
    managed_root = tmp_path / "profile" / "projects"
    monkeypatch.setattr(server, "_managed_projects_root", lambda: managed_root)
    _call("projects.create", {"name": "Existing", "slug": "demo", "folders": [str(tmp_path / "elsewhere")]})

    project = _call("projects.create_managed", {"name": "Demo"})["project"]

    assert project["slug"] == "demo-2"
    assert project["primary_path"] == str(managed_root / "demo-2")
    assert (managed_root / "demo-2").is_dir()


def test_create_managed_project_removes_empty_folder_after_db_failure(tmp_path, monkeypatch):
    from hermes_cli import projects_db as pdb

    managed_root = tmp_path / "profile" / "projects"
    monkeypatch.setattr(server, "_managed_projects_root", lambda: managed_root)

    def fail_create(*_args, **_kwargs):
        raise OSError("database boom")

    monkeypatch.setattr(pdb, "_create_project_locked", fail_create)
    response = server._methods["projects.create_managed"](1, {"name": "Rollback Me"})

    assert response["error"]["message"] == "database boom"
    assert not (managed_root / "rollback-me").exists()


def test_create_managed_project_cleans_up_if_reserved_slug_races(tmp_path, monkeypatch):
    managed_root = tmp_path / "profile" / "projects"
    monkeypatch.setattr(server, "_managed_projects_root", lambda: managed_root)
    real_create = server._create_managed_project_folder

    def collide_once(root, slug):
        if slug == "race":
            (root / slug).mkdir(parents=True)
            raise FileExistsError(slug)
        return real_create(root, slug)

    monkeypatch.setattr(server, "_create_managed_project_folder", collide_once)

    project = _call("projects.create_managed", {"name": "Racing Project", "slug": "race"})["project"]

    assert project["slug"] == "race-2"
    assert (managed_root / "race").is_dir()
    assert (managed_root / "race-2").is_dir()


def test_managed_projects_root_uses_the_active_profile_home(tmp_path, monkeypatch):
    import hermes_constants

    profile_home = tmp_path / "named-profile"
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: profile_home)

    root = server._managed_projects_root()

    assert root == (profile_home / "projects").resolve()
    assert root.is_dir()


def test_create_managed_project_rejects_traversal_slug(tmp_path, monkeypatch):
    managed_root = tmp_path / "profile" / "projects"
    monkeypatch.setattr(server, "_managed_projects_root", lambda: managed_root)

    response = server._methods["projects.create_managed"](1, {"name": "Escape", "slug": "../outside"})

    assert response["error"]["code"] == 5063
    assert not (tmp_path / "profile" / "outside").exists()


def test_create_managed_project_skips_an_existing_directory_without_adopting_it(tmp_path, monkeypatch):
    managed_root = tmp_path / "profile" / "projects"
    existing = managed_root / "demo"
    existing.mkdir(parents=True)
    marker = existing / "keep.txt"
    marker.write_text("do not touch")
    monkeypatch.setattr(server, "_managed_projects_root", lambda: managed_root)

    project = _call("projects.create_managed", {"name": "Demo"})["project"]

    assert project["slug"] == "demo-2"
    assert project["primary_path"] == str(managed_root / "demo-2")
    assert (managed_root / "demo-2").is_dir()
    assert marker.read_text() == "do not touch"


def test_create_managed_project_after_delete_preserves_old_folder_and_uses_next_slug(tmp_path, monkeypatch):
    managed_root = tmp_path / "profile" / "projects"
    monkeypatch.setattr(server, "_managed_projects_root", lambda: managed_root)

    first = _call("projects.create_managed", {"name": "Demo"})["project"]
    marker = managed_root / "demo" / "keep.txt"
    marker.write_text("retained files")
    _call("projects.delete", {"id": first["id"]})

    second = _call("projects.create_managed", {"name": "Demo"})["project"]

    assert second["slug"] == "demo-2"
    assert second["primary_path"] == str(managed_root / "demo-2")
    assert marker.read_text() == "retained files"


def test_create_managed_project_rejects_root_symlink_swap(tmp_path, monkeypatch):
    managed_root = tmp_path / "profile" / "projects"
    original_root = tmp_path / "profile" / "projects-original"
    external = tmp_path / "external"
    managed_root.mkdir(parents=True)
    external.mkdir()

    def swapped_root():
        managed_root.rename(original_root)
        managed_root.symlink_to(external, target_is_directory=True)
        return managed_root

    monkeypatch.setattr(server, "_managed_projects_root", swapped_root)

    response = server._methods["projects.create_managed"](1, {"name": "Escape"})

    assert response["error"]["code"] == 5061
    assert "managed projects root" in response["error"]["message"]
    assert list(external.iterdir()) == []


def test_create_managed_project_rolls_back_db_and_folder_if_set_active_fails(tmp_path, monkeypatch):
    from hermes_cli import projects_db as pdb

    managed_root = tmp_path / "profile" / "projects"
    monkeypatch.setattr(server, "_managed_projects_root", lambda: managed_root)

    def fail_set_active(*_args, **_kwargs):
        raise OSError("active pointer boom")

    monkeypatch.setattr(pdb, "_set_active_locked", fail_set_active)
    response = server._methods["projects.create_managed"](1, {"name": "Rollback Me", "use": True})

    assert response["error"]["message"] == "active pointer boom"
    assert _call("projects.list")["projects"] == []
    assert not (managed_root / "rollback-me").exists()


def test_concurrent_managed_creates_serialize_slug_and_folder_reservation(tmp_path, monkeypatch):
    managed_root = tmp_path / "profile" / "projects"
    monkeypatch.setattr(server, "_managed_projects_root", lambda: managed_root)
    _call("projects.list")  # initialize this test's projects DB before the race

    handler = server._methods["projects.create_managed"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: handler(1, {"name": "Demo"}), range(2)))

    assert all("error" not in response for response in responses), responses
    slugs = sorted(response["result"]["project"]["slug"] for response in responses)
    assert slugs == ["demo", "demo-2"]
    assert (managed_root / "demo").is_dir()
    assert (managed_root / "demo-2").is_dir()


def test_managed_projects_root_rejects_a_symlink_escape(tmp_path, monkeypatch):
    import hermes_constants

    profile_home = tmp_path / "profile"
    external = tmp_path / "external"
    profile_home.mkdir()
    external.mkdir()
    (profile_home / "projects").symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: profile_home)

    response = server._methods["projects.create_managed"](1, {"name": "Escape"})

    assert response["error"]["code"] == 5061
    assert "symlink" in response["error"]["message"]
    assert list(external.iterdir()) == []


def test_for_cwd_is_a_long_handler():
    # git-probe handler must run off the dispatch thread.
    assert "projects.for_cwd" in server._LONG_HANDLERS


def test_repo_root_cache_does_not_freeze_a_not_yet_repo(monkeypatch):
    # We `git init` a new project's folder on first worktree; the cache must not
    # have frozen the pre-init "" result, or the main lane mislabels by basename.
    # Negative results are TTL-cached; TTL=0 here makes them expire immediately so
    # this verifies the "never permanently frozen" contract directly.
    from tui_gateway import git_probe

    monkeypatch.setattr(git_probe, "_NEG_TTL", 0)
    cwd = "/tmp/baby pics"
    git_probe.invalidate()
    state = {"root": ""}  # flips once the folder becomes a repo
    monkeypatch.setattr(git_probe, "run_git", lambda c, *a: state["root"] if c == cwd else "")

    assert git_probe.repo_root(cwd) == ""  # pre-init: not a repo (expires at once)

    state["root"] = cwd  # `git init` happened
    assert git_probe.repo_root(cwd) == cwd  # re-probed, not frozen
    assert git_probe.repo_root(cwd) == cwd  # now cached


def test_negative_results_are_ttl_cached_then_re_probed(monkeypatch):
    # A non-repo cwd is re-derived on every session in a project-tree build, so a
    # "not a repo" answer must be cached briefly to avoid re-spawning git dozens
    # of times — but only until the TTL elapses, so a folder that later becomes a
    # repo is still picked up.
    from tui_gateway import git_probe

    git_probe.invalidate()
    calls = {"n": 0}

    def probe(_cwd, *_a):
        calls["n"] += 1
        return ""  # never a repo

    monkeypatch.setattr(git_probe, "run_git", probe)
    monkeypatch.setattr(git_probe, "_NEG_TTL", 1000)  # effectively no expiry here

    cwd = "/not/a/repo"
    assert git_probe.repo_root(cwd) == ""
    for _ in range(10):
        assert git_probe.repo_root(cwd) == ""
    assert calls["n"] == 1  # cached: probed once, not 11 times

    # Once the TTL lapses, the next lookup re-probes (a `git init` may have run).
    monkeypatch.setattr(git_probe, "_NEG_TTL", 0)
    git_probe._cache._neg[cwd] = 0.0  # force-expire the cached negative
    assert git_probe.repo_root(cwd) == ""
    assert calls["n"] == 2


def test_repo_root_cache_is_single_flight(monkeypatch):
    # Concurrent identical probes share one git invocation (gateway long handlers
    # run on worker threads).
    import threading

    from tui_gateway import git_probe

    git_probe.invalidate()
    calls = {"n": 0}
    started = threading.Event()

    def slow(_cwd, *_a):
        calls["n"] += 1
        started.set()
        time = __import__("time")
        time.sleep(0.05)
        return "/repo"

    monkeypatch.setattr(git_probe, "run_git", slow)
    out: list[str] = []
    threads = [threading.Thread(target=lambda: out.append(git_probe.repo_root("/repo/x"))) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert out == ["/repo"] * 6
    assert calls["n"] == 1


def test_warm_roots_probes_in_parallel_and_fills_the_cache(monkeypatch):
    # Cold first paint must not serialize one git subprocess per cwd.
    import threading
    import time

    from tui_gateway import git_probe

    git_probe.invalidate()
    lock = threading.Lock()
    live = {"now": 0, "peak": 0, "calls": 0}

    def slow(cwd, *_a):
        with lock:
            live["now"] += 1
            live["calls"] += 1
            live["peak"] = max(live["peak"], live["now"])
        time.sleep(0.02)
        with lock:
            live["now"] -= 1
        return cwd  # show-toplevel → cwd is its own root

    monkeypatch.setattr(git_probe, "run_git", slow)
    cwds = [f"/repo{i}" for i in range(8)]
    git_probe.warm_roots(cwds, max_workers=8)

    assert live["peak"] > 1  # ran concurrently, not serialized
    # Cache is warm: resolving again triggers no further probes.
    before = live["calls"]
    assert git_probe.repo_root("/repo0") == "/repo0"
    assert live["calls"] == before


def test_create_list_roundtrip(tmp_path):
    created = _call("projects.create", {"name": "Demo", "folders": [str(tmp_path)], "use": True})
    assert created["project"]["slug"] == "demo"

    listing = _call("projects.list")
    assert [p["slug"] for p in listing["projects"]] == ["demo"]
    assert listing["active_id"] == created["project"]["id"]


def test_add_folder_and_for_cwd(tmp_path):
    folder = tmp_path / "repo"
    folder.mkdir()
    pid = _call("projects.create", {"name": "Repo", "folders": [str(folder)]})["project"]["id"]

    nested = folder / "src"
    nested.mkdir()
    resolved = _call("projects.for_cwd", {"cwd": str(nested)})
    assert resolved["project"]["id"] == pid
    # branch key is present (empty string when not a git repo).
    assert "branch" in resolved


def test_assign_session_rpc_moves_session_between_projects(tmp_path):
    target_dir = tmp_path / "target"
    natural_dir = tmp_path / "natural"
    target_dir.mkdir()
    natural_dir.mkdir()

    target = _call("projects.create", {"name": "Target", "folders": [str(target_dir)]})["project"]
    natural = _call("projects.create", {"name": "Natural", "folders": [str(natural_dir)]})["project"]

    db = server._get_db()
    assert db is not None
    db.create_session("assigned-session", "cli", cwd=str(natural_dir))
    db.append_message("assigned-session", role="user", content="move me")

    payload = _call("projects.assign_session", {"id": target["id"], "session_id": "assigned-session"})
    assert payload == {
        "cwd": str(target_dir),
        "project_id": target["id"],
        "session_id": "assigned-session",
    }
    assert db.get_session("assigned-session")["cwd"] == str(target_dir)

    tree = _call("projects.project_sessions", {"project_id": target["id"]})["project"]
    assert tree["id"] == target["id"]
    assert tree["sessionCount"] == 1
    assert tree["repos"][0]["groups"][0]["sessions"][0]["id"] == "assigned-session"

    natural_tree = _call("projects.project_sessions", {"project_id": natural["id"]})["project"]
    assert natural_tree["sessionCount"] == 0

    overview = _call("projects.tree", {"preview_limit": 3})
    assert overview["session_project_assignments"] == {"assigned-session": target["id"]}

    detached = _call("projects.unassign_session", {"session_id": "assigned-session"})
    assert detached == {"cwd": str(natural_dir), "project_id": None, "session_id": "assigned-session"}
    assert db.get_session("assigned-session")["cwd"] == str(natural_dir)
    assert _call("projects.tree", {"preview_limit": 3})["session_project_assignments"] == {
        "assigned-session": None
    }
    assert _call("projects.project_sessions", {"project_id": target["id"]})["project"]["sessionCount"] == 0
    assert _call("projects.project_sessions", {"project_id": natural["id"]})["project"]["sessionCount"] == 0


def test_assign_session_rpc_rejects_session_missing_from_active_profile(tmp_path):
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    target = _call("projects.create", {"name": "Target", "folders": [str(target_dir)]})["project"]

    resp = server._methods["projects.assign_session"](1, {"id": target["id"], "session_id": "other-profile-session"})

    assert resp["error"]["code"] == 5063
    assert "active profile" in resp["error"]["message"]
    assert _call("projects.tree", {"preview_limit": 3})["session_project_assignments"] == {}


def test_assign_rolls_back_workspace_if_assignment_write_fails(tmp_path, monkeypatch):
    from hermes_cli import projects_db as pdb

    target_dir = tmp_path / "target"
    old_dir = tmp_path / "old"
    target_dir.mkdir()
    old_dir.mkdir()
    target = _call("projects.create", {"name": "Target", "folders": [str(target_dir)]})["project"]
    db = server._get_db()
    db.create_session("s-fail-assign", "cli", cwd=str(old_dir))
    db.append_message("s-fail-assign", role="user", content="move me")

    def fail_assign(*_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr(pdb, "assign_session", fail_assign)
    response = server._methods["projects.assign_session"](
        1, {"id": target["id"], "session_id": "s-fail-assign"}
    )

    assert response["error"]["message"] == "boom"
    assert db.get_session("s-fail-assign")["cwd"] == str(old_dir)
    assert _call("projects.tree")["session_project_assignments"] == {}


def test_assign_failure_restores_a_null_workspace(tmp_path, monkeypatch):
    from hermes_cli import projects_db as pdb

    target_dir = tmp_path / "target"
    target_dir.mkdir()
    target = _call("projects.create", {"name": "Target", "folders": [str(target_dir)]})["project"]
    db = server._get_db()
    db.create_session("s-null-cwd", "cli")
    db.append_message("s-null-cwd", role="user", content="move me")

    def fail_assign(*_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr(pdb, "assign_session", fail_assign)
    response = server._methods["projects.assign_session"](
        1, {"id": target["id"], "session_id": "s-null-cwd"}
    )

    assert response["error"]["message"] == "boom"
    assert db.get_session("s-null-cwd")["cwd"] is None
    assert _call("projects.tree")["session_project_assignments"] == {}


def test_live_emit_failure_does_not_undo_a_durable_assignment(tmp_path, monkeypatch):
    target_dir = tmp_path / "target"
    old_dir = tmp_path / "old"
    target_dir.mkdir()
    old_dir.mkdir()
    target = _call("projects.create", {"name": "Target", "folders": [str(target_dir)]})["project"]
    db = server._get_db()
    db.create_session("s-live", "cli", cwd=str(old_dir))
    db.append_message("s-live", role="user", content="move me")
    server._sessions["runtime-live"] = {"agent": None, "session_key": "s-live", "cwd": str(old_dir)}

    def fail_emit(*_args, **_kwargs):
        raise OSError("emit boom")

    monkeypatch.setattr(server, "_emit", fail_emit)
    assigned = _call("projects.assign_session", {"id": target["id"], "session_id": "s-live"})

    assert assigned["cwd"] == str(target_dir)
    assert db.get_session("s-live")["cwd"] == str(target_dir)
    assert server._sessions["runtime-live"]["cwd"] == str(target_dir)
    assert _call("projects.tree")["session_project_assignments"] == {"s-live": target["id"]}


def test_assign_does_not_write_assignment_if_state_move_fails(tmp_path, monkeypatch):
    target_dir = tmp_path / "target"
    old_dir = tmp_path / "old"
    target_dir.mkdir()
    old_dir.mkdir()
    target = _call("projects.create", {"name": "Target", "folders": [str(target_dir)]})["project"]
    db = server._get_db()
    db.create_session("s-state-fail", "cli", cwd=str(old_dir))
    db.append_message("s-state-fail", role="user", content="move me")

    def fail_state(*_args, **_kwargs):
        raise OSError("state boom")

    monkeypatch.setattr(db, "replace_session_cwd", fail_state)
    response = server._methods["projects.assign_session"](
        1, {"id": target["id"], "session_id": "s-state-fail"}
    )

    assert response["error"]["message"] == "state boom"
    assert db.get_session("s-state-fail")["cwd"] == str(old_dir)
    assert _call("projects.tree")["session_project_assignments"] == {}


def test_unassign_rolls_back_workspace_if_detach_write_fails(tmp_path, monkeypatch):
    from hermes_cli import projects_db as pdb

    target_dir = tmp_path / "target"
    old_dir = tmp_path / "old"
    target_dir.mkdir()
    old_dir.mkdir()
    target = _call("projects.create", {"name": "Target", "folders": [str(target_dir)]})["project"]
    db = server._get_db()
    db.create_session("s-fail-detach", "cli", cwd=str(old_dir))
    db.append_message("s-fail-detach", role="user", content="move me")
    _call("projects.assign_session", {"id": target["id"], "session_id": "s-fail-detach"})

    def fail_exclude(*_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr(pdb, "exclude_session", fail_exclude)
    response = server._methods["projects.unassign_session"](1, {"session_id": "s-fail-detach"})

    assert response["error"]["message"] == "boom"
    assert db.get_session("s-fail-detach")["cwd"] == str(target_dir)
    assert _call("projects.tree")["session_project_assignments"] == {"s-fail-detach": target["id"]}


def test_unassign_keeps_assignment_if_state_restore_fails(tmp_path, monkeypatch):
    target_dir = tmp_path / "target"
    old_dir = tmp_path / "old"
    target_dir.mkdir()
    old_dir.mkdir()
    target = _call("projects.create", {"name": "Target", "folders": [str(target_dir)]})["project"]
    db = server._get_db()
    db.create_session("s-state-detach", "cli", cwd=str(old_dir))
    db.append_message("s-state-detach", role="user", content="move me")
    _call("projects.assign_session", {"id": target["id"], "session_id": "s-state-detach"})

    def fail_state(*_args, **_kwargs):
        raise OSError("state boom")

    monkeypatch.setattr(db, "replace_session_cwd", fail_state)
    response = server._methods["projects.unassign_session"](1, {"session_id": "s-state-detach"})

    assert response["error"]["message"] == "state boom"
    assert db.get_session("s-state-detach")["cwd"] == str(target_dir)
    assert _call("projects.tree")["session_project_assignments"] == {"s-state-detach": target["id"]}


def test_unassign_uses_home_when_saved_and_default_cwds_are_missing(tmp_path, monkeypatch):
    target_dir = tmp_path / "target"
    old_dir = tmp_path / "old"
    target_dir.mkdir()
    old_dir.mkdir()
    target = _call("projects.create", {"name": "Target", "folders": [str(target_dir)]})["project"]
    db = server._get_db()
    db.create_session("s-fallback", "cli", cwd=str(old_dir))
    db.append_message("s-fallback", role="user", content="move me")
    _call("projects.assign_session", {"id": target["id"], "session_id": "s-fallback"})
    old_dir.rmdir()
    monkeypatch.setattr(server, "_default_session_cwd", lambda: str(tmp_path / "also-missing"))

    detached = _call("projects.unassign_session", {"session_id": "s-fallback"})

    assert detached["cwd"] == os.path.expanduser("~")
    assert db.get_session("s-fallback")["cwd"] == os.path.expanduser("~")


def test_update_and_archive(tmp_path):
    pid = _call("projects.create", {"name": "Orig", "folders": [str(tmp_path)]})["project"]["id"]

    updated = _call("projects.update", {"id": pid, "name": "Renamed"})
    assert updated["project"]["name"] == "Renamed"

    payload = _call("projects.archive", {"id": pid})
    assert all(p["id"] != pid or p["archived"] for p in payload["projects"])


def test_get_unknown_returns_error():
    resp = server._methods["projects.get"](1, {"id": "nope"})
    assert "error" in resp


def test_delete_removes_project(tmp_path):
    pid = _call("projects.create", {"name": "Doomed", "folders": [str(tmp_path)]})["project"]["id"]
    payload = _call("projects.delete", {"id": pid})

    assert all(p["id"] != pid for p in payload["projects"])
    assert "projects.delete" in server._methods


def test_discover_repos_is_registered_long_handler():
    assert "projects.discover_repos" in server._methods
    assert "projects.discover_repos" in server._LONG_HANDLERS
    assert "projects.record_repos" in server._methods
    assert "projects.record_repos" in server._LONG_HANDLERS


def test_record_repos_persists_and_shows_zero_session_repo(tmp_path):
    repo = tmp_path / "fresh-repo"
    repo.mkdir()

    # Repo-first: a scanned repo with no hermes sessions still surfaces.
    _call("projects.record_repos", {"repos": [{"root": str(repo), "label": "fresh-repo"}]})

    by_label = {r["label"]: r for r in _call("projects.discover_repos")["repos"]}
    assert "fresh-repo" in by_label
    assert by_label["fresh-repo"]["sessions"] == 0


def test_discover_repos_from_full_history(tmp_path):
    repo = tmp_path / "myrepo"
    (repo / "src").mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    plain = tmp_path / "plain"
    plain.mkdir()

    db = server._get_db()
    db.create_session("s1", "cli", cwd=str(repo))
    db.create_session("s2", "cli", cwd=str(repo / "src"))
    db.create_session("s3", "cli", cwd=str(plain))  # not a git repo → excluded

    repos = _call("projects.discover_repos")["repos"]
    by_label = {r["label"]: r for r in repos}

    assert "myrepo" in by_label
    assert by_label["myrepo"]["sessions"] == 2  # both repo cwds aggregate
    assert "plain" not in by_label  # non-git dir never promoted

    # The probe is persisted back onto the session rows (membership at the source).
    assert os.path.realpath(db.get_session("s1")["git_repo_root"]) == os.path.realpath(str(repo))
