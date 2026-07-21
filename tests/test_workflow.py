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
from crucible.target import ReviewTarget, target_from_events, target_sha256
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


# A minimal revision-unbound diff-file target — the smallest valid pr-review target — so a pr-review
# fixture binds its gates to a real loaded target identity.
_TARGET_MANIFEST = {
    "version": 1, "kind": "diff-file", "revision_bound": False, "repository": None,
    "diff_sha256": "0" * 64, "changed_files": ["a.py"], "intent": {"title": "t", "body": "b"},
}
_TARGET_SHA = target_sha256(ReviewTarget.from_dict(_TARGET_MANIFEST))


def _load_target(run):
    """Append the one immutable ``target_loaded`` event and return its authoritative target hash."""
    target = ReviewTarget.from_dict(_TARGET_MANIFEST)
    sha = target_sha256(target)
    run.append("target_loaded", target=target.to_dict(), target_sha256=sha)
    return sha


def _peers(gate, rnd, bindings, outer_objs=()):
    """A valid persisted A/B peers object matching the CLI write path (round-9): each slot carries its
    configured model/effort, a `raw` JSON string, and a parsed `attestation` bound to the decision;
    peer objections are the outer namespaced aggregate de-namespaced to their slot, with a consistent
    verdict."""
    from crucible.symmetric import peer_slot_provenance
    prov = peer_slot_provenance(Config.from_dict({}))
    per = {"A": [], "B": []}
    for o in outer_objs:
        slot, _, base = str(o["id"]).partition(":")
        if slot in per:
            per[slot].append({**o, "id": base})
    peers = {}
    for slot in ("A", "B"):
        objs = per[slot]
        has_blocking = any(o["severity"] in ("blocker", "major") for o in objs)
        att = {"peer": slot, "gate": gate, "round": rnd,
               "verdict": "REQUEST_CHANGES" if has_blocking else "APPROVE",
               "summary": f"peer {slot} review", "objections": objs, **bindings}
        peers[slot] = {**prov[slot], "raw": json.dumps(att), "attestation": att}
    return peers


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


def _symmetric_run(tmp_path, *, accepted="valid", final=None, verdict="match", plan="valid"):
    """A pr-review run with one done node 'a' backed by a symmetric dependency gate, plus optional
    FINAL. ``accepted`` selects a dependency accepted-set fault: ``"valid"``, ``"binding"`` (its
    bindings differ from the terminal), or ``"malformed"`` (its payload is not a valid finding set).
    ``verdict`` selects a peer-decision fault: ``"match"`` (the symmetric_verdict binds the same
    artifact/DAG/node as the accepted set + terminal) or ``"mismatch"`` (it binds a different
    artifact, so the accepted result is not the candidate the peers reviewed).
    ``final`` selects the FINAL accepted-set inclusion: ``None`` (no FINAL gate), ``"valid"`` (adds a
    ``source_gate: final`` extra), or ``"drops"`` (omits the accepted dependency finding).
    ``plan`` selects the symmetric PLAN attestation: ``"valid"`` (a bound symmetric_verdict backs the
    accepted PLAN terminal), ``"bare"`` (a terminal with no symmetric_verdict), ``"critic"`` (a
    build-style critic_verdict instead of two-peer), ``"wrong_round"``/``"wrong_bindings"``/
    ``"wrong_outcome"`` (a symmetric_verdict that does not correspond to the terminal)."""
    cfg = Config.from_dict({"final_review": final is not None})
    run = init_run("sym", cfg, base_dir=tmp_path, workflow="pr-review")
    tgt = _load_target(run)
    dag = _dag(status="done")
    run.save_dag(dag.to_dict())
    dsha, nsha = dag_sha256(dag), node_sha256(dag, "a")

    plan_art = artifact_sha256(b"plan")
    run.append("builder_output", gate="plan", round=1, payload="plan", artifact_sha256=plan_art)
    plan_bind = {"artifact_sha256": plan_art, "dag_sha256": dsha, "target_sha256": tgt}
    if plan == "critic":
        run.append("critic_verdict", gate="plan", round=1, verdict="APPROVE", **plan_bind)
    elif plan != "bare":
        v_round = 2 if plan == "wrong_round" else 1
        v_bind = ({**plan_bind, "artifact_sha256": "7" * 64} if plan == "wrong_bindings"
                  else plan_bind)
        v_outcome = "CHANGES" if plan == "wrong_outcome" else "CONSENSUS"
        run.append("symmetric_verdict", gate="plan", round=v_round, outcome=v_outcome,
                   objections=[], peers=_peers("plan", v_round, v_bind), **v_bind)
    run.append("gate_consensus", gate="plan", round=1, **plan_bind)

    candidate = {"summary": "", "findings": [_af("dep:a", "F1")]}
    cand_text = json.dumps(candidate)
    cand_art = artifact_sha256(cand_text.encode("utf-8"))
    dep_bind = {"artifact_sha256": cand_art, "dag_sha256": dsha, "node_sha256": nsha,
                "target_sha256": tgt}
    run.append("builder_output", gate="dep:a", round=1, payload=cand_text, artifact_sha256=cand_art)
    ver_bind = dep_bind if verdict == "match" else {**dep_bind, "artifact_sha256": "9" * 64}
    run.append("symmetric_verdict", gate="dep:a", round=1, outcome="CONSENSUS", objections=[],
               peers=_peers("dep:a", 1, ver_bind), candidate=candidate, **ver_bind)
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
        fbind = {"artifact_sha256": final_art, "dag_sha256": dsha, "target_sha256": tgt}
        run.append("builder_output", gate="final", round=1, payload=final_text,
                   artifact_sha256=final_art)
        run.append("symmetric_verdict", gate="final", round=1, outcome="CONSENSUS", objections=[],
                   peers=_peers("final", 1, fbind), candidate=final_payload, **fbind)
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


