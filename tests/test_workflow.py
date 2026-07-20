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
    require_reproduce_ready,
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
    impl = artifact_sha256(b"impl")
    run.append("builder_output", gate="dep:a", round=1, payload="impl", artifact_sha256=impl)
    run.append("gate_consensus", gate="dep:a", round=1, artifact_sha256=impl,
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


def test_workflow_issues_flags_unbound_terminal_artifact(tmp_path):
    # A PLAN terminal whose artifact_sha256 has no same-gate/same-round builder_output to bind it is
    # an "invalid" artifact-binding issue (the accepted decision refers to no reviewed artifact).
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _dag(status="done")
    run.save_dag(dag.to_dict())
    # PLAN consensus is DAG-bound and carries an artifact hash, but NO builder_output backs it.
    run.append("gate_consensus", gate="plan", round=1, artifact_sha256="a" * 64,
               dag_sha256=dag_sha256(dag))
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "PLAN" in i.message for i in issues)


def test_workflow_issues_flags_stale_artifact_binding(tmp_path):
    # The PLAN terminal's artifact_sha256 disagrees with the bytes of its same-gate/same-round
    # builder_output payload -> an "invalid" artifact-binding issue.
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _dag(status="done")
    run.save_dag(dag.to_dict())
    run.append("builder_output", gate="plan", round=1, payload="reviewed plan",
               artifact_sha256=artifact_sha256(b"reviewed plan"))
    run.append("gate_consensus", gate="plan", round=1, artifact_sha256="b" * 64,
               dag_sha256=dag_sha256(dag))
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "PLAN" in i.message for i in issues)


def test_workflow_issues_flags_out_of_order_final(tmp_path):
    # FINAL recorded before the node's dependency terminal / done transition, though the tree ends
    # done, is an "invalid" out-of-order issue naming FINAL (log order, not just current DAG state).
    cfg = Config.from_dict({"final_review": True})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _dag(status="done")
    _bind_plan(run, dag)
    fa = artifact_sha256(b"final artifact")
    run.append("builder_output", gate="final", round=1, payload="final artifact", artifact_sha256=fa)
    run.append("gate_consensus", gate="final", round=1, artifact_sha256=fa, dag_sha256=dag_sha256(dag))
    # The dependency terminal and the node's done transition are appended AFTER the FINAL terminal.
    da = artifact_sha256(b"impl a")
    run.append("builder_output", gate="dep:a", round=1, payload="impl a", artifact_sha256=da)
    run.append("gate_consensus", gate="dep:a", round=1, artifact_sha256=da,
               dag_sha256=dag_sha256(dag), node_sha256=node_sha256(dag, "a"))
    run.append("node_status_change", node="a", status="done",
               dag_sha256=dag_sha256(dag), node_sha256=node_sha256(dag, "a"))
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "FINAL" in i.message for i in issues)


def test_workflow_issues_flags_dep_terminal_after_done(tmp_path):
    # A dependency terminal recorded AFTER the node's non-forced done transition is out of order
    # (the node was marked done before its review reached consensus) -> "invalid".
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _dag(status="done")
    _bind_plan(run, dag)
    run.append("node_status_change", node="a", status="done",
               dag_sha256=dag_sha256(dag), node_sha256=node_sha256(dag, "a"))
    da = artifact_sha256(b"impl a")
    run.append("builder_output", gate="dep:a", round=1, payload="impl a", artifact_sha256=da)
    run.append("gate_consensus", gate="dep:a", round=1, artifact_sha256=da,
               dag_sha256=dag_sha256(dag), node_sha256=node_sha256(dag, "a"))
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "a" in i.message for i in issues)


def _bind_dep_a(run, dag, *, payload=b"impl a"):
    """Record a schema-v2 bound ``dep:a`` terminal (builder_output + gate_consensus) against ``dag``."""
    a = artifact_sha256(payload)
    run.append("builder_output", gate="dep:a", round=1, payload=payload.decode("utf-8"),
               artifact_sha256=a)
    run.append("gate_consensus", gate="dep:a", round=1, artifact_sha256=a,
               dag_sha256=dag_sha256(dag), node_sha256=node_sha256(dag, "a"))


