# Changelog

All notable changes to Crucible are documented here.
The format is loosely based on [Keep a Changelog](https://keepachangelog.com/).
Crucible follows [Semantic Versioning](https://semver.org/). See
[RELEASING.md](RELEASING.md) for how releases are cut.

## [Unreleased]

## [0.1.0] - 2026-06-22

Initial release: a two-model adversarial planning + implementation workflow on top of
[Superpowers](https://github.com/obra/superpowers).

### Added
- **Two-model adversarial loop.** A **Builder** model (default Opus 4.8) plans and implements; a
  **Critic** model (default GPT-5.5 xhigh) adversarially reviews the plan, the dependency tree, and
  each dependency. The Critic is realized as a **superpowers reviewer** on the critic model at
  every gate: the writing-plans **plan-document-reviewer** (+ brainstorming **spec-document-reviewer**)
  at the PLAN gate, and the **code-reviewer** agent at the IMPLEMENT/FINAL gates. Each gate loops
  until **consensus** (Critic `APPROVE`) or a
  configured per-gate round cap, after which an `on_cap` policy (`halt` / `proceed_with_flags`) applies.
- **Deterministic Python core** (`scripts/crucible/`): `config` (models, caps, policies), `dag`
  (cycle-detection, topological order, ready-set, status), `verdict` (consensus decision +
  resolutions/rebuttals), `runlog` (append-only provenance with full raw text), `report`
  (deterministic Markdown/HTML), and a thin `cli` exposing them.
- **Superpowers orchestrator** (`skills/crucible/SKILL.md` + `references/`) that drives the loop and
  calls the CLI for every deterministic decision, plus a `/crucible` command and plugin manifests.
- **Governance & CI.** Structural validation, Markdown-link, ShellCheck, pytest, changelog, and
  release-dry-run gates; a tag-driven release workflow; owner/Dependabot squash auto-merge;
  Dependabot for Actions; CODEOWNERS; PR/issue templates; a `scripts/check.py` unified local check
  wired as a `.githooks/pre-commit` hook; and a version⇄CHANGELOG consistency guard.