# --- Task 2: pr-review target integrity (missing / invalid / mismatched binding) -----------------

def _rebind_target(events, gate, value):
    """Rewrite ``target_sha256`` to ``value`` on every binding event (and nested peer attestation +
    raw provenance) for ``gate`` — a consistent forged rebinding the CLI write path never produces."""
    for e in events:
        if e.get("gate") != gate:
            continue
        if "target_sha256" in e:
            e["target_sha256"] = value
        peers = e.get("peers")
        if isinstance(peers, dict):
            for slot in peers.values():
                if not isinstance(slot, dict):
                    continue
                att = slot.get("attestation")
                if isinstance(att, dict) and "target_sha256" in att:
                    att["target_sha256"] = value
                    slot["raw"] = json.dumps(att)
    return events


def test_workflow_issues_init_only_pr_review_missing_target(tmp_path):
    # An init-only pr-review run (no target loaded yet, no protocol work) is merely IN PROGRESS: the
    # target is not yet loaded -> a `missing` issue, never `invalid`.
    cfg = Config.from_dict({"final_review": False})
    run = init_run("sym", cfg, base_dir=tmp_path, workflow="pr-review")
    dag = _dag(status="pending")
    run.save_dag(dag.to_dict())
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "missing" and "target" in i.message.lower() for i in issues)
    assert not any(i.kind == "invalid" and "target" in i.message.lower() for i in issues)


def test_workflow_issues_pr_review_protocol_without_target_invalid(tmp_path):
    # DAG/PLAN/review work recorded with no loaded target is an integrity violation (a target must
    # precede protocol work) -> invalid.
    cfg = Config.from_dict({"final_review": False})
    run = init_run("sym", cfg, base_dir=tmp_path, workflow="pr-review")
    dag = _dag(status="pending")
    run.save_dag(dag.to_dict())
    run.append("builder_output", gate="plan", round=1, payload="plan",
               artifact_sha256=artifact_sha256(b"plan"))
    run.append("gate_consensus", gate="plan", round=1,
               artifact_sha256=artifact_sha256(b"plan"), dag_sha256=dag_sha256(dag))
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "target" in i.message.lower() for i in issues)


def test_workflow_issues_target_in_build_run_invalid(tmp_path):
    cfg = Config.from_dict({"final_review": False})
    run = init_run("b", cfg, base_dir=tmp_path)  # build
    dag = _dag(status="pending")
    run.save_dag(dag.to_dict())
    _load_target(run)
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "target" in i.message.lower() for i in issues)


def test_workflow_issues_target_in_deep_dive_run_invalid(tmp_path):
    cfg = Config.from_dict({"final_review": False})
    run = init_run("d", cfg, base_dir=tmp_path, workflow="deep-dive")
    dag = _dag(status="pending")
    run.save_dag(dag.to_dict())
    _load_target(run)
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "target" in i.message.lower() for i in issues)


def test_workflow_issues_duplicate_target_invalid(tmp_path):
    cfg = Config.from_dict({"final_review": False})
    run = init_run("sym", cfg, base_dir=tmp_path, workflow="pr-review")
    _load_target(run)
    _load_target(run)  # a target is immutable — a second load is invalid
    dag = _dag(status="pending")
    run.save_dag(dag.to_dict())
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "target" in i.message.lower() for i in issues)


