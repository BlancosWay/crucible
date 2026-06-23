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
