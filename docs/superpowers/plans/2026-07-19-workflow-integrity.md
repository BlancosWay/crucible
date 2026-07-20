# Workflow Integrity and Artifact Binding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Design spec:**
[`../specs/2026-07-19-workflow-integrity-design.md`](../specs/2026-07-19-workflow-integrity-design.md).
**Status:** implemented — Task 1 (schema-v2 integrity primitives), Task 2 (bind gate verdicts +
freeze accepted plans), Task 3 (workflow prerequisites, approval, and legal node transitions), and
Task 4 (binding- and configuration-aware reports) landed the deterministic CLI/report contract; Task 5
(`docs: bind every orchestration gate to reviewed artifacts`) migrated all three skills, their
references, and every public/security/CLI surface to the binding handshake. The step boxes below are
checked off as each task landed.

**Goal:** Bind every gate decision to the exact reviewed artifact/DAG/node, enforce configured stage
and node ordering, and allow `CLEAN` reports only for complete valid schema-v2 workflows.

**Architecture:** Introduce schema-v2 runs plus canonical SHA-256 bindings in a new integrity owner.
The CLI exposes binding metadata that Critic verdicts must echo, and a shared workflow validator
enforces prerequisites for both commands and reports. Legacy runs remain readable but are
`LEGACY / UNVERIFIED` and cannot be mutated.

**Tech Stack:** Python 3.11+ stdlib (`dataclasses`, `hashlib`, `json`), argparse CLI, JSONL run log,
pytest, Markdown skill/protocol documentation.

## Global Constraints

- New runs use immutable `RUN_SCHEMA_VERSION = 2`.
- Critic verdicts echo CLI-generated `artifact_sha256` and gate-specific DAG/node bindings.
- PLAN/DAG and dependency definitions cannot change after accepted PLAN.
- Required order is configured REPRODUCE → PLAN → optional approval → dependency work → optional FINAL.
- `pending -> done`, review while pending, and FINAL before completion are rejected.
- `CLEAN` requires every configured phase, valid ordering, current bindings, and done nodes.
- Legacy runs are report/status/clean readable, `LEGACY / UNVERIFIED`, and mutation requires a fresh run.
- Existing forced node completion remains explicit, rationale-bearing, and `FLAGGED`.
- No external dependencies, signing keys, migration guesses, or concurrency redesign.

---

### Task 1: Add schema-v2 canonical integrity primitives

**Files:**
- Create: `scripts/crucible/integrity.py`
- Create: `tests/test_integrity.py`
- Modify: `scripts/crucible/dag.py`
- Modify: `scripts/crucible/runlog.py`
- Modify: `tests/test_dag.py`
- Modify: `tests/test_runlog.py`
- Modify: `tests/validate_structure.py`

**Interfaces:**
- Produces:
  - `RUN_SCHEMA_VERSION: int = 2`
  - `artifact_sha256(data: bytes) -> str`
  - `read_artifact(path: str | Path) -> tuple[str, str]`
  - `canonical_json_sha256(value: Any) -> str`
  - `dag_sha256(dag: DAG) -> str`
  - `node_sha256(dag: DAG, node_id: str) -> str`
  - `run_schema_version(events: list[dict]) -> int | None`
  - `require_current_schema(run: RunLog) -> None`
  - `DAG.definition_dict() -> dict`
  - `DAG.node_definition_dict(node_id: str) -> dict`
- Consumed by Tasks 2-4.

- [x] **Step 1: Write failing digest/schema tests**

Create `tests/test_integrity.py`:

