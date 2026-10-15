"""Behaviour of the rpi5 migration helper.

Fixtures are fake unit text. They must never contain a real credential, and
the helper's stdout must not grow one out of an assignment in the fixture.
"""

from __future__ import annotations

import io
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "docs" / "ops"))

import rpi5_migration as mig


SECRET = "supersecretvalue"
ALICE_GAMES = "/srv/alice/games"
ALICE_PROFILE = "/srv/alice/profile"

ALICE_UNIT = f"""\
[Service]
ExecStartPre=/usr/bin/python3 /opt/hermes/render_kid_matrix_env.py --bw-item alice --token {SECRET}
Environment="ALICE_GAMES_ROOT={ALICE_GAMES}"
Environment=ALICE_PROFILE_ROOT={ALICE_PROFILE}
EnvironmentFile=-/run/hermes/alice.env
ExecStart=/usr/bin/python3 -m hermes_cli.main gateway run
"""

KATIE_UNIT = f"""\
[Service]
ExecStartPre=/usr/bin/env TOKEN={SECRET} python3 /opt/hermes/render_kid_matrix_env.py --item katie
EnvironmentFile=/run/hermes/katie.env
ExecStart=/home/pi/.hermes/venvs/hermes-py314-v0.21.7/bin/hermes gateway run
"""

WIFE_UNIT = """\
[Service]
ExecStart=/opt/hermes/wife-gateway.sh --config /opt/hermes/wife.yaml
"""

WEBUI_UNIT = """\
[Service]
ExecStart=/opt/hermes/hermex-webui-start.sh
"""

WEBUI_DROPIN = """\
[Service]
Environment=VIRTUAL_ENV=/home/pi/.hermes/venvs/hermes-py314-v0.21.7
"""

PIN_BODY = "[Service]\nExecStart=/old/venv/bin/python -m hermes_cli.main gateway run\n"
MEMORY_BODY = "[Service]\nMemoryMax=2G\nMemoryHigh=1500M\n"


def _assert_secret_free(text: str) -> None:
    assert SECRET not in text
    assert ALICE_GAMES not in text
    assert ALICE_PROFILE not in text


def test_program_label_drops_arguments_and_env_assignments():
    label = mig.program_label(
        f"/usr/bin/env TOKEN={SECRET} python3 /opt/hermes/render_kid_matrix_env.py --item katie"
    )
    assert label == "/usr/bin/env python3 /opt/hermes/render_kid_matrix_env.py"
    assert SECRET not in label
    bare = mig.program_label(f"-/opt/hermes/render_kid_matrix_env.py --token {SECRET}")
    assert bare == "/opt/hermes/render_kid_matrix_env.py"
    assert SECRET not in bare


def test_inventory_lists_directive_names_and_omits_values():
    units = mig.scan_units(
        [
            ("hermes-gateway-alice.service", ALICE_UNIT),
            ("hermes-gateway-katie.service", KATIE_UNIT),
            ("hermes-gateway-wife.service", WIFE_UNIT),
            ("hermes-webui.service", WEBUI_UNIT),
            ("hermes-webui.service", WEBUI_DROPIN),
        ]
    )
    text = mig.format_inventory(units)
    _assert_secret_free(text)
    assert "ExecStartPre: /usr/bin/python3 /opt/hermes/render_kid_matrix_env.py" in text
    assert "Environment: ALICE_GAMES_ROOT, ALICE_PROFILE_ROOT" in text
    assert "EnvironmentFile: /run/hermes/alice.env" in text
    assert "EnvironmentFile: /run/hermes/katie.env" in text
    assert "profiles/alice/.env" in text
    assert "profiles/katie/.env" in text
    assert "profiles/wife/.env" in text
    assert "profiles/dad/.env" in text
    assert "secrets.bitwarden" in text
    assert "render_kid_matrix_env.py is retired" in text
    assert "Do not merge the rendered env file" in text
    assert "_build_safe_env" in text
    assert (
        "ExecStart program /opt/hermes/wife-gateway.sh is not a credential source."
        in text
    )
    assert "Render those values into" not in text
    assert (
        f"Do not add this profile's EnvironmentFile or Environment= to {mig.GATEWAY_UNIT}"
        in text
    )
    assert "Do not append wife, dad, katie, or alice credentials" in text
    assert "hermex-webui-start.sh" in text
    assert "Interpreter drop-in: retarget" in text
    assert "hermes dashboard" in text
    assert "Do not replace ExecStart with hermes dashboard." in text