def test_workflow_issues_late_target_invalid(tmp_path):
    cfg = Config.from_dict({"final_review": False})
    run = init_run("sym", cfg, base_dir=tmp_path, workflow="pr-review")
    dag = _dag(status="pending")
    run.save_dag(dag.to_dict())
    run.append("dag_loaded", gate="plan", nodes=1)  # protocol work begins
    _load_target(run)  # loaded AFTER protocol work — a target must precede it
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "target" in i.message.lower() for i in issues)


def test_workflow_issues_consistent_wrong_target_trio_invalid(tmp_path):
    # A fully consistent dep trio (verdict/accepted/terminal/peers all rebind the same WRONG target)
    # passes the internal binding-consistency checks yet is not bound to the loaded target -> invalid.
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    events = _rebind_target(run.read_events(), "dep:a", "9" * 64)
    issues = workflow_issues(events, dag, cfg)
    assert any(i.kind == "invalid" and "target" in i.message.lower() for i in issues)


def test_workflow_issues_mismatched_target_on_accepted_set_invalid(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    events = run.read_events()
    for e in events:
        if e.get("event") == "accepted_finding_set" and e.get("gate") == "dep:a":
            e["target_sha256"] = "9" * 64
    issues = workflow_issues(events, dag, cfg)
    assert any(i.kind == "invalid" and "target" in i.message.lower() for i in issues)


def test_workflow_issues_mismatched_target_on_verdict_invalid(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    events = run.read_events()
    for e in events:
        if e.get("event") == "symmetric_verdict" and e.get("gate") == "dep:a":
            e["target_sha256"] = "9" * 64
    issues = workflow_issues(events, dag, cfg)
    assert any(i.kind == "invalid" and "target" in i.message.lower() for i in issues)


def test_workflow_issues_mismatched_target_on_terminal_invalid(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    events = run.read_events()
    for e in events:
        if e.get("event") == "gate_consensus" and e.get("gate") == "dep:a":
            e["target_sha256"] = "9" * 64
    issues = workflow_issues(events, dag, cfg)
    assert any(i.kind == "invalid" and "target" in i.message.lower() for i in issues)


def test_workflow_issues_source_materialized_wrong_target_invalid(tmp_path):
    # A source snapshot bound to a different target than the loaded one is invalid history.
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    run.append("source_materialized", kind="diff-file", target_sha256="9" * 64,
               archive_sha256="a" * 64)
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and ("target" in i.message.lower()
                                        or "source" in i.message.lower()) for i in issues)


def test_workflow_issues_valid_pr_review_has_no_target_issue(tmp_path):
    # A valid pr-review run (target loaded, every in-scope gate bound to it) has NO target issue.
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid", final="valid")
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert not any("target" in i.message.lower() for i in issues)
    assert issues == []


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


def _forge_objection_gate(run, gate, terminal, objections, bindings):
    """Append a forged ``symmetric_verdict -> capped/proceeded terminal`` pair carrying objections
    but NO accepted set (a CAPPED halt persists no accepted set) — the out-of-scope history a real
    CLI write path never produces. ``terminal`` is ``gate_capped`` or ``gate_proceeded_with_flags``."""
    outcome = "PROCEED_WITH_FLAGS" if terminal == "gate_proceeded_with_flags" else "CAPPED"
    run.append("symmetric_verdict", gate=gate, round=1, outcome=outcome, objections=objections,
               candidate={"summary": "", "findings": []}, **bindings)
    run.append(terminal, gate=gate, round=1, open_findings=[o["id"] for o in objections], **bindings)


def _obj(oid="A:G1", severity="blocker"):
    return {"id": oid, "severity": severity, "location": "candidate:F1", "claim": "c",
            "suggestion": "s"}


def test_workflow_issues_flags_out_of_scope_capped_dependency_gate(tmp_path):
    # Round-4: a forged dep:ghost gate that CAPS with a blocking objection but persists NO accepted
    # set. The accepted-set scope guard would miss it (no accepted set); the protocol-wide guard flags
    # it invalid so an out-of-scope objection can never reach the recommendation.
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")  # one done node 'a', final_review off
    _forge_objection_gate(run, "dep:ghost", "gate_capped", [_obj("A:G1", "blocker")],
                          {"artifact_sha256": "e" * 64, "dag_sha256": dag_sha256(dag),
                           "node_sha256": "f" * 64})
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "ghost" in i.message for i in issues)


def test_workflow_issues_flags_out_of_scope_proceeded_dependency_gate(tmp_path):
    # Round-4: same, but the forged out-of-scope ghost gate PROCEEDS WITH FLAGS.
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    _forge_objection_gate(run, "dep:ghost", "gate_proceeded_with_flags", [_obj("A:G1", "blocker")],
                          {"artifact_sha256": "e" * 64, "dag_sha256": dag_sha256(dag),
                           "node_sha256": "f" * 64})
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "ghost" in i.message for i in issues)


