# Deterministic Symmetric Consensus Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Require two separately produced peer attestations for symmetric gates, persist structured
accepted findings, and derive `pr-review` recommendations deterministically.

**Architecture:** Add immutable workflow-kind metadata and a focused `symmetric.py` owner for peer
attestations/finding sets. A new atomic `symmetric-verdict` command validates both peer files against
one bound candidate before recording a decision. Accepted finding events feed deterministic
`accepted-findings`, `review-result`, and symmetric report rendering; the asymmetric build path stays
on the existing `verdict` command.

**Tech Stack:** Python 3.11+ stdlib dataclasses/JSON, schema-v2 artifact bindings, argparse CLI,
JSONL provenance, pytest, Markdown skill protocols.

## Global Constraints

- Workflow kind is immutable run metadata: `build | deep-dive | pr-review`; missing metadata on an
  existing schema-v2 run means `build`.
- `verdict` is build-only; `symmetric-verdict` is deep-dive/pr-review-only.
- A symmetric decision requires separate Peer A and Peer B attestation files validated together
  before any decision event is appended.
- Both attestations echo the exact current artifact/DAG/node bindings.
- The CLI proves two configured slots attested; it does not claim cryptographic proof of two model
  processes.
- Gate progress is based on peer objections, never accepted target-finding severity.
- Dependency/FINAL finding sets are structured and persisted.
- FINAL cannot drop or alter accepted dependency findings.
- PR recommendation uses the run's `blocking_severities`: any accepted finding in that set, or any
  unresolved blocking objection, → `REQUEST_CHANGES`; accepted nonblocking finding only → `COMMENT`;
  empty → `APPROVE`.
- Workflow `CLEAN` and PR recommendation are separate report fields.
- Build workflow behavior and verdict semantics remain unchanged.

---

### Task 1: Add workflow kinds and symmetric data models

**Files:**
- Create: `scripts/crucible/symmetric.py`
- Create: `tests/test_symmetric.py`
- Modify: `scripts/crucible/runlog.py`
- Modify: `scripts/crucible/cli.py`
- Modify: `tests/test_runlog.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/validate_structure.py`

**Interfaces:**
- Produces:
  - `VALID_WORKFLOWS = ("build", "deep-dive", "pr-review")`
  - `SYMMETRIC_WORKFLOWS = ("deep-dive", "pr-review")`
  - `workflow_kind(events: list[dict]) -> str`
  - `AcceptedFinding`
  - `FindingSet`
  - `PeerAttestation`
  - `SymmetricDecision`
  - `decide_symmetric(peer_a, peer_b, cfg, round_index, max_rounds) -> SymmetricDecision`
- Consumed by Tasks 2-4.

- [ ] **Step 1: Write failing workflow/schema tests**

Create `tests/test_symmetric.py`:

```python
import pytest

from crucible.config import Config
from crucible.symmetric import (
    AcceptedFinding,
    FindingSet,
    PeerAttestation,
    decide_symmetric,
    workflow_kind,
)


def _finding(source_gate="dep:auth", fid="F1", severity="major"):
    return {
        "source_gate": source_gate,
        "id": fid,
        "severity": severity,
        "location": "src/auth.py:42",
        "claim": "Expired token accepted.",
        "suggestion": "Reject it.",
    }


def _peer(peer, verdict="APPROVE", objections=None):
    return PeerAttestation.from_dict({
        "peer": peer,
        "gate": "dep:auth",
        "round": 1,
        "verdict": verdict,
        "summary": "review",
        "objections": objections or [],
        "artifact_sha256": "a" * 64,
        "dag_sha256": "d" * 64,
        "node_sha256": "n" * 64,
    })


def test_workflow_kind_defaults_missing_metadata_to_build():
    assert workflow_kind([{"event": "run_start"}]) == "build"


def test_finding_set_rejects_duplicate_source_gate_and_id():
    with pytest.raises(ValueError, match="duplicate"):
        FindingSet.from_dict({"findings": [_finding(), _finding()]})


def test_dependency_finding_set_requires_current_source_gate():
    fs = FindingSet.from_dict({"findings": [_finding(source_gate="dep:other")]})
    with pytest.raises(ValueError, match="source_gate"):
        fs.validate_for_gate("dep:auth")


def test_peer_approve_rejects_blocking_objection():
    peer = _peer("A", objections=[{
        "id": "O1", "severity": "major", "location": "candidate:F1",
        "claim": "Finding lacks evidence.", "suggestion": "Add citation.",
    }])
    assert peer.consistency_error(Config.from_dict({})) is not None


def test_two_approvals_reach_consensus_even_when_candidate_contains_blocker():
    decision = decide_symmetric(
        _peer("A"), _peer("B"), Config.from_dict({}), 1, 5
    )
    assert decision.outcome == "CONSENSUS"
    assert decision.open_objections == []
```

