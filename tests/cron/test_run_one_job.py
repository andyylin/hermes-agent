"""Characterization + unit tests for the `run_one_job` shared helper (Phase 4A).

`tick`'s per-job body (`_process_job`) is the execute → save → deliver → mark
sequence that fires ONE due job. Phase 4A extracts it into a module-level
`run_one_job(job, *, adapters=None, loop=None, verbose=False)` so the external
Chronos provider's `fire_due` can reuse the IDENTICAL body — no duplicated
correctness.

The first test characterizes the sequence as driven through `tick()` (proving
the extraction didn't change `tick`'s behavior); the rest unit-test the
extracted helper directly.
"""
import cron.scheduler as s


def _patch_pipeline(monkeypatch, *, success=True, output="out", final="final response",
                    error=None, silent_marker_in=None):
    """Patch the job pipeline primitives and record the call order."""
    calls = []

    def fake_run_job(job):
        calls.append(("run_job", job["id"]))
        fr = final if silent_marker_in is None else silent_marker_in
        return (success, output, fr, error)

    def fake_save(jid, out):
        calls.append(("save", jid))
        return f"/tmp/{jid}.txt"

    def fake_deliver(job, content, adapters=None, loop=None):
        calls.append(("deliver", job["id"]))
        return None

    def fake_mark(jid, ok, err=None, delivery_error=None):
        calls.append(("mark", jid, ok))

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", fake_save)
    monkeypatch.setattr(s, "_deliver_result", fake_deliver)
    monkeypatch.setattr(s, "mark_job_run", fake_mark)
    return calls


def test_tick_process_job_sequence(monkeypatch):
    """Characterization: a single due job driven through tick() runs the
    sequence run_job → save → deliver → mark, in that order."""
    calls = _patch_pipeline(monkeypatch)
    monkeypatch.setattr(s, "get_due_jobs", lambda: [{"id": "j1", "name": "t"}])
    monkeypatch.setattr(s, "advance_next_run", lambda jid: True)

    s.tick(verbose=False, sync=True)

    assert [c[0] for c in calls] == ["run_job", "save", "deliver", "mark"]
    assert calls[-1] == ("mark", "j1", True)


def test_run_one_job_success_sequence(monkeypatch):
    """The extracted helper runs the same execute→save→deliver→mark sequence
    for a successful job."""
    calls = _patch_pipeline(monkeypatch)

    ok = s.run_one_job({"id": "j2", "name": "t"})

    assert ok is True
    assert [c[0] for c in calls] == ["run_job", "save", "deliver", "mark"]
    assert calls[-1] == ("mark", "j2", True)


def test_run_one_job_silent_skips_delivery(monkeypatch):
    """A [SILENT] final response saves output + marks the run but does NOT
    deliver."""
    calls = _patch_pipeline(monkeypatch, silent_marker_in="[SILENT]")

    s.run_one_job({"id": "j3", "name": "t"})

    kinds = [c[0] for c in calls]
    assert "run_job" in kinds and "save" in kinds and "mark" in kinds
    assert "deliver" not in kinds


def test_run_one_job_empty_response_is_soft_failure(monkeypatch):
    """An empty final response marks the run as NOT ok (issue #8585)."""
    calls = _patch_pipeline(monkeypatch, final="   ")

    s.run_one_job({"id": "j4", "name": "t"})

    mark = [c for c in calls if c[0] == "mark"][0]
    assert mark == ("mark", "j4", False)


def test_run_one_job_failed_job_delivers_error(monkeypatch):
    """A failed job still delivers when repair-gate routing is disabled."""
    calls = _patch_pipeline(monkeypatch, success=False, final="", error="boom")
    monkeypatch.setattr(s, "_cron_repair_gate_alert_routing_enabled", lambda: False)

    s.run_one_job({"id": "j5", "name": "t"})

    kinds = [c[0] for c in calls]
    assert "deliver" in kinds
    mark = [c for c in calls if c[0] == "mark"][0]
    assert mark == ("mark", "j5", False)


def test_run_one_job_failed_job_routes_to_repair_gate_when_available(monkeypatch):
    """All source job failures are held for Supervisor when the bridge is healthy."""
    calls = _patch_pipeline(monkeypatch, success=False, final="", error="boom")
    monkeypatch.setattr(s, "_cron_repair_gate_alert_routing_enabled", lambda: True)
    monkeypatch.setattr(s, "_cron_repair_gate_available", lambda: True)

    s.run_one_job({"id": "j5b", "name": "t"})

    kinds = [c[0] for c in calls]
    assert "deliver" not in kinds
    mark = [c for c in calls if c[0] == "mark"][0]
    assert mark == ("mark", "j5b", False)


def test_run_one_job_failed_job_delivers_when_repair_gate_unavailable(monkeypatch):
    """If the Supervisor bridge is broken, source jobs fall back to direct email."""
    calls = _patch_pipeline(monkeypatch, success=False, final="", error="boom")
    monkeypatch.setattr(s, "_cron_repair_gate_alert_routing_enabled", lambda: True)
    monkeypatch.setattr(s, "_cron_repair_gate_available", lambda: False)

    s.run_one_job({"id": "j5c", "name": "t"})

    kinds = [c[0] for c in calls]
    assert "deliver" in kinds
    mark = [c for c in calls if c[0] == "mark"][0]
    assert mark == ("mark", "j5c", False)


def test_run_one_job_warning_output_routes_to_repair_gate_when_available(monkeypatch):
    """Alert-shaped successful output is held; normal successful reports deliver."""
    calls = _patch_pipeline(monkeypatch, success=True, final="Attention needed: upstream broken")
    monkeypatch.setattr(s, "_cron_repair_gate_alert_routing_enabled", lambda: True)
    monkeypatch.setattr(s, "_cron_repair_gate_available", lambda: True)

    s.run_one_job({"id": "j5d", "name": "t"})

    kinds = [c[0] for c in calls]
    assert "deliver" not in kinds
    mark = [c for c in calls if c[0] == "mark"][0]
    assert mark == ("mark", "j5d", True)


def test_run_one_job_exception_marks_failure(monkeypatch):
    """If run_job raises, the helper marks the run failed and returns False
    rather than propagating."""
    def boom(job):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(s, "run_job", boom)
    marks = []
    monkeypatch.setattr(
        s, "mark_job_run",
        lambda jid, ok, err=None, delivery_error=None: marks.append((jid, ok)),
    )

    ok = s.run_one_job({"id": "j6", "name": "t"})

    assert ok is False
    assert marks == [("j6", False)]
