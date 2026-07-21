import json
import os
import subprocess
import sys
import hashlib
from pathlib import Path

from crucible.config import Config
from crucible.dag import DAG
from crucible.integrity import artifact_sha256, dag_sha256, node_sha256
from crucible.report import render_markdown
from crucible.runlog import init_run
from crucible.target import ReviewTarget, target_sha256


ROOT = Path(__file__).resolve().parents[1]

# A minimal revision-unbound diff-file target (empty patch) — the smallest valid pr-review target, so
# a pr-review consensus fixture binds its gates to a real loaded target identity.
_TARGET_MANIFEST = {
    "version": 1, "kind": "diff-file", "revision_bound": False, "repository": None,
    "diff_sha256": hashlib.sha256(b"").hexdigest(), "changed_files": [],
    "intent": {"title": "t", "body": "b"},
}
_TGT = target_sha256(ReviewTarget.from_dict(_TARGET_MANIFEST))


def _load_target(run):
    """Append the one immutable ``target_loaded`` event directly; return its authoritative hash."""
    run.append("target_loaded", target=ReviewTarget.from_dict(_TARGET_MANIFEST).to_dict(),
               target_sha256=_TGT)
    return _TGT


def _load_target_cli(run_path, tmp_path):
    """Load the minimal diff-file target through the CLI so a pr-review run passes the target guard."""
    diff = Path(tmp_path) / "target.diff"
    diff.write_bytes(b"")
    manifest = Path(tmp_path) / "target.json"
    manifest.write_text(json.dumps(_TARGET_MANIFEST))
    r = _run(["load-target", "--run", run_path, "--file", str(manifest), "--diff", str(diff)])
    assert r.returncode == 0, r.stderr


def _append_plan_consensus(run, dsha, payload="investigation plan"):
    """Append a target-bound PLAN consensus trio (Builder output + two-peer verdict + terminal)."""
    plan_art = artifact_sha256(payload.encode("utf-8"))
    plan_bind = {"artifact_sha256": plan_art, "dag_sha256": dsha, "target_sha256": _TGT}
    run.append("builder_output", gate="plan", round=1, payload=payload, artifact_sha256=plan_art)
    run.append("symmetric_verdict", gate="plan", round=1, outcome="CONSENSUS", objections=[],
               peers=_peers("plan", 1, plan_bind), **plan_bind)
    run.append("gate_consensus", gate="plan", round=1, **plan_bind)


def _peers(gate, rnd, bindings, outer_objs=()):
    """A valid persisted A/B peers object matching the CLI write path (round-9)."""
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
    _load_target_cli(str(run_path), tmp_path)  # a pr-review run must load its target before any gate
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
    _load_target(run)
    dsha, nsha = dag_sha256(dag), node_sha256(dag, "auth")

    _append_plan_consensus(run, dsha)

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
    bindings = {"artifact_sha256": cand_art, "dag_sha256": dsha, "node_sha256": nsha,
                "target_sha256": _TGT}
    run.append("builder_output", gate="dep:auth", round=1, payload=cand_text,
               artifact_sha256=cand_art)
    run.append("symmetric_verdict", gate="dep:auth", round=1, outcome="CONSENSUS", objections=[],
               peers=_peers("dep:auth", 1, bindings), candidate=candidate, **bindings)
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
    _load_target(run)
    dsha, nsha = dag_sha256(dag), node_sha256(dag, "auth")

    _append_plan_consensus(run, dsha)

    candidate = {
        "summary": "accepted findings",
        "findings": [{
            "source_gate": "dep:auth", "id": "F1", "severity": "minor",
            "location": "src/auth.py:42", "claim": "A nit.", "suggestion": "Tidy it.",
        }],
    }
    cand_text = json.dumps(candidate)
    cand_art = artifact_sha256(cand_text.encode("utf-8"))
    bindings = {"artifact_sha256": cand_art, "dag_sha256": dsha, "node_sha256": nsha,
                "target_sha256": _TGT}
    run.append("builder_output", gate="dep:auth", round=1, payload=cand_text,
               artifact_sha256=cand_art)
    run.append("symmetric_verdict", gate="dep:auth", round=1, outcome="CONSENSUS", objections=[],
               peers=_peers("dep:auth", 1, bindings), candidate=candidate, **bindings)
    run.append("accepted_finding_set", gate="dep:auth", round=1, payload=candidate, **bindings)
    run.append("gate_consensus", gate="dep:auth", round=1, **bindings)
    run.append("node_status_change", node="auth", status="done")

    # Forge a valid-looking FINAL trio that would flip the recommendation to REQUEST_CHANGES.
    final_payload = {"summary": "final", "findings": candidate["findings"] + [{
        "source_gate": "final", "id": "C1", "severity": "blocker",
        "location": "src/auth.py:1", "claim": "Injected blocker.", "suggestion": "n/a",
    }]}
    fbind = {"artifact_sha256": artifact_sha256(json.dumps(final_payload).encode("utf-8")),
             "dag_sha256": dsha, "target_sha256": _TGT}
    run.append("symmetric_verdict", gate="final", round=1, outcome="CONSENSUS", objections=[],
               peers=_peers("final", 1, fbind), candidate=final_payload, **fbind)
    run.append("accepted_finding_set", gate="final", round=1, payload=final_payload, **fbind)
    run.append("gate_consensus", gate="final", round=1, **fbind)

    result = _run(["review-result", "--run", str(run.path)])

    assert result.returncode != 0, result.stdout


