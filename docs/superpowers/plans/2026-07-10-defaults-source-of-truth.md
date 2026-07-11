# Configuration Defaults Source of Truth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `config.defaults.json` the only live source of Crucible's shipped configuration
values so future model-default changes require one functional file edit.

**Architecture:** Rename the full example configuration to `config.defaults.json` and load it from
`scripts/crucible/config.py` using a module-relative path. Keep schema and validation in Python,
derive tests from the loaded mapping, and make orchestration read each run's resolved
`RUN/config.json` while live documentation references the source instead of copying literals.

**Tech Stack:** Python 3.11+, stdlib `json`/`pathlib`, pytest, JSON, Markdown, GitHub Actions.

## Global Constraints

- `config.defaults.json` is the only live source of shipped configuration values.
- JSON remains the format; add no YAML parser or other dependency.
- `config.py` owns schema and validation rules; the JSON file owns values only.
- Existing partial `--config` overrides and run-log formats remain compatible.
- After `init-run`, `RUN/config.json` is authoritative for that run, including overrides.
- Current-facing docs and orchestration files must not repeat shipped model identifiers.
- Historical changelog entries and dated design or implementation documents remain unchanged.
- Missing or invalid shipped defaults must fail explicitly; never fall back to code literals.
- A future model-default change may still add a required changelog entry, but must not edit runtime
  code, tests, examples, commands, or current-facing documentation.

---

### Task 1: Load shipped defaults from JSON

**Files:**
- Rename: `config.example.json` -> `config.defaults.json`
- Modify: `scripts/crucible/config.py:1-143`
- Modify: `tests/test_config.py:1-239`
- Modify: `tests/test_report.py:1-35`
- Modify: `tests/validate_structure.py:57-105`
- Modify: `.github/workflows/validate.yml:27-32`
- Modify: `README.md:33-50,95-108`
- Modify: `AGENTS.md:22-27`
- Modify: `CHANGELOG.md:8-15`

**Interfaces:**
- Produces: `DEFAULTS_PATH: pathlib.Path`.
- Produces: `load_defaults(path: str | pathlib.Path = DEFAULTS_PATH) -> dict[str, Any]`.
- Preserves: `DEFAULTS: dict[str, Any]`, `Config.from_dict`, `Config.to_dict`, and `load_config`.
- Produces for Task 2: `init-run` continues writing the resolved values to `RUN/config.json`.

- [ ] **Step 1: Write failing tests for an external defaults source**

In `tests/test_config.py`, import the new loader symbols:

```python
from crucible.config import Config, DEFAULTS, DEFAULTS_PATH, load_config, load_defaults
```

Replace the existing `test_defaults_match_spec` function (do not add a second function and leave
the literal assertions behind) with:

```python
def test_defaults_match_shipped_file():
    cfg = Config.from_dict({})
    assert cfg.to_dict() == DEFAULTS
    assert DEFAULTS_PATH.name == "config.defaults.json"
    assert load_defaults() == DEFAULTS
```

Add loader failure coverage:

```python
def test_load_defaults_surfaces_missing_and_malformed_files(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_defaults(tmp_path / "missing.json")

    p = tmp_path / "malformed.json"
    p.write_text("{")
    with pytest.raises(json.JSONDecodeError):
        load_defaults(p)


def test_load_defaults_rejects_non_object(tmp_path):
    p = tmp_path / "defaults.json"
    p.write_text("[]")
    with pytest.raises(ValueError, match="JSON object"):
        load_defaults(p)


def test_load_defaults_rejects_missing_or_unknown_keys(tmp_path):
    missing = dict(DEFAULTS)
    missing.pop("on_cap")
    p = tmp_path / "missing.json"
    p.write_text(json.dumps(missing))
    with pytest.raises(ValueError, match="missing default config keys"):
        load_defaults(p)

    extra = {**DEFAULTS, "surprise": True}
    p = tmp_path / "extra.json"
    p.write_text(json.dumps(extra))
    with pytest.raises(ValueError, match="unknown default config keys"):
        load_defaults(p)


def test_load_defaults_rejects_invalid_role_shape(tmp_path):
    data = {**DEFAULTS, "critic": {"model": "x"}}
    p = tmp_path / "role.json"
    p.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="critic default keys"):
        load_defaults(p)
```