```python
import json

import pytest

from crucible.config import Config
from crucible.dag import DAG
from crucible.integrity import (
    RUN_SCHEMA_VERSION,
    artifact_sha256,
    dag_sha256,
    node_sha256,
    read_artifact,
    require_current_schema,
    run_schema_version,
)
from crucible.runlog import RunLog, init_run


def _dag(status="pending", file_name="a.py"):
    return DAG.from_dict({
        "nodes": [{
            "id": "a", "title": "A", "description": "d",
            "files": [file_name], "test_plan": "pytest tests/a -q", "status": status,
        }],
        "edges": [],
    })


def test_new_run_records_schema_version(tmp_path):
    run = init_run("g", Config.from_dict({}), base_dir=tmp_path)
    assert run_schema_version(run.read_events()) == RUN_SCHEMA_VERSION == 2


def test_dag_digest_ignores_status_but_not_definition():
    assert dag_sha256(_dag("pending")) == dag_sha256(_dag("done"))
    assert dag_sha256(_dag(file_name="a.py")) != dag_sha256(_dag(file_name="b.py"))


def test_node_digest_includes_dependencies():
    base = {
        "nodes": [
            {"id": "a", "status": "pending"},
            {"id": "b", "status": "pending"},
        ],
        "edges": [],
    }
    with_dep = json.loads(json.dumps(base))
    with_dep["edges"] = [{"from": "b", "depends_on": "a"}]
    assert node_sha256(DAG.from_dict(base), "b") != node_sha256(DAG.from_dict(with_dep), "b")


def test_artifact_hash_preserves_crlf_bytes():
    assert artifact_sha256(b"a\r\n") != artifact_sha256(b"a\n")


def test_read_artifact_hashes_original_bytes(tmp_path):
    path = tmp_path / "artifact.txt"
    path.write_bytes(b"a\r\n")
    text, digest = read_artifact(path)
    assert text == "a\r\n"
    assert digest == artifact_sha256(b"a\r\n")


def test_legacy_run_is_not_mutable(tmp_path):
    path = tmp_path / "legacy"
    path.mkdir()
    run = RunLog(path)
    run.append("run_start", goal="old", config=Config.from_dict({}).to_dict())
    with pytest.raises(SystemExit, match="legacy.*fresh run"):
        require_current_schema(run)
```

Update `tests/test_dag.py` to assert definition dictionaries preserve node order, sort dependencies,
and omit `status`. Update `tests/test_runlog.py` to assert `run_start.schema_version == 2`.

- [x] **Step 2: Run tests and verify RED**

Run:

```bash
/Users/sri/personal/crucible/.venv/bin/python -m pytest \
  tests/test_integrity.py tests/test_dag.py tests/test_runlog.py -q
```

Expected: collection/import failure because `crucible.integrity` and definition methods do not exist.

- [x] **Step 3: Add canonical definition methods**

In `DAG`:

```python
def definition_dict(self) -> dict[str, Any]:
    return {
        "nodes": [{
            "id": n.id,
            "title": n.title,
            "description": n.description,
            "files": list(n.files),
            "test_plan": n.test_plan,
        } for n in (self.nodes[nid] for nid in self.order)],
        "edges": [
            {"from": nid, "depends_on": dep}
            for nid in self.order
            for dep in sorted(self.deps[nid])
        ],
    }

def node_definition_dict(self, node_id: str) -> dict[str, Any]:
    n = self.node(node_id)
    return {
        "id": n.id,
        "title": n.title,
        "description": n.description,
        "files": list(n.files),
        "test_plan": n.test_plan,
        "depends_on": sorted(self.deps[node_id]),
    }
```

- [x] **Step 4: Implement `integrity.py` and schema recording**

Use:

```python
RUN_SCHEMA_VERSION = 2

def canonical_json_sha256(value) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

def artifact_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def read_artifact(path: str | Path) -> tuple[str, str]:
    data = Path(path).read_bytes()
    return data.decode("utf-8"), artifact_sha256(data)
```

`run_schema_version` reads the first `run_start`; `require_current_schema` rejects missing/older
versions. `init_run` appends `schema_version=RUN_SCHEMA_VERSION`.

Register `integrity` in `tests/validate_structure.py::PACKAGE_MODULES`.

- [x] **Step 5: Run focused tests and verify GREEN**

Run the Step 2 command. Expected: all tests PASS.

- [x] **Step 6: Commit after Crucible node consensus**

```bash
git add scripts/crucible/integrity.py scripts/crucible/dag.py scripts/crucible/runlog.py \
  tests/test_integrity.py tests/test_dag.py tests/test_runlog.py tests/validate_structure.py
git commit -m "feat(cli): add schema-v2 integrity primitives"
```

