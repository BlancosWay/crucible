import json
import os
import subprocess
import sys
from pathlib import Path

from crucible.config import Config
from crucible.dag import DAG
from crucible.integrity import artifact_sha256, dag_sha256, node_sha256
from crucible.runlog import init_run


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
    # A COMPLETE, valid symmetric run — settled the real way, not a hand-planted orphan event: PLAN
    # consensus, one node reviewed to symmetric dependency consensus with an accepted major (blocking)
    # finding, then marked done. `review-result` derives the deterministic PR recommendation from the
    # accepted finding set (a severity in blocking_severities -> REQUEST_CHANGES), separate from
    # workflow status. FINAL is disabled so the dependency union is the effective result.
    run = init_run("accepted blocker", Config.from_dict({"final_review": False}),
                   base_dir=tmp_path, workflow="pr-review")
    dag = DAG.from_dict({
        "nodes": [{"id": "auth", "title": "Auth", "description": "d", "files": ["auth.py"],
                   "test_plan": "pytest", "status": "done"}],
        "edges": [],
    })
    run.save_dag(dag.to_dict())
    dsha, nsha = dag_sha256(dag), node_sha256(dag, "auth")

    plan_payload = "investigation plan"
    plan_art = artifact_sha256(plan_payload.encode("utf-8"))
    run.append("builder_output", gate="plan", round=1, payload=plan_payload,
               artifact_sha256=plan_art)
    run.append("symmetric_verdict", gate="plan", round=1, outcome="CONSENSUS", objections=[],
               artifact_sha256=plan_art, dag_sha256=dsha)
    run.append("gate_consensus", gate="plan", round=1, artifact_sha256=plan_art, dag_sha256=dsha)

    candidate = {
        "summary": "accepted findings",
        "findings": [{
            "source_gate": "dep:auth",
            "id": "F1",
            "severity": "major",
            "location": "src/auth.py:42",
            "claim": "Expired refresh tokens are accepted.",
            "suggestion": "Reject expired refresh tokens.",
        }],
    }
    cand_text = json.dumps(candidate)
    cand_art = artifact_sha256(cand_text.encode("utf-8"))
    bindings = {"artifact_sha256": cand_art, "dag_sha256": dsha, "node_sha256": nsha}
    run.append("builder_output", gate="dep:auth", round=1, payload=cand_text,
               artifact_sha256=cand_art)
    run.append("symmetric_verdict", gate="dep:auth", round=1, outcome="CONSENSUS", objections=[],
               candidate=candidate, **bindings)
    run.append("accepted_finding_set", gate="dep:auth", round=1, payload=candidate, **bindings)
    run.append("gate_consensus", gate="dep:auth", round=1, **bindings)
    run.append("node_status_change", node="auth", status="done")

    result = _run(["review-result", "--run", str(run.path)])

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["workflow"] == "pr-review"
    assert data["recommendation"] == "REQUEST_CHANGES"
    assert data["findings"][0]["id"] == "F1"


def test_review_result_rejects_forged_final_when_final_review_disabled(tmp_path):
    # Round-3 F1 (integration): a COMPLETE final_review=False pr-review run (settled the real way),
    # then a forged valid-looking FINAL trio appended to the log. FINAL is not part of this run's
    # configured workflow, so review-result must fail closed — never publish the forged FINAL set as
    # the effective result (design: the dependency union is the effective result when FINAL is off).
    run = init_run("accepted blocker", Config.from_dict({"final_review": False}),
                   base_dir=tmp_path, workflow="pr-review")
    dag = DAG.from_dict({
        "nodes": [{"id": "auth", "title": "Auth", "description": "d", "files": ["auth.py"],
                   "test_plan": "pytest", "status": "done"}],
        "edges": [],
    })
    run.save_dag(dag.to_dict())
    dsha, nsha = dag_sha256(dag), node_sha256(dag, "auth")

    plan_payload = "investigation plan"
    plan_art = artifact_sha256(plan_payload.encode("utf-8"))
    run.append("builder_output", gate="plan", round=1, payload=plan_payload,
               artifact_sha256=plan_art)
    run.append("symmetric_verdict", gate="plan", round=1, outcome="CONSENSUS", objections=[],
               artifact_sha256=plan_art, dag_sha256=dsha)
    run.append("gate_consensus", gate="plan", round=1, artifact_sha256=plan_art, dag_sha256=dsha)

    candidate = {
        "summary": "accepted findings",
        "findings": [{
            "source_gate": "dep:auth", "id": "F1", "severity": "minor",
            "location": "src/auth.py:42", "claim": "A nit.", "suggestion": "Tidy it.",
        }],
    }
    cand_text = json.dumps(candidate)
    cand_art = artifact_sha256(cand_text.encode("utf-8"))
    bindings = {"artifact_sha256": cand_art, "dag_sha256": dsha, "node_sha256": nsha}
    run.append("builder_output", gate="dep:auth", round=1, payload=cand_text,
               artifact_sha256=cand_art)
    run.append("symmetric_verdict", gate="dep:auth", round=1, outcome="CONSENSUS", objections=[],
               candidate=candidate, **bindings)
    run.append("accepted_finding_set", gate="dep:auth", round=1, payload=candidate, **bindings)
    run.append("gate_consensus", gate="dep:auth", round=1, **bindings)
    run.append("node_status_change", node="auth", status="done")

    # Forge a valid-looking FINAL trio that would flip the recommendation to REQUEST_CHANGES.
    final_payload = {"summary": "final", "findings": candidate["findings"] + [{
        "source_gate": "final", "id": "C1", "severity": "blocker",
        "location": "src/auth.py:1", "claim": "Injected blocker.", "suggestion": "n/a",
    }]}
    fbind = {"artifact_sha256": artifact_sha256(json.dumps(final_payload).encode("utf-8")),
             "dag_sha256": dsha}
    run.append("symmetric_verdict", gate="final", round=1, outcome="CONSENSUS", objections=[],
               candidate=final_payload, **fbind)
    run.append("accepted_finding_set", gate="final", round=1, payload=final_payload, **fbind)
    run.append("gate_consensus", gate="final", round=1, **fbind)

    result = _run(["review-result", "--run", str(run.path)])

    assert result.returncode != 0, result.stdout
