import json
from pathlib import Path

from agent.memory_tree_build import redact_secrets
from agent.memory_tree_privacy import scan_memory_tree_privacy


def _state_with_pack(home: Path, text: str) -> Path:
    pack = home / "data" / "memory-tree-lite" / "recent.md"
    pack.parent.mkdir(parents=True, exist_ok=True)
    pack.write_text(text, encoding="utf-8")
    state = home / "data" / "memory-tree-lite" / "state.json"
    state.write_text(json.dumps({"outputs": {"recent": str(pack)}}), encoding="utf-8")
    return state


def test_redact_secrets_removes_assignment_style_values_from_text():
    text = "EMAIL_PASSWORD=your_16_character_google_app_password\n# EMAIL_OAUTH_TOKEN_CMD=token-command"

    redacted = redact_secrets(text)

    assert "your_16_character_google_app_password" not in redacted
    assert "token-command" not in redacted
    assert "EMAIL_PASSWORD=" not in redacted
    assert "EMAIL_OAUTH_TOKEN_CMD=" not in redacted
    assert redacted.count("[REDACTED_SECRET_ASSIGNMENT]") == 2


def test_privacy_scan_ignores_redacted_assignments_and_context_tokens(tmp_path):
    state = _state_with_pack(
        tmp_path,
        "EMAIL_PASSWORD=[REDACTED]\nEMAIL_TOKEN=***\ncontextTokens=500/dialecticMaxChars=400\n",
    )

    report = scan_memory_tree_privacy(state_path=state)

    assert report.findings == []


def test_privacy_scan_flags_unredacted_assignment_without_leaking_value(tmp_path):
    state = _state_with_pack(tmp_path, "EMAIL_PASSWORD=your_16_character_google_app_password\n")

    report = scan_memory_tree_privacy(state_path=state)

    assert len(report.findings) == 1
    assert report.findings[0].kind == "secret_assignment"
    assert "your_16_character_google_app_password" not in report.findings[0].snippet
    assert "EMAIL_PASSWORD=<redacted>" in report.findings[0].snippet
