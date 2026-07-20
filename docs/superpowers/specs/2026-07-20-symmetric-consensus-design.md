# Deterministic Symmetric Consensus and Review Results — Design

**Status:** proposed. **Type:** deterministic CLI/run-log capability for the `deep-dive` and
`pr-review` companion skills.

## Problem

Schema-v2 content bindings prove that a gate decision refers to the exact merged artifact, DAG, and
thread definition selected by the CLI. They do not represent the two additional claims made by the
symmetric skills:

1. **Both configured peer slots signed off.** The current `Verdict` schema contains one
   `APPROVE|REQUEST_CHANGES` label and no Peer A/Peer B attestations. One bound `APPROVE` is sufficient
   for CLI `CONSENSUS`.
2. **The accepted finding set and PR recommendation are deterministic state.** The merged target
   findings live in free-form `builder_output`; the structured verdict's blocking findings control
   gate progress. An accepted target blocker is therefore either invisible to deterministic state
   (artifact only → gate `CONSENSUS`/workflow `CLEAN`) or treated as an unresolved gate objection
   (`verdict.findings` → `CHANGES`/`CAPPED`). No engine code derives
   Approve/Comment/Request-changes from the accepted findings.

The engine must distinguish:

- **target findings** accepted as the investigation/review result; and
- **peer objections** to the completeness or correctness of that candidate finding set.

## Goal

For symmetric workflows:

1. The CLI requires two separately produced, slot-labelled peer attestation files for every round.
2. Both attestations refer to the same bound candidate artifact and are validated together before
   any decision event is recorded.
3. Gate progress is determined from peer objections—not from the severities of accepted target
   findings.
4. Dependency and FINAL finding sets are structured, validated, and persisted.
5. `pr-review` receives a deterministic `APPROVE|COMMENT|REQUEST_CHANGES` recommendation derived
   from the accepted finding set and unresolved objections.
6. Reports clearly separate workflow integrity (`CLEAN`, `FLAGGED`, etc.) from review recommendation.
7. The existing asymmetric Builder/Critic workflow remains unchanged.

## Trust boundary

The CLI proves that two distinct configured **slots** (`A` and `B`) supplied valid attestations bound
to the same artifact. It records each slot's configured model/effort from the run configuration.

It does **not** cryptographically prove that two separate model processes produced the files. Runtime
subagent independence remains a platform/orchestrator property and must not be overclaimed.

## Workflow kind

Add immutable run metadata:

```text
build | deep-dive | pr-review
```

`init-run` gains:

```bash
crucible init-run --goal GOAL --workflow build|deep-dive|pr-review
```

- default: `build`;
- recorded on `run_start` as `workflow`;
- existing schema-v2 runs without the field are interpreted as `build`;
- `verdict` accepts only `build`;
- `symmetric-verdict` accepts only `deep-dive` or `pr-review`.

This is run metadata, not a model/default configuration key, and does not require a schema bump.

## Structured candidate finding sets

PLAN remains a normal text artifact plus DAG. For symmetric dependency and FINAL gates, the logged
Builder/merged artifact must be UTF-8 JSON:

```json
{
  "summary": "Optional concise summary",
  "findings": [
    {
      "source_gate": "dep:auth",
      "id": "F1",
      "severity": "major",
      "location": "src/auth.py:42",
      "claim": "Refresh accepts an expired token.",
      "suggestion": "Reject expired refresh tokens."
    }
  ]
}
```

### Validation

- top-level object, optional string `summary`, required list `findings`;
- every finding uses the existing severity vocabulary;
- `source_gate`, `id`, `severity`, `location`, `claim`, and `suggestion` are non-empty strings;
- `(source_gate, id)` is unique within the set;
- dependency candidate findings must use that exact `dep:<thread>` as `source_gate`;
- PLAN artifacts are not parsed as finding sets.

### FINAL inclusion

The accepted FINAL finding set must contain every previously accepted dependency finding exactly,
keyed by `(source_gate, id)`. It may add cross-cutting findings only with `source_gate: "final"`.

Changing or dropping a prior accepted finding is rejected before peer attestations are recorded.