def test_webui_without_python_pin_is_left_alone():
    units = mig.scan_units([("hermes-webui.service", WEBUI_UNIT)])
    lines = mig.webui_lines(units)
    assert len(lines) == 1
    assert "Interpreter drop-in: leave" in lines[0]
    assert "hermes dashboard" in lines[0]


def test_scan_roots_groups_dropins_with_the_owning_unit(tmp_path: Path):
    unit_dir = tmp_path / "user"
    dropin = unit_dir / "hermes-gateway-alice.service.d"
    dropin.mkdir(parents=True)
    (unit_dir / "hermes-gateway-alice.service").write_text(ALICE_UNIT, encoding="utf-8")
    (dropin / "extra.conf").write_text(
        f"Environment=EXTRA_TOKEN={SECRET}\n",
        encoding="utf-8",
    )
    units = mig.scan_roots([unit_dir])
    assert [unit.name for unit in units] == ["hermes-gateway-alice.service"]
    text = mig.format_inventory(units)
    assert "EXTRA_TOKEN" in text
    _assert_secret_free(text)


def test_dry_run_blockers_abort_even_when_the_command_exits_zero():
    text = (
        "Migration plan (dry run — nothing changed)\n"
        "  ✗ Blockers (fix these first, nothing will be changed):\n"
        "    • Profile 'wife' enables line, which binds its own port and has no "
        "/p/wife/ ingress\n"
        "  Notices:\n"
        "    • Profile 'default': sms is already failing\n"
    )
    findings = mig.parse_dry_run(text, command_exit=0)
    ok, reasons = mig.evaluate_preflight(findings, require_fold=False)
    assert not ok
    assert any("line" in reason for reason in reasons)
    assert not any("sms is already failing" in reason for reason in reasons)


def test_dry_run_redacts_assignment_values_in_blocker_lines():
    text = (
        "  ✗ Blockers (fix these first, nothing will be changed):\n"
        f"    • token={SECRET} shared by two profiles\n"
    )
    findings = mig.parse_dry_run(text, command_exit=0)
    assert findings.blockers
    assert SECRET not in findings.blockers[0]
    assert "[redacted]" in findings.blockers[0]


def test_early_preflight_allows_only_katie_and_alice_left_alone():
    text = (
        "Migration plan (dry run — nothing changed)\n"
        "  Standalone by config (gateway.standalone: true), left alone: "
        "katie, alice\n"
        "  wife         -             -\n"
        "  dad          -             -\n"
    )
    findings = mig.parse_dry_run(text, command_exit=0)
    ok, reasons = mig.evaluate_preflight(findings, require_fold=False)
    assert ok
    assert reasons == ()
    fold_ok, fold_reasons = mig.evaluate_preflight(findings, require_fold=True)
    assert not fold_ok
    assert any("katie" in reason and "alice" in reason for reason in fold_reasons)


def test_early_preflight_aborts_when_wife_or_dad_are_left_alone():
    text = (
        "  Standalone by config (gateway.standalone: true), left alone: wife, katie\n"
    )
    findings = mig.parse_dry_run(text, command_exit=0)
    ok, reasons = mig.evaluate_preflight(findings, require_fold=False)
    assert not ok
    assert any("wife and dad are not standalone" in reason for reason in reasons)
    assert any("wife" in reason for reason in reasons)


