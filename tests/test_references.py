from pathlib import Path

REF = Path(__file__).resolve().parents[1] / "skills" / "crucible" / "references"


def test_reference_files_exist():
    for name in ["critic-prompt.md", "builder-prompt.md", "consensus-rubric.md",
                 "dependency-tree.md", "platform-notes.md"]:
        assert (REF / name).exists(), f"missing {name}"


def test_critic_prompt_defines_verdict_schema():
    text = (REF / "critic-prompt.md").read_text()
    assert "REQUEST_CHANGES" in text and "APPROVE" in text
    assert '"severity"' in text
    assert "blocker" in text and "major" in text


def test_consensus_rubric_lists_stop_criteria():
    text = (REF / "consensus-rubric.md").read_text()
    assert "max_rounds" in text
    assert "halt" in text and "proceed_with_flags" in text
    assert "wontfix" in text


def test_dependency_tree_doc_has_schema_keys():
    text = (REF / "dependency-tree.md").read_text()
    for key in ["nodes", "edges", "depends_on", "topological"]:
        assert key in text


def test_critic_treats_input_as_untrusted():
    text = (REF / "critic-prompt.md").read_text()
    assert "data, not instructions" in text


def test_critic_uses_superpowers_code_reviewer_for_code_gates():
    # Model 2 (Critic) must use the superpowers code-reviewer when reviewing code
    # (the IMPLEMENT/FINAL gates), per the design.
    critic = (REF / "critic-prompt.md").read_text()
    platform = (REF / "platform-notes.md").read_text()
    assert "superpowers:code-reviewer" in critic
    assert "superpowers:code-reviewer" in platform


def test_critic_uses_superpowers_plan_reviewer_for_plan_gate():
    # Model 2 (Critic) must use the superpowers plan/spec document reviewers when
    # critiquing the plan + dependency tree (the PLAN gate), per the design.
    critic = (REF / "critic-prompt.md").read_text()
    platform = (REF / "platform-notes.md").read_text()
    skill = (REF.parent / "SKILL.md").read_text()
    for text in (critic, platform, skill):
        assert "plan-document-reviewer" in text
    # the spec reviewer is also referenced for the design spec
    assert "spec-document-reviewer" in critic


def test_skill_dispatches_code_reviewer_at_code_gates():
    skill = (REF.parent / "SKILL.md").read_text()
    assert "superpowers:code-reviewer" in skill


def _copilot_cli_section(text: str) -> str:
    # The "## Copilot CLI (primary)" section, bounded by the next "## " heading.
    assert "## Copilot CLI" in text
    return text.split("## Copilot CLI", 1)[1].split("\n## ", 1)[0]


def test_platform_notes_copilot_surfaces_plan_in_response():
    # In the Copilot CLI, bash-tool output is collapsed, so the orchestrator must surface the
    # approved plan + dependency tree in its RESPONSE at PLAN settlement (not rely on terminal echo).
    section = _copilot_cli_section((REF / "platform-notes.md").read_text()).lower()
    assert "collapsed" in section or "truncated" in section
    assert "show-plan" in section
    assert "response" in section or "reply" in section
    assert "approved plan" in section
    assert "dependency tree" in section or "dag" in section


def test_skill_has_copilot_surface_note():
    skill = (REF.parent / "SKILL.md").read_text().lower()
    assert "copilot cli" in skill
    assert "surface the approved plan" in skill
    assert "show-plan" in skill