# --- round-5 F1: a same-gate symmetric protocol event after the authoritative terminal is invalid ---

def _copy_bindings(events, gate):
    """The artifact/DAG/node bindings recorded on ``gate``'s symmetric_verdict, for forging residue."""
    sv = next(e for e in events
              if e.get("event") == "symmetric_verdict" and e.get("gate") == gate)
    return {k: sv[k] for k in ("artifact_sha256", "dag_sha256", "node_sha256") if k in sv}


def test_workflow_issues_flags_post_terminal_symmetric_verdict(tmp_path):
    # A valid complete symmetric run, then a forged post-terminal symmetric_verdict for the same dep
    # gate/round. A same-gate protocol event after the authoritative terminal can rewrite the gate's
    # unresolved objections, so it is invalid history — not only a post-terminal accepted set.
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    bind = _copy_bindings(run.read_events(), "dep:a")
    run.append("symmetric_verdict", gate="dep:a", round=1, outcome="CONSENSUS", objections=[],
               candidate={"summary": "", "findings": []}, **bind)
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "dep:a" in i.message for i in issues)


def test_workflow_issues_flags_post_terminal_accepted_set_as_residue(tmp_path):
    # The accepted-set variant of the same rule: an accepted_finding_set appended after the gate's
    # authoritative terminal is post-terminal residue (invalid), never accepted state.
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    bind = _copy_bindings(run.read_events(), "dep:a")
    run.append("accepted_finding_set", gate="dep:a", round=1,
               payload={"summary": "", "findings": []}, **bind)
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "dep:a" in i.message for i in issues)


# --- round-5 F3: an arbitrary non-schema gate carrying symmetric protocol events is out of scope ---

def test_workflow_issues_flags_arbitrary_symmetric_gate(tmp_path):
    # A forged symmetric_verdict + capped terminal for an arbitrary gate name (sidequest) carrying a
    # blocker. It is not plan / an in-scope dep / the enabled FINAL gate, so it is out of scope and
    # flagged invalid before its objection can reach the recommendation.
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    _forge_objection_gate(run, "sidequest", "gate_capped", [_obj("A:S1", "blocker")],
                          {"artifact_sha256": "e" * 64, "dag_sha256": dag_sha256(dag),
                           "node_sha256": "f" * 64})
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "sidequest" in i.message for i in issues)


def test_workflow_issues_flags_reproduce_symmetric_protocol_event(tmp_path):
    # Symmetric protocols have no reproduce gate; a forged reproduce symmetric_verdict + capped
    # terminal is out of scope for a symmetric run and flagged invalid.
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    _forge_objection_gate(run, "reproduce", "gate_capped", [_obj("A:R1", "blocker")],
                          {"artifact_sha256": "e" * 64, "dag_sha256": dag_sha256(dag),
                           "node_sha256": "f" * 64})
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "reproduce" in i.message.lower() for i in issues)


# --- round-7 F1: an accepted DEPENDENCY set whose finding is attributed to another gate is invalid ---

def test_workflow_issues_flags_mis_scoped_dependency_finding(tmp_path):
    # A valid dep:a trio, but its accepted set attributes its finding to source_gate "final" — a
    # forged dep:auth set injecting a FINAL/ghost finding. workflow_issues must surface it as invalid
    # (reusing FindingSet.validate_for_gate), never leave the run CLEAN.
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    events = run.read_events()
    for e in events:
        if e.get("event") == "accepted_finding_set" and e.get("gate") == "dep:a":
            e["payload"]["findings"][0]["source_gate"] = "final"
    issues = workflow_issues(events, dag, cfg)
    assert any(i.kind == "invalid" and "dep:a" in i.message for i in issues)