def test_early_preflight_aborts_when_only_one_profile_is_visible():
    text = (
        "  Standalone by config (gateway.standalone: true), left alone: katie, alice\n"
        "  Only one profile exists: nothing to multiplex.\n"
    )
    findings = mig.parse_dry_run(text, command_exit=0)
    ok, reasons = mig.evaluate_preflight(findings, require_fold=False)
    assert not ok
    assert any("wife and dad" in reason for reason in reasons)


def test_command_failure_aborts_without_a_blocker_section():
    findings = mig.parse_dry_run("", command_exit=1)
    ok, reasons = mig.evaluate_preflight(findings, require_fold=False)
    assert not ok
    assert reasons == ("migrate --dry-run exited non-zero",)


def test_clean_fold_preflight_passes():
    text = (
        "Migration plan (dry run — nothing changed)\n"
        "  profile      gateway pid   service\n"
        "  default      -             hermes-gateway\n"
        "  wife         -             -\n"
    )
    findings = mig.parse_dry_run(text, command_exit=0)
    ok, reasons = mig.evaluate_preflight(findings, require_fold=True)
    assert ok
    assert reasons == ()


def test_orphans_follow_facts_json_not_a_time_window():
    referenced_name = "cee636e2" + "a" * 24
    window_sibling = "cc8eed33" + "b" * 24
    other = "f098ffea" + "c" * 24
    facts = json.dumps(
        {
            "packages": {
                "venv": {
                    "environment": (
                        "/home/pi/.hermes/installs/5935b2ebc06c58fa/environments/"
                        f"{referenced_name}/venv"
                    )
                }
            }
        }
    )
    referenced = mig.referenced_generation(facts)
    orphans = mig.orphan_generations(
        [referenced_name, window_sibling, other], referenced
    )
    assert referenced == referenced_name
    assert referenced_name not in orphans
    assert orphans == tuple(sorted((window_sibling, other)))


def test_orphan_classification_refuses_a_facts_file_without_an_environment():
    with pytest.raises(ValueError):
        mig.referenced_generation("{}")
    with pytest.raises(ValueError):
        mig.orphan_generations(["abc"], "")


def test_gc_gate_uses_taipei_not_a_naive_clock():
    before = datetime(2026, 10, 16, 4, 40, tzinfo=UTC)  # 12:40 Taipei
    at = datetime(2026, 10, 16, 4, 41, tzinfo=UTC)  # 12:41 Taipei
    local = datetime(2026, 10, 16, 12, 41)
    assert not mig.gc_permitted(before)
    assert mig.gc_permitted(at)
    assert not mig.gc_permitted(local)
    assert mig.gc_permitted(at + timedelta(minutes=1))


def test_memory_cap_is_above_the_measured_set_and_below_the_host():
    assert mig.MEMORY_HIGH == "6G"
    assert mig.MEMORY_MAX == "8G"
    text = mig.memory_dropin_text()
    assert "MemoryHigh=6G" in text
    assert "MemoryMax=8G" in text
    assert "2G" not in text


def test_sideline_and_rollback_round_trip(tmp_path: Path):
    dropins = tmp_path / "hermes-gateway.service.d"
    backup = tmp_path / "backup"
    dropins.mkdir()
    originals = {}
    for name in (*mig.PINNING_DROPINS, mig.MEMORY_DROPIN):
        body = MEMORY_BODY if name == mig.MEMORY_DROPIN else PIN_BODY + name + "\n"
        (dropins / name).write_text(body, encoding="utf-8")
        originals[name] = body
    (dropins / mig.RUNTIME_DROPIN).write_text(
        "[Service]\nExecStartPre=/opt/render\n", encoding="utf-8"
    )

    moved = mig.sideline_dropins(dropins, backup)
    assert set(moved) == set(mig.PINNING_DROPINS) | {mig.MEMORY_DROPIN}
    for name in mig.PINNING_DROPINS:
        assert not (dropins / name).exists()
        assert (backup / name).read_text(encoding="utf-8") == originals[name]
    assert "MemoryMax=2G" not in (dropins / mig.NEW_MEMORY_DROPIN).read_text(
        encoding="utf-8"
    )
    assert (dropins / mig.NEW_MEMORY_DROPIN).read_text(
        encoding="utf-8"
    ) == mig.memory_dropin_text()

    again = mig.sideline_dropins(dropins, backup)
    assert again == ()

    actions = mig.rollback_dropins(dropins, backup)
    assert any(f"removed {mig.NEW_MEMORY_DROPIN}" in action for action in actions)
    assert any(f"removed {mig.RUNTIME_DROPIN}" in action for action in actions)
    for name, body in originals.items():
        assert (dropins / name).read_text(encoding="utf-8") == body
        assert not (backup / name).exists()
    assert not (dropins / mig.NEW_MEMORY_DROPIN).exists()
    assert not (dropins / mig.RUNTIME_DROPIN).exists()


