from pathlib import Path

from agent.memory_tree_build import collect_cron_records, iter_recent_cron_outputs


def test_iter_recent_cron_outputs_skips_file_removed_during_scan(tmp_path, monkeypatch):
    home = tmp_path
    output = home / "cron" / "output" / "job1"
    output.mkdir(parents=True)
    vanished = output / "gone.md"
    vanished.write_text("gone", encoding="utf-8")
    kept = output / "kept.md"
    kept.write_text("kept", encoding="utf-8")

    original_stat = Path.stat
    stat_calls = {vanished: 0}

    def flaky_stat(self, *args, **kwargs):
        if self == vanished:
            stat_calls[vanished] += 1
            if stat_calls[vanished] >= 2:
                raise FileNotFoundError(str(self))
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)

    assert iter_recent_cron_outputs(home, 10) == [kept]


def test_collect_cron_records_skips_file_removed_after_discovery(tmp_path, monkeypatch):
    home = tmp_path
    output = home / "cron" / "output" / "job1"
    output.mkdir(parents=True)
    vanished = output / "gone.md"
    vanished.write_text("gone", encoding="utf-8")

    monkeypatch.setattr("agent.memory_tree_build.iter_recent_cron_outputs", lambda home, limit: [vanished])
    vanished.unlink()

    assert collect_cron_records(home, limit=10) == []
