import pytest

from crucible.config import Config
from crucible.dag import DAG
from crucible.symmetric import (
    SYMMETRIC_WORKFLOWS,
    VALID_WORKFLOWS,
    AcceptedFinding,
    FindingSet,
    PeerAttestation,
    SymmetricDecision,
    accepted_findings,
    decide_symmetric,
    review_result,
    validate_final_finding_set,
    workflow_kind,
)


def _finding(source_gate="dep:auth", fid="F1", severity="major"):
    return {
        "source_gate": source_gate,
        "id": fid,
        "severity": severity,
        "location": "src/auth.py:42",
        "claim": "Expired token accepted.",
        "suggestion": "Reject it.",
    }


def _objection(fid="O1", severity="major"):
    return {
        "id": fid,
        "severity": severity,
        "location": "candidate:F1",
        "claim": "Finding lacks evidence.",
        "suggestion": "Add citation.",
    }


def _peer(peer, verdict="APPROVE", objections=None):
    return PeerAttestation.from_dict({
        "peer": peer,
        "gate": "dep:auth",
        "round": 1,
        "verdict": verdict,
        "summary": "review",
        "objections": objections or [],
        "artifact_sha256": "a" * 64,
        "dag_sha256": "d" * 64,
        "node_sha256": "n" * 64,
    })


# --- workflow kind -----------------------------------------------------------

def test_valid_and_symmetric_workflow_constants():
    assert VALID_WORKFLOWS == ("build", "deep-dive", "pr-review")
    assert SYMMETRIC_WORKFLOWS == ("deep-dive", "pr-review")
    # every symmetric workflow is a valid workflow, and build is not symmetric
    assert set(SYMMETRIC_WORKFLOWS) <= set(VALID_WORKFLOWS)
    assert "build" not in SYMMETRIC_WORKFLOWS


def test_workflow_kind_defaults_missing_metadata_to_build():
    assert workflow_kind([{"event": "run_start"}]) == "build"


def test_workflow_kind_reads_recorded_symmetric_workflow():
    assert workflow_kind([{"event": "run_start", "workflow": "pr-review"}]) == "pr-review"
    assert workflow_kind([{"event": "run_start", "workflow": "deep-dive"}]) == "deep-dive"


def test_workflow_kind_defaults_when_no_run_start():
    assert workflow_kind([]) == "build"
    assert workflow_kind([{"event": "builder_output"}]) == "build"


def test_workflow_kind_reads_first_run_start():
    events = [
        {"event": "run_start", "workflow": "deep-dive"},
        {"event": "run_start", "workflow": "pr-review"},
    ]
    assert workflow_kind(events) == "deep-dive"


def test_workflow_kind_defaults_malformed_metadata_to_build():
    # A non-string or unrecognized value can only arise from tampering (init_run validates the
    # value it writes); the reader still returns a valid workflow rather than propagate garbage.
    assert workflow_kind([{"event": "run_start", "workflow": 123}]) == "build"
    assert workflow_kind([{"event": "run_start", "workflow": "bogus"}]) == "build"


# --- accepted finding --------------------------------------------------------

def test_accepted_finding_from_dict_roundtrips():
    finding = AcceptedFinding.from_dict(_finding())
    assert finding.key == ("dep:auth", "F1")
    assert finding.to_dict() == _finding()


@pytest.mark.parametrize("field", ["source_gate", "id", "severity", "location", "claim", "suggestion"])
def test_accepted_finding_requires_non_empty_string_fields(field):
    data = _finding()
    data[field] = ""
    with pytest.raises(ValueError, match=field):
        AcceptedFinding.from_dict(data)
    missing = _finding()
    del missing[field]
    with pytest.raises(ValueError, match=field):
        AcceptedFinding.from_dict(missing)


def test_accepted_finding_rejects_invalid_severity():
    with pytest.raises(ValueError, match="severity"):
        AcceptedFinding.from_dict(_finding(severity="showstopper"))


# --- finding set -------------------------------------------------------------

def test_finding_set_rejects_duplicate_source_gate_and_id():
    with pytest.raises(ValueError, match="duplicate"):
        FindingSet.from_dict({"findings": [_finding(), _finding()]})


def test_finding_set_allows_same_id_across_source_gates():
    fs = FindingSet.from_dict({"findings": [
        _finding(source_gate="dep:auth", fid="F1"),
        _finding(source_gate="final", fid="F1"),
    ]})
    assert set(fs.by_key()) == {("dep:auth", "F1"), ("final", "F1")}


def test_finding_set_requires_findings_list():
    with pytest.raises(ValueError, match="findings"):
        FindingSet.from_dict({"findings": "nope"})
    with pytest.raises(ValueError):
        FindingSet.from_dict([])


def test_finding_set_summary_optional_and_typed():
    assert FindingSet.from_dict({"findings": []}).summary == ""
    assert FindingSet.from_dict({"summary": "s", "findings": []}).summary == "s"
    with pytest.raises(ValueError, match="summary"):
        FindingSet.from_dict({"summary": 1, "findings": []})


def test_finding_set_to_dict_is_canonical():
    fs = FindingSet.from_dict({"summary": "s", "findings": [_finding()]})
    assert fs.to_dict() == {"summary": "s", "findings": [_finding()]}


def test_dependency_finding_set_requires_current_source_gate():
    fs = FindingSet.from_dict({"findings": [_finding(source_gate="dep:other")]})
    with pytest.raises(ValueError, match="source_gate"):
        fs.validate_for_gate("dep:auth")


def test_dependency_finding_set_accepts_matching_source_gate():
    fs = FindingSet.from_dict({"findings": [_finding(source_gate="dep:auth")]})
    fs.validate_for_gate("dep:auth")  # does not raise


# --- peer attestation --------------------------------------------------------

def test_peer_attestation_requires_valid_slot():
    with pytest.raises(ValueError, match="peer"):
        _peer("C")