def test_rollback_refuses_a_partial_backup_before_any_rename(tmp_path: Path):
    dropins = tmp_path / "hermes-gateway.service.d"
    backup = tmp_path / "backup"
    dropins.mkdir()
    backup.mkdir()
    (dropins / mig.NEW_MEMORY_DROPIN).write_text(
        mig.memory_dropin_text(), encoding="utf-8"
    )
    (backup / mig.PINNING_DROPINS[0]).write_text(PIN_BODY, encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        mig.rollback_dropins(dropins, backup)
    assert (dropins / mig.NEW_MEMORY_DROPIN).is_file()
    assert (backup / mig.PINNING_DROPINS[0]).is_file()


def test_locate_dropins_refuses_zero_or_two(tmp_path: Path):
    user = tmp_path / "user"
    system = tmp_path / "system"
    (user / "hermes-gateway.service.d").mkdir(parents=True)
    (system / "hermes-gateway.service.d").mkdir(parents=True)
    assert mig.locate_dropin_dirs([user]) == [user / "hermes-gateway.service.d"]
    found = mig.locate_dropin_dirs([user, system])
    assert len(found) == 2
    assert mig.locate_dropin_dirs([tmp_path / "missing"]) == []


def test_rollback_checklist_covers_the_host_without_gc_or_migrate_apply():
    text = "\n".join(mig.ROLLBACK_CHECKLIST)
    for phrase in (
        "gateway.standalone: true",
        "wife, dad, katie, and alice",
        "facts.json",
        "venvs/hermes-py314-v0.21.7",
        "zzz-main-v0217.conf",
        "memory-limits.conf",
        "zz-multiplex-memory.conf",
        "zz-profile-runtime-env.conf",
        "hermex-webui-start.sh",
        "98-py314-v0217.conf",
        "profile .env",
        "Do not merge credentials",
        "daemon-reload",
        "hermes-gateway-wife",
        "Do not run `hermes gateway migrate --multiplex`",
        "Do not run `hermes pm gc`",
    ):
        assert phrase in text


def test_cli_classify_aborts_on_blockers(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            "  ✗ Blockers (fix these first):\n    • Profile 'dad' enables sms\n",
        ),
    )
    code = mig.main(["classify-dry-run", "--command-exit", "0"])
    captured = capsys.readouterr()
    assert code == 1
    assert "abort" in captured.out
    assert "sms" in captured.out


def test_cli_orphans_prints_names_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    referenced = "cee636e2" + "d" * 24
    other = "aabbccdd" + "e" * 24
    env_root = tmp_path / "environments"
    (env_root / referenced).mkdir(parents=True)
    (env_root / other).mkdir()
    facts = tmp_path / "facts.json"
    facts.write_text(
        json.dumps(
            {"packages": {"venv": {"environment": str(env_root / referenced / "venv")}}}
        ),
        encoding="utf-8",
    )
    code = mig.main(["orphans", "--facts", str(facts), "--environments", str(env_root)])
    captured = capsys.readouterr()
    assert code == 0
    assert f"referenced: {referenced}" in captured.out
    assert f"orphan: {other}" in captured.out
    assert f"orphan: {referenced}" not in captured.out


