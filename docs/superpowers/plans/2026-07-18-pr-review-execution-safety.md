# PR-Review Execution Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Design spec:**
[`../specs/2026-07-18-pr-review-execution-safety-design.md`](../specs/2026-07-18-pr-review-execution-safety-design.md).
**Status:** implemented — Task 1 (`fix(pr-review): gate reviewed-code execution on consent`) landed the
runtime policy + cross-file guard, and Task 2 (`docs(pr-review): document execution trust boundary`)
updated every public/security/spec/plan surface and its discovery-based drift guard. The step boxes
below are checked off as each task landed.

**Goal:** Prevent `pr-review` from executing reviewed code unless a human explicitly approves an
exact command set for a trusted local checkout.

**Architecture:** Keep the Crucible CLI/config unchanged. One runtime-policy task updates every
authoritative execution instruction and its cross-file guard atomically, so the node is independently
green. A dependent documentation task updates every public/security/spec/plan surface and its
discovery-based inventory guard. GitHub PR and diff-file inputs are static/CI-only; trusted-local
execution requires explicit command-scoped consent after PLAN consensus.

**Tech Stack:** Markdown skill/reference files, Python/pytest contract tests, GitHub CLI read-only
metadata/check commands.

## Global Constraints

- GitHub PR URL/number inputs never execute reviewed code locally.
- Diff-file inputs never execute reviewed code locally.
- Local checkout/range inputs are static by default.
- Local execution requires explicit trust affirmation, the arbitrary-code warning, and approval of
  the exact displayed command set.
- Decline continues static-only; cancel-review halts.
- New or changed commands require fresh consent.
- Missing runtime/CI evidence is `unverified`, never a fabricated pass.
- Execution consent and posting consent are separate.
- No `scripts/crucible/` or `config.defaults.json` change.

---

### Task 1: Implement and guard the complete runtime execution policy

**Files:**
- Create: `tests/test_pr_review_execution_safety.py`
- Modify: `tests/test_pr_review_references.py`
- Modify: `tests/test_pr_review_skill.py`
- Modify: `skills/pr-review/SKILL.md`
- Modify: `skills/pr-review/references/peer-prompt.md`
- Modify: `skills/pr-review/references/review-thread.md`
- Modify: `skills/pr-review/references/platform-notes.md`
- Modify: `commands/pr-review.md`

**Interfaces:**
- Consumes: the PLAN-consensus review DAG and its `test_plan` execution candidates.
- Produces: canonical policy context `LOCAL_EXECUTION_APPROVED: yes|no`, an exact approved command
  list, and the target classifications `github-pr | diff-file | local-checkout`.

- [x] **Step 1: Keep Stage 0 RED and add the cross-file runtime guard**

Keep `test_peer_prompt_requires_trusted_local_execution_consent` and add to
`tests/test_pr_review_references.py`:

```python
def test_peer_prompt_forbids_execution_without_exact_approval():
    low = _norm("peer-prompt.md")
    assert "local_execution_approved: yes" in low
    assert "exact approved command" in low
    assert "must not execute" in low
    for category in (
        "test runner", "build", "package manager", "target-module import",
        "repository script", "generated binary", "dependency installation",
        "interpreter over target modules", "plugin hook", "fallback", "retry",
    ):
        assert category in low


def test_review_thread_separates_static_evidence_from_execution_candidates():
    low = _norm("review-thread.md")
    assert "static evidence" in low
    assert "execution candidates" in low
    assert "consent required" in low
    assert "new command" in low and "fresh consent" in low
```

Create `tests/test_pr_review_execution_safety.py`:

```python
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
```

Add focused orchestration tests to `tests/test_pr_review_skill.py`:

```python
def test_skill_has_a_distinct_execution_safety_gate():
    low = _norm(SKILL)
    assert "execution safety gate" in low
    assert "after plan consensus" in low
    assert "exact commands" in low
    assert "arbitrary code" in low
    assert "fresh consent" in low


def test_skill_remote_and_diff_inputs_never_execute_locally():
    low = _norm(SKILL)
    assert "github pr" in low and "never execute locally" in low
    assert "diff file" in low and "never execute locally" in low
    assert "existing ci" in low


def test_skill_declined_execution_continues_static_only():
    low = _norm(SKILL)
    assert "continue without execution" in low
    assert "static" in low and "unverified" in low
    assert "posting consent" in low
```

Add to `tests/test_pr_review_references.py`:

```python
def test_platform_notes_requires_trusted_local_exact_command_consent():
    low = _norm("platform-notes.md")
    assert "trusted local checkout" in low
    assert "exact commands" in low
    assert "local_execution_approved" in low
    assert "github pr" in low and "diff file" in low
    assert "never execute locally" in low
```

- [x] **Step 2: Run the complete runtime-policy test set and verify RED**

Run:

