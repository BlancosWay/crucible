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


def test_dependency_tree_doc_requires_per_node_docs():
    # Each node must own the documentation + CHANGELOG updates for its own deliverable.
    low = (REF / "dependency-tree.md").read_text().lower()
    assert "changelog" in low
    assert "documentation" in low or "docs" in low


def test_builder_prompt_requires_per_node_docs():
    low = (REF / "builder-prompt.md").read_text().lower()
    assert "changelog" in low
    assert "documentation" in low or "docs" in low


def test_critic_prompt_flags_missing_node_docs_at_both_gates():
    # The docs/CHANGELOG-ownership rule must be enforced at BOTH the PLAN gate
    # (Plan / dependency tree) AND the IMPLEMENT/FINAL gate (Dependency diff), so a node
    # whose diff omits its docs is caught at code review — not only at planning.
    text = (REF / "critic-prompt.md").read_text()
    assert "## What to attack" in text
    attack = text.split("## What to attack", 1)[1].split("\n## ", 1)[0]
    assert "Plan / dependency tree:" in attack and "Dependency diff:" in attack
    plan_part, diff_part = attack.split("Dependency diff:", 1)
    for part in (plan_part, diff_part):
        low = part.lower()
        assert "changelog" in low, "missing CHANGELOG rule in a critic attack bullet"
        assert "documentation" in low or "docs" in low, "missing docs rule in a critic attack bullet"