KID_DROPIN = f"""\
[Service]
Environment=PATH=/home/pi/.hermes/profiles/katie/bin:/usr/bin
Environment={mig._KANBAN_ENV}=false
Environment=OTHER_TOKEN={SECRET}
MemoryHigh=1G
MemoryMax=2G
"""

ALICE_LIMITS = """\
[Service]
Environment=PATH=/home/pi/.hermes/profiles/alice/bin:/usr/bin
MemoryHigh=1500M
MemoryMax=2G
"""

WIFE_KANBAN = """\
[Service]
Environment=HERMES_KANBAN_DISPATCH_IN_GATEWAY=false
"""

RENDERER_SCRIPT = f'''\
TOKEN = "{SECRET}"
KEYS = ("MATRIX_HOMESERVER", "MATRIX_ACCESS_TOKEN", "MATRIX_USER_ID")
os.environ["MATRIX_DEVICE_ID"] = fetched["device"]
BWS_ACCESS_TOKEN = "{SECRET}"
'''

ALICE_CONFIG = """\
mcp_servers:
  other:
    command: "python"
    env:
      TOKEN: "supersecretvalue"
  game-workshop:
    command: "python"
    args: ["alice_game_workshop.py"]
    env:
      KEEP: "yes"
"""


def test_carryover_reports_memory_path_and_kanban_without_secrets():
    units = mig.scan_units(
        [
            ("hermes-gateway-katie.service", KATIE_UNIT),
            ("hermes-gateway-katie.service", KID_DROPIN, "10-path.conf"),
            ("hermes-gateway-alice.service", ALICE_UNIT),
            ("hermes-gateway-alice.service", ALICE_LIMITS, "memory.conf"),
            ("hermes-gateway-wife.service", WIFE_UNIT),
            ("hermes-gateway-wife.service", WIFE_KANBAN, "disable-kanban.conf"),
        ]
    )
    text = mig.format_inventory(units)
    _assert_secret_free(text)
    assert "katie: unit MemoryHigh=1G MemoryMax=2G is dropped" in text
    assert "unit PATH includes /home/pi/.hermes/profiles/katie/bin." in text
    assert "alice: unit PATH includes /home/pi/.hermes/profiles/alice/bin." in text
    assert "wife: kanban dispatch is off" in text
    assert "katie: kanban dispatch is off" in text
    assert "No per-profile memory key" in text
    assert "is not read" in text
    assert "disable-kanban.conf" in text
    assert mig.MEMORY_HIGH in text


def test_alice_mcp_env_comes_from_the_unit_and_leaves_other_servers():
    env = mig.read_named_environment(ALICE_UNIT, mig.ALICE_MCP_ENV_KEYS)
    assert env == {
        "ALICE_GAMES_ROOT": ALICE_GAMES,
        "ALICE_PROFILE_ROOT": ALICE_PROFILE,
    }
    assert SECRET not in env.values()
    patched = mig.patch_mcp_server_env(ALICE_CONFIG, "game-workshop", env)
    assert f'ALICE_GAMES_ROOT: "{ALICE_GAMES}"' in patched
    assert f'ALICE_PROFILE_ROOT: "{ALICE_PROFILE}"' in patched
    assert 'KEEP: "yes"' in patched
    assert 'TOKEN: "supersecretvalue"' in patched
    assert patched.index("other:") < patched.index("game-workshop:")
    with pytest.raises(ValueError, match="no game-workshop"):
        mig.patch_mcp_server_env("mcp_servers:\n  other: {}\n", "game-workshop", env)
    inserted = mig.patch_mcp_server_env(
        'mcp_servers:\n  game-workshop:\n    command: "python"\n',
        "game-workshop",
        env,
    )
    assert 'command: "python"' in inserted
    assert f'ALICE_PROFILE_ROOT: "{ALICE_PROFILE}"' in inserted
    with pytest.raises(ValueError, match="not a block"):
        mig.patch_mcp_server_env(
            "mcp_servers:\n  game-workshop: {command: x}\n",
            "game-workshop",
            env,
        )


