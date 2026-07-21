# Consensus & stop criteria — symmetric PR review

A thread (and the plan, and the final review) **loops**: both peers review → one peer assembles the
candidate finding set → **both peers independently attest to it** → repeat. Consensus is decided
**deterministically by `crucible symmetric-verdict`** — never eyeballed, never negotiated in prose.

## Consensus = both peers sign off, grounded

- **Both peers independently attest every round.** Each peer writes its own attestation file —
  `peer-a.json` and `peer-b.json` — carrying an `APPROVE`/`REQUEST_CHANGES` `verdict` and its
  `objections`. `crucible symmetric-verdict --peer-a peer-a.json --peer-b peer-b.json` records
  `CONSENSUS` **iff neither** peer has an open objection whose severity is in `blocking_severities`
  (default `["blocker", "major"]`); otherwise `CHANGES`. There is no single serialized union verdict.
- **Gate progress is decided from peer objections**, never from an accepted candidate finding's
  severity. An **objection** is a defect in the candidate; a **candidate finding** is a review result
  the peers accept. So a candidate that *accepts a blocker* still reaches `CONSENSUS` when both peers
  attest the set is accurate and complete — the engine keeps accepted findings and objections as
  separate state.
- Because both peers attest **every** round, reaching `CONSENSUS` means both peers — each having
  independently reviewed the actual code — signed off on the *same* grounded finding set. This holds
  even for a one-round thread: consensus can never occur after only one peer attested.
- **Consensus is not a vote and not an average.** Two peers agreeing is not enough on its own; the
  finding set is accepted because it is **grounded in re-verifiable evidence** (each finding cites a
  `file:line` either peer reproduced). A disagreement is settled by **returning to the cited source**,
  not by out-voting, splitting the difference, or averaging two severities.
- Objections whose severity is in `defer_severities` (default `["minor", "nit"]`) do not block and
  may be deferred (they still appear in the review as suggestions).

## A decision is bound to the reviewed artifact

Each peer attestation `crucible symmetric-verdict` adjudicates must **echo** the gate's `crucible
bindings` (`artifact_sha256`, plus `dag_sha256` and — for a `dep:<thread>` gate — `node_sha256`) as
trusted CLI metadata. A missing or mismatched binding in either peer file is **rejected before any
outcome** is recorded, so a settled decision is always bound to the exact candidate both peers
reviewed; the accepted review plan/graph and each reviewed thread are immutable thereafter (a change
requires a **fresh run**).

## Resolving a dispute — no `wontfix`

When one peer objects to the candidate, it is resolved **only** by evidence: the disputed claim is
corrected or withdrawn against the cited source, so on the next round **neither peer's attestation**
lists it. A blocking peer objection is **never cleared** with `--resolutions`/`wontfix`.

That Crucible-CLI rebuttal path exists for the asymmetric Builder→Critic `verdict` flow; a symmetric
review uses `crucible symmetric-verdict`, which takes **no** `--resolutions` — clearing a blocking
objection there would let one peer **unilaterally dismiss** the other peer's dispute without the
counterpart ever approving, breaking equal-peer consensus. (Only `minor`/`nit` objections may be
deferred, per `defer_severities`.) The deterministic decision is always a plain `crucible
symmetric-verdict` over the two peer files.

## Round cap — disagreement is flagged, never forced

A gate stops on the **first** of:

1. **Consensus** — no open blocking objection from either peer (`crucible symmetric-verdict` → `CONSENSUS`).
2. **Round cap** — the round index reaches `max_rounds_plan` (plan) or `max_rounds_dep` (each review
   thread; also the final review). Default 5 each.

At the cap without reconciliation, `on_cap` decides — but a genuine peer disagreement is **surfaced,
never forced into a false consensus**:

- `halt` (default): stop and surface the unresolved dispute to the human. `crucible symmetric-verdict`
  returns `CAPPED`. Record it as a **flagged unresolved finding stating both peers' positions + their
  citations**, so the human sees exactly where and why the two models diverged.
- `proceed_with_flags`: continue, tagging the thread/run with the unresolved objections in the report
  (`crucible symmetric-verdict` → `PROCEED_WITH_FLAGS`). The same **both-positions + citations**
  record is carried as a flag.

Never manufacture agreement to clear a cap. Two well-grounded but conflicting reads of the change is
itself a first-class result — report it, with both sides and where to look.

## The overall recommendation is derived, not voted

The deliverable is the consensus finding set. The single **Approve / Comment / Request-changes**
recommendation is a **deterministic projection** of the *accepted* finding set, computed by `crucible
review-result` — not a separate vote:

- any accepted finding whose severity is in `blocking_severities` (default `blocker`/`major`), or any
  unresolved blocking peer objection carried past a capped/proceeded gate → **REQUEST_CHANGES**;
- otherwise, any accepted finding (only `minor`/`nit`) → **COMMENT**;
- no accepted findings at all → **APPROVE**.

Deriving the recommendation from the same grounded finding set the CLI already adjudicated keeps
"consensus is not a vote" intact — the peers never separately ballot an overall verdict. The report's
`**Review recommendation:**` line renders this same `review-result` value, kept separate from the
workflow `CLEAN`/`FLAGGED` status.
