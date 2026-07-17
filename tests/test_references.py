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


def test_critic_prompt_requires_declared_test_existence():
    # A node-declared test that was never written must be a blocker (existence is grep-checkable
    # without a runnable env), not merely "unverified".
    text = (REF / "critic-prompt.md").read_text()
    assert "existence" in text
    assert "declared-but-absent" in text


def test_builder_prompt_requires_completeness_reconciliation():
    # A universal/completeness claim must be reconciled against a fresh tool run, not asserted
    # from memory — closes the enumeration-miss class.
    text = (REF / "builder-prompt.md").read_text()
    assert "completeness" in text.lower()
    assert "reconcile" in text.lower()


def test_builder_prompt_enforces_human_style_comments():
    # Human-style code comments: no safeguard genre-label preambles, and cross-version/compatibility
    # justification is redirected out of the source — closes the "Defense-in-depth:" essay-comment class.
    low = " ".join((REF / "builder-prompt.md").read_text().lower().split())
    # bans safeguard genre-label preambles (the standard TODO/FIXME/NOTE/HACK tags stay allowed)
    assert "write for the next maintainer" in low
    assert "not the reviewer" in low
    assert "defense-in-depth" in low
    assert "belt-and-suspenders" in low
    assert "for safety" in low
    # the standard tags are explicitly preserved (guards the F1 carve-out against regression)
    assert "tags below are fine" in low
    # redirects the load-bearing / compatibility justification to the commit / PR / plan, not the source
    assert "commit message" in low
    assert "not its proof" in low
    # long inline explanations are nudged the same way
    assert "that's usually justification" in low


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


def _no_live_code_reviewer_dispatch(text: str) -> None:
    # The `superpowers:code-reviewer` NAMED agent was removed upstream in superpowers v5.1.0; it
    # must never appear as a live dispatch target. It may still be named, but only in a note that
    # says it was removed — so every line mentioning it must also contain "removed".
    for line in text.splitlines():
        if "superpowers:code-reviewer" in line:
            assert "removed" in line, f"live superpowers:code-reviewer dispatch reference: {line!r}"


def test_critic_uses_superpowers_code_reviewer_for_code_gates():
    # Model 2 (Critic) reviews code (the IMPLEMENT/FINAL gates) via the superpowers
    # requesting-code-review code-reviewer *template*, dispatched as a general-purpose subagent on
    # the critic model. (The superpowers:code-reviewer named agent was removed upstream in v5.1.0.)
    critic = (REF / "critic-prompt.md").read_text()
    platform = (REF / "platform-notes.md").read_text()
    for text in (critic, platform):
        assert "requesting-code-review" in text
        assert "general-purpose" in text
        _no_live_code_reviewer_dispatch(text)
    # the removed named agent is never a Copilot dispatch target
    assert 'agent_type: "superpowers:code-reviewer"' not in platform


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
    assert "requesting-code-review" in skill
    assert "general-purpose" in skill
    _no_live_code_reviewer_dispatch(skill)


def _copilot_cli_section(text: str) -> str:
    # The "## Copilot CLI (primary)" section, bounded by the next "## " heading.
    assert "## Copilot CLI" in text
    return text.split("## Copilot CLI", 1)[1].split("\n## ", 1)[0]


def test_platform_notes_dispatches_from_resolved_run_config():
    section = _copilot_cli_section((REF / "platform-notes.md").read_text())
    assert "config.json" in section
    assert "critic.model" in section
    assert "critic.effort" in section


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


def test_builder_prompt_requires_grounding_claims():
    # #4: the Builder must ground claims in a tool run this turn, cite concrete evidence, never
    # invent specifics, and label unverified. Whitespace-normalized so line-wrapping can't hide a
    # phrase; asserts the specific guardrails, not vacuous synonyms.
    low = " ".join((REF / "builder-prompt.md").read_text().lower().split())
    assert "tool run this turn" in low            # grounding: evidence from THIS turn
    assert "file:line" in low                      # cite concrete evidence (file:line/observed output)
    assert "invent" in low
    for item in ("flag", "path", "api", "config key"):  # the specific forbidden invented specifics
        assert item in low, f"builder-prompt must forbid inventing a {item}"
    assert "unverified" in low


def test_critic_prompt_verifies_test_evidence():
    # #3: the Critic verifies the Builder's cited test evidence and runs a node's test_plan only
    # on doubt/missing-evidence (conditional), never fabricating a pass — NOT mandatory reruns.
    low = " ".join((REF / "critic-prompt.md").read_text().lower().split())
    assert "test_plan" in low
    assert "evidence" in low
    assert "unverified" in low          # degrade to unverified when it can't run
    assert "fabricate" in low or "never fabricate" in low  # no fabricated pass