def test_alice_games_root_must_match_the_recorded_live_path():
    env = {"ALICE_GAMES_ROOT": ALICE_GAMES, "ALICE_PROFILE_ROOT": ALICE_PROFILE}
    with pytest.raises(ValueError, match=mig.ALICE_GAMES_ROOT_EXPECTED):
        mig.require_alice_games_root(env, mig.ALICE_GAMES_ROOT_EXPECTED)
    mig.require_alice_games_root(
        {"ALICE_GAMES_ROOT": mig.ALICE_GAMES_ROOT_EXPECTED},
        mig.ALICE_GAMES_ROOT_EXPECTED,
    )


def test_renderer_keys_are_names_only():
    names = mig.renderer_env_names(RENDERER_SCRIPT)
    assert names == (
        "MATRIX_ACCESS_TOKEN",
        "MATRIX_DEVICE_ID",
        "MATRIX_HOMESERVER",
        "MATRIX_USER_ID",
    )
    assert SECRET not in names
    assert "BWS_ACCESS_TOKEN" not in names


def test_matrix_probe_matches_the_adapter_gate():
    assert mig.matrix_adapter_sees_credentials(
        {"MATRIX_HOMESERVER", "MATRIX_ACCESS_TOKEN"}
    )
    assert mig.matrix_adapter_sees_credentials({"MATRIX_HOMESERVER", "MATRIX_PASSWORD"})
    assert not mig.matrix_adapter_sees_credentials({"MATRIX_ACCESS_TOKEN"})
    assert not mig.matrix_adapter_sees_credentials({"MATRIX_HOMESERVER"})
    lines = mig.matrix_probe_lines({"MATRIX_HOMESERVER", "MATRIX_ACCESS_TOKEN"})
    assert "MATRIX_ACCESS_TOKEN: present" in lines
    assert "MATRIX_PASSWORD: missing" in lines
    assert "matrix adapter would accept credentials: yes" in lines
    assert SECRET not in "\n".join(lines)


def test_config_edits_leave_wife_and_dad_standalone_unset():
    text = "\n".join(mig.CONFIG_EDITS)
    assert "wife config.yaml: no change" in text
    assert "dad config.yaml: no change" in text
    assert "katie config.yaml: remove gateway.standalone" in text
    assert "alice config.yaml: remove gateway.standalone" in text
    assert "do not set kanban.dispatch_in_gateway" in text
    assert "ALICE_PROFILE_ROOT read from her unit" in text
    assert "Do not hardcode the profile root" in text
    assert "BWS_ACCESS_TOKEN only" in text
    assert "/srv/alice-portal/games" in text


def test_cli_alice_mcp_dry_run_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    unit_dir = tmp_path / "user"
    unit_dir.mkdir()
    unit = ALICE_UNIT.replace(ALICE_GAMES, mig.ALICE_GAMES_ROOT_EXPECTED)
    (unit_dir / "hermes-gateway-alice.service").write_text(unit, encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(ALICE_CONFIG, encoding="utf-8")
    code = mig.main(
        [
            "alice-mcp-env",
            "--root",
            str(unit_dir),
            "--config",
            str(config),
            "--dry-run",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert f"ALICE_GAMES_ROOT={mig.ALICE_GAMES_ROOT_EXPECTED}" in captured.out
    assert f"ALICE_PROFILE_ROOT={ALICE_PROFILE}" in captured.out
    assert SECRET not in captured.out
    assert "dry-run: config not written" in captured.out
    assert config.read_text(encoding="utf-8") == ALICE_CONFIG


def test_cli_matrix_probe_prints_presence_only(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        mig,
        "load_profile_secret_names",
        lambda _home: {"MATRIX_HOMESERVER", "MATRIX_PASSWORD"},
    )
    code = mig.main(["matrix-probe", "--home", "/tmp/does-not-matter"])
    captured = capsys.readouterr()
    assert code == 0
    assert "MATRIX_PASSWORD: present" in captured.out
    assert "MATRIX_ACCESS_TOKEN: missing" in captured.out
    assert SECRET not in captured.out
