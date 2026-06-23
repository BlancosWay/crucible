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