Add run-log/CLI tests:

- `init-run --workflow pr-review` records `workflow: "pr-review"` on `run_start`;
- omitted `--workflow` records `build`;
- invalid workflow rejected by argparse;
- existing schema-v2 `run_start` without workflow is interpreted as build.

- [ ] **Step 2: Run tests and verify RED**

```bash
/Users/sri/personal/crucible/.venv/bin/python -m pytest \
  tests/test_symmetric.py tests/test_runlog.py tests/test_cli.py -q
```

Expected: missing symmetric module and `--workflow` support.

- [ ] **Step 3: Implement finding and peer schemas**

`AcceptedFinding` requires six non-empty string fields and an existing severity. `FindingSet`
validates top-level shape and composite uniqueness, exposes `to_dict()`, `by_key()`, and
`validate_for_gate()`.

`PeerAttestation` contains:

```python
peer: str
gate: str
round: int
verdict: str
summary: str
objections: list[Finding]
artifact_sha256: str | None
dag_sha256: str | None
node_sha256: str | None
```

Its `consistency_error` applies existing blocking severities to `objections`.

`decide_symmetric` namespaces objections as `A:<id>` / `B:<id>` and applies the existing cap policy
without resolutions.

- [ ] **Step 4: Record immutable workflow metadata**

Extend:

```python
def init_run(goal, cfg, base_dir="runs", workflow="build") -> RunLog:
```

Validate `workflow`, append it on `run_start`, add `init-run --workflow` choices, and preserve default
build behavior. Register `symmetric` in `PACKAGE_MODULES`.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run Step 2. Expected: all pass.

- [ ] **Step 6: Commit after Crucible node consensus**

```bash
git add scripts/crucible/symmetric.py scripts/crucible/runlog.py scripts/crucible/cli.py \
  tests/test_symmetric.py tests/test_runlog.py tests/test_cli.py tests/validate_structure.py
git commit -m "feat(cli): add symmetric workflow data model"
```

---

### Task 2: Add atomic two-peer symmetric gate decisions

**Files:**
- Modify: `scripts/crucible/symmetric.py`
- Modify: `scripts/crucible/cli.py`
- Modify: `scripts/crucible/report.py`
- Modify: `tests/test_symmetric.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_cli_inprocess.py`
- Modify: `tests/test_report.py`
- Modify: `tests/test_symmetric_consensus.py`

**Interfaces:**
- Consumes workflow kinds, peer/finding schemas, schema-v2 bindings and workflow prerequisites.
- Produces:
  - `symmetric-verdict` CLI command
  - `symmetric_verdict` run-log event containing both peer attestations
  - accepted dependency finding-set events

- [ ] **Step 1: Write failing atomic-quorum tests**

Add CLI helpers that create a symmetric run, settle PLAN with two peer files, start a node, log a
structured candidate, and produce gate bindings.

Add tests:

- `verdict` rejects `pr-review`/`deep-dive` with `use symmetric-verdict`;
- `symmetric-verdict` rejects `build`;
- both `--peer-a` and `--peer-b` are required;
- files labelled `A/A`, `B/B`, or swapped labels reject without a `symmetric_verdict` append;
- binding mismatch in either file rejects without append;
- inconsistent peer verdict/objections rejects;
- two approvals append one event containing both raw/parsed peer slots and configured model/effort;
- a dependency artifact containing an accepted `major`/`blocker` plus two approvals →
  `CONSENSUS` and an `accepted_finding_set`;
- the same candidate plus one peer `REQUEST_CHANGES` blocking objection → `CHANGES` and no accepted
  finding event;
