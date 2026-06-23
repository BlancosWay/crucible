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
  human. Do **not** proceed.
- `proceed_with_flags`: continue, but tag the node/run with the unresolved findings in the report.

## Rebuttals (`wontfix`)

When the Builder rebuts a finding with `wontfix` + rationale:

- Default (`strict_rebuttal: false`): the rebuttal clears the finding from the blocking set; it is
  still logged and shown in the report.
- `strict_rebuttal: true`: the finding stays blocking until the Critic **explicitly accepts** the
  rebuttal in a later round.

The deterministic decision (`CONSENSUS` / `CHANGES` / `CAPPED`) is computed by
`crucible verdict` — the skill never eyeballs it.
