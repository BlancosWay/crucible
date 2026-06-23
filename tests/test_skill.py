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
