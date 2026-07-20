# Workflow Integrity and Artifact Binding — Design

**Status:** implemented. **Type:** deterministic CLI/run-log contract hardening.
**Companion plan:**
[`docs/superpowers/plans/2026-07-19-workflow-integrity.md`](../plans/2026-07-19-workflow-integrity.md).

## Problem

Crucible currently records that a gate reached consensus, but it does not bind that decision to the
exact artifact the Critic reviewed or enforce the configured workflow order.

The confirmed failures fall into three root causes.

### 1. Gate decisions are not bound to artifact content

- A same-round `builder_output` logged after PLAN consensus becomes the displayed "approved plan."
- An all-pending DAG can replace the reviewed DAG after PLAN consensus.
- A prior `dep:x` consensus can authorize a replacement node `x` whose files/description/test plan
  differ from the reviewed node.
- A Builder artifact can change between Critic review and `crucible verdict`; the CLI currently has
  no Critic-echoed artifact identity to compare.

### 2. Stage and node transitions are not enforced

- FINAL can reach consensus before implementation.
- A dependency may be reviewed while still `pending`.
- A node may transition directly from `pending` to `done`.
- `should-reproduce`, `should-approve`, and `should-final` are advisory queries; later commands do not
  enforce their configured prerequisites.

### 3. The report certifies only events that happened

The report does not derive required gates from the run's resolved configuration. A run can omit
configured REPRODUCE, approval, or FINAL phases and still render `CLEAN`.

These gaps contradict Crucible's stated contract that deterministic CLI enforcement—not model
discipline—owns consensus, DAG advancement, and provenance.

## Goal

For every new run:

1. A gate decision is accepted only when the Critic verdict echoes deterministic bindings for the
   exact Builder artifact and relevant DAG/node definition.
2. Terminal events persist those bindings.
3. The approved plan/DAG and reviewed dependency cannot be changed or substituted after acceptance.
4. The CLI enforces required phases and legal state transitions in order.
5. `CLEAN` is possible only when all configured phases are present, ordered, accepted, and bound to
   the current artifacts.
6. Historical runs remain readable but can never be presented as cryptographically/content-bound.

## Non-goals

- Tamper-proofing against an operator who can rewrite arbitrary files and run-log bytes.
- Signing events with an external key.
- Migrating inferred approvals from legacy logs.
- Solving concurrent multi-process access to one run directory.
- Changing model defaults, consensus severity policy, or the two-peer semantics.

## Alternatives considered

### Prompt-only ordering rules

Rejected. The current skills already prescribe correct ordering; the defect is that the CLI and
report certify out-of-order/substituted artifacts anyway.

### Freeze the DAG and reject post-terminal logs

Necessary but insufficient. An artifact can be changed after the Critic reviews it but before the
verdict is submitted. The Critic must echo the identity it reviewed.

### Infer bindings from event order

Rejected. The CLI can identify the latest logged artifact, but cannot prove that the Critic reviewed
that artifact unless the verdict carries the same binding.

### Versioned binding protocol (selected)

New runs use a schema version, canonical SHA-256 bindings, strict transition guards, and
configuration-aware report validation. Legacy runs are read-only and visibly unverified.

## Run schema

Introduce:

```python
RUN_SCHEMA_VERSION = 2
```

`init_run` records `schema_version: 2` in the `run_start` event. The schema version is not user
configuration and cannot be overridden.

### Legacy policy

A run whose `run_start` has no schema version (or a version lower than 2) is **legacy**:

- `report`, `status`, `critic-lenses`, and `clean` remain available;
- the report status is `LEGACY / UNVERIFIED`, never `CLEAN`;
- `load-dag`, `log`, `verdict`, `set-status`, `approve-plan`, and `show-plan` refuse mutation or an
  "approved" rendering and direct the operator to start a fresh run.

No migration command is provided. Reconstructing which bytes a historical Critic actually reviewed
would be guesswork.

## Canonical bindings

Add a focused `scripts/crucible/integrity.py` owner for canonicalization and validation.

### Text artifacts

`artifact_sha256` is SHA-256 over the exact bytes of the non-empty UTF-8 artifact file. The CLI reads
the file with `Path.read_bytes()`, hashes those bytes, then strictly decodes the same bytes as UTF-8
for the run-log payload. It never uses universal-newline text reads before hashing, so CRLF and LF
artifacts remain distinct.

