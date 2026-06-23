"""Run configuration: models, round caps, and consensus policies."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "builder": {"model": "claude-opus-4.8", "effort": "max"},
    "critic": {"model": "gpt-5.5", "effort": "xhigh"},
    "max_rounds_plan": 5,
    "max_rounds_dep": 5,
    "on_cap": "halt",
    "defer_severities": ["minor", "nit"],
    "blocking_severities": ["blocker", "major"],
    "strict_rebuttal": False,
    "final_review": True,
}

VALID_ON_CAP = ("halt", "proceed_with_flags")
VALID_SEVERITIES = ("blocker", "major", "minor", "nit")


@dataclass
class Config:
    builder: dict[str, str]
    critic: dict[str, str]
    max_rounds_plan: int
    max_rounds_dep: int
    on_cap: str
    defer_severities: list[str]
    blocking_severities: list[str]
    strict_rebuttal: bool
    final_review: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        unknown = set(data) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        merged = {**DEFAULTS, **{k: v for k, v in data.items() if v is not None}}
        cfg = cls(
            builder=dict(merged["builder"]),
            critic=dict(merged["critic"]),
            max_rounds_plan=int(merged["max_rounds_plan"]),
            max_rounds_dep=int(merged["max_rounds_dep"]),
            on_cap=str(merged["on_cap"]),
            defer_severities=list(merged["defer_severities"]),
            blocking_severities=list(merged["blocking_severities"]),
            strict_rebuttal=bool(merged["strict_rebuttal"]),
            final_review=bool(merged["final_review"]),
        )
        cfg._validate()
        return cfg

    def _validate(self) -> None:
        if self.on_cap not in VALID_ON_CAP:
            raise ValueError(f"on_cap must be one of {VALID_ON_CAP}, got {self.on_cap!r}")
        if self.max_rounds_plan < 1:
            raise ValueError("max_rounds_plan must be >= 1")
        if self.max_rounds_dep < 1:
            raise ValueError("max_rounds_dep must be >= 1")
        for name in ("defer_severities", "blocking_severities"):
            bad = set(getattr(self, name)) - set(VALID_SEVERITIES)
            if bad:
                raise ValueError(f"{name} has invalid severities: {sorted(bad)}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "builder": dict(self.builder),
            "critic": dict(self.critic),
            "max_rounds_plan": self.max_rounds_plan,
            "max_rounds_dep": self.max_rounds_dep,
            "on_cap": self.on_cap,
            "defer_severities": list(self.defer_severities),
            "blocking_severities": list(self.blocking_severities),
            "strict_rebuttal": self.strict_rebuttal,
            "final_review": self.final_review,
        }


def load_config(path: str | Path) -> Config:
    data = json.loads(Path(path).read_text())
    return Config.from_dict(data)