---

### Task 2: Bind gate verdicts to exact artifacts and freeze accepted plans

**Files:**
- Modify: `scripts/crucible/integrity.py`
- Modify: `scripts/crucible/verdict.py`
- Modify: `scripts/crucible/cli.py`
- Modify: `tests/test_integrity.py`
- Modify: `tests/test_verdict.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_cli_inprocess.py`
- Modify: `tests/test_workflow_integrity.py`

**Interfaces:**
- Consumes Task 1 digests/schema.
- Produces:
  - `BindingSet` dataclass with optional artifact/DAG/node fields
  - `current_bindings(run, gate, round_index) -> BindingSet`
  - `bindings` CLI command
  - schema-v2 verdict fields `artifact_sha256`, `dag_sha256`, `node_sha256`
  - bound terminal events and exact `show-plan`

- [x] **Step 1: Write failing binding-handshake tests**

Extend `tests/test_integrity.py`:

```python
def test_current_plan_bindings_require_builder_output_and_dag(tmp_path):
    run = init_run("g", Config.from_dict({}), base_dir=tmp_path)
    run.save_dag(_dag().to_dict())
    run.append("builder_output", gate="plan", round=1, payload="plan",
               artifact_sha256=artifact_sha256(b"plan"))
    b = current_bindings(run, "plan", 1)
    assert b.artifact_sha256 == artifact_sha256(b"plan")
    assert b.dag_sha256 == dag_sha256(_dag())
    assert b.node_sha256 is None


def test_dep_bindings_include_node(tmp_path):
    run = init_run("g", Config.from_dict({}), base_dir=tmp_path)
    dag = _dag()
    run.save_dag(dag.to_dict())
    run.append(
        "builder_output",
        gate="dep:a",
        round=1,
        payload="diff",
        artifact_sha256=artifact_sha256(b"diff"),
    )
    b = current_bindings(run, "dep:a", 1)
    assert b.artifact_sha256 == artifact_sha256(b"diff")
    assert b.dag_sha256 == dag_sha256(dag)
    assert b.node_sha256 == node_sha256(dag, "a")
```

Use complete setup in the actual test; do not leave the ellipsis.

Add CLI tests:

```python
def test_bindings_command_emits_plan_hashes(tmp_path):
    run_dir = _init(tmp_path)
    _load(run_dir, tmp_path, {"a": "pending"})
    plan = tmp_path / "plan.txt"
    plan.write_text("reviewed plan")
    logged = _run([
        "log", "--run", run_dir, "--event", "builder_output",
        "--gate", "plan", "--round", "1", "--file", str(plan),
    ])
    assert logged.returncode == 0, logged.stderr
    result = _run(["bindings", "--run", run_dir, "--gate", "plan", "--round", "1"])
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert set(data) == {"artifact_sha256", "dag_sha256"}


def test_verdict_rejects_missing_or_mismatched_bindings_without_logging(tmp_path):
    run_dir = _init(tmp_path)
    _load(run_dir, tmp_path, {"a": "pending"})
    plan = tmp_path / "plan.txt"
    plan.write_text("reviewed plan")
    assert _run([
        "log", "--run", run_dir, "--event", "builder_output",
        "--gate", "plan", "--round", "1", "--file", str(plan),
    ]).returncode == 0
    bindings = json.loads(_run([
        "bindings", "--run", run_dir, "--gate", "plan", "--round", "1",
    ]).stdout)
    bindings["artifact_sha256"] = "0" * 64
    verdict = tmp_path / "verdict.json"
    verdict.write_text(json.dumps({
        "gate": "plan", "round": 1, "verdict": "APPROVE",
        "summary": "ok", "findings": [], **bindings,
    }))
    result = _run([
        "verdict", "--run", run_dir, "--gate", "plan",
        "--round", "1", "--file", str(verdict),
    ])
    assert result.returncode != 0
    assert "binding" in result.stderr.lower()
    assert "critic_verdict" not in [e["event"] for e in _events(run_dir)]
```

