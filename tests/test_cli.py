import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(args):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "scripts")}
    return subprocess.run(
        [sys.executable, "-m", "crucible", *args],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )


def test_init_run_prints_run_dir(tmp_path):
    r = _run(["init-run", "--goal", "Add caching", "--base-dir", str(tmp_path)])
    assert r.returncode == 0, r.stderr
    run_dir = Path(r.stdout.strip())
    assert run_dir.exists()
    assert (run_dir / "config.json").exists()


def test_full_dry_run_flow(tmp_path):
    r = _run(["init-run", "--goal", "Add caching", "--base-dir", str(tmp_path)])
    run_dir = r.stdout.strip()

    dag = {
        "nodes": [
            {"id": "a", "title": "A", "description": "", "files": [], "test_plan": "", "status": "pending"},
            {"id": "b", "title": "B", "description": "", "files": [], "test_plan": "", "status": "pending"},
        ],
        "edges": [{"from": "b", "depends_on": "a"}],
    }
    dag_file = Path(tmp_path) / "dag.json"
    dag_file.write_text(json.dumps(dag))
    r = _run(["load-dag", "--run", run_dir, "--file", str(dag_file)])
    assert r.returncode == 0, r.stderr

    r = _run(["next", "--run", run_dir])
    assert r.stdout.strip() == "a"

    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({"gate": "dep:a", "round": 1, "verdict": "APPROVE", "summary": "ok", "findings": []}))
    r = _run(["verdict", "--run", run_dir, "--gate", "dep:a", "--round", "1", "--max-rounds", "5", "--file", str(vfile)])
    assert "CONSENSUS" in r.stdout

    r = _run(["set-status", "--run", run_dir, "--node", "a", "--status", "done"])
    assert r.returncode == 0, r.stderr
    r = _run(["next", "--run", run_dir])
    assert r.stdout.strip() == "b"

    r = _run(["report", "--run", run_dir])
    assert "Add caching" in r.stdout


def test_verdict_capped_outcome(tmp_path):
    r = _run(["init-run", "--goal", "g", "--base-dir", str(tmp_path)])
    run_dir = r.stdout.strip()
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({
        "gate": "plan", "round": 5, "verdict": "REQUEST_CHANGES", "summary": "still broken",
        "findings": [{"id": "F1", "severity": "blocker", "location": "x", "claim": "c", "suggestion": "s"}],
    }))
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "5", "--max-rounds", "5", "--file", str(vfile)])
    assert "CAPPED" in r.stdout


def test_log_appends_full_payload(tmp_path):
    r = _run(["init-run", "--goal", "g", "--base-dir", str(tmp_path)])
    run_dir = r.stdout.strip()
    payload = Path(tmp_path) / "out.txt"
    payload.write_text("B" * 3000)
    r = _run(["log", "--run", run_dir, "--event", "builder_output", "--gate", "plan", "--round", "1", "--file", str(payload)])
    assert r.returncode == 0, r.stderr
    events = [json.loads(l) for l in (Path(run_dir) / "runlog.jsonl").read_text().splitlines() if l.strip()]
    assert events[-1]["payload"] == "B" * 3000


def test_verdict_cap_from_config_when_max_rounds_omitted(tmp_path):
    cfg = Path(tmp_path) / "c.json"
    cfg.write_text(json.dumps({"max_rounds_plan": 1}))
    r = _run(["init-run", "--goal", "g", "--base-dir", str(tmp_path), "--config", str(cfg)])
    run_dir = r.stdout.strip()
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({
        "gate": "plan", "round": 1, "verdict": "REQUEST_CHANGES", "summary": "x",
        "findings": [{"id": "F1", "severity": "blocker", "location": "x", "claim": "c", "suggestion": "s"}],
    }))
    # no --max-rounds: cap should come from config (max_rounds_plan=1) -> CAPPED at round 1
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "1", "--file", str(vfile)])
    assert "CAPPED" in r.stdout, r.stdout + r.stderr


def test_verdict_rejects_gate_mismatch(tmp_path):
    r = _run(["init-run", "--goal", "g", "--base-dir", str(tmp_path)])
    run_dir = r.stdout.strip()
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({"gate": "plan", "round": 1, "verdict": "APPROVE", "summary": "", "findings": []}))
    r = _run(["verdict", "--run", run_dir, "--gate", "dep:a", "--round", "1", "--file", str(vfile)])
    assert r.returncode != 0
    assert "does not match" in r.stderr


def test_verdict_resolutions_and_raw_are_logged(tmp_path):
    r = _run(["init-run", "--goal", "g", "--base-dir", str(tmp_path)])
    run_dir = r.stdout.strip()
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({
        "gate": "plan", "round": 1, "verdict": "REQUEST_CHANGES", "summary": "x",
        "findings": [{"id": "F1", "severity": "major", "location": "x", "claim": "c", "suggestion": "s"}],
    }))
    res = Path(tmp_path) / "res.json"
    res.write_text(json.dumps({"F1": "wontfix"}))
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "1", "--file", str(vfile), "--resolutions", str(res)])
    assert "CONSENSUS" in r.stdout, r.stdout + r.stderr
    events = [json.loads(l) for l in (Path(run_dir) / "runlog.jsonl").read_text().splitlines() if l.strip()]
    kinds = [e["event"] for e in events]
    assert "builder_resolution" in kinds
    cv = [e for e in events if e["event"] == "critic_verdict"][-1]
    assert "raw" in cv and "REQUEST_CHANGES" in cv["raw"]  # full raw verdict text retained


def test_verdict_rejects_invalid_resolution(tmp_path):
    r = _run(["init-run", "--goal", "g", "--base-dir", str(tmp_path)])
    run_dir = r.stdout.strip()
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({"gate": "plan", "round": 1, "verdict": "APPROVE", "summary": "", "findings": []}))
    res = Path(tmp_path) / "res.json"
    res.write_text(json.dumps({"F1": "ignore-it"}))
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "1", "--file", str(vfile), "--resolutions", str(res)])
    assert r.returncode != 0
    assert "invalid resolution" in r.stderr