def test_peer_attestation_requires_valid_verdict():
    with pytest.raises(ValueError, match="verdict"):
        _peer("A", verdict="MAYBE")


def test_peer_attestation_rejects_duplicate_objection_ids():
    with pytest.raises(ValueError, match="duplicate"):
        _peer("A", verdict="REQUEST_CHANGES",
               objections=[_objection("O1"), _objection("O1")])


def test_peer_attestation_echoes_bindings():
    peer = _peer("A")
    assert peer.artifact_sha256 == "a" * 64
    assert peer.dag_sha256 == "d" * 64
    assert peer.node_sha256 == "n" * 64


def test_peer_approve_rejects_blocking_objection():
    peer = _peer("A", objections=[{
        "id": "O1", "severity": "major", "location": "candidate:F1",
        "claim": "Finding lacks evidence.", "suggestion": "Add citation.",
    }])
    assert peer.consistency_error(Config.from_dict({})) is not None


def test_peer_approve_allows_nonblocking_objection():
    peer = _peer("A", objections=[_objection(severity="nit")])
    assert peer.consistency_error(Config.from_dict({})) is None


def test_peer_request_changes_requires_blocking_objection():
    peer = _peer("A", verdict="REQUEST_CHANGES", objections=[_objection(severity="nit")])
    assert peer.consistency_error(Config.from_dict({})) is not None


def test_peer_request_changes_with_blocker_is_consistent():
    peer = _peer("A", verdict="REQUEST_CHANGES", objections=[_objection(severity="major")])
    assert peer.consistency_error(Config.from_dict({})) is None


# --- symmetric decision ------------------------------------------------------

def test_two_approvals_reach_consensus_even_when_candidate_contains_blocker():
    decision = decide_symmetric(
        _peer("A"), _peer("B"), Config.from_dict({}), 1, 5
    )
    assert isinstance(decision, SymmetricDecision)
    assert decision.outcome == "CONSENSUS"
    assert decision.open_objections == []


def test_symmetric_decision_namespaces_and_retains_both_peer_objections():
    peer_a = _peer("A", verdict="REQUEST_CHANGES", objections=[_objection("F1", "major")])
    peer_b = _peer("B", verdict="REQUEST_CHANGES", objections=[_objection("F1", "blocker")])
    decision = decide_symmetric(peer_a, peer_b, Config.from_dict({}), 1, 5)
    assert decision.outcome == "CHANGES"
    ids = [o.id for o in decision.open_objections]
    assert ids == ["A:F1", "B:F1"]


def test_symmetric_decision_ignores_nonblocking_objections():
    peer_a = _peer("A", objections=[_objection(severity="nit")])
    peer_b = _peer("B", objections=[_objection(severity="minor")])
    decision = decide_symmetric(peer_a, peer_b, Config.from_dict({}), 1, 5)
    assert decision.outcome == "CONSENSUS"
    assert decision.open_objections == []


def test_symmetric_decision_caps_when_halt_configured():
    peer_a = _peer("A", verdict="REQUEST_CHANGES", objections=[_objection(severity="major")])
    peer_b = _peer("B")
    cfg = Config.from_dict({"on_cap": "halt"})
    decision = decide_symmetric(peer_a, peer_b, cfg, 5, 5)
    assert decision.outcome == "CAPPED"
    assert [o.id for o in decision.open_objections] == ["A:O1"]


def test_symmetric_decision_proceeds_with_flags_at_cap():
    peer_a = _peer("A", verdict="REQUEST_CHANGES", objections=[_objection(severity="major")])
    peer_b = _peer("B")
    cfg = Config.from_dict({"on_cap": "proceed_with_flags"})
    decision = decide_symmetric(peer_a, peer_b, cfg, 5, 5)
    assert decision.outcome == "PROCEED_WITH_FLAGS"
    assert [o.id for o in decision.open_objections] == ["A:O1"]


def test_symmetric_decision_changes_before_cap_with_single_peer_blocker():
    peer_a = _peer("A", verdict="REQUEST_CHANGES", objections=[_objection(severity="blocker")])
    peer_b = _peer("B")
    decision = decide_symmetric(peer_a, peer_b, Config.from_dict({}), 2, 5)
    assert decision.outcome == "CHANGES"


# --- peer slot provenance (A=builder, B=critic) ------------------------------

def test_peer_slot_provenance_maps_a_to_builder_b_to_critic():
    from crucible.symmetric import peer_slot_provenance

    cfg = Config.from_dict({
        "builder": {"model": "model-a", "effort": "high"},
        "critic": {"model": "model-b", "effort": "low"},
    })
    prov = peer_slot_provenance(cfg)
    assert prov == {
        "A": {"model": "model-a", "effort": "high"},
        "B": {"model": "model-b", "effort": "low"},
    }

# --- accepted-finding aggregation, FINAL inclusion, and review result --------
#
# These exercise the pure result-projection helpers (Task 3): the deterministic union of accepted
# dependency finding sets, FINAL inclusion validation, and the deep-dive/pr-review result +
# recommendation. Events are constructed by hand (no run-log/CLI) so the projection contract is
# proven at the module boundary the CLI commands and the report both call.


def _bindings(a="a", d="d", n="n"):
    return {"artifact_sha256": a * 64, "dag_sha256": d * 64, "node_sha256": n * 64}


def _obj(fid="A:O1", severity="blocker"):
    return {"id": fid, "severity": severity, "location": "candidate:F1",
            "claim": "Set is incomplete.", "suggestion": "Add the missing case."}


