---
description: Run a two-model adversarial planning + implementation workflow (Builder plans/implements, Critic reviews each dependency until consensus).
---

# /crucible

Invoke the **crucible** skill to run a two-model adversarial workflow for the user's goal.

Usage: `/crucible <goal>` — e.g. `/crucible add a Redis-backed rate limiter to the API`.

Follow `skills/crucible/SKILL.md` exactly: PLAN gate (plan + dependency tree, Critic-reviewed to
consensus) -> one IMPLEMENT gate per dependency (Builder implements, Critic reviews, loop to
consensus or cap) -> optional FINAL gate -> run report. Resolve models, effort, caps, and policies
from the `RUN/config.json` written by `init-run`; shipped values live in `config.defaults.json`.
Every gate decision is **bound** to the exact reviewed artifact (schema v2): the CLI hashes the
Builder artifact + DAG/node into SHA-256 bindings the Critic verdict must echo, records human approval
with `crucible approve-plan` when enabled, and freezes the accepted plan/DAG (a change needs a fresh
run; a pre-schema-2 legacy run is read-only, `LEGACY / UNVERIFIED`).
**Engineering tool — never proceed past a gate without consensus unless
`on_cap: proceed_with_flags`.**
