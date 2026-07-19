import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PR = ROOT / "skills" / "pr-review"

RUNTIME_INSTRUCTIONS = {
    PR / "SKILL.md",
    PR / "references" / "peer-prompt.md",
    PR / "references" / "review-thread.md",
    PR / "references" / "platform-notes.md",
    ROOT / "commands" / "pr-review.md",
}
RUNTIME_EXEMPT_REFS = {PR / "references" / "consensus-rubric.md"}

UNSAFE_EXACT_PHRASES = (
    "when a runnable environment exists, run the focused tests",
    "run the test_plan evidence commands",
)
EXECUTION_DIRECTIVE = re.compile(
    r"\b(run|execute|invoke|install|import|launch|retry|fallback)\b"
    r".{0,100}\b(pytest|test runner|build|package manager|dependency|target module|"
    r"interpreter|plugin hook|repository script|generated binary|test_plan)\b"
)
SAFETY_CONTEXT = (
    "consent required",
    "exact approved command",
    "must not execute",
    "never execute locally",
    "execution prohibited",
    "static evidence",
)


def _norm(path: Path) -> str:
    return " ".join(path.read_text().lower().replace("*", "").replace("`", "").split())


def test_runtime_instruction_inventory_is_complete():
    discovered = {
        PR / "SKILL.md",
        ROOT / "commands" / "pr-review.md",
        *set((PR / "references").glob("*.md")),
    } - RUNTIME_EXEMPT_REFS
    assert RUNTIME_INSTRUCTIONS == discovered


def test_runtime_instructions_never_authorize_unconsented_execution():
    for path in RUNTIME_INSTRUCTIONS:
        low = _norm(path)
        for phrase in UNSAFE_EXACT_PHRASES:
            assert phrase not in low, f"{path} retains unsafe instruction {phrase!r}"
        for match in EXECUTION_DIRECTIVE.finditer(low):
            window = low[max(0, match.start() - 180):match.end() + 180]
            assert any(guard in window for guard in SAFETY_CONTEXT), (
                f"{path} has an execution directive without nearby safety context: "
                f"{match.group(0)!r}"
            )
