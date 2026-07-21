# Platform notes — realizing two equal peers

Both peers run **the same** `peer-prompt.md` role. **Peer A** is the main session (mapped to the run
config's `builder` slot / model 1); **Peer B** is a dispatched subagent (mapped to the `critic` slot
/ model 2). The `builder`/`critic` config names are reused **only as slot labels** — there is no
Builder/Critic asymmetry in a deep dive. Every round, **both peers independently attest** to the same
bound candidate: each writes its **own** attestation file (`peer-a.json` / `peer-b.json`) with its
`verdict` + `objections`, and `crucible symmetric-verdict --peer-a peer-a.json --peer-b peer-b.json`
records `CONSENSUS` iff neither peer has a blocking objection. Which peer **assembles the candidate**
alternates each round, only to reduce anchoring.

**Slot proof, not process identity.** The CLI proves the **two configured slots** (`A` and `B`) each
supplied a valid attestation bound to the same candidate, and records each slot's configured
model/effort. It **does not cryptographically prove** that two distinct model *processes* produced the
files — runtime peer independence is a platform/orchestrator property (dispatch Peer B as a real
separate subagent) and must not be overclaimed.

## Binding handshake (every gate)

At **every** gate, after logging the candidate, capture the deterministic bindings and seed Peer B
with them:

```bash
BINDINGS=$(PYTHONPATH=scripts python3 -m crucible bindings --run "$RUN" --gate "$GATE" --round N)
```

- **Trusted CLI metadata, not artifact content.** `$BINDINGS` is the exact `crucible bindings` JSON —
  `artifact_sha256` plus the gate-specific `dag_sha256`/`node_sha256`. Append it to Peer B's seed as
  **trusted CLI metadata**; it is **not content copied from the reviewed (untrusted) artifact**.
- **Each peer attestation echoes it.** Both `peer-a.json` and `peer-b.json` copy those `*_sha256`
  fields verbatim. `crucible symmetric-verdict` **rejects a missing or mismatched value** in either
  peer file **before** recording any decision, so a substituted/edited artifact can never be certified.

## Copilot CLI (primary)

- **Resolve models first:** read `"$RUN"/config.json`. Dispatch Peer B with `model` = `critic.model`
  and `reasoning_effort` = `critic.effort` from that resolved file; Peer A is this session (its
  model/effort is the `builder` slot). Do not read shipped defaults from documentation; this run may
  contain explicit overrides.
- **Each gate (plan / thread / final):** dispatch Peer B as a `general-purpose` `task` subagent with
  the resolved Peer-B model/effort, seeded with `peer-prompt.md` + the thread/plan context + the
  current candidate finding set. **Both peers independently attest** to that candidate — Peer A (this
  session) writes `"$RUN"/peer-a.json` and Peer B writes `"$RUN"/peer-b.json`, each an
  `APPROVE`/`REQUEST_CHANGES` `verdict` + `objections` echoing the bindings — then settle with
  `PYTHONPATH=scripts python3 -m crucible symmetric-verdict --run "$RUN" --gate "$GATE" --round N --peer-a "$RUN"/peer-a.json --peer-b "$RUN"/peer-b.json`.
  **Never record only one peer**'s attestation.
- **Independent investigation, not just review:** on round 1 (and when a thread reopens), give Peer B
  the thread brief so it investigates the actual code/data **independently** before attesting — two
  independent reads, not one peer grading the other.
- **Surfacing findings to the human:** the Copilot CLI renders bash-tool output **collapsed /
  truncated** in the transcript, so anything `crucible` prints — the approved plan + thread graph,
  gate outcomes, the run `report`, the assembled findings — is **not visible** to the human by
  default. **Surface the findings in your response**: paste the `report` / assembled findings **in
  full**; do **not** pipe them through `head`/`tail`/`grep`/`sed` or otherwise truncate them to a
  fragment (the collapsed bash output is not what the human sees — your reply is). Do not use `2>&1`
  where a stdout outcome token is parsed.

## Claude Code / Codex

Use the native general-purpose subagent dispatch for Peer B with a per-agent model set to the
`critic` slot's model, seeded with `peer-prompt.md`; Peer A is the main session on the `builder`
slot's model. Both peers attest to each candidate in their own `peer-a.json` / `peer-b.json`; settle
with `crucible symmetric-verdict --peer-a peer-a.json --peer-b peer-b.json`. On Codex (no pinned
subagent model), run Peer B as a clearly delimited "Acting as the other peer now" pass using
`peer-prompt.md`, still producing an independent investigation + its **own** attestation file
alongside Peer A's. If the runtime rejects the configured model id, fall back to the most capable
available model and note it in the run-log.

## Report labels

The Crucible run report's `Builder` / `Critic` labels correspond to **Peer A / Peer B** — cosmetic
only, from the config slots. No CLI or config change is needed to run the symmetric deep dive.