- one peer requests changes with a blocking objection → `CHANGES`;
- at cap → `CAPPED`/`PROCEED_WITH_FLAGS`;
- peer objection IDs are retained as `A:F1` and `B:F1`;
- Stage 0 `test_single_verdict_cannot_certify_symmetric_workflow` passes using
  `init-run --workflow pr-review` rather than manually editing the log.

- [ ] **Step 2: Run tests and verify RED**

```bash
/Users/sri/personal/crucible/.venv/bin/python -m pytest \
  tests/test_symmetric.py tests/test_cli.py tests/test_cli_inprocess.py \
  tests/test_report.py \
  tests/test_symmetric_consensus.py::test_single_verdict_cannot_certify_symmetric_workflow -q
```

Expected: missing command, workflow routing, and peer events.

- [ ] **Step 3: Implement atomic peer-file validation**

Add:

```bash
crucible symmetric-verdict --run RUN --gate GATE --round N \
  --peer-a A.json --peer-b B.json [--max-rounds N]
```

Before any append:

- require current schema and symmetric workflow;
- enforce terminal/stage/round/node guards;
- compute current bindings;
- read/parse both raw files;
- require exact A/B labels, gate, round, binding shape/value, and consistent verdict;
- parse dependency/FINAL candidate finding set from the current bound Builder artifact;
- dependency candidate `source_gate` must equal the gate.

Validate the candidate, FINAL inclusion, both peers, and the complete decision before any append.
Then log one `symmetric_verdict` event with both configured slot model/effort values, raw
attestations, parsed attestations, namespaced aggregate objections, candidate finding set where
applicable, and bindings.

- [ ] **Step 4: Persist accepted dependency findings**

For dependency/FINAL `CONSENSUS`, append `accepted_finding_set` **after** `symmetric_verdict` and
**before** `gate_consensus`. For `PROCEED_WITH_FLAGS`, append the same event with
`accepted_with_flags: true` and unresolved namespaced objections before
`gate_proceeded_with_flags`. On `CHANGES`/`CAPPED`, do not accept the set.

Append the standard terminal event last. Add workflow/report tests proving a terminal without the
required accepted set, or an accepted set appended after terminal, is invalid.

Round counting for symmetric runs uses prior `symmetric_verdict` events; build continues using
`critic_verdict`.

- [ ] **Step 5: Render peer attestations in gate provenance**

Teach report gate rendering to show Peer A/B verdicts, summaries, and namespaced objections from
`symmetric_verdict`. Do not add final recommendation yet.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run Step 2. Expected: all pass; the second Stage 0 recommendation test remains Task 3 RED.

- [ ] **Step 7: Commit after Crucible node consensus**

```bash
git add scripts/crucible/symmetric.py scripts/crucible/cli.py scripts/crucible/report.py \
  tests/test_symmetric.py tests/test_cli.py tests/test_cli_inprocess.py tests/test_report.py \
  tests/test_symmetric_consensus.py
git commit -m "feat(cli): require two symmetric peer attestations"
```

---

### Task 3: Persist accepted findings and derive review results

**Files:**
- Modify: `scripts/crucible/symmetric.py`
- Modify: `scripts/crucible/cli.py`
- Modify: `scripts/crucible/workflow.py`
- Modify: `tests/test_symmetric.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_workflow.py`
- Modify: `tests/test_symmetric_consensus.py`

**Interfaces:**
- Produces:
  - `accepted_findings(events, dag=None) -> FindingSet`
  - `validate_final_finding_set(candidate, prior) -> None`
  - `review_result(events, cfg, workflow) -> dict`
  - `accepted-findings` and `review-result` CLI commands

- [ ] **Step 1: Write failing result-projection tests**

Add tests:

- dependency accepted sets union in DAG topological order;
- duplicate composite keys across accepted events reject as invalid history;
- FINAL candidate must contain every dependency finding byte-for-structure identical;
- FINAL may add only `source_gate: final`;
- FINAL accepted set replaces dependency union as effective result;
- pr-review accepted finding whose severity is in `cfg.blocking_severities` → `REQUEST_CHANGES`;
- with `blocking_severities=["blocker"]`, an accepted `major` alone → `COMMENT`;
- only minor/nit → `COMMENT`;
- empty → `APPROVE`;
- proceeded-with-flags/capped blocking objections → `REQUEST_CHANGES`;
- deep-dive result omits recommendation;
- `accepted-findings`/`review-result` reject build runs;
- both result commands reject unfinished DAGs or missing required accepted terminals;
- a pre-terminal/orphan `accepted_finding_set` is not returned and makes result commands fail;
- when FINAL is enabled, `review-result` rejects until FINAL has an accepted terminal/set;
- Stage 0 recommendation test uses a valid symmetric run/event setup and passes.

- [ ] **Step 2: Run tests and verify RED**

```bash
/Users/sri/personal/crucible/.venv/bin/python -m pytest \
  tests/test_symmetric.py tests/test_cli.py tests/test_workflow.py \
  tests/test_symmetric_consensus.py -q
```

Expected: result helpers/commands absent and second Stage 0 test fails.

- [ ] **Step 3: Implement accepted finding aggregation**

Read only schema-valid `accepted_finding_set` events whose bindings match accepted gate terminals.
For dependency union, iterate DAG topological order and preserve finding order within each set.
An accepted event counts only when it occurs after `symmetric_verdict` and before a later matching
`gate_consensus`/`gate_proceeded_with_flags` for the same gate/round/bindings. Orphan or
pre-terminal-without-terminal events are incomplete history, never accepted state.

`validate_final_finding_set` requires every prior composite key with exact full content; candidate
extras must use `source_gate: final`.

Wire final validation into `symmetric-verdict` before any append.

- [ ] **Step 4: Implement deterministic recommendation**

`review_result` returns effective findings and unresolved blocking objections. For pr-review derive
the exact uppercase recommendation values. For deep-dive omit the key.

Add CLI parsers/handlers:

```bash
crucible accepted-findings --run RUN
crucible review-result --run RUN
```

Before emitting either command:

- require every DAG node `done` with a valid accepted dependency set;
- reject any orphan accepted-set event;
- for `review-result`, when `final_review` is enabled require accepted FINAL;
- reject incomplete histories with a clear nonzero "incomplete symmetric workflow" error.

Reports use the lower-level aggregation helper to show partial findings from already accepted gates
while status remains `IN PROGRESS`; they do not call the Finish-time completeness guard or derive a
final recommendation until complete.

- [ ] **Step 5: Make workflow validation recognize accepted set integrity**

For symmetric dependency/FINAL terminals, report workflow issues as invalid when the expected
accepted finding event is absent, post-terminal, binding-mismatched, malformed, or inconsistent with
FINAL inclusion.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run Step 2. Expected: both Stage 0 tests pass.

- [ ] **Step 7: Commit after Crucible node consensus**

```bash
git add scripts/crucible/symmetric.py scripts/crucible/cli.py scripts/crucible/workflow.py \
  tests/test_symmetric.py tests/test_cli.py tests/test_workflow.py \
  tests/test_symmetric_consensus.py
git commit -m "feat(cli): derive accepted findings and review results"
```

---

### Task 4: Render symmetric results without conflating workflow status

**Files:**
- Modify: `scripts/crucible/report.py`
- Modify: `scripts/crucible/symmetric.py`
- Modify: `tests/test_report.py`
- Modify: `tests/test_symmetric.py`
- Modify: `tests/test_symmetric_consensus.py`

**Interfaces:**
- Consumes `review_result`.
- Produces symmetric report header, accepted-finding sections, and separate PR recommendation.

- [ ] **Step 1: Write failing report tests**

Add:

- symmetric header shows Peer A/Peer B configured models, not Builder/Critic;
- accepted findings grouped by `source_gate`;
- workflow `CLEAN` can coexist with `Review recommendation: REQUEST_CHANGES`;
- peer objections render separately from accepted findings;
- deep-dive report omits recommendation;
- malformed accepted finding history produces `INVALID`, never a fabricated result.

- [ ] **Step 2: Run tests and verify RED**

```bash
/Users/sri/personal/crucible/.venv/bin/python -m pytest \
  tests/test_report.py tests/test_symmetric.py tests/test_symmetric_consensus.py -q
```

- [ ] **Step 3: Implement mode-aware report rendering**

