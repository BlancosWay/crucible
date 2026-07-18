import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_vs():
    # validate_structure.py must be importable WITHOUT running/exiting (body guarded under
    # __main__ after the refactor), exposing main(), REQUIRED_REFS, and resolve_shared_ref.
    spec = importlib.util.spec_from_file_location("validate_structure",
                                                  ROOT / "tests" / "validate_structure.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_validate_structure_passes_with_three_skills():
    assert _load_vs().main() == 0


def test_validate_registers_deep_dive_required_refs():
    vs = _load_vs()
    assert "deep-dive" in vs.REQUIRED_REFS
    assert set(vs.REQUIRED_REFS["deep-dive"]) == {
        "peer-prompt.md", "consensus-rubric.md", "investigation-thread.md", "platform-notes.md"}


def test_validate_registers_pr_review_required_refs():
    vs = _load_vs()
    assert "pr-review" in vs.REQUIRED_REFS
    assert set(vs.REQUIRED_REFS["pr-review"]) == {
        "peer-prompt.md", "consensus-rubric.md", "review-thread.md", "platform-notes.md"}


def test_validate_keeps_crucible_required_refs():
    # additive-only: crucible's required refs remain registered, unchanged
    vs = _load_vs()
    assert set(vs.REQUIRED_REFS["crucible"]) == {
        "critic-prompt.md", "builder-prompt.md", "consensus-rubric.md",
        "dependency-tree.md", "platform-notes.md"}


def test_shared_doc_refs_still_bind_to_crucible_only():
    # F3 (behavioral, not string-search): README/command bare `references/<x>.md` tokens must still
    # resolve against skills/crucible/references ONLY — not broadened to "any skill". A crucible ref
    # resolves; a deep-dive-only ref does NOT (proving the existing guard was not weakened).
    vs = _load_vs()
    assert vs.resolve_shared_ref("critic-prompt.md") is True
    assert vs.resolve_shared_ref("peer-prompt.md") is False
