import json

import pytest

from crucible.config import Config, DEFAULTS, load_config


def test_defaults_match_spec():
    cfg = Config.from_dict({})
    assert cfg.builder == {"model": "claude-opus-4.8", "effort": "max"}
    assert cfg.critic == {"model": "gpt-5.5", "effort": "xhigh"}
    assert cfg.max_rounds_plan == 5
    assert cfg.max_rounds_dep == 5
    assert cfg.on_cap == "halt"
    assert cfg.defer_severities == ["minor", "nit"]
    assert cfg.blocking_severities == ["blocker", "major"]
    assert cfg.strict_rebuttal is False
    assert cfg.final_review is True


def test_partial_override_keeps_other_defaults():
    cfg = Config.from_dict({"max_rounds_dep": 3, "on_cap": "proceed_with_flags"})
    assert cfg.max_rounds_dep == 3
    assert cfg.on_cap == "proceed_with_flags"
    assert cfg.max_rounds_plan == 5


def test_invalid_on_cap_raises():
    with pytest.raises(ValueError, match="on_cap"):
        Config.from_dict({"on_cap": "yolo"})


def test_invalid_round_cap_raises():
    with pytest.raises(ValueError, match="max_rounds_plan"):
        Config.from_dict({"max_rounds_plan": 0})


def test_load_config_from_file(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"critic": {"model": "gpt-5.4", "effort": "high"}}))
    cfg = load_config(p)
    assert cfg.critic == {"model": "gpt-5.4", "effort": "high"}
    assert cfg.builder == DEFAULTS["builder"]


def test_to_dict_round_trips():
    cfg = Config.from_dict({"final_review": False})
    again = Config.from_dict(cfg.to_dict())
    assert again.to_dict() == cfg.to_dict()
    assert again.final_review is False


def test_example_config_file_is_valid():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    cfg = load_config(root / "config.example.json")
    assert cfg.on_cap in ("halt", "proceed_with_flags")
