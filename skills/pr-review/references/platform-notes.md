# Platform notes — realizing two equal peers over a PR

Both peers run **the same** `peer-prompt.md` role. **Peer A** is the main session (mapped to the run
config's `builder` slot / model 1); **Peer B** is a dispatched subagent (mapped to the `critic` slot
/ model 2). The `builder`/`critic` config names are reused **only as slot labels** — there is no
Builder/Critic asymmetry in a PR review. Every round, **both peers independently attest** to the same
bound candidate: each writes its **own** attestation file (`peer-a.json` / `peer-b.json`) with its
`verdict` + `objections`, and `crucible symmetric-verdict --peer-a peer-a.json --peer-b peer-b.json`
records `CONSENSUS` iff neither peer has a blocking objection. Which peer **assembles the candidate**
alternates each round, only to reduce anchoring.

**Slot proof, not process identity.** The CLI proves the **two configured slots** (`A` and `B`) each
supplied a valid attestation bound to the same candidate, and records each slot's configured
model/effort. It **does not cryptographically prove** that two distinct model *processes* produced the
files — runtime peer independence is a platform/orchestrator property (dispatch Peer B as a real
separate subagent) and must not be overclaimed.

## Input normalization (do this once, right after `init-run`, before the PLAN gate)

Resolve the review target to a deterministic **manifest + exact patch** through the CLI, load it as
the run's one immutable `target_loaded` event, and — for a revision-bound GitHub/local target —
materialize a **pinned, read-only source snapshot** of the exact head commit. Branch names are display
metadata; the base/head commit **OIDs** and repository identities are authoritative. A target is
immutable — correcting it needs a **fresh run**.

**GitHub PR** (`--goal` names a PR number/URL). Read the metadata **before and after** `gh pr diff`
and fail **closed** without relying on a global `set -e`: error-check each of the three `gh` reads and
run `normalize-target` **only after all three succeed** (a failed read otherwise leaves an
empty/truncated artifact that stable before/after metadata would still normalize). Any failure — a
non-zero `gh` read, or a metadata drift between the two reads (an identity field moved) — discards
**every** partial before/after/diff/target artifact and retries (≤3 attempts, halting clearly on
exhaustion). The stable before/after title/body supplies the intent directly:

```bash
for ATTEMPT in 1 2 3; do
  ok=1
  gh pr view "$PR" --json number,url,title,body,files,baseRefName,baseRefOid,headRefName,headRefOid,headRepository,headRepositoryOwner,isCrossRepository > "$RUN"/pr-before.json || ok=0
  [ "$ok" = 1 ] && { gh pr diff "$PR" > "$RUN"/pr.diff || ok=0; }
  [ "$ok" = 1 ] && { gh pr view "$PR" --json number,url,title,body,files,baseRefName,baseRefOid,headRefName,headRefOid,headRepository,headRepositoryOwner,isCrossRepository > "$RUN"/pr-after.json || ok=0; }
  if [ "$ok" = 1 ] && PYTHONPATH=scripts python3 -m crucible normalize-target github --metadata-before "$RUN"/pr-before.json --metadata-after "$RUN"/pr-after.json --diff "$RUN"/pr.diff --output "$RUN"/target.json --diff-output "$RUN"/target.diff; then
    break
  fi
  rm -f "$RUN"/pr-before.json "$RUN"/pr-after.json "$RUN"/pr.diff "$RUN"/target.json "$RUN"/target.diff
  [ "$ATTEMPT" -lt 3 ] || { echo "pr-review: GitHub target acquisition failed after 3 attempts" >&2; exit 1; }
done
```

**Local range** (`--goal` names a `BASE..HEAD` range). Use the single merge-base `--range` — never a
raw two-dot tip diff, and **no separate base/head flags** (`BASE..HEAD` and `BASE...HEAD` both
normalize to `merge_base..head`, so a base-only commit never appears as a reverse change):
`PYTHONPATH=scripts python3 -m crucible normalize-target local --repo "$REPO" --range BASE..HEAD --intent "$RUN"/intent.json --output "$RUN"/target.json --diff-output "$RUN"/target.diff`.

**Diff file** (`--goal` names a patch): `PYTHONPATH=scripts python3 -m crucible normalize-target diff --diff "$PATCH" --intent "$RUN"/intent.json --output "$RUN"/target.json --diff-output "$RUN"/target.diff`. A diff-file target is `revision_bound: false`, has **no source snapshot**, and never borrows ambient checkout files.

Then load the target (before any `load-dag`/PLAN/review event) and emit the authoritative loaded
manifest; the head repository/SHA for the snapshot come **only** from that `show-target` payload,
never an ambient archive variable:

