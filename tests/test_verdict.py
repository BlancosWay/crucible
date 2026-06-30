import pytest

from crucible.config import Config
from crucible.verdict import Verdict, Decision, decide

CFG = Config.from_dict({})


def _verdict(verdict, findings):
    return Verdict.from_dict({
        "gate": "plan",
        "round": 1,
        "verdict": verdict,
        "summary": "s",
        "findings": findings,
    })


def test_parse_minimal_approve():
    v = _verdict("APPROVE", [])
    assert v.verdict == "APPROVE"
    assert v.findings == []


def test_invalid_verdict_value_raises():
    with pytest.raises(ValueError, match="verdict"):
        _verdict("MAYBE", [])


def test_invalid_severity_raises():
    with pytest.raises(ValueError, match="severity"):
        _verdict("REQUEST_CHANGES", [{"id": "F1", "severity": "huge", "location": "x", "claim": "c", "suggestion": "s"}])


def test_open_blocking_findings_helper():
    v = _verdict("REQUEST_CHANGES", [
        {"id": "F1", "severity": "blocker", "location": "x", "claim": "c", "suggestion": "s"},
        {"id": "F2", "severity": "nit", "location": "y", "claim": "c", "suggestion": "s"},
    ])
    assert [f.id for f in v.open_blocking(CFG)] == ["F1"]


def test_decide_consensus_on_approve():
    v = _verdict("APPROVE", [])
    d = decide(v, CFG, round_index=1, max_rounds=5)
    assert d.outcome == "CONSENSUS"


def test_decide_changes_when_blocking_present():
    v = _verdict("REQUEST_CHANGES", [
        {"id": "F1", "severity": "major", "location": "x", "claim": "c", "suggestion": "s"},
    ])
    d = decide(v, CFG, round_index=2, max_rounds=5)
    assert d.outcome == "CHANGES"
    assert [f.id for f in d.open_findings] == ["F1"]


def test_decide_capped_at_last_round_without_consensus():
    v = _verdict("REQUEST_CHANGES", [
        {"id": "F1", "severity": "blocker", "location": "x", "claim": "c", "suggestion": "s"},
    ])
    d = decide(v, CFG, round_index=5, max_rounds=5)
    assert d.outcome == "CAPPED"


def test_decide_proceeds_with_flags_at_cap_when_configured():
    cfg = Config.from_dict({"on_cap": "proceed_with_flags"})
    v = _verdict("REQUEST_CHANGES", [
        {"id": "F1", "severity": "blocker", "location": "x", "claim": "c", "suggestion": "s"},
    ])
    d = decide(v, cfg, round_index=5, max_rounds=5)
    assert d.outcome == "PROCEED_WITH_FLAGS"
    assert [f.id for f in d.open_findings] == ["F1"]


def test_decide_always_halt_caps_instead_of_proceeding():
    # The REPRODUCE gate must never proceed_with_flags: an unconfirmed bug halts regardless of on_cap.
    cfg = Config.from_dict({"on_cap": "proceed_with_flags"})
    v = _verdict("REQUEST_CHANGES", [
        {"id": "F1", "severity": "blocker", "location": "x", "claim": "c", "suggestion": "s"},
    ])
    d = decide(v, cfg, round_index=5, max_rounds=5, always_halt=True)
    assert d.outcome == "CAPPED"
    assert [f.id for f in d.open_findings] == ["F1"]


def test_decide_consensus_beats_cap_even_with_proceed_config():
    cfg = Config.from_dict({"on_cap": "proceed_with_flags"})
    v = _verdict("APPROVE", [])
    assert decide(v, cfg, round_index=5, max_rounds=5).outcome == "CONSENSUS"


def test_decide_before_cap_is_changes_regardless_of_on_cap():
    cfg = Config.from_dict({"on_cap": "proceed_with_flags"})
    v = _verdict("REQUEST_CHANGES", [
        {"id": "F1", "severity": "blocker", "location": "x", "claim": "c", "suggestion": "s"},
    ])
    assert decide(v, cfg, round_index=1, max_rounds=5).outcome == "CHANGES"


def test_decide_resolutions_clearing_all_blockers_at_cap_reaches_consensus():
    cfg = Config.from_dict({"on_cap": "proceed_with_flags"})
    v = _verdict("REQUEST_CHANGES", [
        {"id": "F1", "severity": "major", "location": "x", "claim": "c", "suggestion": "s"},
    ])
    d = decide(v, cfg, round_index=5, max_rounds=5, resolutions={"F1": "wontfix"})
    assert d.outcome == "CONSENSUS"


def test_decide_consensus_even_at_cap_if_approved():
    v = _verdict("APPROVE", [])
    d = decide(v, CFG, round_index=5, max_rounds=5)
    assert d.outcome == "CONSENSUS"


def test_nonblocking_only_reaches_consensus():
    v = _verdict("REQUEST_CHANGES", [
        {"id": "F1", "severity": "minor", "location": "x", "claim": "c", "suggestion": "s"},
    ])
    d = decide(v, CFG, round_index=1, max_rounds=5)
    assert d.outcome == "CONSENSUS"


def test_strict_rebuttal_flag_does_not_break_decide():
    cfg = Config.from_dict({"strict_rebuttal": True})
    v = _verdict("APPROVE", [])
    assert decide(v, cfg, round_index=1, max_rounds=5).outcome == "CONSENSUS"


def test_wontfix_clears_blocking_when_not_strict():
    v = _verdict("REQUEST_CHANGES", [
        {"id": "F1", "severity": "major", "location": "x", "claim": "c", "suggestion": "s"},
    ])
    d = decide(v, CFG, round_index=1, max_rounds=5, resolutions={"F1": "wontfix"})
    assert d.outcome == "CONSENSUS"