Change every assertion about an unchanged shipped value to derive from `DEFAULTS`:

```python
def test_partial_override_keeps_other_defaults():
    cfg = Config.from_dict({"max_rounds_dep": 3, "on_cap": "proceed_with_flags"})
    assert cfg.max_rounds_dep == 3
    assert cfg.on_cap == "proceed_with_flags"
    assert cfg.max_rounds_plan == DEFAULTS["max_rounds_plan"]


def test_partial_builder_override_keeps_default_effort():
    cfg = Config.from_dict({"builder": {"model": "claude-x"}})
    assert cfg.builder == {
        "model": "claude-x",
        "effort": DEFAULTS["builder"]["effort"],
    }


def test_partial_critic_override_keeps_default_effort():
    cfg = Config.from_dict({"critic": {"model": "gpt-x"}})
    assert cfg.critic == {"model": "gpt-x", "effort": DEFAULTS["critic"]["effort"]}


def test_critic_effort_only_override_keeps_default_model():
    cfg = Config.from_dict({"critic": {"effort": "high"}})
    assert cfg.critic == {"model": DEFAULTS["critic"]["model"], "effort": "high"}


def test_config_empty_nested_object_keeps_defaults():
    assert Config.from_dict({"builder": {}}).builder == DEFAULTS["builder"]


def test_config_null_nested_override_keeps_defaults():
    assert Config.from_dict({"builder": None}).builder == DEFAULTS["builder"]


def test_config_partial_nested_override_still_allowed():
    cfg = Config.from_dict({"builder": {"model": "custom-model"}})
    assert cfg.builder["model"] == "custom-model"
    assert cfg.builder["effort"] == DEFAULTS["builder"]["effort"]


def test_empty_defer_severities_allowed():
    cfg = Config.from_dict({"defer_severities": []})
    assert cfg.defer_severities == []
    assert cfg.blocking_severities == DEFAULTS["blocking_severities"]
```

Replace the old `config.example.json` tests with:

```python
def test_defaults_file_is_valid_config():
    assert load_config(DEFAULTS_PATH).to_dict() == DEFAULTS
```

In `tests/test_report.py`, import `DEFAULTS` and derive report expectations:

```python
from crucible.config import Config, DEFAULTS


def test_report_includes_goal_and_config(tmp_path):
    run = _build_run(tmp_path)
    md = render_markdown(run)
    assert "Add rate limiter" in md
    for role in ("builder", "critic"):
        assert DEFAULTS[role]["model"] in md
        assert DEFAULTS[role]["effort"] in md
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
python3 -m pytest -q tests/test_config.py tests/test_report.py
```

Expected: collection fails because `DEFAULTS_PATH` and `load_defaults` do not exist.

- [ ] **Step 3: Rename the configuration file**

Run:

```bash
git mv config.example.json config.defaults.json
```

Keep the JSON content unchanged; it becomes the authoritative values file.

- [ ] **Step 4: Implement the defaults loader and retain existing merge behavior**

At the top of `scripts/crucible/config.py`, replace the literal mapping with:

```python
DEFAULTS_PATH = Path(__file__).resolve().parents[2] / "config.defaults.json"
DEFAULT_CONFIG_KEYS = frozenset({
    "builder",
    "critic",
    "max_rounds_plan",
    "max_rounds_dep",
    "on_cap",
    "defer_severities",
    "blocking_severities",
    "strict_rebuttal",
    "final_review",
    "human_approval",
    "reproduce_gate",
})
ROLE_KEYS = frozenset({"model", "effort"})


def load_defaults(path: str | Path = DEFAULTS_PATH) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("default config must be a JSON object")
    missing = DEFAULT_CONFIG_KEYS - set(data)
    unknown = set(data) - DEFAULT_CONFIG_KEYS
    if missing:
        raise ValueError(f"missing default config keys: {sorted(missing)}")
    if unknown:
        raise ValueError(f"unknown default config keys: {sorted(unknown)}")
    for role in ("builder", "critic"):
        value = data[role]
        if not isinstance(value, dict):
            raise ValueError(f"{role} default must be an object")
        if set(value) != ROLE_KEYS:
            raise ValueError(
                f"{role} default keys must be {sorted(ROLE_KEYS)}, got {sorted(value)}"
            )
    return data


DEFAULTS: dict[str, Any] = load_defaults()
```

