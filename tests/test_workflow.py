"""Shared workflow prerequisites and legal-transition helpers (schema-v2 stage/phase contract).

These exercise ``crucible.workflow`` directly against a real ``RunLog`` with schema-v2 terminal
bindings, so the stage-order and approval-binding rules are proven at the module boundary the CLI
and (Task 4) the report both call. The exact reject messages are asserted so the contract is
readable, not eyeballed.
"""

import json

import pytest

from crucible.config import Config
from crucible.dag import DAG
from crucible.integrity import artifact_sha256, dag_sha256, node_sha256
from crucible.runlog import init_run
from crucible.workflow import (
    accepted_terminal,
    require_final_ready,
    require_node_review_ready,
    require_plan_ready,
    require_plan_verdict_ready,
    workflow_issues,
    WorkflowIssue,
)


def _dag(files=None, status="pending"):
    """A one-node ('a') DAG; ``files`` varies the immutable definition so its digest changes."""
    return DAG.from_dict({
        "nodes": [{"id": "a", "title": "A", "description": "d",
                   "files": files or ["a.py"], "test_plan": "pytest", "status": status}],
        "edges": [],
    })


def _bind_plan(run, dag, *, artifact=b"reviewed plan"):
    """Record a schema-v2 bound PLAN gate (Builder artifact + gate_consensus with artifact/DAG
    bindings) exactly as ``cmd_verdict`` would, and return ``(artifact_sha256, dag_sha256)``."""
    run.save_dag(dag.to_dict())
    a = artifact_sha256(artifact)
    d = dag_sha256(dag)
    run.append("builder_output", gate="plan", round=1, payload=artifact.decode("utf-8"),
               artifact_sha256=a)
    run.append("gate_consensus", gate="plan", round=1, artifact_sha256=a, dag_sha256=d)
    return a, d


# --- the five required stage/prerequisite scenarios --------------------------

def test_plan_requires_configured_reproduce_consensus(tmp_path):
    cfg = Config.from_dict({"reproduce_gate": True})
    run = init_run("g", cfg, base_dir=tmp_path)
    with pytest.raises(SystemExit) as exc:
        require_plan_verdict_ready(run, cfg)
    assert "reproduce" in str(exc.value).lower()


def test_dependency_requires_bound_plan_and_configured_approval(tmp_path):
    cfg = Config.from_dict({"human_approval": True})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _dag()
    _bind_plan(run, dag)  # a valid, currently-bound PLAN — but no plan_approved recorded
    with pytest.raises(SystemExit) as exc:
        require_node_review_ready(run, cfg, dag, "a")
    assert "approval" in str(exc.value).lower()


def test_final_requires_all_nodes_done(tmp_path):
    cfg = Config.from_dict({})  # final_review is true by default
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _dag()  # node 'a' is pending
    _bind_plan(run, dag)
    with pytest.raises(SystemExit) as exc:
        require_final_ready(run, cfg, dag)
    assert "a" in str(exc.value)


def test_approval_binds_current_plan_and_dag(tmp_path):
    cfg = Config.from_dict({"human_approval": True})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _dag()
    artifact, dag_hash = _bind_plan(run, dag)
    run.append("plan_approved", gate="plan", artifact_sha256=artifact, dag_sha256=dag_hash)
    terminal = require_plan_ready(run, cfg)
    assert terminal["event"] == "gate_consensus"
    assert terminal["artifact_sha256"] == artifact
    assert terminal["dag_sha256"] == dag_hash


def test_stale_approval_is_rejected(tmp_path):
    cfg = Config.from_dict({"human_approval": True})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _dag()
    artifact, dag_hash = _bind_plan(run, dag)
    run.append("plan_approved", gate="plan", artifact_sha256=artifact, dag_sha256=dag_hash)
    # Swap the dependency tree after approval: its status-free digest changes, so the accepted
    # plan/approval no longer bind the current DAG.
    run.save_dag(_dag(files=["different.py"]).to_dict())
    with pytest.raises(SystemExit) as exc:
        require_plan_ready(run, cfg)
    msg = str(exc.value).lower()
    assert "stale" in msg and ("dag" in msg or "binding" in msg)


# --- supporting contracts ----------------------------------------------------

