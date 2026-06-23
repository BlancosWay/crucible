import json

from crucible.config import Config
from crucible.runlog import RunLog, init_run, slugify


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
