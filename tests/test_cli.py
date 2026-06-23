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