def test_workflow_issues_flags_mis_scoped_dependency_finding_ghost(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    events = run.read_events()
    for e in events:
        if e.get("event") == "accepted_finding_set" and e.get("gate") == "dep:a":
            e["payload"]["findings"][0]["source_gate"] = "dep:ghost"
    issues = workflow_issues(events, dag, cfg)
    assert any(i.kind == "invalid" and "dep:a" in i.message for i in issues)


# --- round-7 F2: a FINAL symmetric protocol event while final_review is disabled is invalid --------
#
# The earlier terminal-only check only sees a FINAL *terminal*. A FINAL symmetric_verdict or
# accepted_finding_set WITHOUT a terminal (verdict-only / accepted-set-only residue) evaded it while
# _symmetric_accepted_set_issues unconditionally skipped out-of-scope `final`, leaving the run CLEAN.

def test_workflow_issues_flags_disabled_final_verdict_only_residue(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")  # final_review off, no FINAL
    assert cfg.final_review is False
    run.append("symmetric_verdict", gate="final", round=1, outcome="CONSENSUS", objections=[],
               candidate={"summary": "", "findings": []},
               artifact_sha256="e" * 64, dag_sha256=dag_sha256(dag))
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "final" in i.message.lower() for i in issues)


def test_workflow_issues_flags_disabled_final_accepted_set_only_residue(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")  # final_review off, no FINAL
    assert cfg.final_review is False
    run.append("accepted_finding_set", gate="final", round=1,
               payload={"summary": "", "findings": []},
               artifact_sha256="e" * 64, dag_sha256=dag_sha256(dag))
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "final" in i.message.lower() for i in issues)


def test_workflow_issues_clean_when_final_review_disabled_and_no_final_events(tmp_path):
    # Guard against over-flagging: a valid final_review=off run with NO final protocol events stays
    # CLEAN (the disabled-FINAL rule only fires on an actual FINAL protocol event).
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    assert cfg.final_review is False
    assert workflow_issues(run.read_events(), dag, cfg) == []


# --- round-8 F3: a symmetric PLAN accepted terminal must be backed by a two-peer symmetric_verdict ---

def _dep_gate_bindings_w(events, gate):
    sv = next(e for e in events
              if e.get("event") == "symmetric_verdict" and e.get("gate") == gate)
    return {k: sv[k] for k in ("artifact_sha256", "dag_sha256", "node_sha256") if k in sv}


@pytest.mark.parametrize("plan", ["bare", "critic", "wrong_round", "wrong_bindings", "wrong_outcome"])
def test_workflow_issues_flags_unattested_symmetric_plan(tmp_path, plan):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid", plan=plan)
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "plan" in i.message.lower() for i in issues), \
        [(i.kind, i.message) for i in issues]


def test_workflow_issues_clean_for_valid_symmetric_plan(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid", plan="valid")
    assert workflow_issues(run.read_events(), dag, cfg) == []


# --- F2: a symmetric PLAN gate never persists an accepted finding set ----------------------------

def _plan_bindings_w(events):
    sv = next(e for e in events
              if e.get("event") == "symmetric_verdict" and e.get("gate") == "plan")
    return {k: sv[k] for k in ("artifact_sha256", "dag_sha256", "node_sha256") if k in sv}


def _inject_plan_accepted_set(events, placement):
    """Return a copy of a valid symmetric run's events with a forged PLAN accepted_finding_set added at
    ``placement``: ``"before"`` (before the plan decision), ``"between"`` (between the plan decision
    and its terminal — an otherwise-valid trio), or ``"after"`` (after the plan terminal)."""
    events = [dict(e) for e in events]
    acc = {"event": "accepted_finding_set", "gate": "plan", "round": 1,
           "payload": {"summary": "", "findings": []}, **_plan_bindings_w(events)}
    sv_idx = next(i for i, e in enumerate(events)
                  if e.get("event") == "symmetric_verdict" and e.get("gate") == "plan")
    term_idx = next(i for i, e in enumerate(events)
                    if e.get("event") == "gate_consensus" and e.get("gate") == "plan")
    if placement == "before":
        events.insert(sv_idx, acc)
    elif placement == "between":
        events.insert(term_idx, acc)
    else:  # after
        events.insert(term_idx + 1, acc)
    return events


@pytest.mark.parametrize("placement", ["before", "between", "after"])
def test_workflow_issues_flags_plan_accepted_finding_set(tmp_path, placement):
    # A symmetric PLAN advances on a two-peer decision and NEVER persists an accepted finding set. Any
    # PLAN accepted set (any placement) is forged history workflow_issues must flag invalid — and the
    # forged set must not make the PLAN attestation appear valid.
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid", plan="valid")
    events = _inject_plan_accepted_set(run.read_events(), placement)
    issues = workflow_issues(events, dag, cfg)
    assert any(i.kind == "invalid" and "plan" in i.message.lower()
               and "accepted finding set" in i.message.lower() for i in issues), \
        [(i.kind, i.message) for i in issues]


def test_workflow_issues_flags_orphan_plan_accepted_finding_set(tmp_path):
    # A PLAN accepted set with no PLAN terminal at all (orphan) is also forged history — flagged even
    # though the run's PLAN is otherwise "missing" (in progress).
    run, dag, cfg = _symmetric_no_plan(tmp_path)
    run.append("accepted_finding_set", gate="plan", round=1,
               payload={"summary": "", "findings": []},
               artifact_sha256="a" * 64, dag_sha256=dag_sha256(dag))
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "plan" in i.message.lower()
               and "accepted finding set" in i.message.lower() for i in issues)


