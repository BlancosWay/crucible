# Opus 4.8 Builder and GPT-5.5 Critic Defaults Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Claude Opus 4.8 max the default Builder and GPT-5.5 xhigh the default Critic for newly initialized Crucible runs.

**Architecture:** Keep `config.defaults.json` as the sole live source of shipped values. Change the two role defaults there, document the user-visible change under Unreleased, and leave runtime code, schemas, live documentation, and historical records untouched.

**Tech Stack:** JSON configuration, Python 3.11+, pytest, Markdown, `scripts/check.py`

## Global Constraints

- Builder default must be exactly `{"model": "claude-opus-4.8", "effort": "max"}`.
- Critic default must be exactly `{"model": "gpt-5.5", "effort": "xhigh"}`.
- `config.defaults.json` remains the only live source that stores these default model identifiers.
- Explicit `--config` overrides and every non-role setting remain unchanged.
- Existing runs retain the values already recorded in their `RUN/config.json`.
- Preserve released changelog entries and dated specifications and plans.
- Do not add model literals to runtime Python, live documentation, or committed tests.

---

### Task 1: Change the shipped default pairing

**Files:**
- Modify: `config.defaults.json:2-3`
- Modify: `CHANGELOG.md:8-10`
- Reference: `docs/superpowers/specs/2026-07-12-opus48-builder-gpt55-critic-defaults-design.md`
- Test: `tests/test_config.py`
- Test: `tests/test_report.py`
- Test: `tests/test_docs.py`

**Interfaces:**
- Consumes: `load_defaults(path: str | Path = DEFAULTS_PATH) -> dict[str, Any]` and `Config.from_dict(data: dict[str, Any]) -> Config` from `scripts/crucible/config.py`.
- Produces: resolved `Config.builder == {"model": "claude-opus-4.8", "effort": "max"}` and `Config.critic == {"model": "gpt-5.5", "effort": "xhigh"}` when no role overrides are supplied.

- [ ] **Step 1: Define the red acceptance probe**

Use this one-off probe instead of committing a hard-coded model assertion, which would duplicate
the authoritative JSON:

```bash
PYTHONPATH=scripts python3 - <<'PY'
from crucible.config import Config

cfg = Config.from_dict({})
assert cfg.builder == {"model": "claude-opus-4.8", "effort": "max"}, cfg.builder
assert cfg.critic == {"model": "gpt-5.5", "effort": "xhigh"}, cfg.critic
PY
```

- [ ] **Step 2: Run the acceptance probe to verify it fails**

Run the command from Step 1.

Expected: exit 1 with an `AssertionError` showing the current Builder value
`{"model": "gpt-5.6-sol", "effort": "max"}`.

- [ ] **Step 3: Update the authoritative defaults**

Replace the role entries at the top of `config.defaults.json` with:

```json
{
  "builder": { "model": "claude-opus-4.8", "effort": "max" },
  "critic": { "model": "gpt-5.5", "effort": "xhigh" },
  "max_rounds_plan": 5,
  "max_rounds_dep": 5,
  "on_cap": "halt",
  "defer_severities": ["minor", "nit"],
  "blocking_severities": ["blocker", "major"],
  "strict_rebuttal": false,
  "final_review": true,
  "human_approval": false,
  "reproduce_gate": false
}
```

- [ ] **Step 4: Add the Unreleased changelog entry**

Change the top of `CHANGELOG.md` to:

```markdown
## [Unreleased]

### Changed
- **The default model roles are Claude Opus 4.8 max for Builder and GPT-5.5 xhigh for Critic.**
  Explicit configuration overrides remain supported, and existing runs keep their resolved
  `RUN/config.json` values.

## [0.14.0] - 2026-07-11
```

Do not edit the v0.13.0 entry or any dated design or plan.

- [ ] **Step 5: Run the acceptance probe to verify it passes**

Run the command from Step 1.

Expected: exit 0 with no output.

- [ ] **Step 6: Run targeted configuration and rendering tests**

Run:

```bash
python3 -m pytest -q tests/test_config.py tests/test_report.py tests/test_docs.py
```

Expected: exit 0 with every selected test passing.

- [ ] **Step 7: Run the full governance suite**

Run:

```bash
python3 scripts/check.py
```

Expected:

```text
PASS  structural
PASS  links
PASS  suite
PASS  shellcheck

All checks passed.
```

- [ ] **Step 8: Inspect the final diff**

Run:

```bash
git diff --check
git diff -- config.defaults.json CHANGELOG.md
git status --short
```

Expected: no whitespace errors; only `config.defaults.json` and `CHANGELOG.md` are modified beyond
the already committed design and plan documents.

- [ ] **Step 9: Commit the implementation**

```bash
git add config.defaults.json CHANGELOG.md
git commit -m "feat: restore Opus builder and GPT critic defaults" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```
