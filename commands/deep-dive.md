---
description: Run a two-model symmetric adversarial deep dive (two equal peers investigate the actual code/data independently, cross-examine, and converge on an evidence-grounded consensus finding set).
---

# /deep-dive

Invoke the **deep-dive** skill to run a two-model **symmetric** adversarial investigation of the
user's question against the actual code or data.

Usage: `/deep-dive <question>` — e.g. `/deep-dive how is rate limiting actually enforced across the API?`.

Follow `skills/deep-dive/SKILL.md` exactly: PLAN gate (investigation plan + thread graph, both peers
review to consensus) -> one THREAD gate per investigation thread (both peers investigate the real
code/data independently, both review the merged set, loop to consensus or cap) -> optional FINAL gate
-> run report + assembled findings. Two **equal peers** (no Builder/Critic asymmetry); the recorded
verdict each round is the union of both peers' findings, and consensus is grounded in re-verifiable
citations — never a vote or an average. Resolve models, effort, caps, and policies from the
`RUN/config.json` written by `init-run`; shipped values live in `config.defaults.json`. Every gate
decision is **bound** to the exact merged artifact both peers reviewed (schema v2): the CLI hashes it
into SHA-256 bindings the union verdict must echo, and the accepted plan/thread-graph is frozen (a
legacy pre-schema-2 run is read-only, `LEGACY / UNVERIFIED`).

**Engineering tool — never advance a gate without consensus unless `on_cap: proceed_with_flags`, and
never clear a blocking peer dispute with a rebuttal; resolve it against the cited source or flag both
positions.** The deliverable is the findings; the deep dive is read-only over the target.