def test_workflow_issues_flags_final_missing_dag_binding(tmp_path):
    # F3: a FINAL terminal with a valid artifact but NO dag_sha256 over a done, otherwise-valid tree
    # is an "invalid" DAG-binding issue naming FINAL (a final review not bound to the current tree).
    cfg = Config.from_dict({"final_review": True})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _dag(status="done")
    _bind_plan(run, dag)
    _bind_dep_a(run, dag)
    fa = artifact_sha256(b"final artifact")
    run.append("builder_output", gate="final", round=1, payload="final artifact", artifact_sha256=fa)
    run.append("gate_consensus", gate="final", round=1, artifact_sha256=fa)  # NO dag_sha256
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "FINAL" in i.message and "DAG binding" in i.message
               for i in issues)


def test_workflow_issues_flags_final_wrong_dag_binding(tmp_path):
    # F3: a FINAL terminal binding a WRONG dag_sha256 is an "invalid" DAG-binding issue naming FINAL.
    cfg = Config.from_dict({"final_review": True})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _dag(status="done")
    _bind_plan(run, dag)
    _bind_dep_a(run, dag)
    fa = artifact_sha256(b"final artifact")
    run.append("builder_output", gate="final", round=1, payload="final artifact", artifact_sha256=fa)
    run.append("gate_consensus", gate="final", round=1, artifact_sha256=fa, dag_sha256="0" * 64)
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "FINAL" in i.message and "DAG binding" in i.message
               for i in issues)


def test_workflow_issues_flags_artifact_after_terminal(tmp_path):
    # F4: a PLAN terminal whose only same-gate/same-round builder_output is appended AFTER the
    # terminal has no pre-terminal reviewed artifact -> "invalid" artifact-binding issue naming PLAN.
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _dag(status="done")
    run.save_dag(dag.to_dict())
    a = artifact_sha256(b"reviewed plan")
    run.append("gate_consensus", gate="plan", round=1, artifact_sha256=a, dag_sha256=dag_sha256(dag))
    run.append("builder_output", gate="plan", round=1, payload="reviewed plan", artifact_sha256=a)
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "PLAN" in i.message for i in issues)


def test_workflow_issues_ignores_post_terminal_builder_output(tmp_path):
    # F4 (pre-terminal-only guard): a valid pre-terminal PLAN artifact binds the terminal; a later
    # same-gate/same-round builder_output appended AFTER the terminal is ignored and does not turn a
    # satisfied minimal config into an issue (the exact terminal binding cannot be replaced/bypassed).
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _dag(status="done")
    _bind_plan(run, dag)
    _bind_dep_a(run, dag)
    run.append("builder_output", gate="plan", round=1, payload="tampered later plan",
               artifact_sha256=artifact_sha256(b"tampered later plan"))
    assert workflow_issues(run.read_events(), dag, cfg) == []


# --- F1: the REPRODUCE gate is accepted only when reproduce_gate is enabled ---

def test_require_reproduce_ready_rejects_when_disabled(tmp_path):
    # Design: "reproduce is accepted only when reproduce_gate: true." With it disabled the REPRODUCE
    # gate is not part of the configured workflow, so the shared readiness guard rejects it (the CLI
    # log/bindings/verdict handshake delegates here, so a forbidden gate never records or certifies).
    cfg = Config.from_dict({"reproduce_gate": False})
    with pytest.raises(SystemExit) as exc:
        require_reproduce_ready(cfg)
    assert "reproduce_gate" in str(exc.value)


def test_require_reproduce_ready_allows_when_enabled(tmp_path):
    cfg = Config.from_dict({"reproduce_gate": True})
    require_reproduce_ready(cfg)  # does not raise — REPRODUCE is Stage 0 of the configured workflow