def test_wontfix_stays_blocking_when_strict():
    cfg = Config.from_dict({"strict_rebuttal": True})
    v = _verdict("REQUEST_CHANGES", [
        {"id": "F1", "severity": "major", "location": "x", "claim": "c", "suggestion": "s"},
    ])
    d = decide(v, cfg, round_index=1, max_rounds=5, resolutions={"F1": "wontfix"})
    assert d.outcome == "CHANGES"


def test_deferred_does_not_clear_blocking_finding():
    # deferring a major finding is invalid (major not in defer_severities) -> stays blocking
    v = _verdict("REQUEST_CHANGES", [
        {"id": "F1", "severity": "major", "location": "x", "claim": "c", "suggestion": "s"},
    ])
    d = decide(v, CFG, round_index=1, max_rounds=5, resolutions={"F1": "deferred"})
    assert d.outcome == "CHANGES"


def test_fixed_keeps_finding_open_for_another_round():
    v = _verdict("REQUEST_CHANGES", [
        {"id": "F1", "severity": "blocker", "location": "x", "claim": "c", "suggestion": "s"},
    ])
    d = decide(v, CFG, round_index=1, max_rounds=5, resolutions={"F1": "fixed"})
    assert d.outcome == "CHANGES"


def test_duplicate_finding_ids_rejected():
    with pytest.raises(ValueError, match="duplicate finding ids"):
        _verdict("REQUEST_CHANGES", [
            {"id": "F1", "severity": "major", "location": "x", "claim": "c", "suggestion": "s"},
            {"id": "F1", "severity": "blocker", "location": "y", "claim": "c2", "suggestion": "s2"},
        ])


# --- input type validation (M7) ----------------------------------------------

def test_verdict_from_dict_rejects_non_dict():
    with pytest.raises(ValueError, match="must be a JSON object"):
        Verdict.from_dict([{"verdict": "APPROVE"}])


def test_verdict_from_dict_rejects_non_list_findings():
    with pytest.raises(ValueError, match='"findings" must be a list'):
        Verdict.from_dict({"gate": "plan", "round": 1, "verdict": "APPROVE", "findings": "oops"})


def test_finding_from_dict_rejects_non_dict():
    with pytest.raises(ValueError, match="finding"):
        Verdict.from_dict({"gate": "plan", "round": 1, "verdict": "REQUEST_CHANGES",
                           "findings": ["oops"]})


def test_verdict_round_must_be_int():
    for bad in ([], {}, "1", 1.5, True):
        with pytest.raises(ValueError, match="round must be an integer"):
            Verdict.from_dict({"gate": "plan", "round": bad, "verdict": "APPROVE", "findings": []})


def test_verdict_gate_must_be_non_empty_string():
    with pytest.raises(ValueError, match="gate must be a non-empty string"):
        Verdict.from_dict({"gate": 1, "round": 1, "verdict": "APPROVE", "findings": []})


def test_finding_id_must_be_non_empty_string():
    with pytest.raises(ValueError, match="id must be a non-empty string"):
        Verdict.from_dict({"gate": "plan", "round": 1, "verdict": "REQUEST_CHANGES",
                           "findings": [{"id": 1, "severity": "blocker"}]})


def _finding(sev, fid="F1"):
    return {"id": fid, "severity": sev, "location": "x", "claim": "c", "suggestion": "s"}


# --- verdict/severity consistency (H1) ---------------------------------------
# The Critic's APPROVE/REQUEST_CHANGES label must be consistent with whether any
# finding is blocking under the run's blocking_severities (consensus-rubric.md).

def test_consistency_approve_with_blocking_is_inconsistent():
    v = _verdict("APPROVE", [_finding("blocker")])
    assert v.consistency_error(CFG) is not None


def test_consistency_approve_with_major_is_inconsistent():
    v = _verdict("APPROVE", [_finding("major")])
    assert v.consistency_error(CFG) is not None


def test_consistency_request_changes_with_no_findings_is_inconsistent():
    v = _verdict("REQUEST_CHANGES", [])
    assert v.consistency_error(CFG) is not None


def test_consistency_request_changes_with_only_minor_is_inconsistent():
    v = _verdict("REQUEST_CHANGES", [_finding("minor")])
    assert v.consistency_error(CFG) is not None


def test_consistency_ok_approve_empty():
    assert _verdict("APPROVE", []).consistency_error(CFG) is None


def test_consistency_ok_approve_with_only_nonblocking():
    v = _verdict("APPROVE", [_finding("minor"), _finding("nit", "F2")])
    assert v.consistency_error(CFG) is None


def test_consistency_ok_request_changes_with_major():
    assert _verdict("REQUEST_CHANGES", [_finding("major")]).consistency_error(CFG) is None


def test_consistency_ok_request_changes_with_blocker():
    assert _verdict("REQUEST_CHANGES", [_finding("blocker")]).consistency_error(CFG) is None


def test_consistency_is_config_aware():
    # Under a blocker-only consensus policy a `major` is NOT blocking, so the SAME
    # verdict flips validity: APPROVE+major becomes consistent, REQUEST_CHANGES+major not.
    cfg = Config.from_dict({"blocking_severities": ["blocker"]})
    assert _verdict("APPROVE", [_finding("major")]).consistency_error(cfg) is None
    assert _verdict("REQUEST_CHANGES", [_finding("major")]).consistency_error(cfg) is not None
