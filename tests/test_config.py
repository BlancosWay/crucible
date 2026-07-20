import json

import pytest

from crucible.config import (
    Config,
    DEFAULTS,
    DEFAULTS_PATH,
    load_config,
    load_defaults,
    resolved_config_shape_error,
)


def test_defaults_match_shipped_file():
    cfg = Config.from_dict({})
    assert cfg.to_dict() == DEFAULTS
    assert DEFAULTS_PATH.name == "config.defaults.json"
    assert load_defaults() == DEFAULTS


def test_load_defaults_surfaces_missing_and_malformed_files(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_defaults(tmp_path / "missing.json")

    p = tmp_path / "malformed.json"
    p.write_text("{")
    with pytest.raises(json.JSONDecodeError):
        load_defaults(p)


def test_load_defaults_rejects_non_object(tmp_path):
    p = tmp_path / "defaults.json"
    p.write_text("[]")
    with pytest.raises(ValueError, match="JSON object"):
        load_defaults(p)


def test_load_defaults_rejects_missing_or_unknown_keys(tmp_path):
    missing = dict(DEFAULTS)
    missing.pop("on_cap")
    p = tmp_path / "missing.json"
    p.write_text(json.dumps(missing))
    with pytest.raises(ValueError, match="missing default config keys"):
        load_defaults(p)

    extra = {**DEFAULTS, "surprise": True}
    p = tmp_path / "extra.json"
    p.write_text(json.dumps(extra))
    with pytest.raises(ValueError, match="unknown default config keys"):
        load_defaults(p)


def test_load_defaults_rejects_invalid_role_shape(tmp_path):
    data = {**DEFAULTS, "critic": {"model": "x"}}
    p = tmp_path / "role.json"
    p.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="critic default keys"):
        load_defaults(p)


def test_partial_override_keeps_other_defaults():
    cfg = Config.from_dict({"max_rounds_dep": 3, "on_cap": "proceed_with_flags"})
    assert cfg.max_rounds_dep == 3
    assert cfg.on_cap == "proceed_with_flags"
    assert cfg.max_rounds_plan == DEFAULTS["max_rounds_plan"]


def test_invalid_on_cap_raises():
    with pytest.raises(ValueError, match="on_cap"):
        Config.from_dict({"on_cap": "yolo"})


def test_from_dict_rejects_non_dict():
    # G5: a top-level non-object (list/str/number/None) must raise a clean ValueError, not
    # a raw AttributeError when from_dict reaches into `.items()` / `set(data)`.
    for bad in ([], ["a"], "x", 3, None):
        with pytest.raises(ValueError, match="object"):
            Config.from_dict(bad)


def test_empty_model_or_effort_rejected():
    # C6: an empty/blank model or effort string validates structurally but yields an unusable
    # dispatch config; require a non-empty (non-whitespace) string.
    for role in ("builder", "critic"):
        with pytest.raises(ValueError, match=f"{role}.model"):
            Config.from_dict({role: {"model": ""}})
        with pytest.raises(ValueError, match=f"{role}.effort"):
            Config.from_dict({role: {"effort": "  "}})


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


def test_string_boolean_strict_rebuttal_raises():
    with pytest.raises(ValueError, match="strict_rebuttal must be a boolean"):
        Config.from_dict({"strict_rebuttal": "false"})


def test_string_boolean_final_review_raises():
    with pytest.raises(ValueError, match="final_review must be a boolean"):
        Config.from_dict({"final_review": "false"})


def test_string_boolean_human_approval_raises():
    with pytest.raises(ValueError, match="human_approval must be a boolean"):
        Config.from_dict({"human_approval": "true"})


def test_human_approval_enabled_round_trips():
    cfg = Config.from_dict({"human_approval": True})
    again = Config.from_dict(cfg.to_dict())
    assert again.human_approval is True


def test_string_boolean_reproduce_gate_raises():
    with pytest.raises(ValueError, match="reproduce_gate must be a boolean"):
        Config.from_dict({"reproduce_gate": "true"})


def test_reproduce_gate_enabled_round_trips():
    cfg = Config.from_dict({"reproduce_gate": True})
    again = Config.from_dict(cfg.to_dict())
    assert again.reproduce_gate is True


def test_numeric_boolean_raises():
    with pytest.raises(ValueError, match="strict_rebuttal must be a boolean"):
        Config.from_dict({"strict_rebuttal": 1})


def test_real_booleans_are_accepted():
    cfg = Config.from_dict({"strict_rebuttal": True, "final_review": False})
    assert cfg.strict_rebuttal is True
    assert cfg.final_review is False


def test_partial_builder_override_keeps_default_effort():
    cfg = Config.from_dict({"builder": {"model": "claude-x"}})
    assert cfg.builder == {
        "model": "claude-x",
        "effort": DEFAULTS["builder"]["effort"],
    }


def test_partial_critic_override_keeps_default_effort():
    cfg = Config.from_dict({"critic": {"model": "gpt-x"}})
    assert cfg.critic == {"model": "gpt-x", "effort": DEFAULTS["critic"]["effort"]}


def test_critic_effort_only_override_keeps_default_model():
    cfg = Config.from_dict({"critic": {"effort": "high"}})
    assert cfg.critic == {"model": DEFAULTS["critic"]["model"], "effort": "high"}


def test_config_rejects_unknown_nested_builder_key():
    with pytest.raises(ValueError, match="unknown builder keys"):
        Config.from_dict({"builder": {"modle": "x"}})


def test_config_rejects_unknown_nested_critic_key():
    with pytest.raises(ValueError, match="unknown critic keys"):
        Config.from_dict({"critic": {"reasoning_effort": "low"}})


