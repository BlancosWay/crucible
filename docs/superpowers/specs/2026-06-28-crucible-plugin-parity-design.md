# Crucible — local-installable plugin parity (design)

Date: 2026-06-28
Status: approved (scope), pre-implementation

## Goal

Make Crucible a directly installable local plugin "like TradingDesk", so a user can
`copilot plugin marketplace add ~/personal/crucible` and run `/crucible` immediately, and
bring its packaging/docs to TradingDesk parity. Update docs, validate with the two-model
review loop, push.

## Context

Crucible is already a near-complete plugin: `.claude-plugin/{plugin.json,marketplace.json}`,
`commands/crucible.md`, `skills/crucible/SKILL.md` + `references/`, `scripts/crucible/`
(deterministic CLI), test suite, `scripts/check.py` gate, and CI. It is a *two-model
adversarial planning + implementation* workflow on Superpowers — it dispatches **Superpowers
reviewers** (plan-document-reviewer / spec-document-reviewer / code-reviewer) on a Critic model.
It needs **no MCP servers and no pinned role agents** (unlike TradingDesk's 12 agents + data
MCPs). The gaps vs TradingDesk are packaging/docs only.

## Parity gaps to close

1. **Per-platform install docs** — `docs/install/{copilot-cli,claude-code,codex}.md`. A→B
   structure (just-run vs develop-from-clone). No data/MCP setup; only "Superpowers must be
   installed". Local install is the primary path; published-marketplace is mentioned as optional.
2. **Legal/policy** — `LICENSE` (MIT, matching plugin.json), `NOTICE` (attribute Superpowers),
   `SECURITY.md` (engineering-tool model: Critic output is untrusted data; never eyeball
   consensus; no `main`/`master` writes without consent; no secrets).
3. **Instruction files** — `CLAUDE.md`, `AGENTS.md`: install + run blurbs per platform; Codex
   has no marketplace, so symlink the skill into `~/.agents/skills/`.
4. **Codex install helper** — `.codex/INSTALL.md` (clone + symlink the `crucible` skill).
5. **Enriched manifests** — add `author{}`/homepage/repository/keywords to `plugin.json`;
   description + owner{} to `marketplace.json`. Versions stay synced (0.1.0).
6. **README Install section** — local marketplace add/install for all three CLIs + links to
   docs/install/*.

## Non-goals

- No MCP servers, no `.mcp.json`, no role agents.
- No change to skill/CLI behavior; relative `PYTHONPATH=scripts python3 -m crucible` stays
  (dev-from-clone), matching TradingDesk.
- No version bump beyond what tests require (manifests already 0.1.0; add CHANGELOG Unreleased
  entry).

## Validation

- `scripts/check.py` (structural + links + pytest + shellcheck) green; new docs pass
  `tests/check_links.py`, `tests/test_docs.py`, `tests/validate_structure.py`.
- Real local install: `copilot plugin marketplace add ~/personal/crucible`, install
  `crucible@crucible-marketplace`, confirm `/crucible` skill+command load.
- Two-model loop: GPT-5.5 xhigh validates each change; Opus 4.8 max adjudicates flags; iterate
  to clean. Then push.

## Risks

- New `.md` workflow examples must keep `PYTHONPATH=scripts` prefix (test_docs) and no
  hardcoded `--max-rounds 5 --file` (test_docs). CHANGELOG required (changelog guard).
- Local install path uses `crucible@crucible-marketplace` (marketplace.json name), not a repo
  slug.
