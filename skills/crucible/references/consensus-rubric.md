# Consensus & stop criteria

A gate **loops**: Builder produces -> Critic reviews -> Builder revises -> ... The loop ends on
the **first** of these:

1. **Consensus** — the Critic returns `APPROVE`, i.e. there are **no open findings** whose
   severity is in `blocking_severities` (default `["blocker", "major"]`). Findings of severity in
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
`crucible verdict --resolutions res.json` (`{"F1": "wontfix"}`); `decide()` then applies
`defer_severities` and `strict_rebuttal` and records a `builder_resolution` event.