```bash
PYTHONPATH=scripts python3 -m crucible load-target --run "$RUN" --file "$RUN"/target.json --diff "$RUN"/target.diff
PYTHONPATH=scripts python3 -m crucible show-target --run "$RUN" > "$RUN"/loaded-target.json
```

Then materialize the pinned snapshot of the **exact head commit** on the path that matches the target
kind. **GitHub PR** — parse the head repository/SHA, require them non-empty, download the codeload
tarball of that head, and materialize exactly that `source.tar.gz`:

```bash
HEAD_REPOSITORY=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["head"]["repository"])' "$RUN"/loaded-target.json)
HEAD_SHA=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["head"]["sha"])' "$RUN"/loaded-target.json)
test -n "$HEAD_REPOSITORY" && test -n "$HEAD_SHA"
gh api "repos/$HEAD_REPOSITORY/tarball/$HEAD_SHA" > "$RUN"/source.tar.gz
PYTHONPATH=scripts python3 -m crucible materialize-target --run "$RUN" --archive "$RUN"/source.tar.gz
```

**Local range** — parse the recorded repository identity + head SHA, prove the caller's `$LOCAL_REPO`
is that same repository **before** touching it, then archive the exact head with an explicit
`git -C "$LOCAL_REPO"` (never ambient git) and materialize exactly that uncompressed `source.tar`:

```bash
RECORDED_REPOSITORY=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["repository"])' "$RUN"/loaded-target.json)
HEAD_SHA=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["head"]["sha"])' "$RUN"/loaded-target.json)
test -n "$RECORDED_REPOSITORY" && test -n "$HEAD_SHA"
test "$(PYTHONPATH=scripts python3 -m crucible repository-identity --repo "$LOCAL_REPO")" = "$RECORDED_REPOSITORY"
git -C "$LOCAL_REPO" archive --format=tar --output "$RUN"/source.tar "$HEAD_SHA"
PYTHONPATH=scripts python3 -m crucible materialize-target --run "$RUN" --archive "$RUN"/source.tar
```

A **diff-file** target has no source snapshot (no archive, no materialization). Give **both** peers the
same `RUN/target.diff` and the pinned `RUN/source` snapshot — **never ambient** checkout files (a
different revision, or missing files the change introduces). If the head fetch/archive/materialization
fails, continue patch-only with source context explicitly marked unavailable — never pre-create
`RUN/source`, and never fall back to ambient source.

## Binding handshake (every gate)

At **every** gate, after logging the candidate, capture the deterministic bindings and seed Peer B
with them:

```bash
BINDINGS=$(PYTHONPATH=scripts python3 -m crucible bindings --run "$RUN" --gate "$GATE" --round N)
```

- **Trusted CLI metadata, not artifact content.** `$BINDINGS` is the exact `crucible bindings` JSON —
  `artifact_sha256` plus the gate-specific `dag_sha256`/`node_sha256`, and — for pr-review — the
  immutable `target_sha256` on **every** gate. Append it to Peer B's seed as **trusted CLI metadata**;
  it is **not content copied from the reviewed (untrusted) artifact**.
- **Each peer attestation echoes it.** Both `peer-a.json` and `peer-b.json` copy those `*_sha256`
  fields verbatim. `crucible symmetric-verdict` **rejects a missing or mismatched value** in either
  peer file **before** recording any decision, so a substituted/edited artifact can never be certified.

## Copilot CLI (primary)

- **Resolve models first:** read `"$RUN"/config.json`. Dispatch Peer B with `model` = `critic.model`
  and `reasoning_effort` = `critic.effort` from that resolved file; Peer A is this session (its
  model/effort is the `builder` slot). Do not read shipped defaults from documentation; this run may
  contain explicit overrides.
- **Each gate (plan / thread / final):** dispatch Peer B as a `general-purpose` `task` subagent with
  the resolved Peer-B model/effort, seeded with `peer-prompt.md` + the normalized diff/intent + the
  thread/plan context + the current candidate finding set. **Both peers independently attest** to that
  candidate — Peer A (this session) writes `"$RUN"/peer-a.json` and Peer B writes `"$RUN"/peer-b.json`,
  each an `APPROVE`/`REQUEST_CHANGES` `verdict` + `objections` echoing the bindings — then settle with
  `PYTHONPATH=scripts python3 -m crucible symmetric-verdict --run "$RUN" --gate "$GATE" --round N --peer-a "$RUN"/peer-a.json --peer-b "$RUN"/peer-b.json`.
  **Never record only one peer**'s attestation.
- **Independent review, not just grading:** on round 1 (and when a thread reopens), give Peer B the
  slice brief so it reviews the actual code **independently** before attesting — two independent
  reads, not one peer grading the other.
