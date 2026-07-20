import pytest

from crucible.config import Config
from crucible.symmetric import (
    SYMMETRIC_WORKFLOWS,
    VALID_WORKFLOWS,
    AcceptedFinding,
    FindingSet,
    PeerAttestation,
    SymmetricDecision,
    decide_symmetric,
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
