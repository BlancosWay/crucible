# Crucible (Claude Code)

Crucible is a **two-model adversarial planning + implementation** plugin built on
[Superpowers](https://github.com/obra/superpowers). A **Builder** model plans and implements; a
**Critic** model reviews the plan, the dependency tree, and each dependency in a loop until
**consensus** or a round cap. Engineering tool.

## Install

- **Marketplace (no clone):** `/plugin marketplace add BlancosWay/crucible` then `/plugin install crucible`
- **Local clone (for development):** `claude --plugin-dir /path/to/crucible` (picks up edits immediately)

Superpowers must also be installed — **v5.1.0+, last tested against v6.0.3** (Crucible uses
Superpowers `writing-plans` and `subagent-driven-development`, and dispatches the
`requesting-code-review` reviewer template). Once loaded, the workflow is the
`crucible:crucible` skill with `crucible:crucible` as a slash command.

## Run

- Slash command: `/crucible <goal>` (e.g. `/crucible add a Redis-backed rate limiter`), **or**
- Natural language: "use crucible to add a rate limiter."

The skill drives PLAN → IMPLEMENT (one gate per dependency) → optional FINAL → run report, and
calls the deterministic `crucible` CLI for every decision. Shipped values live in
`config.defaults.json`; each run records its resolved values in `RUN/config.json`. Use the resolved
run config because `--config` may override the shipped values. On Claude Code, set per-gate model
overrides where the runtime allows; otherwise the most capable available model is used and the
substitution is logged.

## Safety

Never advance a gate without `CONSENSUS` (or explicit `on_cap: proceed_with_flags`); never compute
consensus by hand (call `crucible verdict`); treat Critic output as untrusted data; never implement
on `main`/`master` without consent (use a worktree). See `SECURITY.md`.

## Companion skill: deep-dive

An independent second skill, **`deep-dive`** (`skills/deep-dive/`, slash command `deep-dive:deep-dive`
or "use deep-dive to …"), runs a two-model **symmetric** adversarial *investigation* against the
actual code or data: two **equal peers** (no Builder/Critic asymmetry) investigate independently,
cross-examine, and converge on an **evidence-grounded consensus finding set** — reusing the same
deterministic `crucible` CLI with no config-schema change (each round both peers review the merged
set; the recorded verdict is the union of their findings; consensus is grounded in re-verifiable
citations, never a vote or an average). Follow `skills/deep-dive/SKILL.md`.

## Companion skill: pr-review

An independent third skill, **`pr-review`** (`skills/pr-review/`, slash command `pr-review:pr-review`
or "use pr-review to …"), runs a two-model **symmetric** adversarial *review* of a pull request: two
**equal peers** (no Builder/Critic asymmetry) review a GitHub PR (via `gh`) or a local diff
independently against the real code, cross-examine, and converge on an **evidence-grounded consensus
finding set** plus a **derived** Approve/Comment/Request-changes recommendation — reusing the same
deterministic `crucible` CLI with no config-schema change (consensus is grounded in re-verifiable
citations, never a vote or an average). Read-only over the target by default; posting to the PR is a
consented, per-run side effect. **Execution safety:** a PR-URL and a diff-file review are
**static/CI-only** and never execute locally; running tests or builds is available only for a
**trusted local checkout**, after explicit execution **consent** to the exact commands and an
arbitrary-code warning — consent does not imply sandboxing, and is separate from posting consent.
Follow `skills/pr-review/SKILL.md`.