Add five explicit CLI tests named
`test_load_dag_rejects_legacy_run`, `test_log_rejects_legacy_run`,
`test_bindings_rejects_legacy_run`, `test_verdict_rejects_legacy_run`, and
`test_show_plan_rejects_legacy_run`. Each creates a `RunLog` whose `run_start` lacks
`schema_version`, supplies otherwise-valid command files/arguments, and asserts nonzero stderr
containing both `legacy` and `fresh run`; the verdict test also asserts no `critic_verdict` was
appended.

Adapt `_approve` in `tests/test_workflow_integrity.py` to call `bindings`, merge returned fields into
the verdict JSON, and preserve every Stage 0 assertion.

Migrate every existing schema-v2 CLI success fixture:

- Add `_log_artifact_and_get_bindings(run_dir, tmp_path, gate, round_index, payload)` in
  `tests/test_cli.py`; write artifact bytes, call `log`, call `bindings`, and return parsed JSON.
- Add this helper, which merges bindings into success-path verdict files:

  ```python
  def _write_bound_verdict(
      tmp_path, run_dir, gate, round_index, verdict, findings
  ) -> Path:
      bindings_result = _run([
          "bindings", "--run", run_dir, "--gate", gate,
          "--round", str(round_index),
      ])
      assert bindings_result.returncode == 0, bindings_result.stderr
      path = tmp_path / f"{gate.replace(':', '-')}-{round_index}-verdict.json"
      path.write_text(json.dumps({
          "gate": gate,
          "round": round_index,
          "verdict": verdict,
          "summary": "test verdict",
          "findings": findings,
          **json.loads(bindings_result.stdout),
      }))
      return path
  ```
- Update every successful `verdict` invocation in `tests/test_cli.py` and
  `tests/test_cli_inprocess.py`. Tests intentionally exercising missing/mismatched bindings remain
  explicit negative cases.
- Replace the old "PLAN consensus without DAG succeeds" test with a negative test proving PLAN
  bindings/verdict require a DAG.
- Keep pure parser/decision tests in `tests/test_verdict.py` capable of constructing optional legacy
  `Verdict` objects; CLI schema-v2 tests always provide binding fields.

- [x] **Step 2: Run tests and verify RED**

```bash
/Users/sri/personal/crucible/.venv/bin/python -m pytest \
  tests/test_integrity.py tests/test_verdict.py tests/test_cli.py tests/test_cli_inprocess.py \
  tests/test_workflow_integrity.py -q
```

Expected: failures for missing `BindingSet`, command, verdict fields, fixture migration, and current
artifact-substitution vulnerabilities.

- [x] **Step 3: Implement binding lookup and verdict schema**

`BindingSet`:

```python
@dataclass(frozen=True)
class BindingSet:
    artifact_sha256: str
    dag_sha256: str | None = None
    node_sha256: str | None = None

    def to_dict(self) -> dict[str, str]:
        return {
            key: value
            for key, value in asdict(self).items()
            if value is not None
        }
```

`current_bindings` requires the latest non-empty `builder_output` for the exact gate/round and checks
its recorded hash. Add DAG/node hashes by gate.

Extend `Verdict` with optional binding fields for parsing historical files. For schema-v2
`cmd_verdict`, compare every expected field exactly and reject missing/extra mismatches before any
log append.

- [x] **Step 4: Harden `log`, `load-dag`, terminal events, and `show-plan`**

- `log` uses `read_artifact`, requires current schema, round >= 1, current expected round, non-empty
  Builder `--file`, records the exact-byte `artifact_sha256`, and rejects terminal gates.
- `load-dag` requires current schema and rejects any PLAN-terminal run even with `--force`; its
  `dag_loaded` event records `dag_sha256`.
- Add `bindings` parser/handler.
- `bindings`, `verdict`, and `show-plan` require current schema; `verdict` never appends to a legacy
  run.
- `critic_verdict` and terminal events persist validated binding fields.
- `show-plan` requires current schema, verifies terminal/current DAG hash, and selects the exact
  pre-terminal Builder event matching terminal `artifact_sha256`.
