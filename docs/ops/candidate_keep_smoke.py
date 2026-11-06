"""Behavior probes for the KEEP surfaces named in PATCH-SCORE.md.

Run with ``PYTHONPATH`` pointed at the candidate tree and ``HERMES_HOME`` set
to a scratch directory before the interpreter starts. Imports stay at module
level; the parent process isolates the home.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from agent.secret_sources.bitwarden import _disk_cache_path, _purge_plaintext_disk_cache
from cron.scheduler_delivery import _format_cron_email_subject
from cron.session_delivery import deliver_cron_output_to_session
from plugins.platforms.email.adapter import _render_email_bodies
from plugins.platforms.line.adapter import LineAdapter
from plugins.platforms.mattermost.token_scope import profile_owns_mattermost_token
from tools.send_message_tool import _filter_standalone_kwargs
from tools.skill_manager_tool import _validate_frontmatter


def _skill(_home: Path) -> None:
    body = "---\nname: demo\ndescription: {desc}\n---\nDo the thing.\n"
    missing = _validate_frontmatter(
        body.format(desc="Loads the demo skill."), new_skill=True)
    present = _validate_frontmatter(
        body.format(desc="Loads the demo skill, not the other one."), new_skill=True)
    edit = _validate_frontmatter(
        body.format(desc="Loads the demo skill."), new_skill=False)
    knot = _validate_frontmatter(
        body.format(desc="Loads the knot skill notes."), new_skill=True)
    if not missing or "Not exclusion" not in missing:
        raise AssertionError(missing)
    if present is not None or edit is not None:
        raise AssertionError((present, edit))
    if not knot or "Not exclusion" not in knot:
        raise AssertionError(knot)


def _bitwarden(home: Path) -> None:
    path = _disk_cache_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("plaintext-cache-marker", encoding="utf-8")
    _purge_plaintext_disk_cache(home)
    if path.exists():
        raise AssertionError(f"plaintext cache survived at {path}")


def _mattermost(home: Path) -> None:
    for name in ("wife", "dad"):
        profile = home / "profiles" / name
        profile.mkdir(parents=True, exist_ok=True)
        (profile / ".env").write_text("OTHER_TOKEN=shared\n", encoding="utf-8")
        if profile_owns_mattermost_token(profile):
            raise AssertionError(f"{name} inherited a token it does not own")
        (profile / ".env").write_text("MATTERMOST_TOKEN=owned-by-profile\n", encoding="utf-8")
        if not profile_owns_mattermost_token(profile):
            raise AssertionError(f"{name} rejected its own token")


def _email(_home: Path) -> None:
    plain, html = _render_email_bodies("Hello <3 there>")
    if plain != "Hello <3 there>" or "&lt;3" not in html:
        raise AssertionError((plain, html))
    plain_html, sanitized = _render_email_bodies("<p>Hi</p><script>alert(1)</script>")
    if "alert" in sanitized.lower() or "Hi" not in plain_html:
        raise AssertionError((plain_html, sanitized))


def _cron_subject(_home: Path) -> None:
    if _format_cron_email_subject({"id": "job"}) is not None:
        raise AssertionError("missing template should not invent a subject")
    stamped = _format_cron_email_subject({
        "id": "job",
        "name": "brief",
        "email_subject_template": "Brief {date}",
        "email_thread_key": "daily-brief",
    })
    if not isinstance(stamped, dict):
        raise AssertionError(stamped)
    if stamped.get("thread_anchor_key") != "daily-brief":
        raise AssertionError(stamped)
    if not str(stamped.get("subject", "")).startswith("Brief "):
        raise AssertionError(stamped)


def _session_delivery(_home: Path) -> None:
    if not callable(deliver_cron_output_to_session):
        raise AssertionError("stored-session delivery is not callable")


def _line(home: Path) -> None:
    adapter = LineAdapter.__new__(LineAdapter)
    adapter._archive_read_only_message(
        {"source": {"type": "group", "groupId": "G1", "userId": "U1"},
         "message": {"id": "m1"}, "timestamp": 1},
        text="archived hello", msg_type="text", media_urls=[], media_types=[])
    archive = home / "data" / "line-read-only" / "G1.jsonl"
    if not archive.is_file() or "archived hello" not in archive.read_text(encoding="utf-8"):
        raise AssertionError(archive)


def _accepts_subject(body: str, subject: dict | None = None) -> dict | None:
    return subject


def _rejects_subject(body: str) -> str:
    return body


def _send_subject(_home: Path) -> None:
    kept = _filter_standalone_kwargs(
        _accepts_subject, {"body": "x", "subject": {"subject": "Hi"}})
    if kept.get("subject") != {"subject": "Hi"}:
        raise AssertionError(kept)
    dropped = _filter_standalone_kwargs(
        _rejects_subject, {"body": "x", "subject": {"subject": "Hi"}})
    if "subject" in dropped:
        raise AssertionError(dropped)


PROBES = (
    ("skill-not", _skill),
    ("bitwarden-purge", _bitwarden),
    ("mattermost-scope", _mattermost),
    ("email-multipart", _email),
    ("cron-subject", _cron_subject),
    ("session-delivery", _session_delivery),
    ("line-archive", _line),
    ("send-subject", _send_subject),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", type=Path, required=True)
    args = parser.parse_args(argv)
    home = args.home.resolve()
    if Path(os.environ.get("HERMES_HOME", "")).resolve() != home:
        print(json.dumps({"ok": False, "failed": ["HERMES_HOME does not match --home"]}))
        return 1
    failed = []
    for name, probe in PROBES:
        try:
            probe(home)
        except Exception as exc:  # health: allow BLE001 -- one KEEP probe must not hide the next
            failed.append(f"{name}:{type(exc).__name__}:{exc}")
    print(json.dumps({"ok": not failed, "failed": failed}, sort_keys=True))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