def test_config_partial_nested_override_still_allowed():
    cfg = Config.from_dict({"builder": {"model": "custom-model"}})
    assert cfg.builder["model"] == "custom-model"
    assert cfg.builder["effort"] == DEFAULTS["builder"]["effort"]


def test_config_empty_nested_object_keeps_defaults():
    assert Config.from_dict({"builder": {}}).builder == DEFAULTS["builder"]


def test_config_null_nested_override_keeps_defaults():
    assert Config.from_dict({"builder": None}).builder == DEFAULTS["builder"]


def test_non_dict_builder_raises():
    with pytest.raises(ValueError, match="builder must be an object"):
        Config.from_dict({"builder": "oops"})


# --- scalar/field type validation (N3) ---------------------------------------

def test_max_rounds_non_int_raises():
    for bad in ([], {}, "5", 1.5, True):
        with pytest.raises(ValueError, match="max_rounds_plan must be an integer"):
            Config.from_dict({"max_rounds_plan": bad})


def test_severities_must_be_a_list():
    with pytest.raises(ValueError, match="defer_severities must be a list"):
        Config.from_dict({"defer_severities": "major"})  # would otherwise char-explode
    with pytest.raises(ValueError, match="blocking_severities must be a list"):
        Config.from_dict({"blocking_severities": 5})


def test_severity_list_elements_must_be_strings():
    for bad in ([[]], ["bad", 1], [None]):
        with pytest.raises(ValueError, match="must be a list of severity strings"):
            Config.from_dict({"defer_severities": bad})


def test_builder_critic_fields_must_be_strings():
    with pytest.raises(ValueError, match="builder.model must be a non-empty string"):
        Config.from_dict({"builder": {"model": 1, "effort": "max"}})
    with pytest.raises(ValueError, match="critic.effort must be a non-empty string"):
        Config.from_dict({"critic": {"model": "x", "effort": []}})


def test_valid_scalars_still_parse():
    cfg = Config.from_dict({"max_rounds_plan": 3, "defer_severities": ["minor"]})
    assert cfg.max_rounds_plan == 3 and cfg.defer_severities == ["minor"]


def test_overlapping_defer_and_blocking_severities_raises():
    with pytest.raises(ValueError, match="disjoint"):
        Config.from_dict({"defer_severities": ["major", "minor", "nit"],
                          "blocking_severities": ["blocker", "major"]})


def test_empty_blocking_severities_rejected():
    # An empty blocking set makes every REQUEST_CHANGES fail verdict.consistency_error
    # (no finding can be blocking), so a gate could never legitimately request changes.
    with pytest.raises(ValueError, match="blocking_severities must be non-empty"):
        Config.from_dict({"blocking_severities": []})


def test_empty_defer_severities_allowed():
    # Empty defer is legitimate (nothing is deferrable); only blocking must be non-empty.
    cfg = Config.from_dict({"defer_severities": []})
    assert cfg.defer_severities == []
    assert cfg.blocking_severities == DEFAULTS["blocking_severities"]


def test_defaults_file_is_valid_config():
    assert load_config(DEFAULTS_PATH).to_dict() == DEFAULTS


def test_critic_checklists_defaults_empty_and_round_trips():
    assert Config.from_dict({}).critic_checklists == []
    cfg = Config.from_dict({"critic_checklists": ["/abs/lens.md", "/abs/other.md"]})
    assert cfg.critic_checklists == ["/abs/lens.md", "/abs/other.md"]
    assert Config.from_dict(cfg.to_dict()).critic_checklists == ["/abs/lens.md", "/abs/other.md"]


def test_critic_checklists_must_be_a_list_of_strings():
    with pytest.raises(ValueError, match="critic_checklists must be a list"):
        Config.from_dict({"critic_checklists": "/abs/lens.md"})  # a bare string, not a list
    with pytest.raises(ValueError, match="critic_checklists must be a list"):
        Config.from_dict({"critic_checklists": ["/abs/ok.md", 5]})  # a non-string element


def test_critic_checklists_rejects_empty_or_whitespace_entries():
    for bad in ([""], ["   "], ["/abs/ok.md", ""]):
        with pytest.raises(ValueError, match="critic_checklists must be a list"):
            Config.from_dict({"critic_checklists": bad})


def test_resolved_config_shape_accepts_exact_to_dict_output():
    # The exact shape init_run records (Config.to_dict) is a well-formed resolved config.
    assert resolved_config_shape_error(Config.from_dict({}).to_dict()) is None
    assert resolved_config_shape_error(
        Config.from_dict({"final_review": False, "reproduce_gate": True}).to_dict()) is None


def test_resolved_config_shape_rejects_absent_or_non_object():
    for bad in (None, [], "x", 5):
        assert resolved_config_shape_error(bad) == "is absent or not a JSON object"


def test_resolved_config_shape_rejects_partial_or_extra_top_level_keys():
    # from_dict OVERRIDE semantics accept these (filling defaults); the resolved shape must not.
    assert Config.from_dict({"final_review": False})  # parses fine as an override
    assert (resolved_config_shape_error({"final_review": False})
            == "does not carry exactly the resolved configuration keys")
    extra = Config.from_dict({}).to_dict()
    extra["unexpected"] = 1
    assert (resolved_config_shape_error(extra)
            == "does not carry exactly the resolved configuration keys")


def test_resolved_config_shape_rejects_incomplete_nested_role_keys():
    # A role missing `effort` is accepted by from_dict (deep-merge) but is not a resolved shape.
    for role in ("builder", "critic"):
        data = Config.from_dict({}).to_dict()
        data[role] = {"model": data[role]["model"]}  # drop nested `effort`
        assert (resolved_config_shape_error(data)
                == f"does not carry exactly the resolved {role} role keys")
        data[role] = "not-an-object"
        assert (resolved_config_shape_error(data)
                == f"does not carry exactly the resolved {role} role keys")
