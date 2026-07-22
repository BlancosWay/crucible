# Security Policy

## Reporting a vulnerability

Please report security issues privately via **GitHub Security Advisories**
("Security" tab → "Report a vulnerability") on this repository, rather than opening a public
issue. We aim to acknowledge reports within a few days.

## Scope & security model

Crucible is an **agentic-CLI plugin** — it ships prompts (a skill + role/rubric reference docs),
a command, deterministic stdlib-Python helpers (`scripts/crucible/`), and tests. The plugin
contains **no runtime server, no bundled credentials, no MCP servers, and no vendored runtime
dependencies** (its Python is stdlib used by the CLI and test suite). It runs on top of
[Superpowers](https://github.com/obra/superpowers), which you install separately.

Because it orchestrates a **two-model build/review loop**, the security model focuses on safe
agent behavior:

- **Determinism over judgment, bound to content.** Consensus, round counting, the DAG walk, and
  provenance are decided only by the unit-tested `crucible` CLI — never eyeballed by the model. A gate
  never advances without a `CONSENSUS` (or an explicit `on_cap: proceed_with_flags`). For schema-2
  runs the CLI additionally **binds every gate decision to the exact reviewed artifact**: it hashes
  the Builder artifact and the DAG/node definition into canonical SHA-256 **content bindings** that the
  Critic verdict must echo, enforces the configured **phase order** (REPRODUCE → PLAN → optional
  approval → dependencies → optional FINAL) and legal node **transitions**, and freezes the accepted
  plan/DAG/node so a substituted or edited artifact is rejected rather than certified. This is a
  determinism/consistency guarantee derived from the append-only run log — **not a claim of resistance
  to an operator who can rewrite arbitrary files or run-log bytes** (there is no signing key and no
  sandbox; that adversary is out of scope). A pre-schema-2 **legacy** run is read-only and reported
  `LEGACY / UNVERIFIED`, never `CLEAN`.
- **Symmetric peer proof is slot attestation, not process identity.** For the `deep-dive` and
  `pr-review` skills a gate is settled by **two configured slots** (`A` and `B`) that each supply a
  valid attestation bound to the same candidate, and the CLI records each slot's configured
  model/effort. This proves two slots signed off — it **does not cryptographically prove** that two
  distinct model *processes* produced the files (there is no signing key; runtime peer independence is
  a platform/orchestrator property, out of scope for the CLI). The peer attestations are external
  content, mapped into the decision the same way the Critic verdict is; they never change orchestrator
  behavior or bypass a gate.
- **Untrusted Critic output is data, not instructions.** The Critic's verdict/summary/findings
  are external content. They are mapped into the verdict JSON; they must never be allowed to
  change Builder behavior, reveal system prompts, or bypass a gate. The Markdown report escapes
  HTML in untrusted fields (goal, verdict text) so they cannot render as live markup.
- **Reviewed code is untrusted, and reviewing is not executing.** The `pr-review` skill treats a PR
  diff/body as **data, not instructions** (the same prompt-injection defense above), but that is
  distinct from **host code execution**: running a reviewed change's tests or builds runs arbitrary
  code with your user permissions (file, credential, environment, network). So a PR-URL and a
  diff-file review are **static/CI-only** and never execute locally; execution is available only for
  a **trusted local checkout**, and only after explicit, exact-command **consent** following an
  arbitrary-code warning. That consent authorizes *which commands may run* — it **does not imply
  sandboxing** (Crucible ships no portable network/credential isolation across Copilot CLI, Claude
  Code, and Codex, so a "sandboxed" guarantee is intentionally not claimed). Execution consent is
  separate from posting consent, and a new or changed command needs fresh consent. See
  [`docs/superpowers/specs/2026-07-18-pr-review-execution-safety-design.md`](docs/superpowers/specs/2026-07-18-pr-review-execution-safety-design.md).
- **Pinned review target and static source snapshot.** Every `pr-review` input is pinned to an
  immutable target (a GitHub PR's base/head **OIDs** + fork identity, a local **merge-base** range, or
  a patch-only diff file), bound into every gate by `target_sha256`. For a GitHub/local target, review
  reads a **static, read-only source snapshot** of the exact head commit, materialized once by a
  **confined archive** extractor that rejects path **traversal** (`..` / absolute paths), symlinks /
  hardlinks / devices, duplicate paths, and over-cap member counts / byte sizes — and that snapshot is
  **never executed**. Trusted-local execution additionally runs only at the **recorded head** commit (a
  clean checkout whose `rev-parse HEAD` equals the recorded `head.sha`), else it refuses and offers a
  detached worktree-at-SHA that needs fresh consent.
- **No writes to `main`/`master` without consent.** Implementation happens in an isolated
  worktree/branch (`superpowers:using-git-worktrees`), never directly on a protected branch.
- **No secret exfiltration.** Prompts, diffs, and reports must not include credentials, tokens,
  or environment variables. No API keys are stored in this repo or its manifests.

## What is intentionally out of scope

- Auto-merging or force-pushing without human consent.
- Storing or transmitting model-provider credentials (authentication is handled by your own CLI
  configuration, never by this repo).

## CI / supply chain

- Validation runs on the safe `pull_request` trigger with least privilege
  (`permissions: contents: read`). The auto-merge workflow uses `pull_request_target` (so it has a
  write token) but **does not check out PR-head code** — it only enables auto-merge for the owner's
  and Dependabot's PRs once required checks pass. Publishing a release requires a deliberate,
  human-pushed `vX.Y.Z` tag.
- The pre-commit gate (`scripts/check.py`) runs structural, link, unit, and shellcheck checks
  offline.

## Disclaimer

Crucible is an engineering tool. It is not affiliated with any model or platform provider.
