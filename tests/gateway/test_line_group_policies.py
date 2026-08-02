"""Regression tests for private LINE group collection policies."""

from __future__ import annotations

import asyncio
import json
import stat
from unittest.mock import AsyncMock

from gateway.config import PlatformConfig
from tests.gateway._plugin_adapter_loader import load_plugin_adapter

_line = load_plugin_adapter("line")
LineAdapter = _line.LineAdapter


def _event(*, chat_id: str, text: str = "hello", msg_type: str = "text") -> dict:
    message = {"id": "m1", "type": msg_type}
    if msg_type == "text":
        message["text"] = text
    return {
        "type": "message",
        "timestamp": 123,
        "webhookEventId": "evt1",
        "replyToken": "reply-token",
        "source": {"type": "group", "groupId": chat_id, "userId": "Uother"},
        "message": message,
    }


def _adapter(**extra):
    cfg = PlatformConfig(
        enabled=True,
        extra={
            "channel_access_token": "tok",
            "channel_secret": "sec",
            **extra,
        },
    )
    adapter = LineAdapter(cfg)
    adapter.handle_message = AsyncMock()
    return adapter


def test_group_policy_env_is_profile_scoped_in_multiplex(monkeypatch):
    from agent import secret_scope as ss

    monkeypatch.setenv("LINE_ALLOWED_GROUPS", "Cother-profile")
    monkeypatch.setenv("LINE_READ_ONLY_GROUPS", "Cother-profile")
    ss.set_multiplex_active(True)
    token = ss.set_secret_scope({"LINE_READ_ONLY_GROUPS": "Cscoped"})
    try:
        adapter = _adapter()
    finally:
        ss.reset_secret_scope(token)
        ss.set_multiplex_active(False)

    assert adapter.allowed_groups == set()
    assert adapter.read_only_groups == {"Cscoped"}


def test_group_policy_env_installs_default_profile_scope_when_multiplex_unscoped(
    monkeypatch, tmp_path
):
    from agent import secret_scope as ss
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    scoped = {
        "LINE_ALLOW_ALL_USERS": "false",
        "LINE_ALLOWED_USERS": "Uscoped",
        "LINE_ALLOWED_GROUPS": "Callowed-scoped",
        "LINE_READ_ONLY_GROUPS": "Creadonly-scoped",
        "LINE_ARCHIVE_GROUPS": "Carchive-scoped",
        "LINE_REQUIRE_PREFIX_GROUPS": "Cprefix-scoped",
        "LINE_GROUP_PREFIXES": "Scoped:,Hermes:",
        "LINE_ALLOWED_ROOMS": "Rscoped",
    }
    (tmp_path / ".env").write_text(
        "\n".join(f"{name}={value}" for name, value in scoped.items()) + "\n",
        encoding="utf-8",
    )
    for name in scoped:
        monkeypatch.setenv(name, "true" if name == "LINE_ALLOW_ALL_USERS" else "poison")

    ss.set_multiplex_active(True)
    home_token = set_hermes_home_override(str(tmp_path))
    try:
        assert ss.current_secret_scope() is None
        adapter = _adapter()
    finally:
        reset_hermes_home_override(home_token)
        ss.set_multiplex_active(False)

    assert adapter.allow_all is False
    assert adapter.allowed_users == {"Uscoped"}
    assert adapter.allowed_groups == {"Callowed-scoped"}
    assert adapter.read_only_groups == {"Creadonly-scoped"}
    assert adapter.archive_groups == {"Carchive-scoped"}
    assert adapter.require_prefix_groups == {"Cprefix-scoped"}
    assert adapter.group_prefixes == ["Scoped:", "Hermes:"]
    assert adapter.allowed_rooms == {"Rscoped"}


def test_read_only_group_archives_without_dispatch(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _adapter(read_only_groups=["Creadonly"])

    asyncio.run(adapter._dispatch_event(_event(chat_id="Creadonly", text="archive me")))

    adapter.handle_message.assert_not_awaited()
    assert "Creadonly" not in adapter._reply_tokens
    archive = tmp_path / "data" / "line-read-only" / "Creadonly.jsonl"
    row = json.loads(archive.read_text(encoding="utf-8").splitlines()[-1])
    assert row["chat_id"] == "Creadonly"
    assert row["text"] == "archive me"


def test_archive_group_records_and_dispatches_prefixed_message(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _adapter(
        allowed_groups=["Carchive"],
        archive_groups=["Carchive"],
        require_prefix_groups=["Carchive"],
        group_prefixes=["Hermes:"],
    )

    asyncio.run(adapter._dispatch_event(_event(chat_id="Carchive", text="Hermes: summarize")))

    adapter.handle_message.assert_awaited_once()
    assert adapter.handle_message.await_args.args[0].text == "summarize"
    archive = tmp_path / "data" / "line-read-only" / "Carchive.jsonl"
    row = json.loads(archive.read_text(encoding="utf-8").splitlines()[-1])
    assert row["text"] == "Hermes: summarize"


def test_line_archive_uses_owner_only_permissions(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _adapter(read_only_groups=["Creadonly"])

    asyncio.run(adapter._dispatch_event(_event(chat_id="Creadonly", text="private")))

    archive_dir = tmp_path / "data" / "line-read-only"
    archive = archive_dir / "Creadonly.jsonl"
    assert stat.S_IMODE(archive_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600


def test_line_archive_rotates_before_unbounded_growth(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(_line, "LINE_ARCHIVE_MAX_BYTES", 128)
    adapter = _adapter(read_only_groups=["Creadonly"])
    archive_dir = tmp_path / "data" / "line-read-only"
    archive_dir.mkdir(parents=True)
    archive = archive_dir / "Creadonly.jsonl"
    archive.write_bytes(b"x" * 128)

    asyncio.run(adapter._dispatch_event(_event(chat_id="Creadonly", text="rotate")))

    assert (archive_dir / "Creadonly.jsonl.1").read_bytes() == b"x" * 128
    assert "rotate" in archive.read_text(encoding="utf-8")


def test_prefix_required_group_drops_unprefixed_text():
    adapter = _adapter(
        allowed_groups=["Cprefix"],
        require_prefix_groups=["Cprefix"],
        group_prefixes=["Hermes:"],
    )

    asyncio.run(adapter._dispatch_event(_event(chat_id="Cprefix", text="not for Hermes")))

    adapter.handle_message.assert_not_awaited()
    assert "Cprefix" not in adapter._reply_tokens


def test_prefix_required_group_drops_non_text_message():
    adapter = _adapter(
        allowed_groups=["Cprefix"],
        require_prefix_groups=["Cprefix"],
    )
    adapter._download_media = AsyncMock(return_value=("/tmp/image.jpg", "image/jpeg"))

    asyncio.run(adapter._dispatch_event(_event(chat_id="Cprefix", msg_type="image")))

    adapter.handle_message.assert_not_awaited()
    assert "Cprefix" not in adapter._reply_tokens
