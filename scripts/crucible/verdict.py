"""Critic verdict parsing and the consensus/stop decision."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from crucible.config import Config

VALID_VERDICTS = ("APPROVE", "REQUEST_CHANGES")
VALID_SEVERITIES = ("blocker", "major", "minor", "nit")


@dataclass
class Finding:
    id: str
    severity: str
    location: str
    claim: str
    suggestion: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Finding":
        sev = data["severity"]
        if sev not in VALID_SEVERITIES:
            raise ValueError(f"invalid severity: {sev}")
        return cls(
            id=data["id"],
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
        verdict = data["verdict"]
        if verdict not in VALID_VERDICTS:
            raise ValueError(f"invalid verdict: {verdict}")
        return cls(
            gate=data["gate"],
            round=int(data["round"]),
            verdict=verdict,
            summary=data.get("summary", ""),
            findings=[Finding.from_dict(f) for f in data.get("findings", [])],
        )

    def open_blocking(self, cfg: Config) -> list[Finding]:
        blocking = set(cfg.blocking_severities)
        return [f for f in self.findings if f.severity in blocking]


@dataclass
class Decision:
    outcome: str
    open_findings: list[Finding]


def decide(verdict: Verdict, cfg: Config, round_index: int, max_rounds: int) -> Decision:
    open_blocking = verdict.open_blocking(cfg)
    if not open_blocking:
        return Decision(outcome="CONSENSUS", open_findings=[])
    if round_index >= max_rounds:
        return Decision(outcome="CAPPED", open_findings=open_blocking)
    return Decision(outcome="CHANGES", open_findings=open_blocking)
