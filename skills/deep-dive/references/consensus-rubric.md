# Consensus & stop criteria — symmetric deep dive

A thread (and the plan, and the final review) **loops**: both peers investigate → one peer serializes
the merged candidate finding set → **both peers review it** → repeat. Consensus is decided
**deterministically by `crucible verdict`** — never eyeballed, never negotiated in prose.

## Consensus = both peers sign off, grounded

- **Both peers review the merged set every round.** The round's recorded `critic_verdict` is the
  **union** of both peers' findings on that merged set (deduped by id). It is `APPROVE` **iff
  neither** peer has an open finding whose severity is in `blocking_severities` (default
  `["blocker", "major"]`); otherwise `REQUEST_CHANGES`.
- Because both peers review **every** round, reaching `CONSENSUS` means both peers — each having
  independently investigated the actual code/data — signed off on the *same* grounded finding set.
  This holds even for a one-round thread: consensus can never occur after only one peer reviewed.
- **Consensus is not a vote and not an average.** Two peers agreeing is not enough on its own; the
  finding set is accepted because it is **grounded in re-verifiable evidence** (each finding cites a
  `file:line` / data locator either peer reproduced). A disagreement is settled by **returning to the
  cited source**, not by out-voting, splitting the difference, or averaging two numbers.
- Findings whose severity is in `defer_severities` (default `["minor", "nit"]`) do not block and may
  be deferred.

## Resolving a dispute — no `wontfix`

When one peer disputes another's finding, it is resolved **only** by evidence: the disputed claim is
corrected or withdrawn against the cited source, so on the next round **both peers' union verdict**
no longer lists it. A blocking peer dispute is **never** cleared with `--resolutions`/`wontfix`.

That Crucible-CLI rebuttal path exists for the asymmetric Builder→Critic flow; in a symmetric deep
dive it would let one peer **unilaterally dismiss** the other peer's dispute without the counterpart
ever approving — breaking equal-peer consensus. So the deep-dive skill **never** passes
`--resolutions` with `wontfix` for a `blocker`/`major` finding. (Only `minor`/`nit` may be deferred,
per `defer_severities`.) The deterministic decision is always a plain `crucible verdict`.

## Round cap — disagreement is flagged, never forced

A gate stops on the **first** of:

1. **Consensus** — no open blocking findings in the union verdict (`crucible verdict` → `CONSENSUS`).
2. **Round cap** — the round index reaches `max_rounds_plan` (plan) or `max_rounds_dep` (each
   investigation thread; also the final review). Default 5 each.

At the cap without reconciliation, `on_cap` decides — but a genuine peer disagreement is **surfaced,
never forced into a false consensus**:

- `halt` (default): stop and surface the unresolved dispute to the human. `crucible verdict` returns
  `CAPPED`. Record it as a **flagged unresolved finding stating both peers' positions + their
  citations**, so the human sees exactly where and why the two models diverged.
- `proceed_with_flags`: continue, tagging the thread/run with the unresolved findings in the report
  (`crucible verdict` → `PROCEED_WITH_FLAGS`). The same **both-positions + citations** record is
  carried as a flag.

Never manufacture agreement to clear a cap. Two well-grounded but conflicting reads of the evidence
is itself a first-class finding the user asked for — report it, with both sides and where to look.