def _gate_events(gate, *, findings=None, outcome="CONSENSUS", objections=None, bindings=None, rnd=1):
    """A gate's symmetric_verdict [-> accepted_finding_set] -> terminal, in atomic-decision order.

    ``outcome`` is CONSENSUS / PROCEED_WITH_FLAGS (both advance and persist an accepted set) or
    CAPPED (no accepted set). ``objections`` are namespaced aggregate objection dicts carried on the
    verdict + terminal for non-consensus outcomes.
    """
    b = bindings or _bindings()
    objections = objections or []
    payload = {"summary": "", "findings": findings if findings is not None else []}
    evs = [{"event": "symmetric_verdict", "gate": gate, "round": rnd, "outcome": outcome,
            "objections": objections, **b}]
    if outcome in ("CONSENSUS", "PROCEED_WITH_FLAGS"):
        acc = {"event": "accepted_finding_set", "gate": gate, "round": rnd, "payload": payload, **b}
        if outcome == "PROCEED_WITH_FLAGS":
            acc["accepted_with_flags"] = True
            acc["open_objections"] = [o["id"] for o in objections]
        evs.append(acc)
    terminal = {"CONSENSUS": "gate_consensus", "PROCEED_WITH_FLAGS": "gate_proceeded_with_flags",
                "CAPPED": "gate_capped"}[outcome]
    tev = {"event": terminal, "gate": gate, "round": rnd, **b}
    if outcome in ("PROCEED_WITH_FLAGS", "CAPPED"):
        tev["open_findings"] = [o["id"] for o in objections]
    evs.append(tev)
    return evs


def _dep_events(node, **kwargs):
    return _gate_events(f"dep:{node}", **kwargs)


def _two_done_dag():
    """A two-node DAG where 'b' depends on 'a', so topological order is [a, b]."""
    return DAG.from_dict({
        "nodes": [
            {"id": "b", "title": "B", "description": "", "files": [], "test_plan": "",
             "status": "done"},
            {"id": "a", "title": "A", "description": "", "files": [], "test_plan": "",
             "status": "done"},
        ],
        "edges": [{"from": "b", "depends_on": "a"}],
    })


def test_accepted_findings_unions_dependency_sets_in_topological_order():
    dag = _two_done_dag()
    events = (
        _dep_events("b", findings=[_finding("dep:b", "F1")], bindings=_bindings("b", "d", "nb"))
        + _dep_events("a", findings=[_finding("dep:a", "F1")], bindings=_bindings("a", "d", "na"))
    )
    fs = accepted_findings(events, dag)
    # 'a' is a dependency of 'b', so its findings come first regardless of event/log order.
    assert [f.source_gate for f in fs.findings] == ["dep:a", "dep:b"]


def test_accepted_findings_preserves_finding_order_within_a_set():
    events = _dep_events("auth", findings=[
        _finding("dep:auth", "F2"), _finding("dep:auth", "F1"),
    ])
    fs = accepted_findings(events)
    assert [f.id for f in fs.findings] == ["F2", "F1"]


def test_accepted_findings_excludes_pre_terminal_orphan_events():
    # An accepted set with no matching advancing terminal is incomplete history, never accepted.
    events = [e for e in _dep_events("auth", findings=[_finding("dep:auth", "F1")])
              if e["event"] != "gate_consensus"]
    assert accepted_findings(events).findings == []


def test_accepted_findings_excludes_post_terminal_orphan_events():
    b = _bindings()
    events = _dep_events("auth", findings=[_finding("dep:auth", "F1")], bindings=b)
    events.append({"event": "accepted_finding_set", "gate": "dep:auth", "round": 1,
                   "payload": {"summary": "", "findings": [_finding("dep:auth", "F2")]}, **b})
    # only the pre-terminal accepted set counts; the post-terminal one is orphan residue.
    assert [f.id for f in accepted_findings(events).findings] == ["F1"]


def test_accepted_findings_excludes_binding_mismatched_accepted_set():
    # The accepted set's bindings differ from the gate's advancing terminal => not effective.
    events = _dep_events("auth", findings=[_finding("dep:auth", "F1")], bindings=_bindings())
    for e in events:
        if e["event"] == "accepted_finding_set":
            e["artifact_sha256"] = "0" * 64
    assert accepted_findings(events).findings == []


def test_accepted_findings_excludes_peer_decision_binding_mismatch():
    # F1: the two peers' symmetric_verdict binds artifact A, but the accepted set + terminal bind
    # artifact B (consistent with each other). The accepted result is NOT the candidate the peers
    # reviewed, so it is corrupt history and never effective.
    events = _dep_events("auth", findings=[_finding("dep:auth", "F1")], bindings=_bindings())
    for e in events:
        if e["event"] == "symmetric_verdict":
            e["artifact_sha256"] = "0" * 64
    assert accepted_findings(events).findings == []


def test_accepted_finding_set_for_gate_excludes_peer_decision_binding_mismatch():
    # The single-gate lookup mirrors the union: a binding-mismatched peer decision yields no
    # effective accepted set for the gate.
    from crucible.symmetric import accepted_finding_set_for_gate

    events = _dep_events("auth", findings=[_finding("dep:auth", "F1")], bindings=_bindings())
    for e in events:
        if e["event"] == "symmetric_verdict":
            e["dag_sha256"] = "0" * 64
    assert accepted_finding_set_for_gate(events, "dep:auth") is None


def test_accepted_findings_excludes_two_pre_terminal_accepted_sets_distinct_ids():
    # Exactly one accepted set may bracket a gate's advancing terminal. Two pre-terminal accepted
    # sets for the same gate/round/bindings — even with DISTINCT finding IDs, so no key collision —
    # violate the atomic symmetric_verdict -> accepted_finding_set -> terminal contract, so NEITHER
    # is effective and the gate has no accepted state.
    from crucible.symmetric import accepted_finding_set_for_gate

    b = _bindings()
    events = _dep_events("auth", findings=[_finding("dep:auth", "F1")], bindings=b)
    # events == [symmetric_verdict, accepted_finding_set, gate_consensus]; insert a SECOND accepted
    # set (distinct id F2) still before the terminal.
    events.insert(2, {"event": "accepted_finding_set", "gate": "dep:auth", "round": 1,
                      "payload": {"summary": "", "findings": [_finding("dep:auth", "F2")]}, **b})
    assert accepted_findings(events).findings == []
    assert accepted_finding_set_for_gate(events, "dep:auth") is None


