from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace


def test_redact_secrets_masks_keys_and_token_values():
    from agent.memory_tree_build import redact_secrets

    payload = {
        "api_key": "sk-thismustnotleak1234567890",
        "nested": {
            "message": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
            "safe": "keep me",
        },
    }

    redacted = redact_secrets(payload)

    assert redacted["api_key"] == "[REDACTED]"
    assert "Bearer" not in redacted["nested"]["message"]
    assert "[REDACTED]" in redacted["nested"]["message"]
    assert redacted["nested"]["safe"] == "keep me"


def test_build_memory_tree_packs_writes_state_and_redacted_outputs(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "s1.jsonl").write_text(
        '{"role":"user","content":"Remember project Alpha"}\n', encoding="utf-8"
    )
    ledger = tmp_path / "data" / "active-work" / "ledger.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "id": "job-1",
                        "title": "Sensitive job",
                        "status": "active",
                        "source_of_truth": "Hermes cron abc",
                        "runtime": {"api_token": "ghp_abcdefghijklmnopqrstuvwxyz123456"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    cron_out = tmp_path / "cron" / "output" / "cron-1" / "latest.md"
    cron_out.parent.mkdir(parents=True)
    cron_out.write_text("Cron output with sk-thismustnotleak1234567890", encoding="utf-8")

    from agent.memory_tree_build import BuildOptions, build_memory_tree_packs

    state = build_memory_tree_packs(
        BuildOptions(
            hermes_home=tmp_path,
            session_limit=5,
            ledger_limit=5,
            cron_limit=5,
            max_record_chars=1200,
        )
    )

    assert state["schema"] == "memory-tree-lite-state-v1"
    assert state["counts"] == {"records_total": 3, "sessions": 1, "active_work": 1, "cron_outputs": 1}
    assert Path(state["outputs"]["recent"]).exists()
    recent = Path(state["outputs"]["recent"]).read_text(encoding="utf-8")
    assert "Remember project Alpha" in recent
    assert "[REDACTED]" in recent
    assert "sk-thismustnotleak" not in recent
    assert "ghp_" not in recent


def test_build_report_is_compact_and_includes_changed_counts(tmp_path):
    from agent.memory_tree_build import BuildOptions, build_memory_tree_packs, format_build_report

    state = build_memory_tree_packs(BuildOptions(hermes_home=tmp_path, session_limit=0, ledger_limit=0, cron_limit=0))
    report = format_build_report(state)

    assert report.startswith("Memory Tree Lite build complete")
    assert "records: 0" in report
    assert "recent:" in report
    assert "index:" in report
