# Crucible

> **Status: design phase (WIP).** This README is a placeholder concept sketch. The validated
> design will live in `docs/superpowers/specs/` before any feature code is written.

**Two-model adversarial planning and implementation, on top of [Superpowers](https://github.com/obra/superpowers).**

Crucible runs software work through a crucible: one model proposes, a second model
adversarially critiques — at the plan, at the dependency tree, and at every dependency as it's
implemented — looping through configured stages until the critic signs off (consensus) or a
round cap is hit.

## The idea

- **Model 1 (Builder)** — does the planning and the implementation.
- **Model 2 (Critic)** — adversarially critiques the plan and each implementation step.
- After planning, work is expressed as a **dependency tree (DAG)**; implementation walks it in
  topological order.
- **Each dependency** passes through an adversarial review loop: Builder implements → Critic
  attacks → Builder revises → … until the Critic reaches **consensus** or the configured
  **max rounds** is reached.
- Built **on Superpowers** — it orchestrates `brainstorming → writing-plans →
  subagent-driven-development`, injecting the two-model adversarial loop at each gate.

Defaults (configurable): Builder = Opus 4.8, Critic = GPT-5.5 (xhigh), max rounds = 5.

**Research/engineering tool — see the spec for full design.**