def test_accepted_findings_excludes_non_immediate_peer_decision():
    # The matching peer decision must be the protocol event IMMEDIATELY preceding the accepted set.
    # A matching symmetric_verdict followed by a LATER mismatched one before the accepted set breaks
    # the atomic trio (the immediate predecessor bound a different candidate), so it is not effective.
    b = _bindings()
    events = _dep_events("auth", findings=[_finding("dep:auth", "F1")], bindings=b)
    mismatched = {"event": "symmetric_verdict", "gate": "dep:auth", "round": 1,
                  "outcome": "CONSENSUS", "objections": [],
                  **{**b, "artifact_sha256": "9" * 64}}
    events.insert(1, mismatched)  # [sv(match), sv(mismatch), accepted_finding_set, terminal]
    assert accepted_findings(events).findings == []


def test_accepted_findings_excludes_intervening_protocol_event_before_terminal():
    # No same-gate/round protocol event may intervene between the accepted set and its terminal: the
    # accepted set must be the protocol event immediately before the advancing terminal.
    b = _bindings()
    events = _dep_events("auth", findings=[_finding("dep:auth", "F1")], bindings=b)
    intervening = {"event": "symmetric_verdict", "gate": "dep:auth", "round": 1,
                   "outcome": "CONSENSUS", "objections": [], **b}
    events.insert(2, intervening)  # [sv, accepted_finding_set, sv(intervening), terminal]
    assert accepted_findings(events).findings == []


def test_accepted_findings_rejects_duplicate_composite_keys_across_gates():
    # Two DIFFERENT dependency gates whose (individually valid) accepted trios both carry the same
    # (source_gate, id) union to a duplicate composite key and are rejected. A cross-gate duplicate is
    # only reachable via forged history — write-time AND the DAG-aware Finish path both reject a
    # mis-scoped source_gate (round-7 F1) — so the duplicate-key rule is exercised through the no-DAG
    # partial helper, whose deterministic union still fails closed on a duplicate key.
    events = (
        _dep_events("a", findings=[_finding("dep:a", "F1")], bindings=_bindings("a", "d", "na"))
        + _dep_events("b", findings=[_finding("dep:a", "F1")], bindings=_bindings("b", "d", "nb"))
    )
    with pytest.raises(ValueError, match="duplicate"):
        accepted_findings(events)


def test_accepted_findings_rejects_malformed_effective_payload():
    b = _bindings()
    events = [
        {"event": "symmetric_verdict", "gate": "dep:auth", "round": 1, "outcome": "CONSENSUS", **b},
        {"event": "accepted_finding_set", "gate": "dep:auth", "round": 1,
         "payload": {"findings": "not-a-list"}, **b},
        {"event": "gate_consensus", "gate": "dep:auth", "round": 1, **b},
    ]
    with pytest.raises(ValueError):
        accepted_findings(events)


# --- FINAL inclusion ---------------------------------------------------------

def test_validate_final_finding_set_accepts_inclusive_candidate():
    prior = FindingSet.from_dict({"findings": [_finding("dep:auth", "F1")]})
    candidate = FindingSet.from_dict({"findings": [
        _finding("dep:auth", "F1"), _finding("final", "C1"),
    ]})
    validate_final_finding_set(candidate, prior)  # does not raise


def test_validate_final_finding_set_rejects_dropped_prior_finding():
    prior = FindingSet.from_dict({"findings": [_finding("dep:auth", "F1")]})
    candidate = FindingSet.from_dict({"findings": [_finding("final", "C1")]})
    with pytest.raises(ValueError, match="F1"):
        validate_final_finding_set(candidate, prior)


def test_validate_final_finding_set_rejects_altered_prior_finding():
    prior = FindingSet.from_dict({"findings": [_finding("dep:auth", "F1", severity="major")]})
    candidate = FindingSet.from_dict({"findings": [_finding("dep:auth", "F1", severity="nit")]})
    with pytest.raises(ValueError):
        validate_final_finding_set(candidate, prior)


def test_validate_final_finding_set_rejects_non_final_extra():
    prior = FindingSet.from_dict({"findings": [_finding("dep:auth", "F1")]})
    candidate = FindingSet.from_dict({"findings": [
        _finding("dep:auth", "F1"), _finding("dep:auth", "F2"),
    ]})
    with pytest.raises(ValueError, match="final"):
        validate_final_finding_set(candidate, prior)


# --- deterministic review result + recommendation ----------------------------

def test_review_result_pr_review_blocking_severity_requests_changes():
    events = _dep_events("auth", findings=[_finding("dep:auth", "F1", "major")])
    result = review_result(events, Config.from_dict({}), "pr-review")
    assert result["workflow"] == "pr-review"
    assert result["recommendation"] == "REQUEST_CHANGES"
    assert result["findings"][0]["id"] == "F1"


def test_review_result_nonblocking_major_is_comment_when_only_blocker_blocks():
    events = _dep_events("auth", findings=[_finding("dep:auth", "F1", "major")])
    cfg = Config.from_dict({"blocking_severities": ["blocker"]})
    assert review_result(events, cfg, "pr-review")["recommendation"] == "COMMENT"


def test_review_result_minor_or_nit_only_is_comment():
    events = _dep_events("auth", findings=[_finding("dep:auth", "F1", "minor")])
    assert review_result(events, Config.from_dict({}), "pr-review")["recommendation"] == "COMMENT"


def test_review_result_no_findings_is_approve():
    events = _dep_events("auth", findings=[])
    result = review_result(events, Config.from_dict({}), "pr-review")
    assert result["recommendation"] == "APPROVE"
    assert result["findings"] == []


