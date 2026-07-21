"""Cron delivery must remain inside the job's profile secret scope."""

import pytest

from agent.secret_scope import UnscopedSecretError, get_secret, set_multiplex_active
import cron.scheduler as scheduler


def test_run_one_job_keeps_profile_scope_through_delivery(monkeypatch, tmp_path):
    set_multiplex_active(True)
    monkeypatch.setattr(
        "agent.secret_scope.build_profile_secret_scope",
        lambda _home: {"EMAIL_PASSWORD": "scoped-password"},
    )
    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(scheduler, "mark_execution_running", lambda _execution_id: None)
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda job, defer_agent_teardown: (True, "output", "deliver me", None),
    )
    monkeypatch.setattr(scheduler, "save_job_output", lambda *_args: tmp_path / "output.md")
    monkeypatch.setattr(scheduler, "_is_interrupted", lambda _job_id: False)
    monkeypatch.setattr(scheduler, "_consume_interrupted_flag", lambda _job_id: False)
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduler, "finish_execution", lambda *_args, **_kwargs: None)

    seen = {}

    def fake_deliver(*_args, **_kwargs):
        seen["password"] = get_secret("EMAIL_PASSWORD")
        return None

    monkeypatch.setattr(scheduler, "_deliver_result", fake_deliver)

    try:
        assert scheduler.run_one_job(
            {"id": "job-1", "name": "Scoped job", "execution_id": "exec-1"}
        ) is True
        assert seen["password"] == "scoped-password"
        with pytest.raises(UnscopedSecretError):
            get_secret("EMAIL_PASSWORD")
    finally:
        set_multiplex_active(False)
