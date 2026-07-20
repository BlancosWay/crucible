import json
import os
import subprocess
import sys
from pathlib import Path

from crucible.config import Config
from crucible.runlog import RunLog, init_run


ROOT = Path(__file__).resolve().parents[1]


def _run(args):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "scripts")}
    return subprocess.run(
        [sys.executable, "-m", "crucible", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
    )


def _set_workflow(run_dir, workflow):
    path = Path(run_dir) / "runlog.jsonl"
    lines = path.read_text().splitlines()
    start = json.loads(lines[0])
    start["workflow"] = workflow
    lines[0] = json.dumps(start)
    path.write_text("\n".join(lines) + "\n")


def test_single_verdict_cannot_certify_symmetric_workflow(tmp_path):
    # The symmetric workflow is selected the real way — `init-run --workflow pr-review` — not by
    # hand-editing the log, so this proves the recorded run metadata (not a test hack) routes the
    # single-attestation `verdict` command to the two-peer `symmetric-verdict` command.
    proc = _run([
        "init-run", "--goal", "symmetric review", "--workflow", "pr-review",
        "--base-dir", str(tmp_path),
    ])
    assert proc.returncode == 0, proc.stderr
    run_path = Path(proc.stdout.strip())
    dag_file = tmp_path / "dag.json"
    dag_file.write_text(json.dumps({
        "nodes": [{
            "id": "review",
            "title": "Review",
            "description": "",
            "files": [],
            "test_plan": "",
            "status": "pending",
        }],
        "edges": [],
    }))
    assert _run([
        "load-dag", "--run", str(run_path), "--file", str(dag_file),
    ]).returncode == 0
    plan = tmp_path / "plan.md"
    plan.write_text("review plan")
    assert _run([
        "log", "--run", str(run_path), "--event", "builder_output",
        "--gate", "plan", "--round", "1", "--file", str(plan),
    ]).returncode == 0
    bindings = json.loads(_run([
        "bindings", "--run", str(run_path), "--gate", "plan", "--round", "1",
    ]).stdout)
    verdict = tmp_path / "verdict.json"
    verdict.write_text(json.dumps({
        "gate": "plan",
        "round": 1,
        "verdict": "APPROVE",
        "summary": "one unsigned approval",
        "findings": [],
        **bindings,
    }))

    result = _run([
        "verdict", "--run", str(run_path), "--gate", "plan",
        "--round", "1", "--file", str(verdict),
    ])

    assert result.returncode != 0
    assert "symmetric-verdict" in result.stderr


def test_review_result_derives_request_changes_from_accepted_blocker(tmp_path):
    run = init_run("accepted blocker", Config.from_dict({}), base_dir=tmp_path)
    _set_workflow(run.path, "pr-review")
    run.append(
        "accepted_finding_set",
        gate="dep:auth",
        round=1,
        payload={
            "summary": "accepted findings",
            "findings": [{
                "source_gate": "dep:auth",
                "id": "F1",
                "severity": "major",
                "location": "src/auth.py:42",
                "claim": "Expired refresh tokens are accepted.",
                "suggestion": "Reject expired refresh tokens.",
            }],
        },
        artifact_sha256="a" * 64,
        dag_sha256="d" * 64,
        node_sha256="n" * 64,
    )

    result = _run(["review-result", "--run", str(run.path)])

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["workflow"] == "pr-review"
    assert data["recommendation"] == "REQUEST_CHANGES"
    assert data["findings"][0]["id"] == "F1"