- Before normal `set-status --status done`, recompute current `dag_sha256` and `node_sha256` and require
  them to match the accepted dependency terminal. This binding-only guard belongs to Task 2 and does
  not yet enforce Task 3's legal transition/order rules.

- [x] **Step 5: Verify Stage 0 artifact tests GREEN**

Run:

```bash
/Users/sri/personal/crucible/.venv/bin/python -m pytest \
  tests/test_integrity.py tests/test_verdict.py tests/test_cli.py tests/test_cli_inprocess.py \
  tests/test_workflow_integrity.py::test_post_consensus_same_round_plan_output_is_rejected \
  tests/test_workflow_integrity.py::test_post_consensus_dag_replacement_is_rejected \
  tests/test_workflow_integrity.py::test_stale_same_id_review_cannot_authorize_replacement_node -q
```

Expected: all binding/artifact tests PASS. Ordering/report Stage 0 tests remain owned by later nodes.

- [x] **Step 6: Commit after Crucible node consensus**

```bash
git add scripts/crucible/integrity.py scripts/crucible/verdict.py scripts/crucible/cli.py \
  tests/test_integrity.py tests/test_verdict.py tests/test_cli.py tests/test_cli_inprocess.py \
  tests/test_workflow_integrity.py
git commit -m "feat(cli): bind gate decisions to reviewed artifacts"
```

---

### Task 3: Enforce workflow prerequisites, approval, and legal node transitions

**Files:**
- Create: `scripts/crucible/workflow.py`
- Create: `tests/test_workflow.py`
- Modify: `scripts/crucible/dag.py`
- Modify: `scripts/crucible/cli.py`
- Modify: `tests/test_dag.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_workflow_integrity.py`
- Modify: `tests/validate_structure.py`

**Interfaces:**
- Consumes Task 2 terminal binding fields.
- Produces:
  - `accepted_terminal(events, gate) -> dict | None`
  - `require_plan_verdict_ready(run, cfg) -> None`
  - `require_plan_ready(run, cfg) -> dict`
  - `require_node_review_ready(run, cfg, dag, node_id) -> None`
  - `require_final_ready(run, cfg, dag) -> None`
  - `workflow_issues(events, dag, cfg) -> list[str]` for Task 4
  - `approve-plan` CLI command and `plan_approved` event

- [x] **Step 1: Write failing transition/prerequisite tests**

Create `tests/test_workflow.py` with these exact scenarios, using real `RunLog`, schema-v2 terminal
bindings, and exact expected messages:

- `test_plan_requires_configured_reproduce_consensus`: initialize with `reproduce_gate=True`, omit
  a REPRODUCE terminal, call `require_plan_verdict_ready`, and assert `SystemExit` names
  `reproduce`.
- `test_dependency_requires_bound_plan_and_configured_approval`: create a valid bound PLAN under
  `human_approval=True`, omit `plan_approved`, call `require_node_review_ready`, and assert the error
  names approval.
- `test_final_requires_all_nodes_done`: create a valid PLAN and one pending node, call
  `require_final_ready`, and assert the error names the unfinished node.
- `test_approval_binds_current_plan_and_dag`: append `plan_approved` using the accepted terminal
  hashes and assert `require_plan_ready` returns that terminal.
- `test_stale_approval_is_rejected`: change `dag.json` after approval and assert
  `require_plan_ready` reports a stale DAG binding.

Update `tests/test_dag.py`:

```python
@pytest.mark.parametrize(("start", "target"), [
    ("pending", "done"),
    ("pending", "in_review"),
    ("blocked", "done"),
    ("done", "pending"),
    ("done", "in_progress"),
])
def test_illegal_status_transitions_raise(start, target):
    dag = _dag({"a": start})
    with pytest.raises(ValueError, match=rf"{start}.*{target}"):
        dag.set_status("a", target)
```

Add CLI tests for `approve-plan`, `next` before approval, dependency verdict while pending, FINAL
before done, forced completion prerequisites, and legacy `set-status`/`approve-plan` refusal. Every
mutating command (`load-dag`, `log`, `verdict`, `set-status`, `approve-plan`) must call
`require_current_schema`; read-only `report`, `status`, `critic-lenses`, and `clean` remain available.