### DAG definition

`dag_sha256` is SHA-256 over canonical JSON containing:

- nodes in declared order;
- each node's `id`, `title`, `description`, `files`, and `test_plan`;
- edges/dependencies in deterministic order;
- **excluding mutable node status**.

Node order is included because it determines tie-breaking among otherwise-ready nodes.

### Node definition

`node_sha256` is SHA-256 over canonical JSON containing:

- the node's immutable fields;
- its sorted dependency IDs.

### Canonical JSON

Use UTF-8 JSON with deterministic separators and key ordering. Lists whose order is semantically
meaningful remain ordered; set-like dependencies are sorted.

## Binding handshake

### Builder artifact logging

For schema-2 runs:

- `builder_output` requires `--file` and a non-empty payload;
- `log --round` must equal the CLI-derived current round for that gate;
- logging is rejected after that gate has a terminal event;
- the event records `artifact_sha256`.

Multiple Builder outputs in the current non-terminal round are allowed; the latest is the candidate
artifact.

### `bindings` command

Add:

```text
crucible bindings --run RUN --gate GATE --round N
```

It validates the stage prerequisites and emits machine-readable JSON:

```json
{
  "artifact_sha256": "...",
  "dag_sha256": "...",
  "node_sha256": "..."
}
```

Fields vary by gate:

| Gate | Required bindings |
|---|---|
| `reproduce` | artifact |
| `plan` | artifact + DAG |
| `dep:<id>` | artifact + DAG + node |
| `final` | artifact + DAG |

The orchestrator appends this JSON to the Critic seed as trusted CLI metadata.

### Critic verdict schema

For schema-2 runs, the verdict JSON must echo the binding fields returned by `bindings`. The CLI
rejects a missing or mismatched field before logging the verdict or making a decision.

This proves that the verdict refers to the same artifact/DAG/node identity selected by the CLI.

### Terminal events

`gate_consensus`, `gate_proceeded_with_flags`, and `gate_capped` persist the effective binding fields
alongside gate/round/outcome.

`critic_verdict` also records the validated bindings for readable provenance.

## Artifact immutability

- `load-dag` is allowed only before PLAN reaches a terminal outcome.
- `--force` may replace/reset a pre-consensus DAG, but never a DAG whose PLAN gate concluded.
- `log` rejects Builder/Critic output for a terminal gate.
- `show-plan` finds the exact pre-terminal Builder event whose `artifact_sha256` matches the PLAN
  terminal event and verifies the current `dag_sha256`. It never selects artifacts merely by round.
- A dependency terminal event is valid only for the current DAG/node bindings.
- `set-status ... done` recomputes those bindings and rejects stale consensus.

Any changed plan/DAG after PLAN acceptance requires a fresh run.

## Workflow state machine

Create shared workflow helpers (in `integrity.py` or a focused `workflow.py`) used by both CLI and
reporting. Do not duplicate stage logic.

### Required phase order

For schema-2 runs:

1. If `reproduce_gate` is true: REPRODUCE must reach `gate_consensus`.
2. PLAN may then reach consensus/proceed-with-flags with valid artifact + DAG bindings.
3. If `human_approval` is true: the exact accepted plan/DAG must receive a recorded approval.
4. Dependency work proceeds in DAG order.
5. If `final_review` is true: FINAL may run only after every node is `done`.

### `approve-plan` command

Add:

```text
crucible approve-plan --run RUN
```

It:

- requires `human_approval: true`;
- requires an accepted, currently bound PLAN gate;
- records `plan_approved` with the accepted plan/DAG hashes;
- rejects duplicate or stale approval.

If approval is disabled, the command rejects rather than adding meaningless provenance.

Skills call it only after the human explicitly approves.

### Gate prerequisites

- `reproduce` is accepted only when `reproduce_gate: true`.
- PLAN verdict/logging requires accepted REPRODUCE when configured.
- `dep:<id>` logging/bindings/verdict require:
  - accepted and currently bound PLAN;
  - recorded current approval when configured;
  - the node in `in_progress` or `in_review`;
  - all dependencies `done`.
- FINAL logging/bindings/verdict require:
  - `final_review: true`;
  - accepted/bound PLAN and configured approval;
  - every node `done`;
  - every done node either has a current bound accepted dependency gate or a recorded forced
    override.