Leave `Config.from_dict`, `_validate`, `to_dict`, and `load_config` behavior unchanged.

- [ ] **Step 5: Update structural and CI validation**

In `tests/validate_structure.py`, load `config.defaults.json` with `load_json`, require it to exist,
and replace `config.example.json` in the secret scan with `config.defaults.json`:

```python
defaults = load_json("config.defaults.json")
check(isinstance(defaults, dict), "config.defaults.json must contain a JSON object")

# No secret hardcoded in any manifest or the shipped defaults.
for rel in (".claude-plugin/plugin.json", ".claude-plugin/marketplace.json",
            "config.defaults.json"):
```

In `.github/workflows/validate.yml`, make the JSON loop:

```yaml
for f in .claude-plugin/plugin.json .claude-plugin/marketplace.json config.defaults.json; do
```

- [ ] **Step 6: Update the configuration documentation without value duplication**

In `README.md`, state:

```markdown
Every setting has a shipped default in [`config.defaults.json`](config.defaults.json). Override any
subset via `--config`; `init-run` writes the fully resolved values to `RUN/config.json`.
The defaults file is also a complete valid `--config` template.
```

Replace the configuration table with:

```markdown
| Key | Meaning |
|-----|---------|
| `builder` | Model + effort that plans and implements. Only `model`/`effort` are accepted; an unknown nested key is rejected. |
| `critic` | Model + effort that adversarially reviews every gate. Only `model`/`effort` are accepted; an unknown nested key is rejected. |
| `max_rounds_plan` | Round cap for the PLAN gate before `on_cap` applies. |
| `max_rounds_dep` | Round cap for each IMPLEMENT gate. |
| `on_cap` | At a round cap without consensus: `halt`, or `proceed_with_flags` to advance and record open findings. |
| `blocking_severities` | Severities that keep a gate from reaching consensus. |
| `defer_severities` | Severities the Builder may defer without blocking. |
| `strict_rebuttal` | Whether a Builder `wontfix` rebuttal stays blocking until the Critic accepts it. |
| `final_review` | Whether to run the Stage 3 FINAL gate. |
| `human_approval` | Whether to pause after PLAN consensus before implementation. |
| `reproduce_gate` | Whether bug-fix runs start with the Stage 0 REPRODUCE gate. |
```

Replace the current model-bearing override example with an effort-only partial override:

```json
{"critic": {"effort": "high"}, "on_cap": "proceed_with_flags", "final_review": false}
```

In `AGENTS.md`, replace the complete inline defaults sentence and stale example path with:

```markdown
Shipped values live in `config.defaults.json`; each run records its resolved values in
`RUN/config.json`. Use the resolved run config because `--config` may override the shipped values.
All bookkeeping (DAG walk, round counting, consensus, provenance, report) is decided by the
`crucible` CLI — never eyeballed.
```

Add under `## [Unreleased]` in `CHANGELOG.md`:

```markdown
### Changed
- **Shipped defaults now come from one authoritative `config.defaults.json` file.** Runtime code
  and tests load or derive from it, eliminating synchronized model-value edits across Python,
  examples, and reports.
```

- [ ] **Step 7: Run Task 1 verification**

Run:

```bash
python3 -m pytest -q tests/test_config.py tests/test_report.py tests/test_docs.py
python3 tests/validate_structure.py
```

Expected: all selected tests pass and structural validation prints `PASS`.

- [ ] **Step 8: Commit Task 1**

