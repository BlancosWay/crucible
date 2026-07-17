"""Run configuration: models, round caps, and consensus policies."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULTS_PATH = Path(__file__).resolve().parents[2] / "config.defaults.json"
DEFAULT_CONFIG_KEYS = frozenset({
    "builder",
    "critic",
    "max_rounds_plan",
    "max_rounds_dep",
    "on_cap",
    "defer_severities",
    "blocking_severities",
    "strict_rebuttal",
    "final_review",
    "human_approval",
    "reproduce_gate",
    "critic_checklists",
})
ROLE_KEYS = frozenset({"model", "effort"})


def load_defaults(path: str | Path = DEFAULTS_PATH) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("default config must be a JSON object")
    missing = DEFAULT_CONFIG_KEYS - set(data)
    unknown = set(data) - DEFAULT_CONFIG_KEYS
    if missing:
        raise ValueError(f"missing default config keys: {sorted(missing)}")
    if unknown:
        raise ValueError(f"unknown default config keys: {sorted(unknown)}")
    for role in ("builder", "critic"):
        value = data[role]
        if not isinstance(value, dict):
            raise ValueError(f"{role} default must be an object")
        if set(value) != ROLE_KEYS:
            raise ValueError(
                f"{role} default keys must be {sorted(ROLE_KEYS)}, got {sorted(value)}"
            )
    return data


DEFAULTS: dict[str, Any] = load_defaults()

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
    human_approval: bool
    reproduce_gate: bool
    critic_checklists: list[str]

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
                unknown_nested = set(override) - set(DEFAULTS[role])
                if unknown_nested:
                    raise ValueError(f"unknown {role} keys: {sorted(unknown_nested)}")
                merged[role] = {**DEFAULTS[role], **override}
        for role in ("builder", "critic"):
            for key in ("model", "effort"):
                if not isinstance(merged[role].get(key), str) or not merged[role][key].strip():
                    raise ValueError(f"{role}.{key} must be a non-empty string")
        for name in ("defer_severities", "blocking_severities"):
            val = merged[name]
            if not isinstance(val, list) or not all(isinstance(s, str) for s in val):
                raise ValueError(f"{name} must be a list of severity strings")
        checklists = merged["critic_checklists"]
        if not isinstance(checklists, list) or not all(
            isinstance(s, str) and s.strip() for s in checklists
        ):
            raise ValueError("critic_checklists must be a list of non-empty path strings")
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
            human_approval=_require_bool("human_approval", merged["human_approval"]),
            reproduce_gate=_require_bool("reproduce_gate", merged["reproduce_gate"]),
            critic_checklists=list(merged["critic_checklists"]),
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
        # blocking_severities must be non-empty: with no blocking severity, no finding can ever
        # block, so APPROVE/REQUEST_CHANGES diverge — every REQUEST_CHANGES fails consistency
        # (verdict.consistency_error) and a gate could never legitimately request changes.
        if not self.blocking_severities:
            raise ValueError("blocking_severities must be non-empty (a REQUEST_CHANGES needs at "
                             "least one blocking severity)")
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
            "human_approval": self.human_approval,
            "reproduce_gate": self.reproduce_gate,
            "critic_checklists": list(self.critic_checklists),
        }


def load_config(path: str | Path) -> Config:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Config.from_dict(data)