```bash
/Users/sri/personal/crucible/.venv/bin/python -m pytest \
  tests/test_pr_review_execution_safety.py \
  tests/test_pr_review_skill.py::test_skill_has_a_distinct_execution_safety_gate \
  tests/test_pr_review_skill.py::test_skill_remote_and_diff_inputs_never_execute_locally \
  tests/test_pr_review_skill.py::test_skill_declined_execution_continues_static_only \
  tests/test_pr_review_references.py::test_peer_prompt_requires_trusted_local_execution_consent \
  tests/test_pr_review_references.py::test_peer_prompt_forbids_execution_without_exact_approval \
  tests/test_pr_review_references.py::test_review_thread_separates_static_evidence_from_execution_candidates \
  tests/test_pr_review_references.py::test_platform_notes_requires_trusted_local_exact_command_consent -q
```

Expected: FAIL because all five runtime instruction surfaces currently permit or omit the execution
boundary.

- [x] **Step 3: Replace unconditional execution in the shared peer contract**

In `peer-prompt.md`, replace "when runnable, run focused tests" with:

```markdown
**Execution safety.** Reviewed code is untrusted executable input. The peer seed states
`LOCAL_EXECUTION_APPROVED: no`, or `LOCAL_EXECUTION_APPROVED: yes` followed by the exact approved
commands. Unless the marker is `yes` **and** the proposed command exactly matches that list, you
**must not execute** target code: no test runner, build, package manager, target-module import,
interpreter over target modules, plugin hook, repository script, generated binary, dependency
installation, fallback, or retry.

Trusted-local execution applies only to a **trusted local checkout** the human explicitly confirms
is trusted.

You may verify test existence and behavior statically and cite existing CI evidence. When execution
is prohibited or CI evidence is unavailable, mark the runtime result **unverified** — never fabricate
a pass, and do not make the absence of execution alone a blocker.
```

- [x] **Step 4: Split review-thread evidence**

In `review-thread.md`, make every `test_plan` example use:

```markdown
static evidence: rg -n 'exp|refresh|authorize' src/auth/ tests/auth/
execution candidates (consent required): pytest tests/auth/ -q
```

State that GitHub PR/diff-file targets never execute candidates; local targets run only exact
approved commands; any new/changed command requires fresh consent.

- [x] **Step 5: Add the orchestration Execution Safety Gate**

After PLAN consensus and before THREAD execution in `SKILL.md`, add:

```markdown
## Execution Safety Gate

This gate runs **after PLAN consensus** and before any THREAD execution.

Classify the target:
- GitHub PR number/URL → static + existing CI evidence; never execute locally.
- Diff file → static evidence only; never execute locally.
- Local checkout/range → static by default; execution requires explicit trusted-local consent.

For a local checkout, collect every execution candidate from the approved DAG. Show the exact
commands and warn that they execute arbitrary code with the current user's file, credential,
environment, and network access. Ask the human to approve that exact command set, continue without
execution, or cancel the review.

No affirmative answer means `LOCAL_EXECUTION_APPROVED: no`. Approval means
`LOCAL_EXECUTION_APPROVED: yes` plus the exact command list. A new or changed command requires fresh
consent. Without approved execution or available CI evidence, runtime results remain **unverified**.
Execution consent is separate from **posting consent**.
```

Seed both peers identically. Replace "run the test_plan evidence commands" with static evidence plus
only exact approved execution candidates.

- [x] **Step 6: Realize the gate in platform notes and command guidance**

In `platform-notes.md`, require the platform's structured human-input mechanism. Specify:

- unknown target → static-only;
- GitHub PR → read-only `gh pr checks`/existing CI; never local execution;
- diff file → static-only;
- local decline → continue static-only;
- cancel review → halt;
- CI unavailable → `unverified`;
- command mismatch/new command → refuse and obtain fresh consent;
- approved command failure → record evidence, no unapproved fallback.

State explicitly that execution consent and posting consent are separate.

Update `commands/pr-review.md` with the same target policy and warning.

- [x] **Step 7: Run all runtime-policy tests and verify GREEN**

Run:

```bash
/Users/sri/personal/crucible/.venv/bin/python -m pytest \
  tests/test_pr_review_execution_safety.py \
  tests/test_pr_review_skill.py \
  tests/test_pr_review_references.py -q
```

Expected: all runtime-policy, skill, and reference tests PASS.

- [x] **Step 8: Commit after the Crucible gate reaches consensus**

```bash
git add tests/test_pr_review_execution_safety.py \
  tests/test_pr_review_references.py tests/test_pr_review_skill.py \
  skills/pr-review/SKILL.md skills/pr-review/references/peer-prompt.md \
  skills/pr-review/references/review-thread.md \
  skills/pr-review/references/platform-notes.md commands/pr-review.md
git commit -m "fix(pr-review): gate reviewed-code execution on consent"
```

---