def test_review_result_proceeded_with_flags_objection_requests_changes():
    # The accepted findings are all nonblocking, but an unresolved blocking peer objection remains.
    events = _dep_events("auth", findings=[_finding("dep:auth", "F1", "nit")],
                         outcome="PROCEED_WITH_FLAGS",
                         objections=[_obj("A:O1", "blocker")])
    cfg = Config.from_dict({"on_cap": "proceed_with_flags"})
    result = review_result(events, cfg, "pr-review")
    assert result["recommendation"] == "REQUEST_CHANGES"
    assert result["unresolved_objections"]


def test_review_result_capped_objection_requests_changes():
    events = _dep_events("auth", findings=[], outcome="CAPPED",
                         objections=[_obj("A:O1", "blocker")])
    cfg = Config.from_dict({"on_cap": "halt"})
    result = review_result(events, cfg, "pr-review")
    assert result["recommendation"] == "REQUEST_CHANGES"


def test_review_result_deep_dive_omits_recommendation():
    events = _dep_events("auth", findings=[_finding("dep:auth", "F1", "major")])
    result = review_result(events, Config.from_dict({}), "deep-dive")
    assert "recommendation" not in result
    assert result["findings"][0]["id"] == "F1"


def test_review_result_final_set_replaces_dependency_union():
    events = (
        _dep_events("auth", findings=[_finding("dep:auth", "F1", "major")],
                    bindings=_bindings("a", "d", "na"))
        + _gate_events("final", findings=[
            _finding("dep:auth", "F1", "major"), _finding("final", "C1", "nit"),
        ], bindings=_bindings("f", "d", "n"))
    )
    result = review_result(events, Config.from_dict({}), "pr-review")
    keys = {(f["source_gate"], f["id"]) for f in result["findings"]}
    assert ("final", "C1") in keys and ("dep:auth", "F1") in keys


def test_review_result_ignores_final_set_when_final_review_disabled():
    # Round-3 F1: with final_review disabled a (forged) valid-looking FINAL accepted set must NEVER
    # become the effective result — the dependency union is the run's effective accepted finding set
    # (design: "If FINAL review is disabled, the dependency union is the run's effective accepted
    # finding set"). review_result reads cfg.final_review and never promotes FINAL when it is off.
    events = (
        _dep_events("auth", findings=[_finding("dep:auth", "F1", "major")],
                    bindings=_bindings("a", "d", "na"))
        + _gate_events("final", findings=[
            _finding("dep:auth", "F1", "major"), _finding("final", "C1", "blocker"),
        ], bindings=_bindings("f", "d", "nf"))
    )
    result = review_result(events, Config.from_dict({"final_review": False}), "pr-review")
    keys = {(f["source_gate"], f["id"]) for f in result["findings"]}
    assert ("final", "C1") not in keys
    assert ("dep:auth", "F1") in keys


# --- out-of-scope accepted sets: DAG-aware fail-close vs no-DAG partial helper ---

def _one_done_dag(node="a"):
    """A single done-node DAG (only ``node`` exists), so any other ``dep:<id>`` is out of scope."""
    return DAG.from_dict({
        "nodes": [{"id": node, "title": node.upper(), "description": "", "files": [],
                   "test_plan": "", "status": "done"}],
        "edges": [],
    })


def test_accepted_findings_with_dag_rejects_out_of_scope_dependency_gate():
    # Round-3 F2: a fully bound-looking dep:ghost trio whose node is absent from the current DAG must
    # NOT be silently sorted after known nodes and published. With a DAG supplied, accepted_findings
    # fails closed (deterministic fail-close on an unknown gate).
    dag = _two_done_dag()  # nodes 'a' and 'b'
    events = (
        _dep_events("a", findings=[_finding("dep:a", "F1")], bindings=_bindings("a", "d", "na"))
        + _dep_events("ghost", findings=[_finding("dep:ghost", "G1")],
                      bindings=_bindings("g", "d", "ng"))
    )
    with pytest.raises(ValueError, match="ghost"):
        accepted_findings(events, dag)


def test_accepted_findings_without_dag_is_partial_helper_over_all_dep_gates():
    # No-DAG partial-helper semantics (explicit): the report-time aggregation cannot know the DAG, so
    # it unions every effective dep:<id> accepted set in LOG order WITHOUT scope validation. A gate
    # not in any current tree is still included here — scope enforcement is the DAG-aware Finish-time
    # path's job, never this best-effort partial helper's.
    events = (
        _dep_events("a", findings=[_finding("dep:a", "F1")], bindings=_bindings("a", "d", "na"))
        + _dep_events("ghost", findings=[_finding("dep:ghost", "G1")],
                      bindings=_bindings("g", "d", "ng"))
    )
    fs = accepted_findings(events)  # no dag => partial helper
    assert [f.source_gate for f in fs.findings] == ["dep:a", "dep:ghost"]


def test_out_of_scope_accepted_gates_detects_ghost_dep_and_disabled_final():
    from crucible.symmetric import out_of_scope_accepted_gates

    dag = _one_done_dag("a")
    events = (
        _dep_events("a", findings=[_finding("dep:a", "F1")], bindings=_bindings("a", "d", "na"))
        + _dep_events("ghost", findings=[_finding("dep:ghost", "G1")],
                      bindings=_bindings("g", "d", "ng"))
        + _gate_events("final", findings=[_finding("dep:a", "F1"), _finding("final", "C1")],
                       bindings=_bindings("f", "d", "nf"))
    )
    # FINAL disabled => both the ghost dep and the FINAL set are out of scope.
    assert out_of_scope_accepted_gates(events, dag, final_enabled=False) == ["dep:ghost", "final"]
    # FINAL enabled => only the ghost dep is out of scope.
    assert out_of_scope_accepted_gates(events, dag, final_enabled=True) == ["dep:ghost"]


def test_require_complete_symmetric_run_rejects_out_of_scope_dependency_gate():
    from crucible.symmetric import require_complete_symmetric_run

    dag = _one_done_dag("a")
    events = (
        _dep_events("a", findings=[_finding("dep:a", "F1")], bindings=_bindings("a", "d", "na"))
        + _dep_events("ghost", findings=[_finding("dep:ghost", "G1")],
                      bindings=_bindings("g", "d", "ng"))
    )
    with pytest.raises(ValueError, match="incomplete symmetric workflow"):
        require_complete_symmetric_run(events, dag, require_final=False, final_enabled=False)


