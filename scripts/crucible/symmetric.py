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

from dataclasses import dataclass
from typing import Any

from crucible.config import Config
from crucible.integrity import event_bindings
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

    Missing metadata means ``build``: an existing schema-v2 run predates the field, so it is the
    asymmetric Builder/Critic workflow. ``init_run`` validates the value it writes, so a present but
    non-string/unrecognized value can only arise from tampering; rather than propagate garbage into
    the symmetric/asymmetric routing, this reader still returns a member of :data:`VALID_WORKFLOWS`
    (defaulting to ``build``). Mirrors ``integrity.run_schema_version``: the first ``run_start`` wins.
    """
    for event in events:
        if event.get("event") == "run_start":
            workflow = event.get("workflow")
            return workflow if workflow in VALID_WORKFLOWS else "build"
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
        for key in ("artifact_sha256", "dag_sha256", "node_sha256"):
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

    # Valid trio. Any OTHER accepted set for the gate is post-terminal residue, never accepted state.
    orphans = tuple(i for i in accepted_all if i != accepted_idx)
    issues = (_POST_TERMINAL_RESIDUE,) if orphans else ()
    return GateAcceptance(gate, accepted_idx, terminal_idx, orphans, issues)


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


def _gates_with_accepted_sets(events: list[dict[str, Any]]) -> list[str]:
    """Every distinct gate carrying an ``accepted_finding_set`` event, in first-appearance order."""
    gates: list[str] = []
    for e in events:
        if e.get("event") == "accepted_finding_set":
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
    assembled FROM this union, so it is excluded here). When ``dag`` is given the union follows DAG
    topological order; otherwise it follows log order. Finding order within each set is preserved.
    Raises ``ValueError`` if any effective payload is malformed or if two accepted findings share a
    ``(source_gate, id)`` key (duplicate/forged history).
    """
    effective = [(i, gate, fs) for i, gate, fs in _effective_accepted_events(events)
                 if isinstance(gate, str) and gate.startswith("dep:")]
    if dag is not None:
        position = {f"dep:{nid}": pos for pos, nid in enumerate(dag.topological_order())}
        effective.sort(key=lambda item: (position.get(item[1], len(position)), item[0]))
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


def unresolved_objections(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The namespaced peer objections carried UNRESOLVED past a proceeded-with-flags or capped
    symmetric gate — the peers' still-open disputes with the candidate, taken from that gate's final
    ``symmetric_verdict`` (falling back to the terminal's ``open_findings`` ids)."""
    out: list[dict[str, Any]] = []
    for gate in _ordered_unique_gates(events):
        terminal, _ = _last_terminal(events, gate)
        if terminal is None or terminal.get("event") not in (
                "gate_proceeded_with_flags", "gate_capped"):
            continue
        round_index = terminal.get("round")
        verdicts = [v for v in events if v.get("event") == "symmetric_verdict"
                    and v.get("gate") == gate and v.get("round") == round_index]
        if verdicts:
            out.extend(o for o in verdicts[-1].get("objections", []) if isinstance(o, dict))
        else:  # pragma: no cover - a terminal always follows its symmetric_verdict in practice
            out.extend({"id": oid} for oid in terminal.get("open_findings", []))
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


def review_result(events: list[dict[str, Any]], cfg: Config, workflow: str) -> dict[str, Any]:
    """The deterministic symmetric review deliverable: effective accepted findings, unresolved
    objections, and (pr-review only) the derived recommendation.

    The effective finding set is the accepted FINAL set when FINAL reached an accepted terminal,
    otherwise the accepted dependency union. For ``deep-dive`` the ``recommendation`` key is omitted
    (an investigation returns a finding set, not an Approve/Comment/Request-changes call). This is a
    projection over ``events``; callers (the CLI result commands) enforce completeness first.
    """
    final_set = accepted_finding_set_for_gate(events, "final")
    effective = final_set if final_set is not None else accepted_findings(events)
    objections = unresolved_objections(events)
    result: dict[str, Any] = {"workflow": workflow}
    if workflow == "pr-review":
        result["recommendation"] = _pr_recommendation(effective.findings, objections, cfg)
    result["findings"] = [f.to_dict() for f in effective.findings]
    result["unresolved_objections"] = objections
    return result


def require_complete_symmetric_run(
    events: list[dict[str, Any]], dag: Any, *, require_final: bool
) -> None:
    """Finish-time completeness guard for the result commands (never for reports).

    Rejects (``ValueError`` prefixed ``incomplete symmetric workflow``) unless every DAG node is
    ``done`` and backed by an effective accepted dependency finding set, no orphan/pre-terminal/
    post-terminal accepted set exists, and — when ``require_final`` — the FINAL gate has an effective
    accepted set. A malformed effective accepted payload surfaces its own ``ValueError``.
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
    orphans = _orphan_accepted_indices(events)
    if orphans:
        raise ValueError(
            f"incomplete symmetric workflow: {len(orphans)} accepted finding set event(s) are "
            f"orphan/pre-terminal/post-terminal and are not accepted state"
        )
    if require_final and accepted_finding_set_for_gate(events, "final") is None:
        raise ValueError(
            "incomplete symmetric workflow: the configured FINAL gate has not reached an accepted "
            "finding set"
        )
