"""Discord-specific cron delivery formatting regression tests."""

from unittest.mock import AsyncMock, MagicMock, patch

from cron import scheduler
from gateway.config import Platform


def test_discord_cron_delivery_uses_headings_and_converts_tables():
    job = {"id": "weekly-report", "name": "Weekly report"}
    content = """Bottom line\n\n| Item | Status |\n| --- | --- |\n| Backup | Healthy |\n| Queue | Empty |"""

    rendered = scheduler._format_cron_delivery_content(
        job,
        content,
        for_discord=True,
    )

    assert rendered.startswith("# Cron Alert: Weekly report")
    assert "## Report" in rendered
    assert "- **Item:** Backup; **Status:** Healthy" in rendered
    assert "- **Item:** Queue; **Status:** Empty" in rendered
    assert "| Item | Status |" not in rendered


def test_non_discord_cron_delivery_preserves_existing_wrapper_and_table():
    job = {"id": "weekly-report", "name": "Weekly report"}
    content = "| Item | Status |\n| --- | --- |\n| Backup | Healthy |"

    rendered = scheduler._format_cron_delivery_content(
        job,
        content,
        for_discord=False,
    )

    assert rendered.startswith("Cronjob Response: Weekly report")
    assert "| Item | Status |" in rendered


def test_mixed_target_fanout_formats_each_platform_independently():
    job = {"id": "weekly-report", "name": "Weekly report", "deliver": "all"}
    content = "| Item | Status |\n| --- | --- |\n| Backup | Healthy |"
    targets = [
        {"platform": "discord", "chat_id": "discord-room"},
        {"platform": "email", "chat_id": "andy@example.com"},
    ]

    discord_cfg = MagicMock(enabled=True, extra={})
    email_cfg = MagicMock(enabled=True, extra={})
    gateway_cfg = MagicMock(
        platforms={
            Platform.DISCORD: discord_cfg,
            Platform("email"): email_cfg,
        }
    )
    send_mock = AsyncMock(return_value={"success": True})

    with (
        patch.object(scheduler, "_resolve_delivery_targets", return_value=targets),
        patch.object(scheduler, "_resolve_origin", return_value=None),
        patch.object(scheduler, "load_config", return_value={"cron": {"wrap_response": True}}),
        patch("gateway.config.load_gateway_config", return_value=gateway_cfg),
        patch("tools.send_message_tool._send_to_platform", new=send_mock),
    ):
        assert scheduler._deliver_result(job, content) is None

    sent_by_platform = {
        call.args[0].value: call.args[3]
        for call in send_mock.await_args_list
    }
    assert sent_by_platform["discord"].startswith("# Cron Alert: Weekly report")
    assert "| Item | Status |" not in sent_by_platform["discord"]
    assert sent_by_platform["email"].startswith("Cronjob Response: Weekly report")
    assert "| Item | Status |" in sent_by_platform["email"]