def test_require_complete_symmetric_run_rejects_final_set_when_final_review_disabled():
    from crucible.symmetric import require_complete_symmetric_run

    dag = _one_done_dag("a")
    events = (
        _dep_events("a", findings=[_finding("dep:a", "F1")], bindings=_bindings("a", "d", "na"))
        + _gate_events("final", findings=[_finding("dep:a", "F1"), _finding("final", "C1")],
                       bindings=_bindings("f", "d", "nf"))
    )
    with pytest.raises(ValueError, match="incomplete symmetric workflow"):
        require_complete_symmetric_run(events, dag, require_final=False, final_enabled=False)


def test_require_complete_symmetric_run_accepts_in_scope_final_when_enabled():
    from crucible.symmetric import require_complete_symmetric_run

    dag = _one_done_dag("a")
    events = (
        _dep_events("a", findings=[_finding("dep:a", "F1")], bindings=_bindings("a", "d", "na"))
        + _gate_events("final", findings=[_finding("dep:a", "F1"), _finding("final", "C1")],
                       bindings=_bindings("f", "d", "nf"))
    )
    # When FINAL is configured, the in-scope FINAL set does not trip the out-of-scope guard.
    require_complete_symmetric_run(events, dag, require_final=True, final_enabled=True)


# --- round-4: shared protocol-wide scope guard over objection-bearing gates -------------------
#
# The accepted-set scope guard (out_of_scope_accepted_gates) only sees gates that persisted an
# accepted finding SET. A CAPPED (halt) gate — and a forged out-of-scope trio that only records a
# symmetric_verdict + capped/proceeded terminal — carries the peers' objections but NO accepted set,
# so it slips past the accepted-set guard entirely while its objections still feed the recommendation.
# The protocol-wide guard closes that gap over ANY symmetric_verdict/accepted/terminal event.

def test_out_of_scope_protocol_gates_detects_capped_ghost_and_disabled_final():
    from crucible.symmetric import out_of_scope_accepted_gates, out_of_scope_protocol_gates

    dag = _one_done_dag("a")
    events = (
        _dep_events("a", findings=[_finding("dep:a", "F1")], bindings=_bindings("a", "d", "na"))
        + _dep_events("ghost", outcome="CAPPED", objections=[_obj("A:G1", "blocker")],
                      bindings=_bindings("g", "d", "ng"))
        + _gate_events("final", outcome="CAPPED", objections=[_obj("A:C1", "blocker")],
                       bindings=_bindings("f", "d", "nf"))
    )
    # These capped gates carry objections but NO accepted set, so the accepted-set guard misses them.
    assert out_of_scope_accepted_gates(events, dag, final_enabled=False) == []
    # The protocol-wide guard catches both: the ghost dep, and — with FINAL off — the FINAL gate.
    assert out_of_scope_protocol_gates(events, dag, final_enabled=False) == ["dep:ghost", "final"]
    # FINAL enabled => only the ghost dep remains out of scope.
    assert out_of_scope_protocol_gates(events, dag, final_enabled=True) == ["dep:ghost"]


def test_out_of_scope_protocol_gates_ignores_in_scope_and_non_finding_gates():
    from crucible.symmetric import out_of_scope_protocol_gates

    dag = _one_done_dag("a")
    events = (
        # An in-scope PLAN symmetric decision must never be flagged (it is not a dependency/FINAL
        # finding gate; it is validated by the PLAN checks in workflow_issues).
        _gate_events("plan", outcome="CAPPED", objections=[_obj("A:P1", "blocker")],
                     bindings=_bindings("p", "d", "np"))
        + _dep_events("a", outcome="PROCEED_WITH_FLAGS", objections=[_obj("A:F1", "blocker")],
                      bindings=_bindings("a", "d", "na"))
    )
    assert out_of_scope_protocol_gates(events, dag, final_enabled=False) == []


def test_unresolved_objections_fails_closed_on_out_of_scope_ghost_dep():
    from crucible.symmetric import unresolved_objections

    dag = _one_done_dag("a")
    events = (
        _dep_events("a", outcome="PROCEED_WITH_FLAGS", objections=[_obj("A:F1", "blocker")],
                    bindings=_bindings("a", "d", "na"))
        + _dep_events("ghost", outcome="CAPPED", objections=[_obj("A:G1", "blocker")],
                      bindings=_bindings("g", "d", "ng"))
    )
    # Fail closed — never silently drop the forged objection, never publish it.
    with pytest.raises(ValueError, match="ghost"):
        unresolved_objections(events, dag, final_enabled=False)


def test_unresolved_objections_fails_closed_on_forbidden_final_objection():
    from crucible.symmetric import unresolved_objections

    dag = _one_done_dag("a")
    events = (
        _dep_events("a", outcome="PROCEED_WITH_FLAGS", objections=[_obj("A:F1", "blocker")],
                    bindings=_bindings("a", "d", "na"))
        + _gate_events("final", outcome="CAPPED", objections=[_obj("A:C1", "blocker")],
                       bindings=_bindings("f", "d", "nf"))
    )
    # FINAL is off => a FINAL objection is a configured-forbidden phase; refuse to publish it.
    with pytest.raises(ValueError, match="final"):
        unresolved_objections(events, dag, final_enabled=False)
    # FINAL on => the same objection is in scope and retained (both peers' disputes survive).
    objs = unresolved_objections(events, dag, final_enabled=True)
    assert {o["id"] for o in objs} == {"A:F1", "A:C1"}