# --- F1: corrupt run workflow metadata fails workflow_issues closed without crashing -------------

@pytest.mark.parametrize("bad", [None, 123, "bogus"])
def test_workflow_issues_flags_corrupt_workflow_metadata(tmp_path, bad):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    events = run.read_events()
    for e in events:
        if e.get("event") == "run_start":
            e["workflow"] = bad  # present but null/non-string/unrecognized => corrupt
    issues = workflow_issues(events, dag, cfg)  # must not raise
    assert issues
    assert all(i.kind == "invalid" for i in issues)
    assert any("workflow" in i.message.lower() and "corrupt" in i.message.lower() for i in issues)


def test_workflow_issues_absent_workflow_reads_as_build(tmp_path):
    # Regression guard: an ABSENT workflow key stays build (legacy readability) and is NOT treated as
    # corrupt — a build run with a clean PLAN + dep stays CLEAN.
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)  # default build workflow
    dag = _dag(status="done")
    _bind_plan(run, dag)
    _bind_dep_a(run, dag)
    run.append("node_status_change", node="a", status="done")
    events = [dict(e) for e in run.read_events()]
    for e in events:
        if e.get("event") == "run_start":
            e.pop("workflow", None)  # legacy schema-v2 metadata: the field predates the run
    assert workflow_issues(events, dag, cfg) == []


def test_workflow_issues_build_plan_critic_verdict_unchanged(tmp_path):
    # F3 must not touch the asymmetric build workflow: a build run's PLAN is a critic_verdict +
    # terminal and stays CLEAN (the symmetric attestation rule is symmetric-only).
    cfg = Config.from_dict({"final_review": False})
    run = init_run("g", cfg, base_dir=tmp_path)  # default = build workflow
    dag = _dag(status="done")
    _bind_plan(run, dag)
    _bind_dep_a(run, dag)
    run.append("node_status_change", node="a", status="done")
    assert workflow_issues(run.read_events(), dag, cfg) == []


# --- round-8 F2: symmetric protocol integrity still runs when PLAN is missing --------------------

def _symmetric_no_plan(tmp_path):
    """A pr-review run whose PLAN never reached a terminal (no plan events at all)."""
    cfg = Config.from_dict({"final_review": False})
    run = init_run("sym", cfg, base_dir=tmp_path, workflow="pr-review")
    dag = _dag(status="pending")
    run.save_dag(dag.to_dict())
    return run, dag, cfg


def test_workflow_issues_flags_arbitrary_residue_when_plan_missing(tmp_path):
    run, dag, cfg = _symmetric_no_plan(tmp_path)
    _forge_objection_gate(run, "sidequest", "gate_capped", [_obj("A:S1", "blocker")],
                          {"artifact_sha256": "e" * 64, "dag_sha256": dag_sha256(dag),
                           "node_sha256": "f" * 64})
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "missing" and "PLAN" in i.message for i in issues)
    assert any(i.kind == "invalid" and "sidequest" in i.message for i in issues)


def test_workflow_issues_flags_ghost_dep_residue_when_plan_missing(tmp_path):
    run, dag, cfg = _symmetric_no_plan(tmp_path)
    _forge_objection_gate(run, "dep:ghost", "gate_proceeded_with_flags", [_obj("A:G1", "blocker")],
                          {"artifact_sha256": "e" * 64, "dag_sha256": dag_sha256(dag),
                           "node_sha256": "f" * 64})
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "ghost" in i.message for i in issues)


def test_workflow_issues_flags_disabled_final_residue_when_plan_missing(tmp_path):
    run, dag, cfg = _symmetric_no_plan(tmp_path)  # final_review off
    run.append("symmetric_verdict", gate="final", round=1, outcome="CONSENSUS", objections=[],
               candidate={"summary": "", "findings": []},
               artifact_sha256="e" * 64, dag_sha256=dag_sha256(dag))
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "final" in i.message.lower() for i in issues)


def test_workflow_issues_flags_disabled_final_terminal_when_plan_missing(tmp_path):
    run, dag, cfg = _symmetric_no_plan(tmp_path)  # final_review off
    fb = {"artifact_sha256": "e" * 64, "dag_sha256": dag_sha256(dag)}
    run.append("symmetric_verdict", gate="final", round=1, outcome="CONSENSUS", objections=[],
               candidate={"summary": "", "findings": []}, **fb)
    run.append("accepted_finding_set", gate="final", round=1,
               payload={"summary": "", "findings": []}, **fb)
    run.append("gate_consensus", gate="final", round=1, **fb)
    issues = workflow_issues(run.read_events(), dag, cfg)
    assert any(i.kind == "invalid" and "final" in i.message.lower() for i in issues)