- `next` refuses to schedule work before PLAN/approval prerequisites.

### Legal node transitions

Normal transitions:

```text
pending -> in_progress | blocked
in_progress -> in_review | done | blocked | pending
in_review -> in_progress | done | blocked | pending
blocked -> pending
done -> (terminal)
```

`done` requires current bound dependency consensus/proceed-with-flags and normally begins from
`in_progress` or `in_review`.

`--force --status done` remains the explicit reviewed-gate bypass:

- requires a rationale;
- requires accepted/bound PLAN and configured approval;
- may start from a non-done state;
- records current DAG/node hashes;
- remains `FLAGGED` in the report.

`--force` does not permit changing a `done` node or replacing an accepted DAG.

## Configuration-aware report

For schema-2 runs, `render_markdown` passes the immutable configuration from `run_start` into shared
workflow validation.

### Status precedence

1. `LEGACY / UNVERIFIED` — pre-schema-2 run.
2. `INVALID` — binding mismatch, illegal order, stale approval, or impossible transition in the log.
3. `BLOCKED` — a required gate capped.
4. `FLAGGED` — proceeded-with-flags or forced node completion.
5. `CLEAN` — every configured phase is present, ordered, accepted, and currently bound; all nodes
   are done.
6. `IN PROGRESS` — required phases/nodes remain incomplete.

The summary lists missing/invalid required phases explicitly.

### Required checks

- configured REPRODUCE presence and order;
- exact PLAN artifact/DAG binding;
- configured approval binding;
- every dependency's artifact/DAG/node binding;
- dependency terminal before node `done`;
- configured FINAL presence after all node completion;
- no accepted gate followed by output/DAG substitution.

The report remains deterministic from the append-only run log plus current DAG. It does not claim
tamper resistance.

## Skill protocol updates

All three skills (`crucible`, `deep-dive`, `pr-review`) must:

1. log the Builder/merged artifact;
2. call `crucible bindings` for that gate/round;
3. include the exact JSON in the Critic seed;
4. require the Critic verdict to echo the binding fields;
5. record human approval with `crucible approve-plan` when enabled.

Role prompts and verdict schemas include the binding fields. Platform notes specify that bindings are
trusted CLI metadata—not content copied from the reviewed artifact.

## Error handling

- Missing Builder artifact: reject before dispatch/verdict.
- Missing/mismatched Critic binding: reject without logging a verdict.
- DAG change after PLAN terminal: reject even with `--force`.
- Stale node terminal at `done`: reject and require a fresh review/run as applicable.
- Missing configured phase: later stage commands reject; report remains `IN PROGRESS`.
- Out-of-order or mismatched historical event in a schema-2 log: report `INVALID`.
- Legacy mutation: reject with a clear fresh-run instruction.

No broad catch or success-shaped fallback is added.

## Testing

### Reproduction contracts

Add end-to-end CLI tests for all confirmed subclaims:

- same-round post-consensus Builder output rejected and cannot replace approved plan;
- post-consensus DAG replacement rejected;
- same-ID/different-node stale consensus rejected;
- FINAL before all nodes done rejected;
- dependency verdict while `pending` rejected;
- direct `pending -> done` rejected;
- configured REPRODUCE/approval/FINAL omission prevents `CLEAN`.

### Binding tests

- canonical digest stability and status exclusion;
- semantic DAG/node changes alter hashes;
- `bindings` output by gate;
- verdict missing/mismatched hashes rejected without log mutation;
- terminal events persist validated hashes;
- `show-plan` selects exact bound artifact.

### State/report tests

- legal and illegal transitions;
- approval command and stale approval;
- required phase ordering;
- config-aware status precedence;
- legacy report/mutation behavior;
- forced override remains flagged;
- existing normal Crucible/deep-dive/pr-review workflows remain green after adding binding handshakes.

### Verification

Run targeted module/CLI/report/skill tests, then the full Python 3.11-compatible suite and
`scripts/check.py`.

## Documentation

Update:

- README workflow and safety guarantees;
- `docs/cli.md` for schema version, `bindings`, `approve-plan`, transition and legacy behavior;
- `SECURITY.md`;
- all three skills and references;
- CHANGELOG under `## [Unreleased]`;
- implementation plan and this spec status.