def test_unresolved_objections_retains_in_scope_objections_with_dag():
    from crucible.symmetric import unresolved_objections

    dag = _one_done_dag("a")
    events = _dep_events("a", outcome="PROCEED_WITH_FLAGS", objections=[_obj("A:F1", "blocker")],
                         bindings=_bindings("a", "d", "na"))
    assert [o["id"] for o in unresolved_objections(events, dag, final_enabled=False)] == ["A:F1"]


def test_unresolved_objections_without_dag_is_unvalidated_partial_helper():
    from crucible.symmetric import unresolved_objections

    # No DAG => no scope to validate against (mirrors accepted_findings' no-DAG partial helper): the
    # legacy/direct call cannot fail closed on scope, so it just returns objections in log order.
    events = _dep_events("ghost", outcome="CAPPED", objections=[_obj("A:G1", "blocker")],
                         bindings=_bindings("g", "d", "ng"))
    assert [o["id"] for o in unresolved_objections(events)] == ["A:G1"]


def test_require_complete_symmetric_run_rejects_capped_out_of_scope_dependency_gate():
    from crucible.symmetric import require_complete_symmetric_run

    dag = _one_done_dag("a")
    events = (
        _dep_events("a", findings=[_finding("dep:a", "F1")], bindings=_bindings("a", "d", "na"))
        + _dep_events("ghost", outcome="CAPPED", objections=[_obj("A:G1", "blocker")],
                      bindings=_bindings("g", "d", "ng"))
    )
    with pytest.raises(ValueError, match="incomplete symmetric workflow"):
        require_complete_symmetric_run(events, dag, require_final=False, final_enabled=False)


def test_require_complete_symmetric_run_rejects_capped_final_when_final_review_disabled():
    from crucible.symmetric import require_complete_symmetric_run

    dag = _one_done_dag("a")
    events = (
        _dep_events("a", findings=[_finding("dep:a", "F1")], bindings=_bindings("a", "d", "na"))
        + _gate_events("final", outcome="CAPPED", objections=[_obj("A:C1", "blocker")],
                       bindings=_bindings("f", "d", "nf"))
    )
    with pytest.raises(ValueError, match="incomplete symmetric workflow"):
        require_complete_symmetric_run(events, dag, require_final=False, final_enabled=False)


def test_review_result_fails_closed_on_out_of_scope_objection_with_dag():
    dag = _one_done_dag("a")
    events = (
        _dep_events("a", outcome="PROCEED_WITH_FLAGS", objections=[_obj("A:F1", "blocker")],
                    bindings=_bindings("a", "d", "na"))
        + _dep_events("ghost", outcome="CAPPED", objections=[_obj("A:G1", "blocker")],
                      bindings=_bindings("g", "d", "ng"))
    )
    with pytest.raises(ValueError, match="ghost"):
        review_result(events, Config.from_dict({"final_review": False}), "pr-review", dag)


def test_review_result_retains_in_scope_objection_with_dag():
    dag = _one_done_dag("a")
    events = _dep_events("a", outcome="PROCEED_WITH_FLAGS", objections=[_obj("A:F1", "blocker")],
                         bindings=_bindings("a", "d", "na"))
    result = review_result(events, Config.from_dict({"final_review": False}), "pr-review", dag)
    assert result["recommendation"] == "REQUEST_CHANGES"
    assert [o["id"] for o in result["unresolved_objections"]] == ["A:F1"]


# --- round-5 F1: unresolved objections come from the TERMINAL-BOUND peer decision ---------------
#
# A gate's unresolved objections are the peers' still-open disputes recorded by the symmetric_verdict
# that led to the gate's authoritative terminal. A same-gate/same-round symmetric_verdict appended
# AFTER that terminal is forged/crash residue and must never be selected — otherwise an empty
# post-terminal verdict could erase the terminal's blocker (REQUEST_CHANGES -> COMMENT) or a padded
# one could inflate it.

def _post_terminal_verdict(gate, objections, bindings, rnd=1):
    return {"event": "symmetric_verdict", "gate": gate, "round": rnd,
            "outcome": "PROCEED_WITH_FLAGS", "objections": objections,
            "candidate": {"summary": "", "findings": []}, **bindings}


def test_unresolved_objections_ignores_post_terminal_verdict_erasure():
    from crucible.symmetric import unresolved_objections

    b = _bindings("a", "d", "na")
    events = _dep_events("a", outcome="PROCEED_WITH_FLAGS",
                         objections=[_obj("A:F1", "blocker")], bindings=b)
    # A forged post-terminal verdict with NO objections must not erase the terminal's blocker.
    events.append(_post_terminal_verdict("dep:a", [], b))
    assert [o["id"] for o in unresolved_objections(events)] == ["A:F1"]


def test_unresolved_objections_ignores_post_terminal_verdict_inflation():
    from crucible.symmetric import unresolved_objections

    b = _bindings("a", "d", "na")
    events = _dep_events("a", outcome="PROCEED_WITH_FLAGS",
                         objections=[_obj("A:F1", "blocker")], bindings=b)
    # A forged post-terminal verdict with extra objections must not inflate the terminal's set.
    events.append(_post_terminal_verdict(
        "dep:a", [_obj("A:F1", "blocker"), _obj("A:FAKE", "blocker")], b))
    assert [o["id"] for o in unresolved_objections(events)] == ["A:F1"]


def test_review_result_ignores_post_terminal_verdict_erasing_blocker():
    # The erase attack at the projection boundary: the accepted findings are empty and the only
    # blocker is an unresolved proceeded-with-flags objection. A post-terminal empty verdict would
    # flip the recommendation to APPROVE; the terminal-bound derivation keeps it REQUEST_CHANGES.
    b = _bindings("a", "d", "na")
    events = _dep_events("a", findings=[], outcome="PROCEED_WITH_FLAGS",
                         objections=[_obj("A:F1", "blocker")], bindings=b)
    events.append(_post_terminal_verdict("dep:a", [], b))
    cfg = Config.from_dict({"on_cap": "proceed_with_flags"})
    assert review_result(events, cfg, "pr-review")["recommendation"] == "REQUEST_CHANGES"


