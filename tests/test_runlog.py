import json

import pytest

from crucible.config import Config
from crucible.runlog import RunLog, RunLogCorruptError, init_run, slugify


def test_slugify_basic():
    assert slugify("Add a Rate Limiter!") == "add-a-rate-limiter"
    assert slugify("  multiple   spaces  ") == "multiple-spaces"
    assert slugify("UPPER/lower#mix") == "upper-lower-mix"


def test_init_run_creates_dir_and_files(tmp_path):
    cfg = Config.from_dict({})
    run = init_run("Add rate limiter", cfg, base_dir=tmp_path)
    assert run.path.exists()
    assert (run.path / "config.json").exists()
    events = run.read_events()
    assert events[0]["event"] == "run_start"
    assert events[0]["goal"] == "Add rate limiter"
    saved = json.loads((run.path / "config.json").read_text())
    assert saved == cfg.to_dict()


def test_append_event_is_append_only_and_full_text(tmp_path):
    cfg = Config.from_dict({})
    run = init_run("g", cfg, base_dir=tmp_path)
    big = "X" * 5000
    run.append("builder_output", gate="plan", round=1, payload=big)
    run.append("critic_verdict", gate="plan", round=1, payload={"verdict": "APPROVE"})
    events = run.read_events()
    assert events[-2]["event"] == "builder_output"
    assert events[-2]["payload"] == big
    assert events[-1]["payload"] == {"verdict": "APPROVE"}
    lines = (run.path / "runlog.jsonl").read_text().strip().splitlines()
    assert len(lines) == len(events)


def test_open_existing_run(tmp_path):
    cfg = Config.from_dict({})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.append("gate_consensus", gate="plan", round=2)
    reopened = RunLog(run.path)
    assert reopened.read_events()[-1]["event"] == "gate_consensus"


def test_save_and_load_dag(tmp_path):
    cfg = Config.from_dict({})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag_data = {
        "nodes": [{"id": "a", "title": "A", "description": "", "files": [], "test_plan": "", "status": "pending"}],
        "edges": [],
    }
    run.save_dag(dag_data)
    assert (run.path / "dag.json").exists()
    assert run.load_dag()["nodes"][0]["id"] == "a"


def test_two_runs_same_goal_same_second_do_not_collide(tmp_path):
    cfg = Config.from_dict({})
    a = init_run("Same Goal", cfg, base_dir=tmp_path)
    b = init_run("Same Goal", cfg, base_dir=tmp_path)
    assert a.path != b.path
    # each keeps its own independent run_start
    assert a.read_events()[0]["event"] == "run_start"
    assert b.read_events()[0]["event"] == "run_start"


def test_run_dir_slug_has_no_trailing_hyphen(tmp_path):
    # truncating the slug must not re-introduce a trailing hyphen in the run-dir name
    run = init_run("a" * 39 + " trailing", Config.from_dict({}), base_dir=tmp_path)
    assert not run.path.name.endswith("-")


# --- resilient read of the append-only log (M1) ------------------------------

def _run(tmp_path):
    return init_run("g", Config.from_dict({}), base_dir=tmp_path)


def test_read_events_recovers_from_torn_final_line(tmp_path, capsys):
    run = _run(tmp_path)
    run.append("gate_consensus", gate="plan", round=1)
    # simulate a crash mid-append: a partial final record with no trailing newline
    with (run.path / "runlog.jsonl").open("ab") as fh:
        fh.write(b'{"ts": "x", "event": "partial"')
    events = run.read_events()
    assert [e["event"] for e in events] == ["run_start", "gate_consensus"]
    assert "partial trailing record" in capsys.readouterr().err


def test_read_events_recovers_from_torn_utf8_tail(tmp_path, capsys):
    run = _run(tmp_path)
    with (run.path / "runlog.jsonl").open("ab") as fh:
        fh.write(b'{"event": "x", "title": "caf\xc3')  # truncated mid-UTF-8 sequence
    events = run.read_events()
    assert [e["event"] for e in events] == ["run_start"]
    assert "partial trailing record" in capsys.readouterr().err


def test_read_events_only_torn_line_returns_empty(tmp_path):
    run = RunLog(init_run("g", Config.from_dict({}), base_dir=tmp_path).path)
    (run.path / "runlog.jsonl").write_bytes(b'{"event": "partial"')
    assert run.read_events() == []


def test_read_events_raises_on_interior_corrupt_line(tmp_path):
    run = _run(tmp_path)
    (run.path / "runlog.jsonl").write_bytes(b'{"event": "a"}\nNOT_JSON\n{"event": "b"}\n')
    with pytest.raises(RunLogCorruptError, match="line 2"):
        run.read_events()


def test_read_events_raises_on_corrupt_final_line_with_newline(tmp_path):
    run = _run(tmp_path)
    (run.path / "runlog.jsonl").write_bytes(b'{"event": "a"}\nNOT_JSON\n')
    with pytest.raises(RunLogCorruptError, match="line 2"):
        run.read_events()


def test_read_events_raises_on_non_object_line(tmp_path):
    run = _run(tmp_path)
    (run.path / "runlog.jsonl").write_bytes(b'42\n')
    with pytest.raises(RunLogCorruptError):
        run.read_events()


def test_read_events_raises_on_missing_event_field(tmp_path):
    run = _run(tmp_path)
    (run.path / "runlog.jsonl").write_bytes(b'{"ts": "x"}\n')
    with pytest.raises(RunLogCorruptError):
        run.read_events()


def test_read_events_raises_on_complete_but_invalid_unterminated_final_records(tmp_path):
    # A record that PARSES as valid JSON but has the wrong shape is a COMPLETE write, so it
    # is real corruption even without a trailing newline — only torn (unparseable) tails are OK.
    for payload in (b"42", b"{}", b'{"ts": "x"}'):
        run = _run(tmp_path)
        (run.path / "runlog.jsonl").write_bytes(payload)  # no trailing newline
        with pytest.raises(RunLogCorruptError):
            run.read_events()
