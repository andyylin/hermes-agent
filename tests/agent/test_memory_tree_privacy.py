from pathlib import Path

from agent.memory_tree_privacy import scan_memory_tree_privacy


def test_memory_tree_privacy_ignores_token_counters_and_placeholders(tmp_path: Path) -> None:
    pack = tmp_path / "pack.md"
    pack.write_text(
        '\n'.join(
            [
                '"evidence": "kept Honcho recallMode=tools/contextTokens=500/dialecticMaxChars=400"',
                'EMAIL_PASSWORD=your_16_character_google_app_password',
            ]
        ),
        encoding="utf-8",
    )
    state = tmp_path / "state.json"
    state.write_text('{"outputs": {"daily": "%s"}}' % str(pack), encoding="utf-8")

    report = scan_memory_tree_privacy(state_path=state)

    assert report.findings == []


def test_memory_tree_privacy_still_reports_real_secret_shapes(tmp_path: Path) -> None:
    pack = tmp_path / "pack.md"
    pack.write_text("SERVICE_TOKEN=abcd1234abcd1234\n", encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text('{"outputs": {"daily": "%s"}}' % str(pack), encoding="utf-8")

    report = scan_memory_tree_privacy(state_path=state)

    assert len(report.findings) == 1
    assert report.findings[0].kind == "secret_assignment"
    assert "<redacted>" in report.findings[0].snippet
