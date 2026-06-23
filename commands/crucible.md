---
description: Run a two-model adversarial planning + implementation workflow (Builder plans/implements, Critic reviews each dependency until consensus).
---

# /crucible

Invoke the **crucible** skill to run a two-model adversarial workflow for the user's goal.

Usage: `/crucible <goal>` — e.g. `/crucible add a Redis-backed rate limiter to the API`.

Follow `skills/crucible/SKILL.md` exactly: PLAN gate (plan + dependency tree, Critic-reviewed to
consensus) -> one IMPLEMENT gate per dependency (Builder implements, Critic reviews, loop to
consensus or cap) -> optional FINAL gate -> run report. Defaults: Builder = Opus 4.8, Critic =
GPT-5.5 (xhigh), 5 rounds per gate. **Engineering tool — never proceed past a gate without
consensus unless `on_cap: proceed_with_flags`.**