def test_report_recommendation_matches_review_result_cli(tmp_path):
    # Task 4 integration: the rendered report and the deterministic `review-result` CLI agree on the
    # PR recommendation for the SAME complete run, and the report keeps workflow status (CLEAN)
    # separate from the recommendation (REQUEST_CHANGES). Both derive it from the accepted finding
    # set, never from workflow integrity.
    run = init_run("report matches cli", Config.from_dict({"final_review": False}),
                   base_dir=tmp_path, workflow="pr-review")
    dag = DAG.from_dict({
        "nodes": [{"id": "auth", "title": "Auth", "description": "d", "files": ["auth.py"],
                   "test_plan": "pytest", "status": "done"}],
        "edges": [],
    })
    run.save_dag(dag.to_dict())
    _load_target(run)
    dsha, nsha = dag_sha256(dag), node_sha256(dag, "auth")

    _append_plan_consensus(run, dsha)

    candidate = {
        "summary": "accepted findings",
        "findings": [{
            "source_gate": "dep:auth", "id": "F1", "severity": "major",
            "location": "src/auth.py:42", "claim": "Expired refresh tokens are accepted.",
            "suggestion": "Reject expired refresh tokens.",
        }],
    }
    cand_text = json.dumps(candidate)
    cand_art = artifact_sha256(cand_text.encode("utf-8"))
    bindings = {"artifact_sha256": cand_art, "dag_sha256": dsha, "node_sha256": nsha,
                "target_sha256": _TGT}
    run.append("builder_output", gate="dep:auth", round=1, payload=cand_text,
               artifact_sha256=cand_art)
    run.append("symmetric_verdict", gate="dep:auth", round=1, outcome="CONSENSUS", objections=[],
               peers=_peers("dep:auth", 1, bindings), candidate=candidate, **bindings)
    run.append("accepted_finding_set", gate="dep:auth", round=1, payload=candidate, **bindings)
    run.append("gate_consensus", gate="dep:auth", round=1, **bindings)
    run.append("node_status_change", node="auth", status="done")

    cli = _run(["review-result", "--run", str(run.path)])
    assert cli.returncode == 0, cli.stderr
    recommendation = json.loads(cli.stdout)["recommendation"]
    assert recommendation == "REQUEST_CHANGES"

    md = render_markdown(run)
    assert "**Status:** CLEAN" in md  # workflow integrity is CLEAN ...
    assert f"**Review recommendation:** {recommendation}" in md  # ... yet recommends REQUEST_CHANGES


def test_report_flagged_recommendation_matches_review_result_cli(tmp_path):
    # A COMPLETE pr-review run settled via gate_proceeded_with_flags (on_cap: proceed_with_flags) is
    # FLAGGED, not CLEAN — yet it is a complete result under the review-result command semantics (every
    # node done and backed). The deterministic `review-result` CLI derives REQUEST_CHANGES from the
    # unresolved blocking objection, and the rendered report must AGREE: Summary FLAGGED with the same
    # separate recommendation, never suppressed as partial.
    run = init_run("flagged matches cli",
                   Config.from_dict({"final_review": False, "on_cap": "proceed_with_flags"}),
                   base_dir=tmp_path, workflow="pr-review")
    dag = DAG.from_dict({
        "nodes": [{"id": "auth", "title": "Auth", "description": "d", "files": ["auth.py"],
                   "test_plan": "pytest", "status": "done"}],
        "edges": [],
    })
    run.save_dag(dag.to_dict())
    _load_target(run)
    dsha, nsha = dag_sha256(dag), node_sha256(dag, "auth")

    _append_plan_consensus(run, dsha)

    candidate = {
        "summary": "accepted findings",
        "findings": [{
            "source_gate": "dep:auth", "id": "F1", "severity": "nit",
            "location": "src/auth.py:42", "claim": "A nit.", "suggestion": "Tidy it.",
        }],
    }
    cand_text = json.dumps(candidate)
    cand_art = artifact_sha256(cand_text.encode("utf-8"))
    bindings = {"artifact_sha256": cand_art, "dag_sha256": dsha, "node_sha256": nsha,
                "target_sha256": _TGT}
    objections = [{"id": "A:OBJ1", "severity": "blocker", "location": "candidate:F1",
                   "claim": "Peer A disputes the finding set.",
                   "suggestion": "Add the missing case."}]
    run.append("builder_output", gate="dep:auth", round=1, payload=cand_text,
               artifact_sha256=cand_art)
    run.append("symmetric_verdict", gate="dep:auth", round=1, outcome="PROCEED_WITH_FLAGS",
               objections=objections, peers=_peers("dep:auth", 1, bindings, objections),
               candidate=candidate, **bindings)
    run.append("accepted_finding_set", gate="dep:auth", round=1, payload=candidate,
               accepted_with_flags=True, open_objections=["A:OBJ1"], **bindings)
    run.append("gate_proceeded_with_flags", gate="dep:auth", round=1,
               open_findings=["A:OBJ1"], **bindings)
    run.append("node_status_change", node="auth", status="done")

    cli = _run(["review-result", "--run", str(run.path)])
    assert cli.returncode == 0, cli.stderr
    recommendation = json.loads(cli.stdout)["recommendation"]
    assert recommendation == "REQUEST_CHANGES"

    md = render_markdown(run)
    assert "**Status:** FLAGGED" in md  # proceeded with flags, not CLEAN ...
    assert f"**Review recommendation:** {recommendation}" in md  # ... yet still recommends REQUEST_CHANGES
