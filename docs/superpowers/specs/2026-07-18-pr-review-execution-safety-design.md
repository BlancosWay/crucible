# PR-Review Execution Safety — Design

**Status:** proposed. **Type:** security behavior change for the `pr-review` skill.

## Problem

`pr-review` correctly treats a reviewed PR as untrusted text, but its current evidence workflow also
instructs both peers to run focused tests whenever an environment is available:

- `skills/pr-review/references/peer-prompt.md` says to run focused tests;
- `skills/pr-review/SKILL.md` says to run each thread's `test_plan`;
- `skills/pr-review/references/review-thread.md` uses `pytest` commands in its examples.

Test discovery is code execution. Pytest imports `conftest.py`, test modules, plugins, and application
modules during collection; equivalent hooks exist in other build/test ecosystems. An external PR can
therefore execute arbitrary code with the agent's filesystem, credential, and network access before
the human has accepted that trust boundary.

The existing "read-only" and consent rules govern intentional repository/PR writes (especially
posting the review), not execution. They do not isolate or authorize running target code.

## Goal

Make local execution **prohibited by default** and require a distinct, explicit trust/consent gate
before `pr-review` executes any reviewed code.

The policy is:

1. GitHub PR URL/number reviews are **static/CI-only** and never execute the PR locally.
2. Diff-file reviews are **static-only** and never execute the diff locally.
3. A local checkout/range may execute code only after the human:
   - explicitly affirms that the checkout is trusted;
   - sees the exact proposed commands and the arbitrary-code warning; and
   - explicitly approves that command set for this run.
4. Declining or cancelling execution consent does not abort the review. The peers continue with
   static/CI evidence and label runtime results `unverified`.
5. Consent is exact-command scoped. A command not shown in the approved set requires fresh consent.

No Crucible CLI or config-schema change is required. This is the trust policy of the instruction-based
`pr-review` skill.

## Evidence classes

The review distinguishes three evidence classes.

### 1. Static evidence — always allowed

Peers may:

- read changed files and surrounding source;
- search symbols and call sites;
- inspect tests without importing or running them;
- inspect the patch and repository metadata;
- verify that a claimed test exists;
- inspect configuration and workflow files.

Static evidence must not invoke a test runner, build system, package manager, interpreter over target
modules, repository script, generated executable, or plugin hook.

### 2. Existing CI evidence — allowed for GitHub PRs

For GitHub PR inputs, peers may consume existing GitHub check status and already-produced CI evidence
through read-only `gh` commands. CI results may be cited as externally observed evidence, but their
absence or inaccessibility is `unverified`, never a fabricated pass.

Fetching metadata or logs is not permission to execute the PR locally.

### 3. Trusted-local execution — explicit consent required

Only a local checkout/range can enter this path. The skill must not infer trust from:

- repository ownership;
- same-repository versus fork status;
- PR author;
- branch name;
- the fact that the checkout already exists.

The human must affirm trust explicitly.

## Execution Safety Gate

The gate occurs after PLAN consensus, when the approved review DAG exposes the complete initial set
of executable `test_plan` commands, and before any THREAD gate executes target code.

The skill presents:

1. target classification (`github-pr`, `diff-file`, or `local-checkout`);
2. the exact commands proposed for execution;
3. this warning:

   > These commands execute code from the reviewed checkout with your current user permissions.
   > They may access files, credentials, environment variables, and the network.

For GitHub PR and diff-file targets, the result is deterministically **execution prohibited**; no
approval question is offered.

For a local checkout, the human chooses:

- **Approve trusted-local execution** — the displayed command set may run;
- **Continue without execution** — static/CI-only review;
- **Cancel review** — halt.

Silence, decline, malformed input, tool cancellation, or an ambiguous answer is not approval.

## Command-set integrity

Consent covers only the commands displayed at the gate.

- Both peers receive the same execution-policy context and approved command list.
- A command must match an approved command exactly before it runs.
- A command proposed after the gate requires a new warning and approval.
- No fallback, retry variant, package installation, setup script, or "equivalent" command is implied
  by approval of another command.
- A failed approved command is evidence. It does not authorize an unapproved fallback.

The review DAG's `test_plan` remains a string for compatibility, but its instructions separate:

- **static evidence commands**; and
- **execution candidates (consent required)**.

## Peer contract

`peer-prompt.md` must no longer say "when runnable, run the focused tests."

Instead, every peer seed carries one of:

- `LOCAL_EXECUTION_APPROVED: no`; or
- `LOCAL_EXECUTION_APPROVED: yes` plus the exact approved command list.

Without the affirmative marker and exact command match, the peer must not:

- run tests or builds;
- import target modules;
- invoke package managers or repository scripts;
- execute binaries produced by the target;
- install target dependencies.

The peer may still verify test existence statically and cite existing CI results. Missing runtime
evidence is labelled `unverified` and calibrated by impact; it is not automatically a blocker merely
because the human declined execution.

## Target-specific behavior

| Target | Static review | Existing CI evidence | Local execution |
|---|---|---|---|
| GitHub PR number/URL | yes | yes, when available | never |
| Diff file | yes | user-supplied only | never |
| Trusted local checkout/range | yes | user-supplied or linked CI | only after explicit command-scoped consent |

An unknown or ambiguous target type uses the safest row: static-only, no local execution.

## Error handling

- CI unavailable or inaccessible: mark runtime status `unverified`; continue.
- Consent declined/cancelled: continue static-only unless the human cancels the whole review.
- Proposed command not in the approved list: do not run it; fresh consent is required.
- Approved command fails: record the observed failure; do not run an unapproved fallback.
- Peer requests execution without approval context: treat it as a workflow violation and refuse.

## Documentation and compatibility

Update:

- `skills/pr-review/SKILL.md`;
- `skills/pr-review/references/peer-prompt.md`;
- `skills/pr-review/references/review-thread.md`;
- `skills/pr-review/references/platform-notes.md`;
- `commands/pr-review.md`;
- the original `2026-07-17-pr-review-skill-design.md`;
- the companion implementation plan;
- README/install guidance where the read-only/static behavior is described;
- `CHANGELOG.md` under `## [Unreleased]`.

The existing posting-consent gate remains separate. Approving test execution never approves posting,
and approving posting never approves code execution.

## Tests

Add focused contract tests that:

- require static/CI-only behavior for GitHub PR and diff-file inputs;
- require explicit trust affirmation and exact-command consent for local execution;
- require the arbitrary-code warning;
- require fresh consent for new commands;
- require declined consent to continue static-only with `unverified` runtime evidence;
- prohibit the old unconditional "when runnable, run focused tests" wording;
- prohibit peer execution without `LOCAL_EXECUTION_APPROVED: yes`;
- keep execution consent distinct from posting consent.

The tests also scan all live `pr-review` instructions so no secondary document reintroduces an
unconditional test/build command.

## Alternatives considered

### Static-only for every target

Safest and simplest, but prevents users from independently running tests on a checkout they already
control and trust.

### Mandatory sandbox

Strongest technical isolation, but Crucible supports Copilot CLI, Claude Code, and Codex without a
portable sandbox capability or a deterministic way to verify network/credential isolation. Claiming
"sandboxed" without a cross-platform enforcement mechanism would recreate the same false safety
boundary.

### Same-repository branches treated as trusted

Rejected. Repository location and author identity do not establish code trust; compromised accounts,
dependency hooks, generated files, and malicious commits remain executable.
