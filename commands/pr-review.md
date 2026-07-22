---
description: Run a two-model symmetric adversarial PR review (two equal peers review a GitHub PR or a local diff independently against the real code, cross-examine, and converge on an evidence-grounded consensus finding set with a derived Approve/Comment/Request-changes recommendation).
---

# /pr-review

Invoke the **pr-review** skill to run a two-model **symmetric** adversarial review of a pull request
or a diff against the actual code.

Usage: `/pr-review <pr-or-diff>` — e.g. `/pr-review #123`, `/pr-review https://github.com/org/repo/pull/123`,
or `/pr-review main..my-branch`.

Follow `skills/pr-review/SKILL.md` exactly: init the run with `--workflow pr-review` and normalize the
input (a GitHub PR via `gh`, a local merge-base `--range`, or a diff file) into an **immutable review
target** — pinned base/head commit OIDs + fork identity (GitHub), a merge-base range (local), or
patch-only for a diff file — loaded once with `crucible load-target` and, for GitHub/local, materialized
into a pinned read-only `RUN/source` snapshot of the exact head commit before PLAN -> PLAN gate (review
plan + review graph, both peers attest to consensus) -> one THREAD
gate per review concern (both peers review that slice independently against the real code, both attest
to the candidate finding set, loop to consensus or cap) -> optional FINAL gate (assembled from
`crucible accepted-findings`) -> `crucible review-result` (findings + derived recommendation) + run
report. Two **equal peers** (no Builder/Critic asymmetry); each round **both peers independently
attest** in their own `peer-a.json` / `peer-b.json` and `crucible symmetric-verdict --peer-a --peer-b`
decides — never the build-only `verdict` — and consensus is grounded in re-verifiable citations, never
a vote or an average. Resolve models, effort, caps, and policies from the `RUN/config.json` written by
`init-run`; shipped values live in `config.defaults.json`. Every **gate decision is bound to the
exact** candidate both peers reviewed (**schema v2**) and — for pr-review — the immutable review
`target_sha256`: the CLI hashes it into SHA-256 **bindings** that **each peer attestation must echo**,
and the accepted review plan/graph is frozen (a legacy pre-schema-2 run is read-only, `LEGACY /
UNVERIFIED`).

**Engineering tool — never advance a gate without consensus unless `on_cap: proceed_with_flags`, and
never clear a blocking peer objection with a rebuttal; resolve it against the cited source or flag both
positions.** The deliverable is the findings; the review is **read-only** over the target by default —
posting to the PR happens only for a GitHub PR, only after consensus, and only with your explicit OK.

**Execution safety — reviewed code is untrusted.** Running a reviewed change is code execution, so
`pr-review` never executes it by default. A **GitHub PR** target and a **diff file** target are
static/CI-only and **never execute locally**. A **local checkout/range** is static by default; after
PLAN consensus the Execution Safety Gate shows the **exact commands** and warns they run **arbitrary
code** with your file, credential, environment, and network access, then runs them only if you
explicitly approve that exact set. Declining continues the review static-only (runtime results
`unverified`); a **new or changed command** needs **fresh consent**; execution consent and posting
consent are separate.
