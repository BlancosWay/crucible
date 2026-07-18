# Platform notes — realizing two equal peers over a PR

Both peers run **the same** `peer-prompt.md` role. **Peer A** is the main session (mapped to the run
config's `builder` slot / model 1); **Peer B** is a dispatched subagent (mapped to the `critic` slot
/ model 2). The `builder`/`critic` config names are reused **only as slot labels** — there is no
Builder/Critic asymmetry in a PR review. Every round, **both peers independently review** the merged
candidate finding set, and one peer serializes the **deduped union** of both peers' findings into the
single verdict JSON the CLI consumes (`APPROVE` iff neither peer has a blocking finding). Which peer
serializes alternates each round, only to reduce anchoring.

## Input normalization (do this once, before the PLAN gate)

Resolve the review target into one triple — `diff`, `changed-files`, `intent` — so the rest of the
flow is source-agnostic:

- **GitHub PR** (`--goal` names a PR number/URL): `gh pr view <n> --json title,body,files,headRefName,baseRefName`
  for the changed files + **stated intent** (title/body), and `gh pr diff <n>` for the diff. Both
  peers may also read the PR's linked issues for intent.
- **Local diff** (`--goal` names a `base..head` range or a diff file):
  - a **range** → `git diff <range>` for the diff and `git diff --name-only <range>` for the changed
    files; intent from `git log <range>` (commit messages) and/or user-supplied text.
  - a **diff file** → read the file itself as the diff and derive the changed files from its patch
    headers (`git apply --numstat -- <file>`, or the `+++`/`---` lines); intent from user-supplied text.

Give **both** peers the same normalized triple, and have both read the surrounding real code (the full
changed files and their callers/callees), not just the patch hunks.

## Copilot CLI (primary)

- **Resolve models first:** read `"$RUN"/config.json`. Dispatch Peer B with `model` = `critic.model`
  and `reasoning_effort` = `critic.effort` from that resolved file; Peer A is this session (its
  model/effort is the `builder` slot). Do not read shipped defaults from documentation; this run may
  contain explicit overrides.
- **Each gate (plan / thread / final):** dispatch Peer B as a `general-purpose` `task` subagent with
  the resolved Peer-B model/effort, seeded with `peer-prompt.md` + the normalized diff/intent + the
  thread/plan context + the current merged candidate set. **Both peers independently review** that
  merged set — Peer A (this session) reviews it directly and Peer B reviews it in its dispatch — and
  you serialize the **deduped union** of both peers' findings into the verdict JSON
  (`APPROVE`/`REQUEST_CHANGES` + findings). **Never record only one peer's** review.
- **Independent review, not just grading:** on round 1 (and when a thread reopens), give Peer B the
  slice brief so it reviews the actual code **independently** before reviewing the merged set — two
  independent reads, not one peer grading the other.
- **Surfacing findings to the human:** the Copilot CLI renders bash-tool output **collapsed /
  truncated** in the transcript, so anything `crucible` prints — the approved plan + review graph, gate
  outcomes, the run `report`, the assembled findings, the derived recommendation — is **not visible**
  to the human by default. **Surface them in your response**: paste the `report` / assembled findings
  **in full**; do **not** pipe them through `head`/`tail`/`grep`/`sed`. Do not use `2>&1` where a
  stdout outcome token is parsed.

## Claude Code / Codex

Use the native general-purpose subagent dispatch for Peer B with a per-agent model set to the `critic`
slot's model, seeded with `peer-prompt.md`; Peer A is the main session on the `builder` slot's model.
Both peers review each merged set; serialize the union verdict. On Codex (no pinned subagent model),
run Peer B as a clearly delimited "Acting as the other peer now" pass using `peer-prompt.md`, still
producing an independent review that is unioned with Peer A's. If the runtime rejects the configured
model id, fall back to the most capable available model and note it in the run-log.

## Optional posting to the PR (consented side effect)

By default the review is **read-only** over the target: findings + the derived recommendation live in
the run dir and your reply, and nothing is written to the PR or repo. **Only after** the review
reaches consensus, and **only for the GitHub-PR input**, you may offer to post the review via `gh`
(`gh pr review <n> --comment` with the assembled summary, and inline comments) — and **only** with the
human's explicit, per-run OK. Posting is never automatic, never done for the local-diff input, and
never done before consensus. Treat the human's decision as the gate; the peers do not decide to post.

## Report labels

The Crucible run report's `Builder` / `Critic` labels correspond to **Peer A / Peer B** — cosmetic
only, from the config slots. No CLI or config change is needed to run the symmetric PR review.