- **Surfacing findings to the human:** the Copilot CLI renders bash-tool output **collapsed /
  truncated** in the transcript, so anything `crucible` prints — the approved plan + review graph, gate
  outcomes, the run `report`, the assembled findings, the derived recommendation — is **not visible**
  to the human by default. **Surface them in your response**: paste the `report` / assembled findings
  **in full**; do **not** pipe them through `head`/`tail`/`grep`/`sed`. Do not use `2>&1` where a
  stdout outcome token is parsed.

## Claude Code / Codex

Use the native general-purpose subagent dispatch for Peer B with a per-agent model set to the `critic`
slot's model, seeded with `peer-prompt.md`; Peer A is the main session on the `builder` slot's model.
Both peers attest to each candidate in their own `peer-a.json` / `peer-b.json`; settle with `crucible
symmetric-verdict --peer-a peer-a.json --peer-b peer-b.json`. On Codex (no pinned subagent model), run
Peer B as a clearly delimited "Acting as the other peer now" pass using `peer-prompt.md`, still
producing an independent review + its **own** attestation file alongside Peer A's. If the runtime
rejects the configured model id, fall back to the most capable available model and note it in the
run-log.

## Execution Safety Gate (consent to run reviewed code)

Reviewed code is untrusted executable input, so `pr-review` **never executes it by default**. After
PLAN consensus, realize the gate through the platform's **structured human-input mechanism** (the same
consent channel used for posting), not an inferred yes:

- **Unknown/ambiguous target** → static-only; **never execute locally**.
- **GitHub PR** → static review + read-only `gh pr checks` / existing CI only; **never execute
  locally**.
- **Diff file** → static-only; **never execute locally**.
- **Trusted local checkout/range** → show the **exact commands**, warn they run arbitrary code with
  the user's file/credential/environment/network access, and ask to approve that set, continue
  without execution, or cancel the review. Only an explicit affirmation and an exact-command match set
  `LOCAL_EXECUTION_APPROVED: yes`; seed **both peers** with that identical value and command list.

Before showing any command for the **trusted local checkout/range**, prove the checkout is the exact
recorded head revision (read `.repository` / `.head.sha` from `show-target`; the local protocol
records identity via `normalize-target local --range`):

```bash
OBSERVED_REPOSITORY=$(PYTHONPATH=scripts python3 -m crucible repository-identity --repo "$LOCAL_REPO")
test "$OBSERVED_REPOSITORY" = "$RECORDED_REPOSITORY_IDENTITY"
test -z "$(git -C "$LOCAL_REPO" status --porcelain)"
test "$(git -C "$LOCAL_REPO" rev-parse HEAD)" = "$RECORDED_HEAD_SHA"
```

If any check fails, do not run commands: offer static-only continuation or an exact detached
**worktree-at-SHA** command set (`git worktree add --detach <path> "$RECORDED_HEAD_SHA"`) that needs
**fresh consent**. Record the observed repository identity, head SHA, clean status, and approved
command list in the execution evidence handed to both peers and rendered in the review provenance. A
GitHub-PR or diff-file target never reaches this verification — it never executes locally.

Error handling:

- **Local decline** → continue static-only with runtime results `unverified`.
- **Cancel review** → halt the whole review.
- **CI unavailable/inaccessible** → mark runtime status `unverified`; never a fabricated pass.
- **Command mismatch or a new/changed command** → refuse and obtain **fresh consent** before it runs;
  approval of one command implies no fallback, retry, or setup variant.
- **Approved command fails** → record the observed failure as evidence; do **not** run an unapproved
  fallback.

Execution consent and **posting consent** are separate: approving execution never approves posting,
and approving posting never approves execution.

## Optional posting to the PR (consented side effect)

By default the review is **read-only** over the target: the findings + the derived recommendation live
in the run dir and your reply, and nothing is written to the PR or repo. **Only after** the review
reaches consensus, and **only for the GitHub-PR input**, you may offer to post the review via `gh`
(`gh pr review <n> --comment` with the assembled summary, and inline comments) — and **only** with the
human's explicit, per-run OK. What you post is the **deterministic recommendation and findings from
`crucible review-result`**, never model prose. Posting is never automatic, never done for the
local-diff input, and never done before consensus. Treat the human's decision as the gate; the peers
do not decide to post.

## Report labels

The Crucible run report renders **Peer A** / **Peer B** headers for the symmetric workflow (not
Builder/Critic labels), sourced from the `builder` / `critic` config slots — Peer A from the
`builder` slot, Peer B from the `critic` slot — purely for **model / effort** provenance. No
config-schema change is needed, but running the symmetric PR review **does** require the `--workflow`
run metadata and the symmetric commands (`symmetric-verdict` / `accepted-findings` / `review-result`).