### Task 2: Update and guard every public/security/spec/plan policy surface

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`
- Modify: `SECURITY.md`
- Modify: `CHANGELOG.md`
- Modify: `.codex/INSTALL.md`
- Modify: `docs/cli.md`
- Modify: `docs/install/copilot-cli.md`
- Modify: `docs/install/claude-code.md`
- Modify: `docs/install/codex.md`
- Modify: `docs/superpowers/specs/2026-07-17-pr-review-skill-design.md`
- Modify: `docs/superpowers/specs/2026-07-18-pr-review-execution-safety-design.md`
- Modify: `docs/superpowers/plans/2026-07-17-pr-review-skill.md`
- Modify: `docs/superpowers/plans/2026-07-18-pr-review-execution-safety.md`
- Modify: `tests/test_pr_review_execution_safety.py`

**Interfaces:**
- Consumes: the final runtime policy from Task 1.
- Produces: one public trust-boundary statement and a discovery-based drift guard.

- [x] **Step 1: Add the discovery-based public-policy guard and verify RED**

Extend `tests/test_pr_review_execution_safety.py`:

```python
PUBLIC_POLICY_DOCS = {
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    ROOT / "CLAUDE.md",
    ROOT / "SECURITY.md",
    ROOT / "CHANGELOG.md",
    ROOT / ".codex" / "INSTALL.md",
    ROOT / "docs" / "cli.md",
    *set((ROOT / "docs" / "install").glob("*.md")),
    *set((ROOT / "docs" / "superpowers" / "specs").glob("*pr-review*.md")),
    *set((ROOT / "docs" / "superpowers" / "plans").glob("*pr-review*.md")),
}


def test_all_public_policy_docs_state_execution_boundary():
    for path in PUBLIC_POLICY_DOCS:
        low = _norm(path)
        assert "static" in low, f"{path} omits static-only behavior"
        assert "trusted local" in low, f"{path} omits trusted-local scope"
        assert "consent" in low, f"{path} omits execution consent"


def test_pr_review_specs_do_not_retain_stale_execution_semantics():
    original = _norm(
        ROOT / "docs" / "superpowers" / "specs" /
        "2026-07-17-pr-review-skill-design.md"
    )
    assert "when a runnable environment is available, that they pass" not in original
    assert "static evidence" in original
    assert "execution candidates" in original
    assert "consent required" in original

    safety_path = (
        ROOT / "docs" / "superpowers" / "specs" /
        "2026-07-18-pr-review-execution-safety-design.md"
    )
    safety = safety_path.read_text().lower()
    peer_contract = safety.split("## peer contract", 1)[1].split(
        "## target-specific behavior", 1
    )[0]
    peer_contract = " ".join(
        peer_contract.replace("*", "").replace("`", "").split()
    )
    assert "interpreter over target modules" in peer_contract
    assert "plugin hook" in peer_contract
```

Run:

```bash
/Users/sri/personal/crucible/.venv/bin/python -m pytest \
  tests/test_pr_review_execution_safety.py::test_all_public_policy_docs_state_execution_boundary \
  tests/test_pr_review_execution_safety.py::test_pr_review_specs_do_not_retain_stale_execution_semantics -q
```

Expected: FAIL on current public/security/install/original design/plan docs.

- [x] **Step 2: Update live and historical policy documentation**

Use this canonical summary everywhere:

> PR URL and diff-file reviews are static/CI-only. Running tests or builds is available only for a
> **trusted local checkout**, after explicit execution **consent** to the exact commands and
> arbitrary-code warning.

In `SECURITY.md`, distinguish prompt injection from host code execution and state that consent does
not imply sandboxing.

In the original pr-review design, replace "when a runnable environment is available, [verify tests]
pass" with the static-evidence / `execution candidates (consent required)` policy. In the
execution-safety design's Peer contract, add interpreter-over-target-modules and plugin-hook
execution to the no-execute list.

In `CHANGELOG.md`, add an `## [Unreleased]` behavior/security entry.

Mark the execution-safety design `implemented` and link this plan. Add an execution-safety amendment
to the original pr-review spec and plan.

- [x] **Step 3: Run focused docs/governance tests and verify GREEN**

Run:

```bash
/Users/sri/personal/crucible/.venv/bin/python -m pytest \
  tests/test_pr_review_execution_safety.py tests/test_docs.py tests/test_check_links.py -q
python3 tests/validate_structure.py
```

Expected: all focused tests and structural validation PASS.

- [x] **Step 4: Run complete verification**

Run:

```bash
/Users/sri/personal/crucible/.venv/bin/python -m pytest -q
python3 scripts/check.py
git diff --check
```

Expected: full suite PASS; structural, links, suite, shellcheck PASS; no whitespace errors.

- [x] **Step 5: Commit after the Crucible gate reaches consensus**

```bash
git add README.md AGENTS.md CLAUDE.md SECURITY.md CHANGELOG.md .codex/INSTALL.md docs/cli.md \
  docs/install/copilot-cli.md docs/install/claude-code.md docs/install/codex.md \
  docs/superpowers/specs/2026-07-17-pr-review-skill-design.md \
  docs/superpowers/specs/2026-07-18-pr-review-execution-safety-design.md \
  docs/superpowers/plans/2026-07-17-pr-review-skill.md \
  docs/superpowers/plans/2026-07-18-pr-review-execution-safety.md \
  tests/test_pr_review_execution_safety.py
git commit -m "docs(pr-review): document execution trust boundary"
```