def test_workflow_issues_flags_disabled_reproduce_terminal(tmp_path):
    # F1: reproduce_gate is disabled, yet a REPRODUCE gate reached a terminal in the log. That is a
    # configured-forbidden phase (an extra gate the workflow does not include) -> "invalid", so the
    # report can never certify such a run CLEAN even though every other phase is validly bound.
    cfg = Config.from_dict({"reproduce_gate": False, "final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _dag(status="done")
    _bind_plan(run, dag)
    ra = artifact_sha256(b"repro")
    run.append("builder_output", gate="reproduce", round=1, payload="repro", artifact_sha256=ra)
    run.append("gate_consensus", gate="reproduce", round=1, artifact_sha256=ra)
    _bind_dep_a(run, dag)  # the node is validly completed, so ONLY the forbidden REPRODUCE is at issue
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "reproduce" in i.message.lower() for i in issues)


def test_workflow_issues_flags_disabled_reproduce_even_when_capped(tmp_path):
    # F1: a disabled REPRODUCE gate that CAPPED (a terminal, not an advance) is still a forbidden
    # phase in the log -> "invalid" (the phase must not appear at all when reproduce_gate is off).
    cfg = Config.from_dict({"reproduce_gate": False, "final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _dag(status="done")
    _bind_plan(run, dag)
    run.append("gate_capped", gate="reproduce", round=1, open_findings=["F1"])
    _bind_dep_a(run, dag)
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "reproduce" in i.message.lower() for i in issues)


def test_workflow_issues_allows_reproduce_terminal_when_enabled(tmp_path):
    # Enabled behavior remains: a bound, accepted REPRODUCE terminal under reproduce_gate: true is not
    # flagged (it is the configured Stage 0), so a fully bound run stays clean.
    cfg = Config.from_dict({"reproduce_gate": True, "final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _dag(status="done")
    ra = artifact_sha256(b"repro")
    run.append("builder_output", gate="reproduce", round=1, payload="repro", artifact_sha256=ra)
    run.append("gate_consensus", gate="reproduce", round=1, artifact_sha256=ra)
    _bind_plan(run, dag)
    _bind_dep_a(run, dag)
    assert workflow_issues(run.read_events(), dag, cfg) == []


# --- F2: configured human approval must precede all dependency / FINAL work ---

def test_workflow_issues_flags_late_approval_after_dependency_work(tmp_path):
    # F2: human_approval is configured; the plan is approved and binds correctly, but the approval is
    # recorded AFTER the dependency was reviewed and the node marked done. Approval must gate
    # dependency work (design phase order: approval before dependency work), so an approval recorded
    # after that work is out of order -> "invalid", never CLEAN.
    cfg = Config.from_dict({"human_approval": True, "final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _dag(status="done")
    artifact, dag_hash = _bind_plan(run, dag)
    _bind_dep_a(run, dag)
    run.append("node_status_change", node="a", status="done",
               dag_sha256=dag_sha256(dag), node_sha256=node_sha256(dag, "a"))
    run.append("plan_approved", gate="plan", artifact_sha256=artifact, dag_sha256=dag_hash)
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "approval" in i.message.lower() for i in issues)


def test_workflow_issues_flags_approval_after_node_start(tmp_path):
    # F2: even a node_status_change to in_progress (status work) recorded before approval is out of
    # order — approval must precede ANY dependency/status work, not just a completed dependency.
    cfg = Config.from_dict({"human_approval": True, "final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _dag(status="in_progress")
    artifact, dag_hash = _bind_plan(run, dag)
    run.append("node_status_change", node="a", status="in_progress")
    run.append("plan_approved", gate="plan", artifact_sha256=artifact, dag_sha256=dag_hash)
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "approval" in i.message.lower() for i in issues)


def test_workflow_issues_allows_approval_before_dependency_work(tmp_path):
    # Valid-order guard: approval recorded BEFORE any dependency work keeps a fully bound, approved
    # run clean (no approval-ordering issue) — the F2 fix must not flag the legitimate order.
    cfg = Config.from_dict({"human_approval": True, "final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)
    dag = _dag(status="done")
    artifact, dag_hash = _bind_plan(run, dag)
    run.append("plan_approved", gate="plan", artifact_sha256=artifact, dag_sha256=dag_hash)
    _bind_dep_a(run, dag)
    assert workflow_issues(run.read_events(), dag, cfg) == []


# --- symmetric accepted-finding-set integrity (Task 3) -----------------------

def _af(source_gate="dep:a", fid="F1", severity="major"):
    return {"source_gate": source_gate, "id": fid, "severity": severity,
            "location": "src/a.py:1", "claim": "c", "suggestion": "s"}


def _symmetric_run(tmp_path, *, accepted="valid", final=None, verdict="match"):
    """A pr-review run with one done node 'a' backed by a symmetric dependency gate, plus optional
    FINAL. ``accepted`` selects a dependency accepted-set fault: ``"valid"``, ``"binding"`` (its
    bindings differ from the terminal), or ``"malformed"`` (its payload is not a valid finding set).
    ``verdict`` selects a peer-decision fault: ``"match"`` (the symmetric_verdict binds the same
    artifact/DAG/node as the accepted set + terminal) or ``"mismatch"`` (it binds a different
    artifact, so the accepted result is not the candidate the peers reviewed).
    ``final`` selects the FINAL accepted-set inclusion: ``None`` (no FINAL gate), ``"valid"`` (adds a
    ``source_gate: final`` extra), or ``"drops"`` (omits the accepted dependency finding)."""
    cfg = Config.from_dict({"final_review": final is not None})
    run = init_run("sym", cfg, base_dir=tmp_path, workflow="pr-review")
    dag = _dag(status="done")
    run.save_dag(dag.to_dict())
    dsha, nsha = dag_sha256(dag), node_sha256(dag, "a")

    plan_art = artifact_sha256(b"plan")
    run.append("builder_output", gate="plan", round=1, payload="plan", artifact_sha256=plan_art)
    run.append("gate_consensus", gate="plan", round=1, artifact_sha256=plan_art, dag_sha256=dsha)

    candidate = {"summary": "", "findings": [_af("dep:a", "F1")]}
    cand_text = json.dumps(candidate)
    cand_art = artifact_sha256(cand_text.encode("utf-8"))
    dep_bind = {"artifact_sha256": cand_art, "dag_sha256": dsha, "node_sha256": nsha}
    run.append("builder_output", gate="dep:a", round=1, payload=cand_text, artifact_sha256=cand_art)
    ver_bind = dep_bind if verdict == "match" else {**dep_bind, "artifact_sha256": "9" * 64}
    run.append("symmetric_verdict", gate="dep:a", round=1, outcome="CONSENSUS", objections=[],
               candidate=candidate, **ver_bind)
    acc_bind = dep_bind if accepted != "binding" else {**dep_bind, "artifact_sha256": "0" * 64}
    acc_payload = candidate if accepted != "malformed" else {"findings": "not-a-list"}
    run.append("accepted_finding_set", gate="dep:a", round=1, payload=acc_payload, **acc_bind)
    run.append("gate_consensus", gate="dep:a", round=1, **dep_bind)
    run.append("node_status_change", node="a", status="done")

    if final is not None:
        if final == "drops":
            final_findings = [_af("final", "C1", "nit")]  # drops the accepted dependency finding
        else:
            final_findings = [_af("dep:a", "F1"), _af("final", "C1", "nit")]
        final_payload = {"summary": "", "findings": final_findings}
        final_text = json.dumps(final_payload)
        final_art = artifact_sha256(final_text.encode("utf-8"))
        fbind = {"artifact_sha256": final_art, "dag_sha256": dsha}
        run.append("builder_output", gate="final", round=1, payload=final_text,
                   artifact_sha256=final_art)
        run.append("symmetric_verdict", gate="final", round=1, outcome="CONSENSUS", objections=[],
                   candidate=final_payload, **fbind)
        run.append("accepted_finding_set", gate="final", round=1, payload=final_payload, **fbind)
        run.append("gate_consensus", gate="final", round=1, **fbind)

    return run, dag, cfg


def test_workflow_issues_clean_for_valid_symmetric_run_with_final(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid", final="valid")
    assert workflow_issues(run.read_events(), dag, cfg) == []


def test_workflow_issues_flags_symmetric_binding_mismatched_accepted_set(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="binding")
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "dep:a" in i.message for i in issues)


def test_workflow_issues_flags_symmetric_peer_decision_binding_mismatch(tmp_path):
    # F1: the symmetric_verdict (the two peers' decision) binds a different artifact than the
    # accepted set + terminal it brackets, so the accepted result is not the reviewed candidate.
    run, dag, cfg = _symmetric_run(tmp_path, verdict="mismatch")
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "dep:a" in i.message for i in issues)


def test_workflow_issues_flags_symmetric_malformed_accepted_set(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="malformed")
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" for i in issues)


def test_workflow_issues_flags_symmetric_final_inclusion_violation(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid", final="drops")
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "final" in i.message.lower() for i in issues)


def _corrupt_dep_trio(events, corruption):
    """Return a copy of a valid symmetric run's events with the ``dep:a`` atomic trio corrupted.

    ``duplicate`` inserts a SECOND pre-terminal accepted set (distinct id); ``nonimmediate`` inserts
    a mismatched ``symmetric_verdict`` immediately before the accepted set (a matching decision then a
    later mismatched one); ``intervening`` inserts an extra ``symmetric_verdict`` between the accepted
    set and its terminal. Each breaks the exact ``symmetric_verdict -> accepted_finding_set ->
    terminal`` adjacency for the same gate/round.
    """
    events = [dict(e) for e in events]
    acc_idx = next(i for i, e in enumerate(events)
                   if e.get("event") == "accepted_finding_set" and e.get("gate") == "dep:a")
    term_idx = next(i for i, e in enumerate(events)
                    if e.get("event") == "gate_consensus" and e.get("gate") == "dep:a")
    sv = next(e for e in events
              if e.get("event") == "symmetric_verdict" and e.get("gate") == "dep:a")
    if corruption == "duplicate":
        dup = dict(events[acc_idx])
        dup["payload"] = {"summary": "", "findings": [_af("dep:a", "F2")]}
        events.insert(term_idx, dup)  # two accepted sets before the terminal (distinct ids)
    elif corruption == "nonimmediate":
        mismatched = dict(sv)
        mismatched["artifact_sha256"] = "9" * 64
        events.insert(acc_idx, mismatched)  # matching sv, then mismatched sv, then accepted set
    elif corruption == "intervening":
        events.insert(term_idx, dict(sv))  # an extra verdict between the accepted set and terminal
    return events


def test_workflow_issues_clean_for_valid_symmetric_dependency_only(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    assert workflow_issues(run.read_events(), dag, cfg) == []


def test_workflow_issues_flags_two_pre_terminal_accepted_sets(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    events = _corrupt_dep_trio(run.read_events(), "duplicate")
    issues = workflow_issues(events, dag, cfg)
    assert any(i.kind == "invalid" and "dep:a" in i.message for i in issues)


def test_workflow_issues_flags_non_immediate_peer_decision(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    events = _corrupt_dep_trio(run.read_events(), "nonimmediate")
    issues = workflow_issues(events, dag, cfg)
    assert any(i.kind == "invalid" and "dep:a" in i.message for i in issues)


def test_workflow_issues_flags_intervening_protocol_event(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    events = _corrupt_dep_trio(run.read_events(), "intervening")
    issues = workflow_issues(events, dag, cfg)
    assert any(i.kind == "invalid" and "dep:a" in i.message for i in issues)


def _forge_trio(run, gate, findings, bindings):
    """Append a fully bound-looking ``symmetric_verdict -> accepted_finding_set -> gate_consensus``
    trio for ``gate`` (the corrupt history a real CLI write path never produces)."""
    payload = {"summary": "", "findings": findings}
    run.append("symmetric_verdict", gate=gate, round=1, outcome="CONSENSUS", objections=[],
               candidate=payload, **bindings)
    run.append("accepted_finding_set", gate=gate, round=1, payload=payload, **bindings)
    run.append("gate_consensus", gate=gate, round=1, **bindings)


def test_workflow_issues_flags_out_of_scope_dependency_accepted_set(tmp_path):
    # Round-3 F2: a fully bound-looking dep:ghost trio whose node is absent from the current DAG must
    # be flagged invalid — never silently accepted — even though every current node is valid.
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")  # one done node 'a', final_review off
    _forge_trio(run, "dep:ghost", [_af("dep:ghost", "G1")],
                {"artifact_sha256": "e" * 64, "dag_sha256": dag_sha256(dag), "node_sha256": "f" * 64})
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "ghost" in i.message for i in issues)


def test_workflow_issues_flags_final_trio_when_final_review_disabled(tmp_path):
    # Round-3 F1: final_review is off, but a valid-looking FINAL trio was forged into the log. FINAL
    # is not part of this run's configured workflow, so its terminal is a configured-forbidden phase
    # (mirrors the disabled-REPRODUCE rule) — workflow invalid.
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")  # final_review off, no FINAL
    assert cfg.final_review is False
    _forge_trio(run, "final", [_af("dep:a", "F1"), _af("final", "C1", "nit")],
                {"artifact_sha256": "c" * 64, "dag_sha256": dag_sha256(dag)})
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "final" in i.message.lower() for i in issues)
