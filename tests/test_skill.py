import re
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "skills" / "crucible" / "SKILL.md"


def test_skill_exists_with_frontmatter():
    text = SKILL.read_text()
    assert text.startswith("---")
    assert re.search(r"^name:\s*crucible\s*$", text, re.MULTILINE)
    assert re.search(r"^description:\s*.+", text, re.MULTILINE)


def test_skill_references_the_two_roles_and_gates():
    text = SKILL.read_text().lower()
    assert "builder" in text and "critic" in text
    assert "plan gate" in text or "plan stage" in text
    assert "dependency tree" in text


def test_skill_invokes_superpowers_subskills():
    text = SKILL.read_text()
    assert "writing-plans" in text
    assert "subagent-driven-development" in text


def test_skill_uses_cli_for_decisions():
    text = SKILL.read_text()
    for cmd in ["init-run", "load-dag", "next", "verdict", "set-status", "report"]:
        assert cmd in text, f"SKILL.md should reference `crucible {cmd}`"


def test_skill_does_not_hardcode_round_cap_override():
    # the cap must come from config; workflow examples should not pass --max-rounds
    text = SKILL.read_text()
    assert "--max-rounds 5" not in text


def test_skill_step6_requires_full_untruncated_plan_paste():
    # The Copilot-CLI surfacing step must require pasting the show-plan output IN FULL and forbid
    # truncating it (observed failure: piped through `tail`, pasted a partial plan).
    text = SKILL.read_text()
    assert "show-plan" in text
    # scope to the surfacing guidance: the 'in full' concept + a no-truncate/tail mechanism
    assert "in full" in text
    low = text.lower()
    assert "truncate" in low
    assert "tail" in low  # names the concrete truncation mechanism to avoid
