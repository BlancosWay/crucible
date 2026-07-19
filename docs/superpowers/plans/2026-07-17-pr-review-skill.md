# PR-Review Skill — Implementation Plan

**Status:** implemented. **Design spec:** [`../specs/2026-07-17-pr-review-skill-design.md`](../specs/2026-07-17-pr-review-skill-design.md).

Adds `pr-review`, Crucible's third skill — a two-model **symmetric** adversarial PR review, structurally
parallel to `deep-dive`, reusing the **unmodified** `crucible` CLI (no `scripts/crucible/` or
`config.defaults.json` change). Built via Crucible itself (Builder/Peer-A + Critic/Peer-B), one gate
per node.

## Dependency tree (build order)

```
references ──▶ skill-command ──▶ structure-registration
     └─────────────┴───────────▶ docs
```

- **references** — `skills/pr-review/references/{peer-prompt,review-thread,consensus-rubric,platform-notes}.md`
  + `tests/test_pr_review_references.py`. The shared symmetric peer role carries the review lenses
  (correctness, error-handling/silent-failures, tests, type design, comments, guideline compliance,
  reuse/ownership, load-bearing-claim audit, PR-intent, simplification-as-suggestion) + the finding
  schema; the rubric adds the derived Approve/Comment/Request-changes recommendation; platform-notes
  covers peer dispatch, input normalization (gh PR vs local diff), and optional consented posting.
- **skill-command** — `skills/pr-review/SKILL.md` + `commands/pr-review.md` + `tests/test_pr_review_skill.py`.
  The orchestrator drives PLAN → THREAD gates → optional FINAL → Finish, reusing the CLI for every
  decision.
- **structure-registration** — register `pr-review` in `tests/validate_structure.py` `REQUIRED_REFS`
  (additive) + `tests/test_validate_structure_multiskill.py`.
- **docs** — README / install guides / AGENTS / CLAUDE / `.codex/INSTALL.md` / `docs/cli.md` / CHANGELOG,
  this plan doc + the spec status flip, and the additive `tests/test_docs.py` / `tests/test_pr_review_skill.py`
  docs-integration guards.

## Mapping onto the CLI (zero CLI/config change)

| pr-review concept | CLI primitive |
|---|---|
| PR / diff under review | `init-run --goal` |
| Review plan + review graph | PLAN gate + DAG (`load-dag`); nodes = review threads |
| One review thread | `dep:<thread>` gate |
| Both peers' union review | `critic_verdict` = union of both peers' findings; `APPROVE` iff neither has a blocker |
| Cap dispute | CAPPED / PROCEED_WITH_FLAGS → flagged (both positions) |
| Whole-PR review | FINAL gate |
| Findings + recommendation | `crucible report` + assembled findings (run dir) |

The union verdict preserves the `APPROVE` ⇔ no-blocking-finding invariant the CLI validates, so no
`scripts/crucible/` or `config.defaults.json` change is needed. Input normalization and optional PR
posting are skill-level actions (`gh`/`git`), not CLI features.

## Testing

Per-node focused tests + a whole-suite `python -m pytest -q` and `python3 scripts/check.py` at the
FINAL gate. New tests: `test_pr_review_references.py`, `test_pr_review_skill.py`; additive registration
in `validate_structure.py`, `test_validate_structure_multiskill.py`, `test_docs.py`.

## Amendment (2026-07-18): execution trust boundary

A follow-up plan —
[`2026-07-18-pr-review-execution-safety.md`](2026-07-18-pr-review-execution-safety.md) (design:
[`../specs/2026-07-18-pr-review-execution-safety-design.md`](../specs/2026-07-18-pr-review-execution-safety-design.md))
— adds the execution trust boundary this plan's evidence model predates. A PR-URL and a diff-file
review are now **static**/CI-only and never execute the reviewed code locally; running tests or builds
is available only for a **trusted local** checkout, after explicit exact-command **consent** at a
post-PLAN Execution Safety Gate (with an arbitrary-code warning). It changes only instructions, docs,
and tests — no `scripts/crucible/` or `config.defaults.json` change — and keeps execution consent
separate from posting consent.