Migrate existing Task 3-selected fixtures to the enforced workflow:

- In `tests/test_dag.py`, positive transition tests use `pending -> in_progress -> done`; tests that
  only need a pre-existing done graph construct it through `DAG.from_dict` instead of calling an
  illegal transition.
- In `tests/test_cli.py`, add/reuse a helper that creates a bound accepted PLAN (and calls
  `approve-plan` when the fixture enables `human_approval`), then sets the target node
  `in_progress` before positive dependency `log`/`bindings`/`verdict` flows.
- Update the full dry-run flow to settle PLAN before the first `next`.
- Scheduling-only tests that are not testing CLI stage prerequisites construct DAG/run-log state
  directly; they do not use `--force` to bypass the new contract.
- Positive forced-done tests first create a bound accepted PLAN/current approval, then assert the
  forced event's hashes/rationale.
- In `tests/test_workflow_integrity.py`,
  `test_final_before_all_nodes_done_is_rejected` asserts the first guarded FINAL operation
  (`log`, `bindings`, or `verdict`, whichever Task 3 owns) is rejected instead of requiring FINAL
  artifact logging to succeed; `test_pending_node_cannot_be_reviewed` likewise asserts the first
  dependency artifact/review operation is rejected while the node remains pending.
- Preserve explicit negative tests for missing PLAN, missing approval, pending review, and illegal
  transitions; do not make helpers silently satisfy the prerequisite a test is intended to omit.

- [x] **Step 2: Run tests and verify RED**

```bash
/Users/sri/personal/crucible/.venv/bin/python -m pytest \
  tests/test_workflow.py tests/test_dag.py tests/test_cli.py \
  tests/test_workflow_integrity.py::test_final_before_all_nodes_done_is_rejected \
  tests/test_workflow_integrity.py::test_pending_node_cannot_be_reviewed \
  tests/test_workflow_integrity.py::test_dag_rejects_direct_pending_to_done_transition -q
```

Expected: missing workflow module/approval command and all ordering contracts fail.

- [x] **Step 3: Implement legal transitions**

In `DAG.set_status`, enforce:

```python
ALLOWED_TRANSITIONS = {
    "pending": {"in_progress", "blocked"},
    "in_progress": {"in_review", "done", "blocked", "pending"},
    "in_review": {"in_progress", "done", "blocked", "pending"},
    "blocked": {"pending"},
    "done": set(),
}
```

Allow same-status idempotence. `cmd_set_status --force --status done` bypasses the transition table
only for non-done nodes after PLAN/approval prerequisites.

- [x] **Step 4: Implement shared workflow prerequisites**

`workflow.py` validates accepted terminal bindings against current artifacts and event order.

- PLAN requires configured REPRODUCE consensus.
- dependency work/review requires accepted current PLAN, configured current approval, dependencies
  done, and node `in_progress|in_review`;
- FINAL requires enabled config, every node done, and accepted/forced dependency completion.

Register `workflow` in `PACKAGE_MODULES`.

- [x] **Step 5: Add `approve-plan` and wire command guards**

`approve-plan` records:

```json
{
  "event": "plan_approved",
  "artifact_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "dag_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
}
```

Wire prerequisites into `log`, `bindings`, `verdict`, `next`, and `set-status`. A dependency terminal
must match current node/DAG bindings before normal `done`.

For `set-status --force --status done`, compute and persist current `dag_sha256` and `node_sha256`
on the `node_status_change` event after validating current PLAN/approval prerequisites. Add a CLI
test that reads the event and compares both fields to the integrity helpers.

- [x] **Step 6: Run focused tests and verify GREEN**

Run Step 2 command. Expected: stage-order and transition Stage 0 tests pass.

- [x] **Step 7: Commit after Crucible node consensus**

```bash
git add scripts/crucible/workflow.py scripts/crucible/dag.py scripts/crucible/cli.py \
  tests/test_workflow.py tests/test_dag.py tests/test_cli.py \
  tests/test_workflow_integrity.py tests/validate_structure.py
git commit -m "feat(cli): enforce workflow stage and node ordering"
```

