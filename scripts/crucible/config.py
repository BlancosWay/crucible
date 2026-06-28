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


def _require_bool(name: str, value: Any) -> bool:
    # bool(...) would silently turn the JSON string "false" into True, inverting intent;
    # require a real boolean instead.
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean (true/false), got {value!r}")
    return value


def _require_int(name: str, value: Any) -> int:
    # int(...) would crash on a list/dict (raw TypeError) and silently coerce bools/floats/
    # numeric strings; require a real integer.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer, got {value!r}")
    return value


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
        if not isinstance(data, dict):
            raise ValueError("config must be a JSON object")
        unknown = set(data) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        merged = {**DEFAULTS, **{k: v for k, v in data.items() if v is not None}}
        # builder/critic are nested dicts: deep-merge so a partial override (e.g. just `model`)
        # keeps the sibling defaults (e.g. `effort`) instead of dropping them.
        for role in ("builder", "critic"):
            override = data.get(role)
            if override is None:
                merged[role] = dict(DEFAULTS[role])
            elif not isinstance(override, dict):
                raise ValueError(f"{role} must be an object")
            else:
                merged[role] = {**DEFAULTS[role], **override}
        for role in ("builder", "critic"):
            for field in ("model", "effort"):
                if not isinstance(merged[role].get(field), str) or not merged[role][field].strip():
                    raise ValueError(f"{role}.{field} must be a non-empty string")
        for name in ("defer_severities", "blocking_severities"):
            val = merged[name]
            if not isinstance(val, list) or not all(isinstance(s, str) for s in val):
                raise ValueError(f"{name} must be a list of severity strings")
        cfg = cls(
            builder=dict(merged["builder"]),
            critic=dict(merged["critic"]),
            max_rounds_plan=_require_int("max_rounds_plan", merged["max_rounds_plan"]),
            max_rounds_dep=_require_int("max_rounds_dep", merged["max_rounds_dep"]),
            on_cap=str(merged["on_cap"]),
            defer_severities=list(merged["defer_severities"]),
            blocking_severities=list(merged["blocking_severities"]),
            strict_rebuttal=_require_bool("strict_rebuttal", merged["strict_rebuttal"]),
            final_review=_require_bool("final_review", merged["final_review"]),
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
        overlap = set(self.defer_severities) & set(self.blocking_severities)
        if overlap:
            raise ValueError("defer_severities and blocking_severities must be disjoint; "
                             f"both contain: {sorted(overlap)}")

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
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Config.from_dict(data)