## Peer attestation schema

Each peer independently emits one JSON file:

```json
{
  "peer": "A",
  "gate": "dep:auth",
  "round": 1,
  "verdict": "APPROVE",
  "summary": "The candidate set is complete and grounded.",
  "objections": [],
  "artifact_sha256": "...",
  "dag_sha256": "...",
  "node_sha256": "..."
}
```

Fields:

- `peer`: exactly `A` or `B`;
- `gate`/`round`: exact command values;
- `verdict`: `APPROVE|REQUEST_CHANGES`;
- `summary`: string;
- `objections`: structured `Finding` objects describing defects/disputes in the **candidate set**;
- gate-specific schema-v2 bindings echoed exactly.

Consistency:

- `APPROVE` requires no blocking objection;
- `REQUEST_CHANGES` requires at least one blocking objection;
- objection IDs need only be unique within one peer file; the engine namespaces them by peer when
  aggregating (`A:F1`, `B:F1`).

## Atomic symmetric decision

Add:

```bash
crucible symmetric-verdict \
  --run RUN --gate GATE --round N \
  --peer-a PEER_A.json --peer-b PEER_B.json
```

The command:

1. requires symmetric workflow kind;
2. enforces existing schema/stage/round/terminal prerequisites;
3. computes the current artifact/DAG/node bindings;
4. parses the candidate finding set for dependency/FINAL gates;
5. validates both attestation files completely before any append;
6. requires one `peer: A` file and one `peer: B` file—no duplicates or swapped labels;
7. validates both files against the same current bindings;
8. computes the union of peer objections, namespaced by slot;
9. applies the existing blocking severities and round-cap/on-cap policy;
10. appends one `symmetric_verdict` event containing both parsed/raw attestations, configured
    model/effort provenance, aggregate objections, and bindings;
11. appends the existing terminal event (`gate_consensus`, `gate_capped`, or
    `gate_proceeded_with_flags`) with bindings.

No partial peer event is written if either file is invalid.

The existing `verdict` command rejects symmetric workflow runs with a message directing the
orchestrator to `symmetric-verdict`. Symmetric workflows never use `--resolutions`.

## Symmetric round outcome

Gate progress is based solely on **peer objections**:

- no blocking objection from either peer → `CONSENSUS`;
- blocking objection before cap → `CHANGES`;
- blocking objection at cap → `CAPPED` or `PROCEED_WITH_FLAGS`.

Therefore, a candidate finding set containing an accepted blocker can correctly reach gate
`CONSENSUS` when both peers attest that the set is accurate and complete.

## Accepted finding events

When a dependency or FINAL gate reaches `CONSENSUS`, append:

```json
{
  "event": "accepted_finding_set",
  "gate": "dep:auth",
  "round": 1,
  "payload": {"summary": "...", "findings": [...]},
  "artifact_sha256": "...",
  "dag_sha256": "...",
  "node_sha256": "..."
}
```

For `PROCEED_WITH_FLAGS`, persist the candidate finding set as accepted-with-flags and record the
unresolved peer objections. A `CAPPED`/halted gate does not create an accepted finding set.

The event is deterministic parsed state, not a report-time interpretation of free-form text.

## Accepted findings and FINAL assembly

Add:

```bash
crucible accepted-findings --run RUN
```

For symmetric runs it emits canonical JSON containing the deterministic union of accepted dependency
finding sets in DAG topological order. Every item retains `source_gate`.

The FINAL artifact starts from this output and may add `source_gate: final` findings. The CLI
validates that no accepted dependency finding was dropped or altered.

If FINAL review is disabled, the dependency union is the run's effective accepted finding set.

## Deterministic review result

Add:

```bash
crucible review-result --run RUN
```

Output:

```json
{
  "workflow": "pr-review",
  "recommendation": "REQUEST_CHANGES",
  "findings": [...],
  "unresolved_objections": [...]
}
```

### Effective finding set

- accepted FINAL finding set when FINAL reached consensus/proceeded-with-flags;
- otherwise the deterministic union of accepted dependency finding sets.

### PR recommendation

For `pr-review`:

- any accepted finding in `blocking_severities` → `REQUEST_CHANGES`;
- otherwise any accepted finding → `COMMENT`;
- no accepted findings → `APPROVE`;
- any unresolved blocking peer objection from a proceeded-with-flags or capped review →
  `REQUEST_CHANGES`.

For `deep-dive`, `recommendation` is omitted; the deterministic finding set is still returned.

`review-result` rejects `build` workflows.

## Report behavior

For symmetric workflows:

- header labels are **Peer A** and **Peer B**, using configured model/effort;
- each gate renders both peer attestations and namespaced objections;
- accepted findings render as structured findings, grouped by `source_gate`;
- workflow status retains its existing meaning (`CLEAN`, `FLAGGED`, etc.);
- a separate line renders `**Review recommendation:** ...` for `pr-review`;
- the report never treats `CLEAN` as synonymous with PR `APPROVE`.

The report uses the same engine helpers as `accepted-findings`/`review-result`; no duplicate
recommendation logic.

## Skill protocol changes

### deep-dive

- initialize with `--workflow deep-dive`;
- peers produce separate A/B attestation JSON files;
- dependency/FINAL merged artifacts use the structured finding-set schema;
- invoke `symmetric-verdict`, never `verdict`;
- use `accepted-findings` to assemble FINAL;
- use `review-result` as the deterministic findings deliverable.

### pr-review

- initialize with `--workflow pr-review`;
- same separate attestation and structured-finding protocol;
- invoke `review-result` for findings plus recommendation;
- optional GitHub posting uses the deterministic recommendation and findings, not model prose.

### build

The existing `crucible` skill continues using `verdict`; no peer attestation or finding-set schema is
introduced into the asymmetric Builder/Critic flow.

## Error handling

- symmetric command on `build` run: reject;
- asymmetric `verdict` on symmetric run: reject;
- one/missing/duplicate/wrong peer slot: reject before any append;
- malformed peer objections or inconsistent peer verdict: reject;
- binding mismatch in either peer file: reject;
- malformed dependency/FINAL finding set: reject;
- dependency candidate with wrong `source_gate`: reject;
- FINAL candidate missing/altering prior accepted finding: reject;
- `accepted-findings`/`review-result` on incomplete or wrong workflow: clear nonzero error where
  required; reports may show in-progress partial results without fabricating acceptance.

No broad catch or success-shaped fallback is added.

## Testing

### Deterministic peer quorum

- one peer file cannot invoke the command;
- duplicate/swapped peer slots rejected;
- one bound APPROVE + missing/invalid second peer never reaches consensus;
- both bound approvals produce consensus and one event containing both slots;
- configured model/effort provenance recorded for each slot;
- existing `verdict` cannot bypass symmetric mode.

### Accepted findings versus objections

- accepted blocker + two approvals → `CONSENSUS` and persisted blocker;
- same blocker as peer objection → `CHANGES`, then `CAPPED` at cap;
- peer objection IDs are namespaced and both retained;
- malformed/duplicate candidate findings rejected;
- proceeded-with-flags retains candidate plus unresolved objections.

### FINAL and result projection

- deterministic dependency union;
- FINAL cannot drop/alter dependency findings;
- FINAL may add only `source_gate: final`;
- PR blocker → `REQUEST_CHANGES`;
- only minor/nit → `COMMENT`;
- empty → `APPROVE`;
- workflow `CLEAN` and recommendation `REQUEST_CHANGES` coexist in report;
- deep-dive result omits recommendation.

### Protocol/docs

- both symmetric skills use `--workflow`, separate A/B files, `symmetric-verdict`,
  `accepted-findings`, and `review-result`;
- per-gate binding tests remain section-scoped;
- build skill remains on `verdict`;
- security/docs explicitly limit the peer proof to configured slots, not cryptographic process
  identity.

Run focused symmetric/CLI/report/protocol tests, Python 3.11 full suite, and `scripts/check.py`.

## Documentation

Update README, `docs/cli.md`, SECURITY, both symmetric skills/references/commands, CHANGELOG, and this
spec/implementation plan provenance.
