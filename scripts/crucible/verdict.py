"""Critic verdict parsing and the consensus/stop decision."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from crucible.config import Config

VALID_VERDICTS = ("APPROVE", "REQUEST_CHANGES")
VALID_SEVERITIES = ("blocker", "major", "minor", "nit")
VALID_RESOLUTIONS = ("fixed", "deferred", "wontfix")


@dataclass
class Finding:
    id: str
    severity: str
    location: str
    claim: str
    suggestion: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Finding":
        if not isinstance(data, dict):
            raise ValueError("finding must be a JSON object")
        sev = data["severity"]
        if sev not in VALID_SEVERITIES:
            raise ValueError(f"invalid severity: {sev}")
        fid = data["id"]
        if not isinstance(fid, str) or not fid:
            raise ValueError("finding.id must be a non-empty string")
        return cls(
            id=fid,
            severity=sev,
            location=data.get("location", ""),
            claim=data.get("claim", ""),
            suggestion=data.get("suggestion", ""),
        )


@dataclass
class Verdict:
    gate: str
    round: int
    verdict: str
    summary: str
    findings: list[Finding]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Verdict":
        if not isinstance(data, dict):
            raise ValueError("verdict must be a JSON object")
        verdict = data["verdict"]
        if verdict not in VALID_VERDICTS:
            raise ValueError(f"invalid verdict: {verdict}")
        findings_raw = data.get("findings", [])
        if not isinstance(findings_raw, list):
            raise ValueError('verdict "findings" must be a list')
        findings = [Finding.from_dict(f) for f in findings_raw]
        ids = [f.id for f in findings]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate finding ids: {dupes}")
        gate = data["gate"]
        if not isinstance(gate, str) or not gate:
            raise ValueError("verdict.gate must be a non-empty string")
        rnd = data["round"]
        if isinstance(rnd, bool) or not isinstance(rnd, int):
            raise ValueError("verdict.round must be an integer")
        return cls(
            gate=gate,
            round=rnd,
            verdict=verdict,
            summary=data.get("summary", ""),
            findings=findings,
        )

    def open_blocking(self, cfg: Config) -> list[Finding]:
        blocking = set(cfg.blocking_severities)
        return [f for f in self.findings if f.severity in blocking]

    def consistency_error(self, cfg: Config) -> str | None:
        """Reject a Critic verdict whose APPROVE/REQUEST_CHANGES label contradicts
        its findings, relative to the run's ``blocking_severities`` (consensus-rubric.md):

        - ``APPROVE`` requires **no** blocking finding present.
        - ``REQUEST_CHANGES`` requires **at least one** blocking finding present.

        Returns a human-readable error string when contradictory, else ``None``. This
        is checked against the raw Critic verdict, before any Builder resolutions, so a
        rebuttal flow (REQUEST_CHANGES + a real blocker later cleared by ``wontfix``)
        stays valid.
        """
        blocking_ids = [f.id for f in self.open_blocking(cfg)]
        sev = sorted(cfg.blocking_severities)
        if self.verdict == "APPROVE" and blocking_ids:
            return (f"inconsistent verdict: APPROVE but {blocking_ids} have a blocking "
                    f"severity {sev}; APPROVE requires no open blocking findings")
        if self.verdict == "REQUEST_CHANGES" and not blocking_ids:
            return (f"inconsistent verdict: REQUEST_CHANGES but no finding has a blocking "
                    f"severity {sev}; REQUEST_CHANGES requires at least one blocking finding")
        return None


@dataclass
class Decision:
    outcome: str  # "CONSENSUS" | "CHANGES" | "CAPPED" | "PROCEED_WITH_FLAGS"
    open_findings: list[Finding]


def _resolution_clears(finding: Finding, resolution, cfg: Config) -> bool:
    """Whether a Builder resolution removes a blocking finding from the open set.

    - ``deferred`` only clears findings whose severity is in ``defer_severities``
      (never a blocker/major); deferring a blocking finding has no effect.
    - ``wontfix`` (a rebuttal) clears the finding unless ``strict_rebuttal`` is set,
      in which case it stays blocking until the Critic itself drops it next round.
    - ``fixed`` / no resolution keep the finding open so the loop runs another round.
    """
    if resolution == "deferred":
        return finding.severity in cfg.defer_severities
    if resolution == "wontfix":
        return not cfg.strict_rebuttal
    return False


def decide(
    verdict: Verdict,
    cfg: Config,
    round_index: int,
    max_rounds: int,
    resolutions=None,
    always_halt: bool = False,
) -> Decision:
    resolutions = resolutions or {}
    open_blocking = [
        f
        for f in verdict.open_blocking(cfg)
        if not _resolution_clears(f, resolutions.get(f.id), cfg)
    ]
    if not open_blocking:
        return Decision(outcome="CONSENSUS", open_findings=[])
    if round_index >= max_rounds:
        # At the cap with unresolved blockers, on_cap decides whether to halt (CAPPED) or
        # advance past the gate carrying the unresolved findings as flags. ``always_halt``
        # gates (e.g. REPRODUCE) must never advance with flags — an unconfirmed result halts.
        if cfg.on_cap == "proceed_with_flags" and not always_halt:
            return Decision(outcome="PROCEED_WITH_FLAGS", open_findings=open_blocking)
        return Decision(outcome="CAPPED", open_findings=open_blocking)
    return Decision(outcome="CHANGES", open_findings=open_blocking)