def test_gate_post_terminal_protocol_indices_detects_residue():
    from crucible.symmetric import gate_post_terminal_protocol_indices

    b = _bindings("a", "d", "na")
    events = _dep_events("a", outcome="PROCEED_WITH_FLAGS",
                         objections=[_obj("A:F1", "blocker")], bindings=b)
    assert gate_post_terminal_protocol_indices(events, "dep:a") == []
    events.append(_post_terminal_verdict("dep:a", [], b))
    # The appended verdict is a same-gate protocol event after the authoritative terminal.
    assert gate_post_terminal_protocol_indices(events, "dep:a") == [len(events) - 1]


# --- round-5 F3: the scope guard accepts only plan / in-scope dep / enabled final ---------------
#
# Legitimate symmetric protocol gates are exactly ``plan``, a current ``dep:<id>``, and — only when
# enabled — ``final``. Any other gate carrying a symmetric protocol event (an arbitrary name like
# ``sidequest``, or ``reproduce`` which symmetric skills never use) is out of scope, so a forged
# capped/proceeded objection on it can never reach the recommendation.

def test_out_of_scope_protocol_gates_rejects_arbitrary_gate_name():
    from crucible.symmetric import out_of_scope_protocol_gates

    dag = _one_done_dag("a")
    events = (
        _dep_events("a", outcome="PROCEED_WITH_FLAGS", objections=[_obj("A:F1", "blocker")],
                    bindings=_bindings("a", "d", "na"))
        + _gate_events("sidequest", outcome="CAPPED", objections=[_obj("A:S1", "blocker")],
                       bindings=_bindings("s", "d", "ns"))
    )
    assert out_of_scope_protocol_gates(events, dag, final_enabled=True) == ["sidequest"]


def test_out_of_scope_protocol_gates_rejects_reproduce_protocol_event():
    from crucible.symmetric import out_of_scope_protocol_gates

    dag = _one_done_dag("a")
    # Symmetric protocols have no reproduce gate; a symmetric protocol event on ``reproduce`` is
    # out of scope (design: accept plan, in-scope dep, enabled final only).
    events = (
        _gate_events("plan", outcome="CONSENSUS", bindings=_bindings("p", "d", "np"))
        + _gate_events("reproduce", outcome="CONSENSUS", bindings=_bindings("r", "d", "nr"))
        + _dep_events("a", findings=[_finding("dep:a", "F1")], bindings=_bindings("a", "d", "na"))
    )
    assert out_of_scope_protocol_gates(events, dag, final_enabled=False) == ["reproduce"]


def test_unresolved_objections_fails_closed_on_arbitrary_gate():
    from crucible.symmetric import unresolved_objections

    dag = _one_done_dag("a")
    events = (
        _dep_events("a", outcome="PROCEED_WITH_FLAGS", objections=[_obj("A:F1", "blocker")],
                    bindings=_bindings("a", "d", "na"))
        + _gate_events("sidequest", outcome="CAPPED", objections=[_obj("A:S1", "blocker")],
                       bindings=_bindings("s", "d", "ns"))
    )
    with pytest.raises(ValueError, match="sidequest"):
        unresolved_objections(events, dag, final_enabled=True)


def test_require_complete_symmetric_run_rejects_post_terminal_verdict():
    from crucible.symmetric import require_complete_symmetric_run

    dag = _one_done_dag("a")
    b = _bindings("a", "d", "na")
    events = _dep_events("a", findings=[_finding("dep:a", "F1")], bindings=b)
    events.append(_post_terminal_verdict("dep:a", [], b))
    with pytest.raises(ValueError, match="post-terminal residue"):
        require_complete_symmetric_run(events, dag, require_final=False, final_enabled=False)


# --- round-7 F1: accepted DEPENDENCY sets must attribute every finding to their own gate ----------
#
# A dependency accepted set for ``dep:<id>`` may only carry findings whose ``source_gate`` is that
# gate. A forged ``dep:auth`` accepted set injecting a ``source_gate: final`` (or ``dep:ghost``)
# finding must never be published by the DAG-aware Finish-time path, and the completeness guard and
# workflow validation must surface it as invalid history (reusing FindingSet.validate_for_gate).

def test_accepted_findings_with_dag_rejects_mis_scoped_final_source_gate():
    dag = _one_done_dag("a")
    events = _dep_events("a", findings=[_finding("final", "X1")], bindings=_bindings("a", "d", "na"))
    with pytest.raises(ValueError, match="source_gate"):
        accepted_findings(events, dag)


def test_accepted_findings_with_dag_rejects_mis_scoped_ghost_source_gate():
    dag = _one_done_dag("a")
    events = _dep_events("a", findings=[_finding("dep:ghost", "G1")],
                         bindings=_bindings("a", "d", "na"))
    with pytest.raises(ValueError, match="source_gate"):
        accepted_findings(events, dag)


def test_accepted_findings_without_dag_stays_best_effort_on_mis_scoped():
    # The no-DAG partial helper is best-effort by design (mirrors its DAG-scope contract): it cannot
    # know the tree, so it does not fail closed on a mis-scoped finding — Finish-time scope
    # enforcement is exclusively the DAG-aware path's job (and workflow_issues flags it INVALID).
    events = _dep_events("a", findings=[_finding("final", "X1")], bindings=_bindings("a", "d", "na"))
    fs = accepted_findings(events)  # no dag => partial helper, no source_gate validation
    assert [f.source_gate for f in fs.findings] == ["final"]


def test_require_complete_symmetric_run_rejects_mis_scoped_dependency_finding():
    from crucible.symmetric import require_complete_symmetric_run

    dag = _one_done_dag("a")
    events = _dep_events("a", findings=[_finding("final", "X1")], bindings=_bindings("a", "d", "na"))
    with pytest.raises(ValueError, match="incomplete symmetric workflow"):
        require_complete_symmetric_run(events, dag, require_final=False, final_enabled=False)
