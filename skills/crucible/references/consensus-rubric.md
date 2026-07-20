# Consensus & stop criteria

A gate **loops**: Builder produces -> Critic reviews -> Builder revises -> ... The loop ends on
the **first** of these:

1. **Consensus** — there are **no open findings** whose severity is in `blocking_severities`
   (default `["blocker", "major"]`). The Critic reaches this by returning `APPROVE`; the Builder
   can also reach it from a `REQUEST_CHANGES` verdict by clearing every blocking finding via a
   `wontfix` rebuttal or (for `defer_severities`) a deferral. Findings of severity in
   `defer_severities` (default `["minor", "nit"]`) may be deferred and do not block.
2. **Round cap** — the round index reaches `max_rounds_plan` (PLAN gate) or `max_rounds_dep`
   (each IMPLEMENT gate). Default 5 each.

## On reaching the cap without consensus — `on_cap`

- `halt` (default): stop the gate, persist the unresolved findings, and surface them to the
  human. Do **not** proceed. `crucible verdict` returns `CAPPED`.
- `proceed_with_flags`: continue past the gate, but tag the node/run with the unresolved findings
  in the report. `crucible verdict` returns `PROCEED_WITH_FLAGS` and records a
  `gate_proceeded_with_flags` event carrying the open finding ids.

## Rebuttals (`wontfix`)

When the Builder rebuts a finding with `wontfix` + rationale:

- Default (`strict_rebuttal: false`): the rebuttal clears the finding from the blocking set; it is
  still logged and shown in the report.
- `strict_rebuttal: true`: the finding stays blocking until the Critic **explicitly accepts** the
  rebuttal in a later round.

The deterministic decision (`CONSENSUS` / `CHANGES` / `CAPPED` / `PROCEED_WITH_FLAGS`) is computed
by `crucible verdict` — the skill never eyeballs it. Pass the Builder's per-finding resolutions via
`crucible verdict --resolutions "$RUN"/res.json` (`{"F1": {"resolution": "wontfix", "rationale": "…"}}`;
a `wontfix`/`deferred` clears a finding without a fix, so it must use the object form with a non-empty
`rationale` — a bare `"wontfix"`/`"deferred"` is rejected); `decide()` then applies
`defer_severities` and `strict_rebuttal` and records a `builder_resolution` event.

## A decision is bound to the reviewed artifact

Consensus is recorded only against the **exact** artifact/DAG/node the Critic reviewed. The verdict
JSON must **echo** the `crucible bindings` fields (`artifact_sha256`, plus `dag_sha256` and — for a
`dep:<node>` gate — `node_sha256`) as trusted CLI metadata; `crucible verdict` rejects a missing or
mismatched binding **before** it records any outcome. So `CONSENSUS`/`PROCEED_WITH_FLAGS` cannot
certify a substituted or edited artifact, and the accepted plan/DAG/node is immutable thereafter — a
change requires a fresh run.

## Undischarged load-bearing assumptions block consensus

A **load-bearing analytical claim** — one that gates durable, cross-version, or concurrent state, or
justifies *skipping* work or tests — that the Builder tagged `assumption`, or that the Critic flagged
as unproven, is an **open blocking finding** (`blocker`/`major`, per the run's `blocking_severities`)
until it is independently **derived** or confirmed. Because its severity is blocking it is **not
deferrable** — `defer_severities` (default `minor`/`nit`) never clears it. A `wontfix` rebuttal is
legitimate **only** when it *supplies the missing derivation* (turning the `assumption` into
`derived`); the Critic must reject a bare "out of scope" / "trust me" `wontfix` of such a claim and
re-raise it. Prose alone cannot override rebuttal semantics — under the default `strict_rebuttal:
false` a `wontfix` still clears any finding — so for deterministic enforcement (a `wontfix` stays
blocking until the Critic explicitly accepts it) run with `strict_rebuttal: true`. Since consensus
requires no open blocking findings, the gate cannot settle while one remains.