def test_critic_prompt_flags_bugfix_without_repro():
    # #7: a behavioral bug-fix plan with no failing reproduction (and no reproduce gate / waiver)
    # is a SOFT, WAIVABLE finding — not an unconditional demand.
    low = " ".join((REF / "critic-prompt.md").read_text().lower().split())
    assert "bug-fix" in low or "bug fix" in low
    assert "reproduc" in low            # failing reproduction / reproduce gate
    assert "waiv" in low                # waivable


def test_platform_notes_copilot_requires_full_untruncated_plan_paste():
    # #F1: platform-notes.md's Copilot 'Surfacing output to the human' bullet must also require the
    # full, untruncated show-plan paste (the other canonical Copilot-surfacing locus).
    section = _copilot_cli_section((REF / "platform-notes.md").read_text())
    assert "show-plan" in section
    assert "in full" in section
    low = section.lower()
    assert "truncate" in low
    assert "tail" in low


def test_builder_prompt_requires_owner_discovery():
    # Existing-owner discipline: before placing new logic the Builder must search for how the
    # codebase already handles that responsibility and prefer reusing/extending the existing owner;
    # a negative owner search is best-effort (unverified), never proof of absence.
    low = " ".join((REF / "builder-prompt.md").read_text().lower().split())
    assert "owner" in low
    assert "reuse" in low or "extend" in low
    assert "proof of absence" in low
    assert "unverified" in low


def test_critic_prompt_reviews_placement_at_both_gates():
    # The existing-owner/placement dimension must be attacked at BOTH the plan and the diff gate,
    # and the diff-side must be scoped so it never re-opens the terminal PLAN placement.
    text = (REF / "critic-prompt.md").read_text()
    assert "## What to attack" in text
    attack = text.split("## What to attack", 1)[1].split("\n## ", 1)[0]
    plan_part, diff_part = attack.split("Dependency diff:", 1)
    assert "owner" in plan_part.lower(), "plan attack bullet must review existing-owner placement"
    assert "owner" in diff_part.lower(), "diff attack bullet must review existing-owner placement"
    # diff-gate placement review is scoped: it must not re-litigate a placement the terminal PLAN blessed
    assert "terminal" in diff_part.lower()
    assert "approved plan" in diff_part.lower()


def test_critic_prompt_calibrates_placement_severity():
    # Placement severity is evidence-calibrated: blocking only when the bypassed owner/convention can
    # be cited in the repo; a placement objection with no cited owner is taste and is never blocking.
    low = " ".join((REF / "critic-prompt.md").read_text().lower().split())
    assert "cite" in low          # "cite in the repo" / "cited owner"
    assert "taste" in low
    assert "never" in low and "blocking" in low


def test_builder_prompt_grounds_analytical_claims():
    # Load-bearing analytical conclusions (compat / determinism-under-replay / no-version-bump /
    # idempotent / ...) are arguments, not facts: the Builder must derive them or tag them
    # `assumption`, and for state that outlives one execution must cover BOTH cross-version
    # directions (the rolling-deploy miss), not just the forward path.
    low = " ".join((REF / "builder-prompt.md").read_text().lower().split())
    assert "analytical" in low
    assert "load-bearing" in low
    assert "assumption" in low
    assert "outlive" in low
    assert "both direction" in low and "old-code over new-state" in low


def test_critic_prompt_audits_load_bearing_claims_at_both_gates():
    # Inside "## What to attack", the Critic must independently re-derive every load-bearing
    # analytical claim (the Builder's conclusion carries no evidentiary weight), enumerate both
    # cross-version directions across deploy/rollback, and calibrate severity so a trivial claim
    # never blocks — otherwise gates never converge.
    text = (REF / "critic-prompt.md").read_text()
    assert "## What to attack" in text
    attack = text.split("## What to attack", 1)[1].split("\n## ", 1)[0]
    low = " ".join(attack.lower().split())
    assert "load-bearing" in low
    assert "re-derive" in low or "first principles" in low
    assert "no evidentiary weight" in low
    assert "both" in low and "deploy and rollback" in low
    assert "never blocking" in low


def test_consensus_rubric_blocks_undischarged_assumptions():
    # An undischarged load-bearing assumption is a non-deferrable (by severity) open blocking finding,
    # and a legitimate wontfix must SUPPLY the derivation. The rubric is honest that prose does not
    # override rebuttal semantics: under the default strict_rebuttal: false a wontfix still clears any
    # finding, so strict_rebuttal: true is named as the deterministic enforcement lever.
    low = " ".join((REF / "consensus-rubric.md").read_text().lower().split())
    assert "load-bearing" in low
    assert "not deferrable" in low
    assert "derived" in low or "derivation" in low
    assert "blocking finding" in low
    assert "strict_rebuttal: false" in low and "still clears any finding" in low
    assert "strict_rebuttal: true" in low
