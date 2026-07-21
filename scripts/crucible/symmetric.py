"""Symmetric-workflow data model: workflow kind, accepted finding sets, peer attestations.

The asymmetric Builder/Critic flow (``verdict.py``) records ONE ``APPROVE``/``REQUEST_CHANGES``
label per gate. The symmetric ``deep-dive`` and ``pr-review`` skills make two additional claims the
build model cannot express:

- **two configured peer slots** (``A`` and ``B``) each signed off on the same bound candidate; and
- the **accepted finding set** is deterministic state distinct from the peers' *objections* to that
  candidate's completeness/correctness.

This module owns the pure schemas and the round decision for those workflows. It never touches the
run-log, filesystem, or CLI (Task 2 wires it in); the asymmetric path is untouched. Objections are
the existing :class:`crucible.verdict.Finding` (defects in the candidate), so peer consistency and
the round decision reuse the run's ``blocking_severities`` exactly like ``verdict.decide``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from crucible.config import Config
from crucible.integrity import artifact_sha256, event_bindings
from crucible.verdict import (
    VALID_SEVERITIES,
    VALID_VERDICTS,
    Finding,
    _optional_hash,
)

# Immutable run metadata recorded on ``run_start`` (see runlog.init_run). This is run metadata, not a
# model/default configuration key, so it does not bump the run schema version.
VALID_WORKFLOWS = ("build", "deep-dive", "pr-review")
# The workflows that use two equal peers instead of the Builder/Critic asymmetry.
SYMMETRIC_WORKFLOWS = ("deep-dive", "pr-review")
# The two configured peer slots a symmetric decision requires.
VALID_PEERS = ("A", "B")
# Symmetric workflows reuse the two configured role slots as the two EQUAL peers, with no
# config-schema change: Peer A is the ``builder`` slot, Peer B is the ``critic`` slot. This is a
# provenance mapping only — the two peers are not a Builder/Critic asymmetry.
PEER_SLOT_ROLES = {"A": "builder", "B": "critic"}


class CorruptWorkflowError(ValueError):
    """The run's first ``run_start`` records a PRESENT but null/non-string/unrecognized ``workflow``.

    Distinct from an ABSENT ``workflow`` key — legacy schema-v2 metadata that predates the field and
    reads as ``build``. A *present* invalid value can only come from tampering (``init_run`` writes
    only a member of :data:`VALID_WORKFLOWS`), so it is corrupt run metadata that must fail closed
    rather than be silently routed as the asymmetric ``build`` workflow. Subclasses ``ValueError`` so
    the CLI's top-level handler renders it as a clean ``crucible: ...`` message with no traceback.
    """


def peer_slot_provenance(cfg: Config) -> dict[str, dict[str, str]]:
    """Configured ``model``/``effort`` for each peer slot, read from the run configuration.

    The CLI records this on every ``symmetric_verdict`` so the report and result projection can
    attribute each slot's attestation to the model/effort that produced it. Per the design's trust
    boundary this proves two configured *slots* attested — never that two distinct model processes
    ran — so it is deliberately derived from config, not claimed cryptographically.
    """
    provenance: dict[str, dict[str, str]] = {}
    for slot, role in PEER_SLOT_ROLES.items():
        role_cfg = getattr(cfg, role)
        provenance[slot] = {"model": role_cfg["model"], "effort": role_cfg["effort"]}
    return provenance


def workflow_kind(events: list[dict[str, Any]]) -> str:
    """Return the immutable workflow recorded on the run's first ``run_start`` event.

    This is the single pure read/validation path for the recorded workflow: every caller (the report,
    the workflow-issue validator, CLI routing, and the run-integrity guard) reads through it, so the
    ``VALID_WORKFLOWS`` check lives in exactly one place and is never re-spelled per caller.

    An ABSENT ``workflow`` key — including a run with no ``run_start`` at all — means ``build``: such a
    run predates the field (legacy schema-v2 metadata), so it is the asymmetric Builder/Critic
    workflow. Legacy readability is preserved.

    A PRESENT but null/non-string/unrecognized value is corrupt: ``init_run`` only ever writes a
    member of :data:`VALID_WORKFLOWS`, so it can only come from tampering. Rather than silently
    default corrupt metadata to ``build`` (and route a tampered run as the asymmetric workflow), this
    raises :class:`CorruptWorkflowError` so the run fails closed. Mirrors
    ``integrity.run_schema_version``: the first ``run_start`` wins (a later ``run_start`` cannot
    launder a corrupt first one).
    """
    for event in events:
        if event.get("event") == "run_start":
            if "workflow" not in event:
                return "build"  # legacy schema-v2 metadata: absent key predates the field
            workflow = event["workflow"]
            if workflow not in VALID_WORKFLOWS:
                raise CorruptWorkflowError(
                    f"run_start records an invalid workflow value {workflow!r}; a recorded workflow "
                    f"must be one of {VALID_WORKFLOWS} — a present null/non-string/unrecognized value "
                    f"is corrupt run metadata (an absent workflow key is legacy and reads as 'build')"
                )
            return workflow
    return "build"


_FINDING_FIELDS = ("source_gate", "id", "severity", "location", "claim", "suggestion")


@dataclass(frozen=True)
class AcceptedFinding:
    """One finding accepted as (part of) the investigation/review result.

    Distinct from :class:`crucible.verdict.Finding`: an accepted finding carries a ``source_gate`` so
    the FINAL set and the deterministic union stay keyed by ``(source_gate, id)``. All six fields are
    required non-empty strings and ``severity`` uses the existing vocabulary.
    """

    source_gate: str
    id: str
    severity: str
    location: str
    claim: str
    suggestion: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AcceptedFinding":
        if not isinstance(data, dict):
            raise ValueError("finding must be a JSON object")
        for field in _FINDING_FIELDS:
            value = data.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"finding.{field} must be a non-empty string")
        if data["severity"] not in VALID_SEVERITIES:
            raise ValueError(f"invalid severity: {data['severity']}")
        return cls(
            source_gate=data["source_gate"],
            id=data["id"],
            severity=data["severity"],
            location=data["location"],
            claim=data["claim"],
            suggestion=data["suggestion"],
        )

    @property
    def key(self) -> tuple[str, str]:
        """The composite ``(source_gate, id)`` identity used for uniqueness and FINAL inclusion."""
        return (self.source_gate, self.id)

    def to_dict(self) -> dict[str, str]:
        return {field: getattr(self, field) for field in _FINDING_FIELDS}


@dataclass
class FindingSet:
    """A validated candidate/accepted finding set: optional ``summary`` plus a list of findings whose
    ``(source_gate, id)`` keys are unique. PLAN artifacts are never parsed as finding sets."""

    summary: str
    findings: list[AcceptedFinding]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FindingSet":
        if not isinstance(data, dict):
            raise ValueError("finding set must be a JSON object")
        summary = data.get("summary", "")
        if not isinstance(summary, str):
            raise ValueError("finding set summary must be a string")
        raw = data.get("findings")
        if not isinstance(raw, list):
            raise ValueError('finding set "findings" must be a list')
        findings = [AcceptedFinding.from_dict(f) for f in raw]
        keys = [f.key for f in findings]
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        if dupes:
            raise ValueError(f"duplicate (source_gate, id) finding keys: {dupes}")
        return cls(summary=summary, findings=findings)

    def by_key(self) -> dict[tuple[str, str], AcceptedFinding]:
        """Map each finding's composite ``(source_gate, id)`` key to the finding (keys are unique)."""
        return {f.key: f for f in self.findings}

    def validate_for_gate(self, gate: str) -> None:
        """Require every finding's ``source_gate`` to equal ``gate`` (a ``dep:<thread>``/``final``).

        A dependency candidate that names some other gate's ``source_gate`` would let one gate's
        review smuggle in findings attributed to another node, so it is rejected.
        """
        wrong = [f.id for f in self.findings if f.source_gate != gate]
        if wrong:
            raise ValueError(
                f"finding(s) {wrong} have a source_gate other than {gate!r}; a candidate finding "
                f"set for gate {gate!r} must use it as every finding's source_gate"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"summary": self.summary, "findings": [f.to_dict() for f in self.findings]}