# --- round-8 F4: decision outcome must match its terminal (dep/final trio) -----------------------

def _find_ev(events, event, gate):
    return next(e for e in events if e.get("event") == event and e.get("gate") == gate)


def test_workflow_issues_flags_consensus_terminal_with_proceed_verdict(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    events = run.read_events()
    _find_ev(events, "symmetric_verdict", "dep:a")["outcome"] = "PROCEED_WITH_FLAGS"
    issues = workflow_issues(events, dag, cfg)
    assert any(i.kind == "invalid" and "dep:a" in i.message for i in issues)


def test_workflow_issues_flags_consensus_terminal_with_changes_verdict(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    events = run.read_events()
    _find_ev(events, "symmetric_verdict", "dep:a")["outcome"] = "CHANGES"
    issues = workflow_issues(events, dag, cfg)
    assert any(i.kind == "invalid" and "dep:a" in i.message for i in issues)


def test_workflow_issues_flags_proceeded_terminal_with_consensus_verdict(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    events = run.read_events()
    term = _find_ev(events, "gate_consensus", "dep:a")
    term["event"] = "gate_proceeded_with_flags"
    term["open_findings"] = []
    issues = workflow_issues(events, dag, cfg)
    assert any(i.kind == "invalid" and "dep:a" in i.message for i in issues)


# --- round-8 F1: decision objections must be blocking and match the terminal's open_findings -----

def test_workflow_issues_flags_proceeded_with_only_nonblocking_objection(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    events = run.read_events()
    sv = _find_ev(events, "symmetric_verdict", "dep:a")
    sv["outcome"] = "PROCEED_WITH_FLAGS"
    sv["objections"] = [_obj("A:m", "minor")]
    acc = _find_ev(events, "accepted_finding_set", "dep:a")
    acc["accepted_with_flags"] = True
    acc["open_objections"] = ["A:m"]
    term = _find_ev(events, "gate_consensus", "dep:a")
    term["event"] = "gate_proceeded_with_flags"
    term["open_findings"] = ["A:m"]
    issues = workflow_issues(events, dag, cfg)
    assert any(i.kind == "invalid" and "dep:a" in i.message for i in issues)


def test_workflow_issues_flags_consensus_decision_with_objections(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    events = run.read_events()
    _find_ev(events, "symmetric_verdict", "dep:a")["objections"] = [_obj("A:x", "blocker")]
    issues = workflow_issues(events, dag, cfg)
    assert any(i.kind == "invalid" and "dep:a" in i.message for i in issues)


def test_workflow_issues_flags_proceeded_open_findings_mismatch(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    events = run.read_events()
    sv = _find_ev(events, "symmetric_verdict", "dep:a")
    sv["outcome"] = "PROCEED_WITH_FLAGS"
    sv["objections"] = [_obj("A:b", "blocker")]
    acc = _find_ev(events, "accepted_finding_set", "dep:a")
    acc["accepted_with_flags"] = True
    acc["open_objections"] = ["A:b"]
    term = _find_ev(events, "gate_consensus", "dep:a")
    term["event"] = "gate_proceeded_with_flags"
    term["open_findings"] = ["A:DIFFERENT"]  # does not match the decision's objection ids
    issues = workflow_issues(events, dag, cfg)
    assert any(i.kind == "invalid" and "dep:a" in i.message for i in issues)


def test_workflow_issues_flags_proceeded_nonblocking_under_custom_severities(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    cfg = Config.from_dict({"final_review": False, "blocking_severities": ["blocker"]})
    events = run.read_events()
    sv = _find_ev(events, "symmetric_verdict", "dep:a")
    sv["outcome"] = "PROCEED_WITH_FLAGS"
    sv["objections"] = [_obj("A:maj", "major")]  # major is NOT blocking here
    acc = _find_ev(events, "accepted_finding_set", "dep:a")
    acc["accepted_with_flags"] = True
    acc["open_objections"] = ["A:maj"]
    term = _find_ev(events, "gate_consensus", "dep:a")
    term["event"] = "gate_proceeded_with_flags"
    term["open_findings"] = ["A:maj"]
    issues = workflow_issues(events, dag, cfg)
    assert any(i.kind == "invalid" and "dep:a" in i.message for i in issues)


# --- round-9: workflow validates the persisted A/B peer attestations + candidate handoff ----------

def _svf(events, gate):
    return next(e for e in events if e.get("event") == "symmetric_verdict" and e.get("gate") == gate)


def test_workflow_issues_clean_for_valid_symmetric_run_with_peers(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid", final="valid")
    assert workflow_issues(run.read_events(), dag, cfg) == []


def test_workflow_issues_flags_missing_dep_peers(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    events = run.read_events()
    del _svf(events, "dep:a")["peers"]
    issues = workflow_issues(events, dag, cfg)
    assert any(i.kind == "invalid" and "dep:a" in i.message for i in issues)


def test_workflow_issues_flags_swapped_dep_peers(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    events = run.read_events()
    peers = _svf(events, "dep:a")["peers"]
    peers["A"], peers["B"] = peers["B"], peers["A"]
    issues = workflow_issues(events, dag, cfg)
    assert any(i.kind == "invalid" and "dep:a" in i.message for i in issues)


def test_workflow_issues_flags_wrong_model_dep_peers(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    events = run.read_events()
    _svf(events, "dep:a")["peers"]["A"]["model"] = "totally-wrong-model"
    issues = workflow_issues(events, dag, cfg)
    assert any(i.kind == "invalid" and "dep:a" in i.message for i in issues)


def test_workflow_issues_flags_divergent_raw_dep_peers(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    events = run.read_events()
    att = dict(_svf(events, "dep:a")["peers"]["B"]["attestation"])
    att["summary"] = "raw diverges from parsed"
    _svf(events, "dep:a")["peers"]["B"]["raw"] = json.dumps(att)
    issues = workflow_issues(events, dag, cfg)
    assert any(i.kind == "invalid" and "dep:a" in i.message for i in issues)


def test_workflow_issues_flags_fabricated_dep_aggregate_objection(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    events = run.read_events()
    # Both peers APPROVE (no objections), but forge the outer aggregate to claim a blocker and flip
    # the CONSENSUS terminal to proceeded-with-flags carrying it.
    sv = _svf(events, "dep:a")
    sv["outcome"] = "PROCEED_WITH_FLAGS"
    sv["objections"] = [{"id": "A:FAB", "severity": "blocker", "location": "c", "claim": "c",
                         "suggestion": "s"}]
    acc = next(e for e in events
               if e.get("event") == "accepted_finding_set" and e.get("gate") == "dep:a")
    acc["accepted_with_flags"] = True
    acc["open_objections"] = ["A:FAB"]
    term = next(e for e in events
                if e.get("event") == "gate_consensus" and e.get("gate") == "dep:a")
    term["event"] = "gate_proceeded_with_flags"
    term["open_findings"] = ["A:FAB"]
    issues = workflow_issues(events, dag, cfg)
    assert any(i.kind == "invalid" and "dep:a" in i.message for i in issues)


def test_workflow_issues_flags_missing_plan_peers(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    events = run.read_events()
    del _svf(events, "plan")["peers"]
    issues = workflow_issues(events, dag, cfg)
    assert any(i.kind == "invalid" and "plan" in i.message.lower() for i in issues)


def test_workflow_issues_flags_missing_final_peers(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid", final="valid")
    events = run.read_events()
    del _svf(events, "final")["peers"]
    issues = workflow_issues(events, dag, cfg)
    assert any(i.kind == "invalid" and "final" in i.message.lower() for i in issues)


def test_workflow_issues_flags_forged_accepted_payload_candidate_handoff(tmp_path):
    # The accepted_finding_set.payload is forged to differ from the decision candidate + the bound
    # Builder artifact the two peers reviewed. The published findings would not be what was attested.
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    events = run.read_events()
    acc = next(e for e in events
               if e.get("event") == "accepted_finding_set" and e.get("gate") == "dep:a")
    acc["payload"] = {"summary": "", "findings": [_af("dep:a", "FORGED", "blocker")]}
    issues = workflow_issues(events, dag, cfg)
    assert any(i.kind == "invalid" and "dep:a" in i.message for i in issues)


def test_workflow_issues_flags_candidate_diverges_from_accepted_set(tmp_path):
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    events = run.read_events()
    _svf(events, "dep:a")["candidate"] = {"summary": "", "findings": [_af("dep:a", "OTHER", "nit")]}
    issues = workflow_issues(events, dag, cfg)
    assert any(i.kind == "invalid" and "dep:a" in i.message for i in issues)


def test_accepted_findings_rejects_forged_accepted_payload_via_workflow(tmp_path):
    # Direct projection at the module boundary: a forged accepted payload is caught by the workflow
    # integrity the result commands enforce before publishing (candidate handoff).
    run, dag, cfg = _symmetric_run(tmp_path, accepted="valid")
    events = run.read_events()
    acc = next(e for e in events
               if e.get("event") == "accepted_finding_set" and e.get("gate") == "dep:a")
    acc["payload"] = {"summary": "", "findings": [_af("dep:a", "FORGED", "blocker")]}
    assert any(i.kind == "invalid" for i in workflow_issues(events, dag, cfg))