---

### Task 4: Make reports binding- and configuration-aware

**Files:**
- Modify: `scripts/crucible/workflow.py`
- Modify: `scripts/crucible/report.py`
- Modify: `tests/test_workflow.py`
- Modify: `tests/test_report.py`
- Modify: `tests/test_workflow_integrity.py`

**Interfaces:**
- Consumes Task 3 `workflow_issues`.
- Produces schema-v2 report statuses `LEGACY / UNVERIFIED`, `INVALID`, `BLOCKED`, `FLAGGED`,
  `CLEAN`, and `IN PROGRESS`.

- [x] **Step 1: Write failing report status tests**

Add these exact tests:

- `test_legacy_report_is_unverified_never_clean`: construct a run with a `run_start` lacking
  `schema_version`; assert `LEGACY / UNVERIFIED` and no `CLEAN`.
- `test_missing_configured_reproduce_approval_final_is_in_progress`: create valid bound PLAN and
  dependency terminals under all three enabled switches, deliberately omit REPRODUCE, approval, and
  FINAL; assert `IN PROGRESS` and all three phase names.
- `test_binding_mismatch_is_invalid`: create a complete bound run, replace current `dag.json` with a
  semantic change, and assert `INVALID` plus `DAG binding`.
- `test_out_of_order_final_is_invalid`: place a valid FINAL terminal before dependency completion in
  log order; assert `INVALID` plus `FINAL`.
- `test_complete_bound_configured_workflow_is_clean`: construct REPRODUCE → PLAN → approval →
  dependency done → FINAL with matching hashes; assert `CLEAN`.
- `test_forced_current_node_remains_flagged`: create a bound PLAN and forced done event with current
  DAG/node hashes and rationale; assert `FLAGGED` and the rationale.

Update Stage 0 report test setup with valid schema-v2 bindings for PLAN/dependency while deliberately
omitting configured phases; assert missing phase names appear.

- [x] **Step 2: Run report tests and verify RED**

```bash
/Users/sri/personal/crucible/.venv/bin/python -m pytest \
  tests/test_workflow.py tests/test_report.py \
  tests/test_workflow_integrity.py::test_report_is_not_clean_when_configured_phases_are_omitted -q
```

Expected: legacy and config-aware statuses absent; omission test still reports CLEAN.

- [x] **Step 3: Implement shared issue classification**

`workflow_issues` returns structured issues:

```python
@dataclass(frozen=True)
class WorkflowIssue:
    kind: str  # "missing" | "invalid" | "flagged"
    message: str
```

It checks configured phase presence/order, artifact bindings, approval, dependency terminal-before-
done ordering, and FINAL ordering.

- [x] **Step 4: Update report status derivation**

Pass `run_start.config` and schema version to `_run_summary_lines`.

Precedence:

```text
LEGACY / UNVERIFIED > INVALID > BLOCKED > FLAGGED > CLEAN > IN PROGRESS
```

List each missing/invalid phase in the summary. Preserve unresolved-finding and override rendering.

- [x] **Step 5: Run report/full tests and verify GREEN**

Run Step 2 command, then:

```bash
/Users/sri/personal/crucible/.venv/bin/python -m pytest -q
```

Expected: all tests PASS.

- [x] **Step 6: Commit after Crucible node consensus**

```bash
git add scripts/crucible/workflow.py scripts/crucible/report.py \
  tests/test_workflow.py tests/test_report.py tests/test_workflow_integrity.py
git commit -m "feat(report): verify configured workflow integrity"
```

---

### Task 5: Migrate all orchestration protocols and user documentation