```bash
git add config.defaults.json scripts/crucible/config.py tests/test_config.py \
  tests/test_report.py tests/validate_structure.py .github/workflows/validate.yml \
  README.md AGENTS.md CHANGELOG.md
git commit -m "refactor: load defaults from JSON" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 2: Remove live default literals from orchestration and docs

**Files:**
- Modify: `skills/crucible/SKILL.md:17-44`
- Modify: `skills/crucible/references/platform-notes.md:1-25`
- Modify: `tests/test_skill.py:14-49`
- Modify: `tests/test_references.py:76-117`
- Modify: `tests/test_docs.py:1-26`
- Modify: `CLAUDE.md:23-26`
- Modify: `commands/crucible.md:11-15`
- Modify: `docs/install/copilot-cli.md:45-52,84-87`
- Modify: `docs/install/claude-code.md:75-78`
- Modify: `docs/install/codex.md:69-72`
- Modify: `CHANGELOG.md:8-18`

**Interfaces:**
- Consumes: Task 1's `config.defaults.json`.
- Consumes: existing `init-run` output file `RUN/config.json`.
- Produces: orchestration contract that dispatches with `critic.model` and `critic.effort` from the
  resolved run config.

- [ ] **Step 1: Write failing orchestration and duplication guards**

Add to `tests/test_skill.py`:

```python
def test_skill_requires_resolved_run_config():
    text = SKILL.read_text()
    assert "RUN/config.json" in text or '"$RUN"/config.json' in text
    assert "authoritative for this run" in text
```

Add to `tests/test_references.py`:

```python
def test_platform_notes_dispatches_from_resolved_run_config():
    section = _copilot_cli_section((REF / "platform-notes.md").read_text())
    assert "config.json" in section
    assert "critic.model" in section
    assert "critic.effort" in section
```

Extend `tests/test_docs.py`:

```python
import json
import re

DEFAULTS = json.loads((ROOT / "config.defaults.json").read_text())
SOURCE_REFERENCE_DOCS = [
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    ROOT / "CLAUDE.md",
    ROOT / "commands" / "crucible.md",
    ROOT / "docs" / "install" / "copilot-cli.md",
    ROOT / "docs" / "install" / "claude-code.md",
    ROOT / "docs" / "install" / "codex.md",
]
RUN_CONFIG_DOCS = [
    ROOT / "skills" / "crucible" / "SKILL.md",
    ROOT / "skills" / "crucible" / "references" / "platform-notes.md",
]
LIVE_DEFAULT_DOCS = [*SOURCE_REFERENCE_DOCS, *RUN_CONFIG_DOCS]
NO_MODEL_LITERAL_FILES = [
    ROOT / "scripts" / "crucible" / "config.py",
    ROOT / "tests" / "test_config.py",
    ROOT / "tests" / "test_report.py",
    *LIVE_DEFAULT_DOCS,
]


def test_live_consumers_do_not_duplicate_default_model_ids():
    for path in NO_MODEL_LITERAL_FILES:
        text = path.read_text()
        for role in ("builder", "critic"):
            model = DEFAULTS[role]["model"]
            assert model not in text, f"{path} duplicates {role} default model {model}"


def test_live_docs_reference_authoritative_configuration():
    for path in SOURCE_REFERENCE_DOCS:
        assert "config.defaults.json" in path.read_text(), f"{path} omits defaults source"

    for path in RUN_CONFIG_DOCS:
        text = path.read_text()
        assert "config.json" in text, f"{path} omits resolved run config"


def test_live_docs_do_not_restate_builder_or_critic_defaults():
    inline_default = re.compile(r"\b(?:Defaults:\s*)?(?:Builder|Critic)\s*=\s*", re.IGNORECASE)
    for path in LIVE_DEFAULT_DOCS:
        assert not inline_default.search(path.read_text()), f"{path} restates role defaults"
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
python3 -m pytest -q tests/test_skill.py tests/test_references.py tests/test_docs.py
```

Expected: failures because the skill does not require `RUN/config.json`, platform notes hardcode the
Critic defaults, live docs omit the authoritative references, and three remaining docs restate
human-readable Builder/Critic defaults.

- [ ] **Step 3: Make the run config authoritative in the skill**

Immediately after the `init-run` example in `skills/crucible/SKILL.md`, add:

```markdown
**Read the resolved run config.** Immediately read `"$RUN"/config.json`.
It is authoritative for this run, including `--config` overrides. Use its `builder` and `critic`
model/effort values for role realization and provenance. Never infer shipped defaults from prose
or hardcode them.
```

- [ ] **Step 4: Dispatch from resolved values in platform notes**

Replace the Copilot PLAN-gate literal defaults with:

```markdown
- **Resolve models first:** read `"$RUN"/config.json`. Dispatch with `model` =
  `critic.model` and `reasoning_effort` = `critic.effort` from that resolved file. Do not read
  shipped defaults from documentation; this run may contain explicit overrides.