Use `workflow_kind` for header labels and `review_result` for findings/recommendation. Preserve the
existing workflow status block unchanged except for accepted-finding integrity issues supplied by
Task 3.

- [ ] **Step 4: Run focused and full tests**

Run Step 2, then full pytest. Expected: all engine tests pass.

- [ ] **Step 5: Commit after Crucible node consensus**

```bash
git add scripts/crucible/report.py scripts/crucible/symmetric.py \
  tests/test_report.py tests/test_symmetric.py tests/test_symmetric_consensus.py
git commit -m "feat(report): render deterministic symmetric review results"
```

---

### Task 5: Migrate symmetric skills and public documentation

**Files:**
- Modify: `skills/deep-dive/SKILL.md`
- Modify: `skills/deep-dive/references/peer-prompt.md`
- Modify: `skills/deep-dive/references/consensus-rubric.md`
- Modify: `skills/deep-dive/references/platform-notes.md`
- Modify: `skills/pr-review/SKILL.md`
- Modify: `skills/pr-review/references/peer-prompt.md`
- Modify: `skills/pr-review/references/consensus-rubric.md`
- Modify: `skills/pr-review/references/platform-notes.md`
- Modify: `commands/deep-dive.md`
- Modify: `commands/pr-review.md`
- Modify: `README.md`
- Modify: `docs/cli.md`
- Modify: `SECURITY.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/superpowers/specs/2026-07-15-deep-dive-skill-design.md`
- Modify: `docs/superpowers/specs/2026-07-17-pr-review-skill-design.md`
- Modify: `docs/superpowers/specs/2026-07-20-symmetric-consensus-design.md`
- Modify: `docs/superpowers/plans/2026-07-15-deep-dive-skill.md`
- Modify: `docs/superpowers/plans/2026-07-17-pr-review-skill.md`
- Modify: `docs/superpowers/plans/2026-07-20-symmetric-consensus.md`
- Modify: `tests/test_deep_dive_skill.py`
- Modify: `tests/test_deep_dive_references.py`
- Modify: `tests/test_pr_review_skill.py`
- Modify: `tests/test_pr_review_references.py`
- Modify: `tests/test_docs.py`

**Interfaces:**
- Consumes symmetric CLI commands/result schema.
- Produces executable protocols and public documentation.

- [ ] **Step 1: Write failing section-scoped protocol guards**

Require both skills to:

- initialize with exact `--workflow`;
- log structured dependency/FINAL finding-set JSON;
- produce separate `peer-a.json` and `peer-b.json`;
- invoke `symmetric-verdict` with both files;
- never invoke normal `verdict`;
- preserve schema-v2 binding echo in each peer file;
- use `accepted-findings` before FINAL;
- use `review-result` at Finish;
- pr-review posting uses deterministic recommendation;
- docs state slot proof is not cryptographic process identity.

- [ ] **Step 2: Run protocol tests and verify RED**

```bash
/Users/sri/personal/crucible/.venv/bin/python -m pytest \
  tests/test_deep_dive_skill.py tests/test_deep_dive_references.py \
  tests/test_pr_review_skill.py tests/test_pr_review_references.py tests/test_docs.py -q
```

- [ ] **Step 3: Rewrite symmetric round protocols**

Replace the single serialized union verdict with:

- one candidate finding-set artifact;
- separate A/B attestation JSON files;
- atomic `symmetric-verdict`;
- accepted finding/result commands.

Update peer output schemas to `objections`, exact slot, and bindings.

- [ ] **Step 4: Update public docs/security/provenance**

Document commands, workflow kinds, structured finding schema, recommendation semantics, report
distinction, and slot-proof limitation. Update CHANGELOG; mark design implemented and link plan.

- [ ] **Step 5: Run complete verification**

Run focused protocol tests, structural validation, full suite, `scripts/check.py`, and
`git diff --check`.

- [ ] **Step 6: Commit after Crucible node consensus**

```bash
git add skills/deep-dive skills/pr-review commands/deep-dive.md commands/pr-review.md \
  README.md docs/cli.md SECURITY.md CHANGELOG.md docs/superpowers tests
git commit -m "docs: migrate symmetric skills to two-peer verdicts"
```