**Files:**
- Modify: `skills/crucible/SKILL.md`
- Modify: `skills/crucible/references/critic-prompt.md`
- Modify: `skills/crucible/references/consensus-rubric.md`
- Modify: `skills/crucible/references/platform-notes.md`
- Modify: `skills/deep-dive/SKILL.md`
- Modify: `skills/deep-dive/references/peer-prompt.md`
- Modify: `skills/deep-dive/references/consensus-rubric.md`
- Modify: `skills/deep-dive/references/platform-notes.md`
- Modify: `skills/pr-review/SKILL.md`
- Modify: `skills/pr-review/references/peer-prompt.md`
- Modify: `skills/pr-review/references/consensus-rubric.md`
- Modify: `skills/pr-review/references/platform-notes.md`
- Modify: `commands/crucible.md`
- Modify: `commands/deep-dive.md`
- Modify: `commands/pr-review.md`
- Modify: `README.md`
- Modify: `docs/cli.md`
- Modify: `SECURITY.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/superpowers/specs/2026-07-19-workflow-integrity-design.md`
- Modify: `docs/superpowers/plans/2026-07-19-workflow-integrity.md`
- Modify: `tests/test_skill.py`
- Modify: `tests/test_references.py`
- Modify: `tests/test_deep_dive_skill.py`
- Modify: `tests/test_deep_dive_references.py`
- Modify: `tests/test_pr_review_skill.py`
- Modify: `tests/test_pr_review_references.py`
- Modify: `tests/test_docs.py`

**Interfaces:**
- Consumes CLI `bindings` and `approve-plan`.
- Produces a consistent binding handshake in every skill and public contract.

- [x] **Step 1: Write failing protocol contract tests**

Across each skill/reference test, require:

- `crucible bindings --run "$RUN" --gate "$GATE" --round N`;
- trusted binding JSON appended to the Critic/peer seed;
- verdict output echoes `artifact_sha256` and gate-specific DAG/node hashes;
- configured human approval invokes `crucible approve-plan --run "$RUN"`;
- no mutation of accepted plan/DAG; legacy runs require fresh run.

Add a docs owner test requiring README/docs/SECURITY/CHANGELOG to mention artifact binding and schema-2
legacy behavior.

- [x] **Step 2: Run protocol/docs tests and verify RED**

```bash
/Users/sri/personal/crucible/.venv/bin/python -m pytest \
  tests/test_skill.py tests/test_references.py \
  tests/test_deep_dive_skill.py tests/test_deep_dive_references.py \
  tests/test_pr_review_skill.py tests/test_pr_review_references.py \
  tests/test_docs.py -q
```

Expected: binding/approval commands and verdict fields absent.

- [x] **Step 3: Update all role schemas and gate loops**

For every gate:

```bash
BINDINGS=$(PYTHONPATH=scripts python3 -m crucible bindings \
  --run "$RUN" --gate "$GATE" --round N)
```

Append the exact JSON as trusted CLI metadata. Critic/peer output includes matching binding fields.
After explicit human PLAN approval:

```bash
PYTHONPATH=scripts python3 -m crucible approve-plan --run "$RUN"
```

Document that accepted plan/DAG changes require a fresh run.

- [x] **Step 4: Update public CLI/security docs and provenance**

- Document `bindings`, `approve-plan`, schema version, legal transitions, immutable accepted DAG,
  legacy read-only behavior, and report statuses.
- Update SECURITY's determinism claim to name content bindings and configured phase enforcement.
- Add `CHANGELOG.md` entry under `## [Unreleased]`.
- Mark design `implemented` and link this plan.

- [x] **Step 5: Run focused and complete verification**

```bash
/Users/sri/personal/crucible/.venv/bin/python -m pytest \
  tests/test_skill.py tests/test_references.py \
  tests/test_deep_dive_skill.py tests/test_deep_dive_references.py \
  tests/test_pr_review_skill.py tests/test_pr_review_references.py \
  tests/test_docs.py -q
python3 tests/validate_structure.py
/Users/sri/personal/crucible/.venv/bin/python -m pytest -q
python3 scripts/check.py
git diff --check
```

Expected: all tests/checks PASS.

- [x] **Step 6: Commit after Crucible node consensus**

```bash
git add skills commands README.md docs/cli.md SECURITY.md CHANGELOG.md \
  docs/superpowers/specs/2026-07-19-workflow-integrity-design.md \
  docs/superpowers/plans/2026-07-19-workflow-integrity.md tests
git commit -m "docs: bind every orchestration gate to reviewed artifacts"
```
