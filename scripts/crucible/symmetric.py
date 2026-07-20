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
