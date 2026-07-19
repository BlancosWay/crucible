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

- **Determinism over judgment.** Consensus, round counting, the DAG walk, and provenance are
  decided only by the unit-tested `crucible` CLI — never eyeballed by the model. A gate never
  advances without a `CONSENSUS` (or an explicit `on_cap: proceed_with_flags`).
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
