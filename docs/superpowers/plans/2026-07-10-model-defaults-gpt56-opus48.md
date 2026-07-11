# GPT-5.6 Sol Builder and Opus 4.8 Critic Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make GPT-5.6 Sol max Crucible's default Builder and Claude Opus 4.8 max its default Critic.

**Architecture:** Change the two values in the existing `DEFAULTS` configuration object without
altering its schema or merge behavior. Keep the change atomic by updating the assertions, example
configuration, current-facing guidance, platform dispatch note, and Unreleased changelog entry in
the same implementation task.

**Tech Stack:** Python 3.11+, pytest, JSON, Markdown, Crucible's `scripts/check.py` governance suite.

## Global Constraints

- Builder default: model `gpt-5.6-sol`, effort `max`.
- Critic default: model `claude-opus-4.8`, effort `max`.
- Explicit `--config` overrides and configuration structure remain unchanged.
- Preserve older changelog entries and dated design or implementation documents.
- Keep the existing README example that explicitly overrides the Critic to `gpt-5.5` at effort
  `high`; it demonstrates override behavior rather than stating a default.

---

### Task 1: Swap the default model pairing

**Files:**
- Modify: `scripts/crucible/config.py:10-12`
- Modify: `tests/test_config.py:8-11,121-128,147-154`
- Modify: `tests/test_report.py:30-35`
- Modify: `config.example.json:1-4`
- Modify: `README.md:33-41`
- Modify: `AGENTS.md:22-25`
- Modify: `CLAUDE.md:23-26`
- Modify: `commands/crucible.md:11-15`
- Modify: `docs/install/copilot-cli.md:45-52`
- Modify: `skills/crucible/references/platform-notes.md:10-19`
- Modify: `CHANGELOG.md:8-10`

**Interfaces:**
- Consumes: `Config.from_dict(data: dict[str, Any]) -> Config` and the existing `DEFAULTS` mapping.
- Produces: The same `Config` API and serialized run configuration, with new Builder and Critic
  default values.

- [ ] **Step 1: Change the existing assertions to specify the new defaults**

Update the relevant assertions in `tests/test_config.py`:

```python
def test_defaults_match_spec():
    cfg = Config.from_dict({})
    assert cfg.builder == {"model": "gpt-5.6-sol", "effort": "max"}
    assert cfg.critic == {"model": "claude-opus-4.8", "effort": "max"}


def test_partial_critic_override_keeps_default_effort():
    cfg = Config.from_dict({"critic": {"model": "gpt-x"}})
    assert cfg.critic == {"model": "gpt-x", "effort": "max"}


def test_critic_effort_only_override_keeps_default_model():
    cfg = Config.from_dict({"critic": {"effort": "high"}})
    assert cfg.critic == {"model": "claude-opus-4.8", "effort": "high"}


def test_config_empty_nested_object_keeps_defaults():
    cfg = Config.from_dict({"builder": {}})
    assert cfg.builder == {"model": "gpt-5.6-sol", "effort": "max"}


def test_config_null_nested_override_keeps_defaults():
    cfg = Config.from_dict({"builder": None})
    assert cfg.builder == {"model": "gpt-5.6-sol", "effort": "max"}
```

Update `tests/test_report.py`:

```python
def test_report_includes_goal_and_config(tmp_path):
    run = _build_run(tmp_path)
    md = render_markdown(run)
    assert "Add rate limiter" in md
    assert "gpt-5.6-sol" in md
    assert "claude-opus-4.8" in md
```

- [ ] **Step 2: Run the focused tests and confirm they fail against the old defaults**

Run:

```bash
python3 -m pytest -q tests/test_config.py tests/test_report.py
```

Expected: FAIL because `Config.from_dict({})` and generated reports still use the previous model
pairing and Critic effort.

- [ ] **Step 3: Change the executable defaults**

Replace the model entries in `scripts/crucible/config.py`:

```python
DEFAULTS: dict[str, Any] = {
    "builder": {"model": "gpt-5.6-sol", "effort": "max"},
    "critic": {"model": "claude-opus-4.8", "effort": "max"},
    "max_rounds_plan": 5,
    "max_rounds_dep": 5,
    "on_cap": "halt",
    "defer_severities": ["minor", "nit"],
    "blocking_severities": ["blocker", "major"],
    "strict_rebuttal": False,
    "final_review": True,
    "human_approval": False,
    "reproduce_gate": False,
}
```

- [ ] **Step 4: Update current-facing examples and guidance**

Set the same JSON values in `config.example.json` and the README configuration table:

```json
"builder": { "model": "gpt-5.6-sol", "effort": "max" },
"critic": { "model": "claude-opus-4.8", "effort": "max" }
```

Use this wording in `AGENTS.md`, `CLAUDE.md`, `commands/crucible.md`, and
`docs/install/copilot-cli.md`, preserving each file's surrounding platform-specific text:

```text
Defaults: Builder = GPT-5.6 Sol (max), Critic = Opus 4.8 (max), 5 rounds per gate
```

Update the Copilot CLI Critic dispatch defaults in
`skills/crucible/references/platform-notes.md`:

```text
model = the critic model id (default `claude-opus-4.8`) and
reasoning_effort = the critic effort (default `max`)
```

Add this entry under `## [Unreleased]` in `CHANGELOG.md`:

```markdown
### Changed
- **The default model roles are now GPT-5.6 Sol max for Builder and Claude Opus 4.8 max for
  Critic.** Configuration overrides remain supported, so existing explicit model selections keep
  their behavior.
```

- [ ] **Step 5: Run the focused tests and confirm the new defaults pass**

Run:

```bash
python3 -m pytest -q tests/test_config.py tests/test_report.py
```

Expected: PASS.

- [ ] **Step 6: Reconcile stale default wording in current-facing files**

Run:

```bash
rg -n 'xhigh|Builder = Opus 4\.8|Critic = GPT-5\.5|default `gpt-5\.5`|"builder": \{ ?"model": "claude-opus-4\.8"|\| `builder` \| `\{"model": "claude-opus-4\.8"|cfg\.builder == \{"model": "claude-opus-4\.8"|cfg\.critic == \{"model": "gpt-5\.5"' \
  README.md AGENTS.md CLAUDE.md config.example.json commands docs/install \
  skills/crucible scripts tests
```

Expected: no matches. Do not alter the explicit GPT-5.5 override example in `README.md`, older
changelog entries, or dated design and implementation documents.

- [ ] **Step 7: Run the complete governance suite**

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

- [ ] **Step 8: Commit the implementation**

```bash
git add scripts/crucible/config.py tests/test_config.py tests/test_report.py \
  config.example.json README.md AGENTS.md CLAUDE.md commands/crucible.md \
  docs/install/copilot-cli.md skills/crucible/references/platform-notes.md CHANGELOG.md
git commit -m "feat: swap default Crucible models" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```
