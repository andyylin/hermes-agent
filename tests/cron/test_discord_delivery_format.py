"""Discord-specific cron delivery formatting regression tests."""

from cron import scheduler


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