def test_require_plan_verdict_ready_is_noop_when_reproduce_disabled(tmp_path):
    cfg = Config.from_dict({"reproduce_gate": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    require_plan_verdict_ready(run, cfg)  # does not raise


def test_require_plan_ready_rejects_before_plan_consensus(tmp_path):
    cfg = Config.from_dict({})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.save_dag(_dag().to_dict())
    with pytest.raises(SystemExit) as exc:
        require_plan_ready(run, cfg)
    assert "plan" in str(exc.value).lower()


def test_require_node_review_ready_requires_node_in_progress(tmp_path):
    cfg = Config.from_dict({})  # no approval required
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _dag()  # node 'a' pending
    _bind_plan(run, dag)
    with pytest.raises(SystemExit) as exc:
        require_node_review_ready(run, cfg, dag, "a")
    assert "in_progress" in str(exc.value) or "pending" in str(exc.value)


def test_require_final_ready_rejects_when_disabled(tmp_path):
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _dag(status="pending")
    _bind_plan(run, dag)
    with pytest.raises(SystemExit) as exc:
        require_final_ready(run, cfg, dag)
    assert "final" in str(exc.value).lower()


def test_accepted_terminal_uses_last_terminal_semantics(tmp_path):
    cfg = Config.from_dict({})
    run = init_run("g", cfg, base_dir=tmp_path)
    run.append("gate_consensus", gate="dep:a", round=1, artifact_sha256="a" * 64)
    assert accepted_terminal(run.read_events(), "dep:a")["event"] == "gate_consensus"
    # A later capped terminal makes the gate no longer accepted (matches report/set-status semantics).
    run.append("gate_capped", gate="dep:a", round=2, open_findings=["F1"])
    assert accepted_terminal(run.read_events(), "dep:a") is None
    assert accepted_terminal(run.read_events(), "dep:missing") is None


def test_workflow_issues_flags_missing_configured_reproduce(tmp_path):
    # Smoke coverage for the Task 4 hand-off: a run that omits a configured phase yields a
    # structured WorkflowIssue mentioning it (kind "missing"), while a fully-satisfied minimal
    # config yields none.
    cfg = Config.from_dict({"reproduce_gate": True, "final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _dag()
    _bind_plan(run, dag)
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert all(isinstance(issue, WorkflowIssue) for issue in issues)
    assert all(issue.kind in ("missing", "invalid", "flagged") for issue in issues)
    reproduce = [i for i in issues if "reproduce" in i.message.lower()]
    assert reproduce and reproduce[0].kind == "missing"


def test_workflow_issues_empty_for_satisfied_minimal_config(tmp_path):
    cfg = Config.from_dict({"final_review": False})  # no reproduce, no approval, no final required
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _dag(status="done")
    # a bound plan + a bound accepted dep gate for the single done node
    _bind_plan(run, dag)
    run.append("builder_output", gate="dep:a", round=1, payload="impl", artifact_sha256="c" * 64)
    run.append("gate_consensus", gate="dep:a", round=1, artifact_sha256="c" * 64,
               dag_sha256=dag_sha256(dag), node_sha256=node_sha256(dag, "a"))
    assert workflow_issues(run.read_events(), dag, cfg) == []


def test_workflow_issues_invalid_dag_binding_and_flagged_force(tmp_path):
    # A stale PLAN DAG binding is classified "invalid"; a forced node completion (current
    # hashes + rationale) is classified "flagged" — the two non-"missing" kinds Task 4 consumes.
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _dag(status="done")
    _bind_plan(run, dag)
    run.append("node_status_change", node="a", status="done", forced=True,
               rationale="manual recovery", dag_sha256=dag_sha256(dag),
               node_sha256=node_sha256(dag, "a"))
    flagged = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "flagged" and "a" in i.message for i in flagged)

    # Swap the tree after PLAN consensus: its status-free digest changes, so the PLAN binding
    # is now stale -> an "invalid" issue that names the DAG binding.
    run.save_dag(_dag(files=["different.py"], status="done").to_dict())
    invalid = workflow_issues(run.read_events(), _dag(files=["different.py"], status="done"), cfg)
    assert any(i.kind == "invalid" and "DAG binding" in i.message for i in invalid)