- **PLAN gate:** dispatch a `general-purpose` `task` subagent with those resolved Critic values,
  seeded with the `superpowers:writing-plans` **`plan-document-reviewer-prompt.md`** template (and
  the `superpowers:brainstorming` **`spec-document-reviewer-prompt.md`** template for the design
  spec) plus the plan and DAG. Require its result mapped into the `critic-prompt.md` verdict JSON.
```

Keep the code-review gate on the same resolved Critic values and preserve the existing fallback and
substitution logging rules.

- [ ] **Step 5: Replace live literal statements with source references**

Replace the two-line defaults statement in `CLAUDE.md` with:

```markdown
Shipped values live in `config.defaults.json`; each run records its resolved values in
`RUN/config.json`. Use the resolved run config because `--config` may override the shipped values.
```

Preserve the following Claude Code platform-substitution sentence after it.

Replace the defaults clause in `commands/crucible.md` with:

```markdown
Resolve models, effort, caps, and policies from the `RUN/config.json` written by `init-run`; shipped
values live in `config.defaults.json`.
```

Replace the standalone defaults sentence in `docs/install/copilot-cli.md` with:

```markdown
Shipped values live in `config.defaults.json`; `init-run` records the resolved values (including
`--config` overrides) in `RUN/config.json`.
```

In `docs/install/copilot-cli.md`, `docs/install/claude-code.md`, and `docs/install/codex.md`, replace
the stale footer target:

```markdown
[Defaults](../../README.md#defaults)
```

with:

```markdown
[Defaults](../../config.defaults.json)
```

`AGENTS.md` was already migrated with the file rename in Task 1; leave its source-reference wording
unchanged.

Do not edit dated historical specs/plans or older changelog sections.

Add a second Unreleased changelog bullet:

```markdown
- **Orchestration now reads each run's resolved `config.json`.** Model dispatch honors overrides
  without duplicating shipped model identifiers in skills, platform notes, commands, or install
  documentation.
```

- [ ] **Step 6: Run Task 2 verification and a stale-literal reconciliation**

Run:

```bash
python3 -m pytest -q tests/test_skill.py tests/test_references.py tests/test_docs.py
python3 - <<'PY'
import json
from pathlib import Path

root = Path(".")
defaults = json.loads((root / "config.defaults.json").read_text())
paths = [
    root / "README.md",
    root / "AGENTS.md",
    root / "CLAUDE.md",
    root / "commands/crucible.md",
    root / "docs/install/copilot-cli.md",
    root / "docs/install/claude-code.md",
    root / "docs/install/codex.md",
    root / "skills/crucible/SKILL.md",
    root / "skills/crucible/references/platform-notes.md",
]
inline_default = __import__("re").compile(
    r"\b(?:Defaults:\s*)?(?:Builder|Critic)\s*=\s*",
    __import__("re").IGNORECASE,
)
for path in paths:
    text = path.read_text()
    assert not inline_default.search(text), path
    for role in ("builder", "critic"):
        assert defaults[role]["model"] not in text, (path, role)
print("live default literals: none")
PY
```

Expected: all selected tests pass and the reconciliation prints `live default literals: none`.

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

- [ ] **Step 8: Commit Task 2**

```bash
git add skills/crucible/SKILL.md skills/crucible/references/platform-notes.md \
  tests/test_skill.py tests/test_references.py tests/test_docs.py CLAUDE.md \
  commands/crucible.md docs/install/copilot-cli.md docs/install/claude-code.md \
  docs/install/codex.md CHANGELOG.md
git commit -m "docs: resolve models from run config" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```