@dataclass
class PeerAttestation:
    """One peer slot's independent sign-off on a bound candidate finding set.

    ``objections`` are :class:`crucible.verdict.Finding` objects describing defects/disputes in the
    *candidate set* (not accepted results). The schema-v2 binding hashes are echoed exactly so Task 2
    can prove both peers reviewed the same artifact/DAG/node; they are optional here only so a peer
    file parses before the CLI validates it against the CLI-selected bindings.
    """

    peer: str
    gate: str
    round: int
    verdict: str
    summary: str
    objections: list[Finding]
    artifact_sha256: str | None = None
    dag_sha256: str | None = None
    node_sha256: str | None = None
    target_sha256: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PeerAttestation":
        if not isinstance(data, dict):
            raise ValueError("peer attestation must be a JSON object")
        peer = data.get("peer")
        if peer not in VALID_PEERS:
            raise ValueError(f"peer must be one of {VALID_PEERS}, got {peer!r}")
        gate = data.get("gate")
        if not isinstance(gate, str) or not gate:
            raise ValueError("peer.gate must be a non-empty string")
        rnd = data.get("round")
        if isinstance(rnd, bool) or not isinstance(rnd, int):
            raise ValueError("peer.round must be an integer")
        verdict = data.get("verdict")
        if verdict not in VALID_VERDICTS:
            raise ValueError(f"invalid verdict: {verdict}")
        summary = data.get("summary", "")
        if not isinstance(summary, str):
            raise ValueError("peer.summary must be a string")
        objections_raw = data.get("objections", [])
        if not isinstance(objections_raw, list):
            raise ValueError('peer "objections" must be a list')
        objections = [Finding.from_dict(o) for o in objections_raw]
        ids = [o.id for o in objections]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate objection ids: {dupes}")
        return cls(
            peer=peer,
            gate=gate,
            round=rnd,
            verdict=verdict,
            summary=summary,
            objections=objections,
            artifact_sha256=_optional_hash(data, "artifact_sha256"),
            dag_sha256=_optional_hash(data, "dag_sha256"),
            node_sha256=_optional_hash(data, "node_sha256"),
            target_sha256=_optional_hash(data, "target_sha256"),
        )

    def open_blocking(self, cfg: Config) -> list[Finding]:
        blocking = set(cfg.blocking_severities)
        return [o for o in self.objections if o.severity in blocking]

    def consistency_error(self, cfg: Config) -> str | None:
        """Reject a peer whose ``APPROVE``/``REQUEST_CHANGES`` label contradicts its objections,
        under the run's ``blocking_severities`` — the same rubric as ``verdict.consistency_error``:

        - ``APPROVE`` requires **no** blocking objection;
        - ``REQUEST_CHANGES`` requires **at least one** blocking objection.

        Returns a human-readable error string when contradictory, else ``None``.
        """
        blocking_ids = [o.id for o in self.open_blocking(cfg)]
        sev = sorted(cfg.blocking_severities)
        if self.verdict == "APPROVE" and blocking_ids:
            return (f"inconsistent peer {self.peer}: APPROVE but {blocking_ids} have a blocking "
                    f"severity {sev}; APPROVE requires no blocking objection")
        if self.verdict == "REQUEST_CHANGES" and not blocking_ids:
            return (f"inconsistent peer {self.peer}: REQUEST_CHANGES but no objection has a "
                    f"blocking severity {sev}; REQUEST_CHANGES requires at least one blocking "
                    f"objection")
        return None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "peer": self.peer,
            "gate": self.gate,
            "round": self.round,
            "verdict": self.verdict,
            "summary": self.summary,
            "objections": [
                {"id": o.id, "severity": o.severity, "location": o.location,
                 "claim": o.claim, "suggestion": o.suggestion}
                for o in self.objections
            ],
        }
        for key in ("artifact_sha256", "dag_sha256", "node_sha256", "target_sha256"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


@dataclass
class SymmetricDecision:
    outcome: str  # "CONSENSUS" | "CHANGES" | "CAPPED" | "PROCEED_WITH_FLAGS"
    open_objections: list[Finding]


def decide_symmetric(
    peer_a: PeerAttestation,
    peer_b: PeerAttestation,
    cfg: Config,
    round_index: int,
    max_rounds: int,
) -> SymmetricDecision:
    """Decide a symmetric round from BOTH peers' objections, never from accepted-finding severity.

    The union of the two peers' blocking objections is namespaced by slot (``A:<id>`` / ``B:<id>``)
    so same-id objections from different peers are both retained. Symmetric workflows never use
    Builder resolutions, so the cap policy is the same as ``verdict.decide`` without them:

    - no blocking objection from either peer → ``CONSENSUS`` (a candidate that *accepts* a blocker
      still reaches consensus when both peers attest the set is accurate and complete);
    - a blocking objection before the cap → ``CHANGES``;
    - a blocking objection at the cap → ``PROCEED_WITH_FLAGS`` (``on_cap: proceed_with_flags``) or
      ``CAPPED`` (``on_cap: halt``).
    """
    open_objections: list[Finding] = []
    for peer in (peer_a, peer_b):
        for objection in peer.open_blocking(cfg):
            open_objections.append(Finding(
                id=f"{peer.peer}:{objection.id}",
                severity=objection.severity,
                location=objection.location,
                claim=objection.claim,
                suggestion=objection.suggestion,
            ))
    if not open_objections:
        return SymmetricDecision(outcome="CONSENSUS", open_objections=[])
    if round_index >= max_rounds:
        if cfg.on_cap == "proceed_with_flags":
            return SymmetricDecision(outcome="PROCEED_WITH_FLAGS", open_objections=open_objections)
        return SymmetricDecision(outcome="CAPPED", open_objections=open_objections)
    return SymmetricDecision(outcome="CHANGES", open_objections=open_objections)


# --- accepted-finding aggregation and deterministic review result ------------
#
# Everything below is a PURE projection over the append-only run-log events (plus, for topological
# ordering/completeness, the current DAG). It owns the deterministic state the symmetric skills add
# on top of the schema-v2 bindings: the accepted finding set and — for pr-review — the derived
# recommendation. It never reads the filesystem or mutates the run; the CLI commands
# (accepted-findings / review-result) and the report call these helpers so the result is decided in
# exactly one place, never eyeballed.
#
# The terminal-event vocabulary is duplicated here (as in cli/report/workflow) so this module stays
# free of an import cycle with workflow.py (which imports THIS module).

_ADVANCE_TERMINALS = ("gate_consensus", "gate_proceeded_with_flags")
_TERMINAL_EVENTS = ("gate_consensus", "gate_proceeded_with_flags", "gate_capped")
# The three event kinds of a symmetric gate's atomic decision. Adjacency is defined over THIS
# subsequence for a gate: unrelated same-gate events (e.g. an interleaved ``builder_output``) and
# every other gate's events are ignored, but no OTHER protocol event of the same gate may intervene.
_PROTOCOL_EVENTS = ("symmetric_verdict", "accepted_finding_set", *_TERMINAL_EVENTS)

# The symmetric_verdict.outcome that certifies each terminal event. A terminal is only legitimate
# when its terminal-bound decision carries the matching outcome (round-8 F4).
_TERMINAL_OUTCOME = {
    "gate_consensus": "CONSENSUS",
    "gate_proceeded_with_flags": "PROCEED_WITH_FLAGS",
    "gate_capped": "CAPPED",
}

# Resolver issue reasons (stable, human-readable fragments the workflow layer prefixes with the gate).
_ORPHAN_WITHOUT_TERMINAL = ("records an accepted finding set with no advancing terminal "
                            "(orphan/pre-terminal, not accepted state)")
_TERMINAL_WITHOUT_SET = ("reached an advancing terminal without an accepted finding set recorded "
                         "immediately before it (missing set or an intervening protocol event)")
_SET_BINDING_MISMATCH = ("accepted finding set does not bind the same artifact/DAG/node as its "
                         "advancing terminal")
_NO_MATCHING_DECISION = ("accepted finding set is not immediately preceded by a matching peer "
                         "decision (no symmetric_verdict for this gate/round echoing the same "
                         "artifact/DAG/node directly precedes it — the two peers reviewed a "
                         "different candidate, or a second accepted set intervened)")
_POST_TERMINAL_RESIDUE = ("records an accepted finding set after its advancing terminal "
                          "(post-terminal crash residue, not accepted state)")


@dataclass(frozen=True)
class GateAcceptance:
    """The single resolved acceptance state of one symmetric gate, decided in exactly one place.

    ``accepted_index`` is the index of the gate's ONE effective ``accepted_finding_set`` event (the
    middle of a valid ``symmetric_verdict -> accepted_finding_set -> advancing terminal`` trio), or
    ``None`` when the gate has no accepted state. ``terminal_index`` is the authoritative advancing
    terminal's index (or ``None``). ``orphan_indices`` are every OTHER ``accepted_finding_set`` event
    for the gate (never accepted state). ``issues`` are stable reason fragments for each integrity
    violation, for the workflow layer to prefix with the gate — the projection uses ``accepted_index``
    and ``orphan_indices`` and ignores ``issues``. Payload validity is deferred to the caller.
    """

    gate: str
    accepted_index: int | None
    terminal_index: int | None
    orphan_indices: tuple[int, ...]
    issues: tuple[str, ...]


def resolve_gate_acceptance(events: list[dict[str, Any]], gate: str) -> GateAcceptance:
    """Resolve the ONE effective accepted finding set for ``gate`` (the shared tri-event resolver).

    A gate's accepted state is the middle event of a single atomic trio: a ``symmetric_verdict`` whose
    bindings the ``accepted_finding_set`` echoes, IMMEDIATELY followed (in the gate's protocol
    subsequence) by that accepted set, IMMEDIATELY followed by the gate's authoritative ADVANCING
    terminal binding the same artifact/DAG/node and round. Exactly one accepted set may bracket the
    terminal; a second pre-terminal set, a non-immediate/mismatched peer decision, an intervening
    same-gate protocol event, a binding mismatch, or an accepted set with no advancing terminal all
    mean the gate has NO accepted state. Pure; never raises (payload validity is the caller's job).

    Both the projection (:func:`accepted_findings` and friends) and workflow validation consume this,
    so the atomic-decision contract lives in exactly one place.
    """
    protocol = [i for i, e in enumerate(events)
                if e.get("gate") == gate and e.get("event") in _PROTOCOL_EVENTS]
    accepted_all = tuple(i for i in protocol
                         if events[i].get("event") == "accepted_finding_set")
    terminals = [i for i in protocol if events[i].get("event") in _TERMINAL_EVENTS]

    def _result(accepted_index, terminal_index, *issues):
        orphans = tuple(i for i in accepted_all if i != accepted_index)
        return GateAcceptance(gate, accepted_index, terminal_index, orphans, tuple(issues))

    # No authoritative advancing terminal: any accepted set is orphan/pre-terminal, never state.
    if not terminals or events[terminals[-1]].get("event") not in _ADVANCE_TERMINALS:
        issues = (_ORPHAN_WITHOUT_TERMINAL,) if accepted_all else ()
        return _result(None, None, *issues)

    terminal_idx = terminals[-1]
    tpos = protocol.index(terminal_idx)
    want_round = events[terminal_idx].get("round")
    want_bind = event_bindings(events[terminal_idx])

    # (1) The accepted set must be the protocol event IMMEDIATELY before the terminal. A missing set
    #     or any intervening same-gate protocol event breaks the adjacency.
    if tpos < 1 or events[protocol[tpos - 1]].get("event") != "accepted_finding_set":
        return _result(None, terminal_idx, _TERMINAL_WITHOUT_SET)
    accepted_idx = protocol[tpos - 1]

    # (2) That accepted set must bind the same artifact/DAG/node (and round) as its terminal.
    if (events[accepted_idx].get("round") != want_round
            or event_bindings(events[accepted_idx]) != want_bind):
        return _result(None, terminal_idx, _SET_BINDING_MISMATCH)

    # (3) A matching peer decision must be the protocol event IMMEDIATELY before the accepted set.
    #     This rejects a second pre-terminal accepted set (its predecessor is another accepted set)
    #     and a non-immediate/mismatched peer decision alike.
    if tpos < 2 or events[protocol[tpos - 2]].get("event") != "symmetric_verdict":
        return _result(None, terminal_idx, _NO_MATCHING_DECISION)
    decision_idx = protocol[tpos - 2]
    if (events[decision_idx].get("round") != want_round
            or event_bindings(events[decision_idx]) != want_bind):
        return _result(None, terminal_idx, _NO_MATCHING_DECISION)

    # (4) The decision's OUTCOME must certify THIS advancing terminal (round-8 F4): a CHANGES/CAPPED/
    #     wrong advancing outcome trio has valid STRUCTURE but never certifies an accepted set. The
    #     decision-semantics violation itself is reported by the shared :func:`symmetric_decision_issues`
    #     validator; here we only ensure such a trio yields NO accepted state and is not counted as an
    #     orphan set (avoiding a duplicate diagnostic).
    if events[decision_idx].get("outcome") != _TERMINAL_OUTCOME.get(events[terminal_idx].get("event")):
        return GateAcceptance(gate, None, terminal_idx, (), ())

    # (5) The decision must carry both peers' bound attestations, structurally sound (round-9): a
    #     no-peer / one-slot / extra-slot / swapped / malformed-attestation / gate-round-binding-mismatch
    #     verdict never certifies an accepted set. The cfg-aware validator reports the specifics; here we
    #     only ensure such a trio yields NO accepted state (and no duplicate orphan diagnostic).
    if _peers_structurally_invalid(events[decision_idx], gate):
        return GateAcceptance(gate, None, terminal_idx, (), ())

    # Valid trio. Any OTHER accepted set for the gate is post-terminal residue, never accepted state.
    orphans = tuple(i for i in accepted_all if i != accepted_idx)
    issues = (_POST_TERMINAL_RESIDUE,) if orphans else ()
    return GateAcceptance(gate, accepted_idx, terminal_idx, orphans, issues)


def _terminal_bound_decision_index(
    events: list[dict[str, Any]], gate: str
) -> tuple[int | None, int | None, str | None, int | None]:
    """``(decision_index, terminal_index, terminal_event, accepted_index)`` for ``gate``'s
    authoritative (last) terminal — the ONE place pairing a terminal with the symmetric_verdict bound
    to it, shared by accepted-state resolution, PLAN attestation, and objection projection.

    For an ADVANCING terminal immediately preceded (in the gate's protocol subsequence) by an
    ``accepted_finding_set`` (a dependency/FINAL trio), the decision is the event before that set;
    otherwise (a PLAN advance with no accepted set, or a CAPPED terminal) the decision is the event
    immediately before the terminal. The decision must be a ``symmetric_verdict`` echoing the
    terminal's round + bindings, else ``decision_index`` is ``None`` (a bare terminal, a
    ``critic_verdict``, or a mismatched verdict). ``terminal_index``/``terminal_event`` are ``None``
    only when the gate has no terminal at all. Pure; never raises.
    """
    protocol = [i for i, e in enumerate(events)
                if e.get("gate") == gate and e.get("event") in _PROTOCOL_EVENTS]
    terminals = [i for i in protocol if events[i].get("event") in _TERMINAL_EVENTS]
    if not terminals:
        return (None, None, None, None)
    terminal_idx = terminals[-1]
    term_event = events[terminal_idx].get("event")
    tpos = protocol.index(terminal_idx)
    accepted_idx: int | None = None
    decision_pos = tpos - 1
    if (term_event in _ADVANCE_TERMINALS and tpos >= 1
            and events[protocol[tpos - 1]].get("event") == "accepted_finding_set"):
        accepted_idx = protocol[tpos - 1]
        decision_pos = tpos - 2
    if decision_pos < 0 or events[protocol[decision_pos]].get("event") != "symmetric_verdict":
        return (None, terminal_idx, term_event, accepted_idx)
    decision_idx = protocol[decision_pos]
    if (events[decision_idx].get("round") != events[terminal_idx].get("round")
            or event_bindings(events[decision_idx]) != event_bindings(events[terminal_idx])):
        return (None, terminal_idx, term_event, accepted_idx)
    return (decision_idx, terminal_idx, term_event, accepted_idx)


def symmetric_decision_issues(
    events: list[dict[str, Any]], gate: str, cfg: Config | None = None
) -> list[str]:
    """Validate the SEMANTICS of ``gate``'s terminal-bound symmetric decision (round-8 F1/F4). The
    shared terminal-decision validator: workflow validation, accepted-state resolution, completeness,
    and result projection all agree on what a legitimate decision looks like, so they never diverge.

    Returns stable issue fragments (the workflow layer prefixes them with the gate). Empty when the
    gate has no terminal (in progress) OR no terminal-bound decision — the ACCEPTED-SET resolver owns
    the "no decision" report for a dependency/FINAL trio, and the PLAN-attestation check owns it for
    ``plan``, so this validator never duplicates that. When a bound decision exists it must:

    - carry the outcome that certifies its terminal (``CONSENSUS``/``PROCEED_WITH_FLAGS``/``CAPPED``);
    - list structurally valid, unique objections — and, when ``cfg`` is given, every objection's
      severity must be in ``cfg.blocking_severities`` (a decision only ever carries BLOCKING objections);
    - carry NO objections for ``CONSENSUS`` and at least one for ``PROCEED_WITH_FLAGS``/``CAPPED``;
    - for a proceeded/capped terminal, have ``open_findings`` equal to the objection ids exactly.

    ``cfg`` is optional so the accepted-state resolver can reuse the structural (cfg-free) subset.
    Pure; never raises.
    """
    decision_idx, terminal_idx, term_event, _accepted = _terminal_bound_decision_index(events, gate)
    if terminal_idx is None or decision_idx is None:
        return []
    decision = events[decision_idx]
    terminal = events[terminal_idx]
    issues: list[str] = []
    expected = _TERMINAL_OUTCOME.get(term_event)
    if decision.get("outcome") != expected:
        issues.append(
            f"terminal-bound decision outcome {decision.get('outcome')!r} does not certify a "
            f"{term_event!r} terminal (expected {expected!r})")
    raw = decision.get("objections", [])
    obj_ids: list[str] = []
    if not isinstance(raw, list):
        issues.append("terminal-bound decision objections is not a list")
    else:
        blocking = set(cfg.blocking_severities) if cfg is not None else None
        for objection in raw:
            if not isinstance(objection, dict):
                issues.append("terminal-bound decision has a non-object objection")
                continue
            oid = objection.get("id")
            if isinstance(oid, str) and oid:
                obj_ids.append(oid)
            else:
                issues.append("terminal-bound decision has an objection with no id")
            if blocking is not None and objection.get("severity") not in blocking:
                issues.append(
                    f"terminal-bound objection {oid!r} severity {objection.get('severity')!r} is not "
                    f"a configured blocking severity {sorted(blocking)}")
        dupes = sorted({i for i in obj_ids if obj_ids.count(i) > 1})
        if dupes:
            issues.append(f"terminal-bound decision has duplicate objection ids {dupes}")
    if expected == "CONSENSUS" and obj_ids:
        issues.append("a CONSENSUS decision must carry no open objections")
    if expected in ("PROCEED_WITH_FLAGS", "CAPPED") and not obj_ids:
        issues.append(f"a {expected} decision must carry at least one blocking objection")
    if term_event in ("gate_proceeded_with_flags", "gate_capped"):
        open_ids = terminal.get("open_findings")
        if not isinstance(open_ids, list) or sorted(str(x) for x in open_ids) != sorted(obj_ids):
            issues.append(
                "terminal open_findings do not correspond exactly to the decision's objection ids")
    # round-9: the decision is only trusted if its persisted A/B peer attestations exist, are bound to
    # this decision, agree with the outer aggregate objections, and match configured provenance.
    issues.extend(_peer_attestation_issues(decision, gate, cfg))
    return issues


def _expected_aggregate_objections(
    peer_a: PeerAttestation, peer_b: PeerAttestation, cfg: Config
) -> list[dict[str, Any]]:
    """The deterministic A-then-B union of BOTH peers' CONFIGURED blocking objections, namespaced by
    slot with full fields — the exact ``objections`` :func:`decide_symmetric` + the CLI persist on a
    ``symmetric_verdict``. A forged outer aggregate that fabricates/drops/alters an entry will not
    equal this."""
    out: list[dict[str, Any]] = []
    for peer in (peer_a, peer_b):
        for objection in peer.open_blocking(cfg):
            out.append({"id": f"{peer.peer}:{objection.id}", "severity": objection.severity,
                        "location": objection.location, "claim": objection.claim,
                        "suggestion": objection.suggestion})
    return out


def _peer_attestation_issues(
    decision: dict[str, Any], gate: str, cfg: Config | None = None
) -> list[str]:
    """Validate the PERSISTED A/B peer attestations inside a ``symmetric_verdict`` (round-9). A
    terminal-bound verdict is only trusted if it proves both peers independently attested the SAME
    bound candidate and agree with the outer decision. Pure; never raises.

    Structural (cfg-free) checks — enough for the accepted-state resolver to reject a no-peer /
    swapped / malformed verdict:

    - ``peers`` is an object with EXACTLY slots ``A`` and ``B`` (no missing/duplicate/extra), each
      wrapper an object;
    - each wrapper's ``attestation`` parses via :meth:`PeerAttestation.from_dict`, its ``peer`` equals
      its slot, and its gate/round/schema-v2 bindings equal the outer decision.

    cfg-aware checks (workflow/completeness/result) — enforced only when ``cfg`` is given:

    - each attestation passes :meth:`PeerAttestation.consistency_error` (APPROVE/REQUEST_CHANGES vs the
      run's blocking severities);
    - the wrapper ``model``/``effort`` equal :func:`peer_slot_provenance` for that slot;
    - the wrapper ``raw`` is a JSON string parsing to the same canonical attestation as ``attestation``;
    - the outer namespaced ``objections`` equal exactly the A-then-B union of both peers' configured
      blocking objections (:func:`_expected_aggregate_objections`).
    """
    peers = decision.get("peers")
    if not isinstance(peers, dict):
        return ["decision has no persisted peers object"]
    if set(peers.keys()) != {"A", "B"}:
        return [f"peers must have exactly slots 'A' and 'B' (got {sorted(map(str, peers.keys()))})"]
    issues: list[str] = []
    outer_round = decision.get("round")
    outer_bind = event_bindings(decision)
    parsed: dict[str, PeerAttestation] = {}
    for slot in ("A", "B"):
        wrapper = peers.get(slot)
        if not isinstance(wrapper, dict):
            issues.append(f"peer slot {slot!r} wrapper is not an object")
            continue
        try:
            attestation = PeerAttestation.from_dict(wrapper.get("attestation"))
        except (ValueError, TypeError) as exc:
            issues.append(f"peer slot {slot!r} attestation is not a valid peer attestation ({exc})")
            continue
        parsed[slot] = attestation
        if attestation.peer != slot:
            issues.append(
                f"peer slot {slot!r} holds an attestation for peer {attestation.peer!r} "
                f"(swapped/mislabelled slots)")
        if attestation.gate != gate:
            issues.append(f"peer slot {slot!r} attestation gate {attestation.gate!r} does not match "
                          f"the decision gate {gate!r}")
        if attestation.round != outer_round:
            issues.append(f"peer slot {slot!r} attestation round {attestation.round!r} does not match "
                          f"the decision round {outer_round!r}")
        att_bind = event_bindings({"artifact_sha256": attestation.artifact_sha256,
                                   "dag_sha256": attestation.dag_sha256,
                                   "node_sha256": attestation.node_sha256,
                                   "target_sha256": attestation.target_sha256})
        if att_bind != outer_bind:
            issues.append(f"peer slot {slot!r} attestation bindings do not match the decision bindings")
        if cfg is not None:
            inconsistency = attestation.consistency_error(cfg)
            if inconsistency:
                issues.append(f"peer slot {slot!r} {inconsistency}")
            expected_prov = peer_slot_provenance(cfg).get(slot, {})
            if (wrapper.get("model") != expected_prov.get("model")
                    or wrapper.get("effort") != expected_prov.get("effort")):
                issues.append(f"peer slot {slot!r} model/effort do not match the configured provenance "
                              f"for that slot")
            raw = wrapper.get("raw")
            if not isinstance(raw, str):
                issues.append(f"peer slot {slot!r} raw provenance is not a JSON string")
            else:
                try:
                    raw_attestation = PeerAttestation.from_dict(json.loads(raw))
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    issues.append(f"peer slot {slot!r} raw provenance does not parse to a valid "
                                  f"attestation ({exc})")
                else:
                    if raw_attestation.to_dict() != attestation.to_dict():
                        issues.append(f"peer slot {slot!r} raw provenance diverges from the parsed "
                                      f"attestation")
    if cfg is not None and "A" in parsed and "B" in parsed:
        expected = _expected_aggregate_objections(parsed["A"], parsed["B"], cfg)
        if decision.get("objections") != expected:
            issues.append("outer objections are not the exact A-then-B union of both peers' configured "
                          "blocking objections (fabricated/dropped/altered aggregate)")
    return issues


def _peers_structurally_invalid(decision: dict[str, Any], gate: str) -> bool:
    """True when the decision's persisted peers fail the cfg-free structural attestation checks — used
    by :func:`resolve_gate_acceptance` so a no-peer / swapped / malformed-attestation / binding-mismatch
    verdict yields NO accepted state."""
    return bool(_peer_attestation_issues(decision, gate, cfg=None))


def symmetric_candidate_handoff_issues(events: list[dict[str, Any]], gate: str) -> list[str]:
    """For a dependency/FINAL ADVANCING gate, the decision's persisted ``candidate``, the immediately
    following ``accepted_finding_set.payload``, and the bound Builder JSON artifact the two peers
    reviewed must all represent the SAME :class:`FindingSet` (round-9). Otherwise the published findings
    are not the candidate the peers attested. Empty for gates with no terminal-bound decision + accepted
    set (PLAN carries text, CAPPED/CHANGES persist no set). Pure; cfg-free; never raises.
    """
    decision_idx, terminal_idx, _term, accepted_idx = _terminal_bound_decision_index(events, gate)
    if decision_idx is None or accepted_idx is None:
        return []
    decision = events[decision_idx]
    accepted = events[accepted_idx]
    try:
        candidate_fs = FindingSet.from_dict(decision.get("candidate"))
    except (ValueError, TypeError):
        return ["decision does not persist a valid candidate finding set"]
    try:
        accepted_fs = FindingSet.from_dict(accepted.get("payload"))
    except (ValueError, TypeError):
        return []  # a malformed accepted payload is already reported by the accepted-set payload check
    issues: list[str] = []
    if candidate_fs.to_dict() != accepted_fs.to_dict():
        issues.append("the decision's candidate does not match the accepted finding set payload")
    want_sha = decision.get("artifact_sha256")
    want_round = decision.get("round")
    builder = None
    for event in events:
        if (event.get("event") == "builder_output" and event.get("gate") == gate
                and event.get("round") == want_round and isinstance(event.get("payload"), str)
                and artifact_sha256(event["payload"].encode("utf-8")) == want_sha):
            builder = event
    if builder is None:
        issues.append("no bound Builder finding-set artifact matches the decision's candidate binding")
        return issues
    try:
        builder_fs = FindingSet.from_dict(json.loads(builder["payload"]))
    except (ValueError, TypeError, json.JSONDecodeError):
        issues.append("the bound Builder artifact is not a valid finding set")
    else:
        if builder_fs.to_dict() != accepted_fs.to_dict():
            issues.append("the accepted finding set does not match the bound Builder artifact the two "
                          "peers reviewed")
    return issues


def symmetric_plan_attestation_issues(
    events: list[dict[str, Any]], cfg: Config | None = None
) -> list[str]:
    """The symmetric PLAN gate's attestation issues (round-8 F3). A symmetric PLAN advancing terminal
    must be backed by a terminal-bound TWO-PEER ``symmetric_verdict`` — never a bare terminal, a
    build-style ``critic_verdict``, or a verdict with a different round/bindings — whose decision
    semantics are consistent (:func:`symmetric_decision_issues`). Empty when there is no advancing
    PLAN terminal (a missing/capped PLAN is handled by the PLAN phase checks). Symmetric callers only;
    the asymmetric build PLAN is untouched. Pure; never raises.
    """
    decision_idx, terminal_idx, term_event, _acc = _terminal_bound_decision_index(events, "plan")
    if terminal_idx is None or term_event not in _ADVANCE_TERMINALS:
        return []
    if decision_idx is None:
        return ["accepted terminal is not backed by a terminal-bound symmetric_verdict "
                "(a bare terminal, a critic_verdict, or a verdict with a different round/bindings)"]
    return symmetric_decision_issues(events, "plan", cfg)


def _last_terminal(
    events: list[dict[str, Any]], gate: str
) -> tuple[dict[str, Any] | None, int | None]:
    """The gate's LAST terminal event (any outcome) and its index, or ``(None, None)``."""
    last: dict[str, Any] | None = None
    last_idx: int | None = None
    for i, e in enumerate(events):
        if e.get("gate") == gate and e.get("event") in _TERMINAL_EVENTS:
            last, last_idx = e, i
    return last, last_idx


def gate_post_terminal_protocol_indices(events: list[dict[str, Any]], gate: str) -> list[int]:
    """Indices of same-gate symmetric protocol events recorded AFTER ``gate``'s authoritative (last)
    terminal — forbidden post-terminal residue.

    Once a gate reaches its authoritative terminal the decision is concluded; a later
    ``symmetric_verdict`` (which could rewrite the gate's unresolved objections), ``accepted_finding_set``
    (post-terminal accepted residue), or a second terminal can only be forged or crash residue, never
    part of the concluded decision. The shared resolver already treats a post-terminal accepted SET as
    residue; this closes the broader gap for a post-terminal verdict/terminal too. Pure; never raises.
    """
    _, term_idx = _last_terminal(events, gate)
    if term_idx is None:
        return []
    return [i for i, e in enumerate(events)
            if i > term_idx and e.get("gate") == gate and e.get("event") in _PROTOCOL_EVENTS]


def _gates_with_accepted_sets(events: list[dict[str, Any]]) -> list[str]:
    """Every distinct gate carrying an ``accepted_finding_set`` event, in first-appearance order."""
    gates: list[str] = []
    for e in events:
        if e.get("event") == "accepted_finding_set":
            gate = e.get("gate")
            if isinstance(gate, str) and gate not in gates:
                gates.append(gate)
    return gates


def plan_accepted_finding_set_indices(events: list[dict[str, Any]]) -> list[int]:
    """Indices of every ``accepted_finding_set`` event recorded for the PLAN gate — always forbidden.

    A symmetric PLAN artifact is plain text + a dependency tree; the PLAN gate advances on a two-peer
    ``symmetric_verdict`` and NEVER persists an accepted finding set (only ``dep:<id>`` and ``final``
    gates do). So ANY ``accepted_finding_set`` on the ``plan`` gate — before its decision, between the
    decision and the terminal (an otherwise well-formed trio the dependency/FINAL resolver never scans
    for PLAN), after the terminal, or orphaned with no terminal — is forged/corrupt history. Owned
    here so both :func:`crucible.workflow.workflow_issues` and :func:`require_complete_symmetric_run`
    fail closed on it identically, and a forged PLAN accepted set can never make the PLAN attestation
    look like a valid dependency-style trio. Pure; never raises.
    """
    return [i for i, e in enumerate(events)
            if e.get("event") == "accepted_finding_set" and e.get("gate") == "plan"]


def _gates_in_protocol_events(events: list[dict[str, Any]]) -> list[str]:
    """Every distinct gate carrying ANY symmetric protocol event (``symmetric_verdict``,
    ``accepted_finding_set``, or a terminal), in first-appearance order.

    Superset of :func:`_gates_with_accepted_sets`: it also sees a CAPPED (halt) gate and a
    verdict-only gate, neither of which persists an accepted finding set. Scope validation over this
    set therefore catches an out-of-scope gate whose only trace is a verdict + capped/proceeded
    terminal carrying objections — history an accepted-set-only scan would miss.
    """
    gates: list[str] = []
    for e in events:
        if e.get("event") in _PROTOCOL_EVENTS:
            gate = e.get("gate")
            if isinstance(gate, str) and gate not in gates:
                gates.append(gate)
    return gates


def _effective_accepted_events(
    events: list[dict[str, Any]]
) -> list[tuple[int, str, FindingSet]]:
    """``(index, gate, FindingSet)`` for every EFFECTIVE ``accepted_finding_set`` event, in log
    order. Each gate contributes at most one (the middle of its atomic trio, per
    :func:`resolve_gate_acceptance`). Raises ``ValueError`` when an effective payload is not a valid
    finding set."""
    out: list[tuple[int, str, FindingSet]] = []
    for gate in _gates_with_accepted_sets(events):
        resolution = resolve_gate_acceptance(events, gate)
        if resolution.accepted_index is not None:
            out.append((resolution.accepted_index, gate,
                        FindingSet.from_dict(events[resolution.accepted_index].get("payload"))))
    out.sort(key=lambda item: item[0])
    return out


def _orphan_accepted_indices(events: list[dict[str, Any]]) -> list[int]:
    """Indices of ``accepted_finding_set`` events that are NOT the effective accepted state of their
    gate — orphan, pre-terminal-without-terminal, post-terminal, binding-mismatched, or a duplicate
    pre-terminal set that broke the atomic trio."""
    orphans: list[int] = []
    for gate in _gates_with_accepted_sets(events):
        orphans.extend(resolve_gate_acceptance(events, gate).orphan_indices)
    return sorted(orphans)


def out_of_scope_accepted_gates(
    events: list[dict[str, Any]], dag: Any, *, final_enabled: bool
) -> list[str]:
    """Gates carrying an EFFECTIVE accepted finding set that are NOT part of the configured symmetric
    workflow, sorted for determinism.

    In scope are exactly the current dependency gates (``dep:<id>`` for every node in ``dag``) and —
    only when ``final_enabled`` — the ``final`` gate. Anything else with a valid accepted trio is out
    of scope: a ``dep:<id>`` whose node is absent from the current tree (a ghost/renamed node), or a
    ``final`` set in a run whose ``final_review`` is off. Such a set is fully bound-looking but was
    never legitimately produced against this run's plan, so callers fail closed on it rather than
    publish it. Only EFFECTIVE sets count (via :func:`resolve_gate_acceptance`); orphan/pre-terminal
    residue is handled separately by :func:`_orphan_accepted_indices`. Pure; never raises.
    """
    expected = {f"dep:{nid}" for nid in dag.order}
    if final_enabled:
        expected.add("final")
    out: list[str] = []
    for gate in _gates_with_accepted_sets(events):
        if gate in expected:
            continue
        if resolve_gate_acceptance(events, gate).accepted_index is not None:
            out.append(gate)
    return sorted(out)


def _in_workflow_scope(gate: str, expected: set[str]) -> bool:
    """A symmetric protocol gate is in scope only if it is a legitimate symmetric gate.

    The valid symmetric gate grammar is exactly ``plan``, a current dependency (``dep:<id>`` present
    in ``expected``), and — only when FINAL is enabled — ``final`` (also in ``expected``). Every other
    gate carrying a symmetric protocol event is out of scope: an arbitrary/off-convention name (e.g.
    ``sidequest``), a ``dep:<id>`` whose node is absent from the current tree (a ghost/renamed node),
    the ``final`` gate while ``final_review`` is disabled, or ``reproduce`` — which symmetric skills
    never use (design: symmetric protocols have no reproduce gate). Failing closed on all of these
    stops a forged capped/proceeded objection on such a gate from reaching the recommendation.
    """
    return gate == "plan" or gate in expected


def out_of_scope_protocol_gates(
    events: list[dict[str, Any]], dag: Any, *, final_enabled: bool
) -> list[str]:
    """Gates carrying ANY symmetric protocol event (``symmetric_verdict``, ``accepted_finding_set``,
    or a terminal) that are NOT a legitimate symmetric gate, sorted for determinism.

    The shared protocol-wide scope guard (see :func:`_in_workflow_scope`). In scope are exactly the
    ``plan`` gate, the current dependency gates (``dep:<id>`` for every node in ``dag``) and — only
    when ``final_enabled`` — the ``final`` gate. Everything else is out of scope: an arbitrary
    off-convention name (e.g. ``sidequest``), a ``dep:<id>`` whose node is absent from the current
    tree (a ghost/renamed node), the ``final`` gate in a run whose ``final_review`` is off, or a
    ``reproduce`` gate (symmetric protocols have none). Such a gate was never legitimately produced
    against this run's plan, so callers fail closed on it rather than publish its findings/objections.

    Superset of :func:`out_of_scope_accepted_gates`: that guard only sees gates that persisted an
    accepted finding SET, so a CAPPED (halt) gate — or a forged out-of-scope trio that only records a
    verdict + capped/proceeded terminal carrying objections but no accepted set — slips past it while
    its objections still reach the recommendation. This guard closes that gap by scanning every
    protocol event. The ``plan`` gate is in scope (it carries no dependency/FINAL finding set and is
    validated by the PLAN checks in :func:`crucible.workflow.workflow_issues`). Pure; never raises.
    """
    expected = {f"dep:{nid}" for nid in dag.order}
    if final_enabled:
        expected.add("final")
    out = [gate for gate in _gates_in_protocol_events(events)
           if not _in_workflow_scope(gate, expected)]
    return sorted(out)


def accepted_finding_set_for_gate(
    events: list[dict[str, Any]], gate: str
) -> FindingSet | None:
    """The effective accepted finding set for ``gate`` (``dep:<id>`` or ``final``), or ``None``.

    Raises ``ValueError`` if the effective accepted payload is malformed.
    """
    resolution = resolve_gate_acceptance(events, gate)
    if resolution.accepted_index is None:
        return None
    return FindingSet.from_dict(events[resolution.accepted_index].get("payload"))


def accepted_findings(
    events: list[dict[str, Any]], dag: Any = None
) -> FindingSet:
    """The deterministic union of accepted DEPENDENCY finding sets, keyed by ``(source_gate, id)``.

    Only effective ``accepted_finding_set`` events for ``dep:<id>`` gates contribute (FINAL is
    assembled FROM this union, so it is excluded here). Finding order within each set is preserved.

    Two calling modes with DELIBERATELY different scope contracts:

    - **DAG-aware Finish-time union** (``dag`` given): the union follows DAG topological order and
      FAILS CLOSED — an effective accepted ``dep:<id>`` whose node is absent from the current tree is
      never silently sorted after known nodes and published; it raises ``ValueError``. This is the
      deterministic scope guard the result commands and FINAL assembly rely on (a DAG that changed
      after acceptance, or forged history, cannot inject out-of-scope findings).
    - **No-DAG partial helper** (``dag`` omitted): a best-effort union over EVERY effective
      ``dep:<id>`` accepted set in log order, WITHOUT scope validation — it cannot know the tree, so
      it never fails closed on scope. Reports use this to render in-progress partial results; scope
      enforcement is exclusively the DAG-aware path's job.

    Raises ``ValueError`` if any effective payload is malformed, if two accepted findings share a
    ``(source_gate, id)`` key (duplicate/forged history), or (DAG-aware only) if an accepted
    dependency gate is outside the current tree OR one of its findings is attributed to a
    ``source_gate`` other than that dependency gate (a forged ``dep:<id>`` set smuggling a
    ``final``/ghost finding).
    """
    effective = [(i, gate, fs) for i, gate, fs in _effective_accepted_events(events)
                 if isinstance(gate, str) and gate.startswith("dep:")]
    if dag is not None:
        position = {f"dep:{nid}": pos for pos, nid in enumerate(dag.topological_order())}
        out_of_scope = sorted({gate for _, gate, _ in effective if gate not in position})
        if out_of_scope:
            raise ValueError(
                f"accepted dependency finding set(s) for gate(s) {out_of_scope} reference node(s) "
                f"absent from the current dependency tree; refusing to publish out-of-scope accepted "
                f"findings (the DAG changed after acceptance, or the history is forged)"
            )
        # Every finding in a dependency accepted set must be attributed to that dependency gate; a
        # forged dep:<id> set injecting a `source_gate: final` or `dep:ghost` finding is mis-scoped
        # history the DAG-aware Finish-time path must fail closed on (reusing the write-time rule).
        for _, gate, fs in effective:
            fs.validate_for_gate(gate)
        effective.sort(key=lambda item: (position[item[1]], item[0]))
    else:
        effective.sort(key=lambda item: item[0])
    findings: list[AcceptedFinding] = []
    seen: set[tuple[str, str]] = set()
    for _, _, finding_set in effective:
        for finding in finding_set.findings:
            if finding.key in seen:
                raise ValueError(
                    f"duplicate accepted finding key {finding.key} across accepted finding sets; "
                    f"the accepted history is invalid"
                )
            seen.add(finding.key)
            findings.append(finding)
    return FindingSet(summary="", findings=findings)


def validate_final_finding_set(candidate: FindingSet, prior: FindingSet) -> None:
    """Require a FINAL candidate to CONTAIN every accepted dependency finding exactly and add only
    cross-cutting ``source_gate: final`` findings.

    ``prior`` is the accepted dependency union. Every prior ``(source_gate, id)`` must be present in
    ``candidate`` with byte-for-structure identical content; a dropped or altered prior finding, or a
    candidate extra whose ``source_gate`` is not ``"final"``, is rejected (raises ``ValueError``).
    """
    candidate_by_key = candidate.by_key()
    prior_by_key = prior.by_key()
    dropped = [key[1] for key in prior_by_key if key not in candidate_by_key]
    if dropped:
        raise ValueError(
            f"FINAL finding set drops accepted dependency finding(s) {dropped}; a FINAL set must "
            f"contain every accepted dependency finding unchanged"
        )
    altered = [key[1] for key, prior_finding in prior_by_key.items()
               if candidate_by_key[key].to_dict() != prior_finding.to_dict()]
    if altered:
        raise ValueError(
            f"FINAL finding set alters accepted dependency finding(s) {altered}; accepted dependency "
            f"findings must be carried into FINAL byte-for-structure identical"
        )
    non_final_extra = [finding.id for finding in candidate.findings
                       if finding.key not in prior_by_key and finding.source_gate != "final"]
    if non_final_extra:
        raise ValueError(
            f"FINAL finding set adds finding(s) {non_final_extra} whose source_gate is not 'final'; "
            f"FINAL may only add cross-cutting findings with source_gate 'final'"
        )


def _ordered_unique_gates(events: list[dict[str, Any]]) -> list[str]:
    """Every distinct string ``gate`` in first-appearance (log) order."""
    ordered: list[str] = []
    for e in events:
        gate = e.get("gate")
        if isinstance(gate, str) and gate not in ordered:
            ordered.append(gate)
    return ordered


def unresolved_objections(
    events: list[dict[str, Any]], dag: Any = None, *, cfg: Config | None = None,
    final_enabled: bool = True
) -> list[dict[str, Any]]:
    """The namespaced peer objections carried UNRESOLVED past a proceeded-with-flags or capped
    symmetric gate — the peers' still-open disputes with the candidate, taken from the
    TERMINAL-BOUND ``symmetric_verdict`` (falling back to the terminal's ``open_findings`` ids).

    "Terminal-bound" means the last ``symmetric_verdict`` for the gate/round recorded BEFORE the
    gate's authoritative terminal, echoing the terminal's exact artifact/DAG/node bindings — the peer
    decision that actually led to the terminal. A ``symmetric_verdict`` appended AFTER the terminal is
    forged/crash residue (:func:`gate_post_terminal_protocol_indices` flags it invalid history) and is
    never selected, so it can neither erase the terminal's blocker nor inflate it.

    When ``cfg`` is given the retained objections are DEFENSIVELY filtered to those whose severity is
    in ``cfg.blocking_severities`` (round-8 F1): "unresolved objections" are unresolved BLOCKING
    objections, so a forged terminal-bound verdict carrying nonblocking objections can never inflate
    the recommendation even if some earlier guard were bypassed. Without ``cfg`` the collection is
    unfiltered (the legacy best-effort partial-helper contract).

    Two calling modes with DELIBERATELY different scope contracts (mirroring
    :func:`accepted_findings`):

    - **DAG-aware Finish-time projection** (``dag`` given): FAILS CLOSED — an objection-bearing gate
      outside the valid symmetric grammar (an arbitrary name, a ``dep:<id>`` node absent from the
      current tree, or the ``final`` gate while ``final_enabled`` is false) raises ``ValueError``
      rather than let a forged/stale out-of-scope objection reach the recommendation. In-scope
      capped/proceeded objections are retained.
    - **No-DAG partial helper** (``dag`` omitted): a best-effort collection over EVERY capped/
      proceeded gate in log order, WITHOUT scope validation — it cannot know the tree, so it never
      fails closed on scope.
    """
    blocking = set(cfg.blocking_severities) if cfg is not None else None
    out_of_scope = (set(out_of_scope_protocol_gates(events, dag, final_enabled=final_enabled))
                    if dag is not None else set())
    out: list[dict[str, Any]] = []
    for gate in _ordered_unique_gates(events):
        terminal, terminal_idx = _last_terminal(events, gate)
        if terminal is None or terminal.get("event") not in (
                "gate_proceeded_with_flags", "gate_capped"):
            continue
        if gate in out_of_scope:
            raise ValueError(
                f"unresolved objections reference gate {gate!r} outside the configured symmetric "
                f"workflow (an arbitrary gate, a dependency absent from the current tree, or FINAL "
                f"while final_review is disabled); refusing to publish out-of-scope objections"
            )
        round_index = terminal.get("round")
        want_bind = event_bindings(terminal)
        bound = None
        for i, e in enumerate(events):
            if (i < terminal_idx and e.get("event") == "symmetric_verdict"
                    and e.get("gate") == gate and e.get("round") == round_index
                    and event_bindings(e) == want_bind):
                bound = e
        if bound is not None:
            candidates: list[dict[str, Any]] = [o for o in bound.get("objections", [])
                                                if isinstance(o, dict)]
        else:  # pragma: no cover - a terminal always follows its bound symmetric_verdict in practice
            candidates = [{"id": oid} for oid in terminal.get("open_findings", [])]
        if blocking is not None:
            candidates = [o for o in candidates if o.get("severity") in blocking]
        out.extend(candidates)
    return out


def _pr_recommendation(
    findings: list[AcceptedFinding], objections: list[dict[str, Any]], cfg: Config
) -> str:
    """The deterministic ``APPROVE|COMMENT|REQUEST_CHANGES`` for pr-review, from the EFFECTIVE
    accepted findings and unresolved blocking objections — never from workflow status."""
    blocking = set(cfg.blocking_severities)
    if objections or any(f.severity in blocking for f in findings):
        return "REQUEST_CHANGES"
    if findings:
        return "COMMENT"
    return "APPROVE"


def review_result(
    events: list[dict[str, Any]], cfg: Config, workflow: str, dag: Any = None
) -> dict[str, Any]:
    """The deterministic symmetric review deliverable: effective accepted findings, unresolved
    objections, and (pr-review only) the derived recommendation.

    The effective finding set is the accepted FINAL set when FINAL is CONFIGURED (``final_review``)
    and reached an accepted terminal, otherwise the accepted dependency union. When ``final_review``
    is disabled the FINAL gate is not part of the workflow, so a (forged) FINAL accepted set is never
    promoted — the dependency union is the run's effective result (design: "If FINAL review is
    disabled, the dependency union is the run's effective accepted finding set"). For ``deep-dive``
    the ``recommendation`` key is omitted (an investigation returns a finding set, not an
    Approve/Comment/Request-changes call). This is a projection over ``events``; callers (the CLI
    result commands) enforce completeness and scope first.

    When ``dag`` is supplied (the CLI Finish-time path) the accepted union and the unresolved
    objections are BOTH scoped to the configured workflow and FAIL CLOSED on any out-of-scope
    dependency/FINAL gate, so a forged out-of-scope objection can never inflate the recommendation.
    """
    final_set = accepted_finding_set_for_gate(events, "final") if cfg.final_review else None
    effective = final_set if final_set is not None else accepted_findings(events, dag)
    objections = unresolved_objections(events, dag, cfg=cfg, final_enabled=cfg.final_review)
    result: dict[str, Any] = {"workflow": workflow}
    if workflow == "pr-review":
        result["recommendation"] = _pr_recommendation(effective.findings, objections, cfg)
    result["findings"] = [f.to_dict() for f in effective.findings]
    result["unresolved_objections"] = objections
    return result


def require_complete_symmetric_run(
    events: list[dict[str, Any]], dag: Any, *, require_final: bool, final_enabled: bool,
    expected_target_sha256: str | None = None
) -> None:
    """Finish-time completeness guard for the result commands (never for reports).

    Rejects (``ValueError`` prefixed ``incomplete symmetric workflow``) unless every DAG node is
    ``done`` and backed by an effective accepted dependency finding set whose findings are all
    attributed to that dependency gate (a mis-scoped ``source_gate: final``/ghost finding fails
    closed), no orphan/pre-terminal/post-terminal accepted set exists, no in-scope gate records a
    same-gate symmetric protocol event AFTER its authoritative terminal (post-terminal residue), no
    gate carrying ANY review protocol event is OUT OF SCOPE (an arbitrary name, a ``dep:<id>`` absent
    from the current tree, or the ``final`` gate when ``final_enabled`` is false), and — when
    ``require_final`` — the FINAL gate has an effective accepted set. The scope check spans every
    ``symmetric_verdict``/``accepted_finding_set``/terminal so a forged out-of-scope gate that only
    CAPS or proceeds-with-flags (persisting objections but no accepted set) is caught too.
    ``final_enabled`` mirrors ``cfg.final_review`` (FINAL is part of the configured workflow); it is
    distinct from ``require_final`` because ``accepted-findings`` legitimately precedes FINAL yet must
    still reject a forged FINAL gate in a run where FINAL is disabled. A malformed effective accepted
    payload surfaces its own ``ValueError``.

    ``expected_target_sha256`` (Task 2) is the pr-review run's authoritative loaded-target hash,
    threaded in by the CLI (never re-derived here — this module does not import the target/workflow
    layers). When given, every effective accepted finding set (dependency and FINAL) must bind exactly
    that target, so a result can never be published from accepted state bound to a substituted/absent
    target. ``None`` (build/deep-dive, or when the caller has no target) skips the check.
    """
    if not dag.nodes:
        raise ValueError("incomplete symmetric workflow: no dependency tree is loaded")
    not_done = [nid for nid in dag.order if dag.nodes[nid].status != "done"]
    if not_done:
        raise ValueError(
            f"incomplete symmetric workflow: node(s) {not_done} are not done — the result is a "
            f"Finish-time output, not a partial-progress query"
        )
    unbacked = [nid for nid in dag.order
                if accepted_finding_set_for_gate(events, f"dep:{nid}") is None]
    if unbacked:
        raise ValueError(
            f"incomplete symmetric workflow: node(s) {unbacked} have no valid accepted dependency "
            f"finding set"
        )
    # Each backed dependency's accepted set must attribute every finding to its own gate; a forged
    # dep:<id> set injecting a `source_gate: final`/ghost finding is mis-scoped history that must fail
    # closed at Finish time (reusing FindingSet.validate_for_gate, the write-time rule).
    mis_scoped: list[str] = []
    for nid in dag.order:
        gate = f"dep:{nid}"
        finding_set = accepted_finding_set_for_gate(events, gate)
        if finding_set is None:
            continue
        try:
            finding_set.validate_for_gate(gate)
        except ValueError as exc:
            mis_scoped.append(str(exc))
    if mis_scoped:
        raise ValueError(
            f"incomplete symmetric workflow: an accepted dependency finding set attributes a finding "
            f"to the wrong source_gate ({'; '.join(mis_scoped)})"
        )
    orphans = _orphan_accepted_indices(events)
    if orphans:
        raise ValueError(
            f"incomplete symmetric workflow: {len(orphans)} accepted finding set event(s) are "
            f"orphan/pre-terminal/post-terminal and are not accepted state"
        )
    # A symmetric PLAN gate never persists an accepted finding set (its artifact is text + a DAG). A
    # forged PLAN accepted set can form an otherwise-valid trio the dependency resolver never scans
    # for PLAN, so it evades the orphan/post-terminal checks above — fail closed here independently.
    plan_sets = plan_accepted_finding_set_indices(events)
    if plan_sets:
        raise ValueError(
            f"incomplete symmetric workflow: the PLAN gate records {len(plan_sets)} accepted finding "
            f"set event(s); a symmetric PLAN advances on a two-peer decision and never persists an "
            f"accepted finding set (forged/corrupt history)"
        )
    residue_gates = ["plan", *[f"dep:{nid}" for nid in dag.order]]
    if final_enabled:
        residue_gates.append("final")
    post_terminal = sorted({gate for gate in residue_gates
                            if gate_post_terminal_protocol_indices(events, gate)})
    if post_terminal:
        raise ValueError(
            f"incomplete symmetric workflow: gate(s) {post_terminal} record a symmetric protocol "
            f"event after their authoritative terminal (post-terminal residue that must never "
            f"rewrite the concluded decision)"
        )
    out_of_scope = out_of_scope_protocol_gates(events, dag, final_enabled=final_enabled)
    if out_of_scope:
        raise ValueError(
            f"incomplete symmetric workflow: gate(s) {out_of_scope} record review protocol events "
            f"outside the configured workflow (an arbitrary gate, a dependency absent from the "
            f"current tree, or FINAL while final_review is disabled) — refusing to publish "
            f"out-of-scope results"
        )
    if require_final and accepted_finding_set_for_gate(events, "final") is None:
        raise ValueError(
            "incomplete symmetric workflow: the configured FINAL gate has not reached an accepted "
            "finding set"
        )
    # Task 2: every effective accepted finding set (dependency and FINAL) must bind the authoritative
    # loaded target, so a pr-review result is never published from accepted state bound to a
    # substituted/absent target. The expected hash is threaded in by the CLI (this module stays free
    # of a target/workflow import cycle); ``None`` skips the check (build/deep-dive).
    if expected_target_sha256 is not None:
        for index, gate, _finding_set in _effective_accepted_events(events):
            if events[index].get("target_sha256") != expected_target_sha256:
                raise ValueError(
                    f"incomplete symmetric workflow: the accepted finding set for gate {gate!r} is "
                    f"not bound to the loaded review target (target hash missing or mismatched)"
                )
