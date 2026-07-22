# PR Review Target Binding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve every pr-review input to immutable target identity and bind that identity into every symmetric gate so evidence, citations, reports, and consented execution refer to the reviewed revision.

**Architecture:** A new `crucible.target` module owns target schemas, exact patch identity, local merge-base normalization, GitHub metadata normalization, and confined source-archive extraction. `load-target` records one immutable target event before PLAN; `target_sha256` then rides on every pr-review binding and peer attestation. The pr-review skill consumes the executable normalization commands and reads only the pinned source snapshot.

**Tech Stack:** Python 3.11+ stdlib dataclasses/JSON/hashlib/subprocess/tarfile/pathlib, argparse CLI, schema-v2 JSONL provenance, Git/gh, pytest, Markdown protocol guards.

## Global Constraints

- Target binding applies only to workflow `pr-review`; build and deep-dive binding shapes stay unchanged.
- Only an absent workflow-specific target before any DAG/PLAN work is `missing`; downstream work without a target is `invalid`.
- Exactly one canonical `target_loaded` event is allowed, before `load-dag`, PLAN output, or protocol events.
- Every pr-review PLAN/thread/FINAL binding and peer attestation includes exact `target_sha256`.
- GitHub PR metadata records base/head repository identity plus immutable base/head OIDs and fork status.
- GitHub metadata is read before and after fetching the compare metadata and the merge-base/head
  snapshots; the immutable patch is **derived** PR-style from the merge-base snapshot (base
  `repository@merge_base_commit.sha`, the fork point from the base repo's exact-OID compare endpoint) →
  the head snapshot (never `gh pr diff` or a base-tip two-dot diff, so a base-only commit after the fork
  never appears as a reverse change), and the acquisition fails closed (each `gh pr view` read, the
  `gh api` compare fetch, the merge-base parse, and both `gh api` archive fetches are error-checked,
  `normalize-target` runs only after all succeed), and any failed read/fetch, malformed/mismatched
  compare payload, or drifted identity field discards every partial artifact and retries, at most three
  times, halting clearly on exhaustion.
- Local range normalization always diffs `merge_base_sha..head_sha`, regardless of input spelling `..` or `...`.
- Diff-file targets are patch-bound only (`revision_bound: false`) and never borrow ambient source context.
- GitHub PR and diff-file targets never execute locally.
- Trusted-local execution requires a clean checkout at the recorded local-range `head.sha` and fresh exact-command consent.
- Source archive extraction rejects path escapes, links, special files, duplicate normalized paths, more than 100,000 members, and more than 1 GiB declared file data.
- Source materialization is one-shot into an absent destination; it never deletes or replaces visible source.
- Every pr-review mutation/certification command except target creation/loading uses one shared loaded-target guard.
- No config-schema change; run-log schema remains v2 with additive workflow-specific target state.

---

### Task 1: Add target schemas, normalization, and safe materialization

**Files:**
- Create: `scripts/crucible/target.py`
- Modify: `scripts/crucible/cli.py`
- Create: `tests/test_target.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/validate_structure.py`

**Interfaces:**
- Produces:
  - `TARGET_VERSION = 1`
  - `TARGET_KINDS = ("github-pr", "local-range", "diff-file")`
  - `MAX_ARCHIVE_MEMBERS = 100_000`
  - `MAX_ARCHIVE_BYTES = 1 << 30`
  - `ReviewTarget.from_dict(data) -> ReviewTarget`
  - `ReviewTarget.to_dict() -> dict[str, Any]`
  - `target_sha256(target: ReviewTarget) -> str`
  - `target_from_events(events) -> ReviewTarget | None`
  - `target_event_issues(events, workflow) -> list[str]`
  - `normalize_github_target(metadata_before, metadata_after, diff) -> ReviewTarget`
  - `normalize_local_target(repo, range_text, intent) -> tuple[ReviewTarget, bytes]`
  - `normalize_diff_target(diff, intent) -> ReviewTarget`
  - `normalized_repository_identity(repo) -> str`
  - `safe_extract_source_archive(archive, destination) -> None`
  - CLI commands `normalize-target`, `repository-identity`, `load-target`, `show-target`,
    `materialize-target`
- Consumed by Task 2 binding/workflow/report integration and Task 3 protocol docs.

- [ ] **Step 1: Write failing target schema tests**

Add `tests/test_target.py` with explicit fixtures:

```python
def github_target():
    return {
        "version": 1,
        "kind": "github-pr",
        "revision_bound": True,
        "repository": "base/repo",
        "pr_number": 7,
        "url": "https://github.com/base/repo/pull/7",
        "base": {"repository": "base/repo", "ref": "main", "sha": "1" * 40},
        "head": {"repository": "fork/repo", "ref": "feature", "sha": "2" * 40},
        "is_cross_repository": True,
        "diff_sha256": hashlib.sha256(b"patch").hexdigest(),
        "changed_files": ["src/a.py"],
        "intent": {"title": "Fix A", "body": "Details"},
    }

def test_github_target_round_trips_canonically():
    target = ReviewTarget.from_dict(github_target())
    assert target.to_dict() == github_target()

@pytest.mark.parametrize("mutation", [
    lambda d: d.update(version=2),
    lambda d: d.update(kind="unknown"),
    lambda d: d.update(revision_bound=False),
    lambda d: d["head"].update(sha="not-a-sha"),
    lambda d: d.update(extra=True),
    lambda d: d.update(changed_files=["a.py", "a.py"]),
])
def test_github_target_rejects_invalid_shape(mutation):
    data = github_target()
    mutation(data)
    with pytest.raises(ValueError):
        ReviewTarget.from_dict(data)
```

Add equivalent positive/negative tests for `local-range` and `diff-file`, including:

- local repository credentials stripped or replaced with `local:<hash>`;
- local target requires base/head/merge-base SHAs;
- diff file requires `revision_bound is False` and no base/head source identity;
- changed paths reject absolute paths, `..`, empty strings, backslashes, and duplicates;
- unknown fields reject at every nested level.

- [ ] **Step 2: Run schema tests and verify RED**

Run:

```bash
/Users/sri/personal/crucible/.venv/bin/python -m pytest tests/test_target.py -q
```

Expected: import failure for `crucible.target`.

- [ ] **Step 3: Implement the target model**

Create `scripts/crucible/target.py` with frozen dataclasses and strict key checks:

```python
TARGET_VERSION = 1
TARGET_KINDS = ("github-pr", "local-range", "diff-file")
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

@dataclass(frozen=True)
class Revision:
    repository: str | None
    ref: str
    sha: str

@dataclass(frozen=True)
class ReviewTarget:
    version: int
    kind: str
    revision_bound: bool
    repository: str | None
    diff_sha256: str
    changed_files: tuple[str, ...]
    intent_title: str
    intent_body: str
    pr_number: int | None = None
    url: str | None = None
    base: Revision | None = None
    head: Revision | None = None
    merge_base_sha: str | None = None
    is_cross_repository: bool | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReviewTarget":
        if not isinstance(data, dict):
            raise ValueError("review target must be a JSON object")
        kind = _required_choice(data, "kind", TARGET_KINDS)
        common = {
            "version", "kind", "revision_bound", "repository", "diff_sha256",
            "changed_files", "intent",
        }
        variant = {
            "github-pr": {"pr_number", "url", "base", "head", "merge_base_sha",
                          "is_cross_repository"},
            "local-range": {"base", "head", "merge_base_sha"},
            "diff-file": set(),
        }[kind]
        _require_exact_keys(data, common | variant)
        version = _required_int(data, "version")
        if version != TARGET_VERSION:
            raise ValueError(f"target.version must be {TARGET_VERSION}")
        revision_bound = _required_bool(data, "revision_bound")
        if revision_bound != (kind != "diff-file"):
            raise ValueError("revision_bound must be true for revision targets and false for diff-file")
        title, body = _parse_intent(data["intent"])
        target = cls(
            version=version,
            kind=kind,
            revision_bound=revision_bound,
            repository=_optional_repository(data.get("repository")),
            diff_sha256=_required_hash(data, "diff_sha256", SHA256_RE),
            changed_files=_parse_changed_files(data["changed_files"]),
            intent_title=title,
            intent_body=body,
            pr_number=_required_positive_int(data, "pr_number") if kind == "github-pr" else None,
            url=_required_url(data, "url") if kind == "github-pr" else None,
            base=_parse_revision(data["base"]) if kind != "diff-file" else None,
            head=_parse_revision(data["head"]) if kind != "diff-file" else None,
            merge_base_sha=(
                _required_hash(data, "merge_base_sha", SHA1_RE)
                if kind in ("local-range", "github-pr") else None
            ),
            is_cross_repository=(
                _required_bool(data, "is_cross_repository")
                if kind == "github-pr" else None
            ),
        )
        _validate_variant_relationships(target)
        return target

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "version": self.version,
            "kind": self.kind,
            "revision_bound": self.revision_bound,
            "repository": self.repository,
            "diff_sha256": self.diff_sha256,
            "changed_files": list(self.changed_files),
            "intent": {"title": self.intent_title, "body": self.intent_body},
        }
        if self.kind == "github-pr":
            out.update({
                "pr_number": self.pr_number,
                "url": self.url,
                "base": self.base.to_dict(),
                "head": self.head.to_dict(),
                "merge_base_sha": self.merge_base_sha,
                "is_cross_repository": self.is_cross_repository,
            })
        elif self.kind == "local-range":
            out.update({
                "base": self.base.to_dict(),
                "head": self.head.to_dict(),
                "merge_base_sha": self.merge_base_sha,
            })
        return out
```

Implement `target_sha256` with `canonical_json_sha256(target.to_dict())`. Implement
`target_from_events` so zero events returns `None`, exactly one valid event returns the parsed target,
and duplicates/malformed/hash mismatch raise `ValueError`. Implement the `_required_*`,
`_require_exact_keys`, `_parse_intent`, `_parse_changed_files`, `_parse_revision`,
`_optional_repository`, and `_validate_variant_relationships` helpers immediately above the class;
each helper performs only the named validation and raises `ValueError` with the failing field name.

- [ ] **Step 4: Write failing normalization behavior tests**

In `tests/test_target.py`, create a real divergent repository:

```python
def test_local_normalization_uses_merge_base_not_tip_diff(tmp_path):
    repo = init_diverged_repo(tmp_path)
    target, patch = normalize_local_target(repo, "main..feature", {"title": "t", "body": "b"})
    names = subprocess.run(
        ["git", "-C", str(repo), "diff", "--name-only",
         f"{target.merge_base_sha}..{target.head.sha}"],
        check=True, text=True, capture_output=True,
    ).stdout.splitlines()
    assert names == ["app.py"]
    assert "main-only.py" not in patch.decode()
```

The fixture must create:

1. base commit;
2. `feature` commit changing `app.py`;
3. `main` commit adding `main-only.py`;
4. assertions that raw `git diff main..feature` mentions `main-only.py`, while normalized output does
   not;
5. assertions that inputs `main..feature` and `main...feature` resolve to identical target/patch.

Add GitHub metadata tests preserving `baseRefOid`, `headRefOid`, `headRepository.nameWithOwner`,
`headRepositoryOwner.login`, and `isCrossRepository`. Pass metadata before and after the archive
fetches plus the base repo's exact-OID `compare/<baseRefOid>...<headRefOid>` payload; assert the patch
is derived from the merge-base→head snapshots (a base-only commit after the fork never appears), the
recorded `merge_base_sha` is the compare `merge_base_commit.sha`, and a mismatched compare
(`base_commit.sha` != `baseRefOid`, a missing/invalid merge base, or a disagreeing file list) is
rejected; reject any changed PR number/URL/title/body/files/base/head repository/ref/OID/cross-repository
flag and accept a stable cross-fork tuple. Add diff-file patch-only tests.

- [ ] **Step 5: Implement normalization helpers**

Use argument-vector subprocess calls only:

```python
def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, text=True, capture_output=True,
    ).stdout

def normalize_local_target(repo: Path, range_text: str, intent: dict[str, str]):
    base_ref, head_ref = parse_range(range_text)
    base_sha = _git(repo, "rev-parse", "--verify", f"{base_ref}^{{commit}}").strip()
    head_sha = _git(repo, "rev-parse", "--verify", f"{head_ref}^{{commit}}").strip()
    merge_base = _git(repo, "merge-base", base_sha, head_sha).strip()
    patch = subprocess.run(
        ["git", "-C", str(repo), "diff", "--binary", f"{merge_base}..{head_sha}"],
        check=True, capture_output=True,
    ).stdout
    changed = _git(repo, "diff", "--name-only", f"{merge_base}..{head_sha}").splitlines()
    target = ReviewTarget(
        version=TARGET_VERSION,
        kind="local-range",
        revision_bound=True,
        repository=normalized_repository_identity(repo),
        diff_sha256=hashlib.sha256(patch).hexdigest(),
        changed_files=tuple(changed),
        intent_title=intent["title"],
        intent_body=intent["body"],
        base=Revision(repository=None, ref=base_ref, sha=base_sha),
        head=Revision(repository=None, ref=head_ref, sha=head_sha),
        merge_base_sha=merge_base,
    )
    return target, patch
```

`parse_range` accepts exactly one `..` or `...`; Git ref names cannot contain `..`. The CLI exposes
only `--range`; no separate `--base`/`--head` flags exist. Sanitize remote identity by removing URL
userinfo/query/fragment; fall back to `local:<sha256(realpath)>`.

- [ ] **Step 6: Write failing safe-extraction tests**

Test valid GitHub-style top-level archives and reject:

- `../../escape`;
- absolute paths;
- symlink/hardlink members;
- character/block devices and FIFOs;
- duplicate paths after stripping the common top-level directory;
- 100,001 members;
- declared regular-file bytes above 1 GiB;
- partial extraction after a later invalid member.

Assert extraction is staged into a temporary sibling and atomically replaces `RUN/source` only after
all members validate. `RUN/source` must be absent; duplicate materialization rejects. Simulate an
`os.replace` failure and assert the final path remains absent and staging is removed.

- [ ] **Step 7: Implement safe extraction**

Implement a validation pass over every `tarfile.TarInfo`, then a second extraction pass that copies
regular file streams and creates directories without calling `TarFile.extract`:

```python
def safe_extract_source_archive(archive: Path, destination: Path) -> None:
    if destination.exists():
        raise ValueError(f"source destination already exists: {destination}")
    members = validated_members(archive)
    staging = destination.with_name(destination.name + ".staging")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        staging.mkdir(parents=True)
        for member, relative in members:
            out = staging / relative
            if member.isdir():
                out.mkdir(parents=True, exist_ok=True)
            else:
                out.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as src, out.open("xb") as dst:
                    shutil.copyfileobj(src, dst)
        os.replace(staging, destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
```

Validate all members before creating `staging`; never follow links.

- [ ] **Step 8: Write failing CLI command tests**

Add tests for:

- `normalize-target github|local|diff` writes canonical target/diff without a run;
- `repository-identity --repo` exactly matches the identity stored by local normalization and never
  emits URL credentials or a local path;
- `load-target` accepts only `pr-review`, verifies exact diff bytes, appends one `target_loaded`, and
  writes canonical `RUN/target.json`/`RUN/target.diff`;
- `show-target` emits the canonical manifest from the authoritative run-log event, not the scratch
  file;
- duplicate/late target loads reject with no append;
- `materialize-target` requires a loaded revision-bound GitHub/local target, records
  `source_materialized` with target/archive hashes, rejects diff-file targets, rejects a second
  materialization, rejects after `load-dag`, PLAN output, or any protocol/status event, and never
  creates/replaces `RUN/source` or appends on rejection;
- parameterize `load-dag`, `log`, `set-status`, `bindings`, `symmetric-verdict`,
  `accepted-findings`, `review-result`, `approve-plan`, and `show-plan`: each pr-review command
  rejects before append when the target is missing;
- before a DAG exists, `status` returns zero counts, `report` renders `IN PROGRESS`, and `next`
  exits nonzero with "no dependency tree loaded" rather than empty-success.

- [ ] **Step 9: Implement CLI commands**

Add parsers and handlers in `cli.py`:

```python
normalize = sub.add_parser("normalize-target")
normalize_sub = normalize.add_subparsers(dest="target_kind", required=True)
# github: --metadata-before --metadata-after --diff --output --diff-output
# local: --repo --range --intent --output --diff-output
# diff: --diff --intent --output --diff-output

load = sub.add_parser("load-target")
load.add_argument("--run", required=True)
load.add_argument("--file", required=True)
load.add_argument("--diff", required=True)

materialize = sub.add_parser("materialize-target")
materialize.add_argument("--run", required=True)
materialize.add_argument("--archive", required=True)

identity = sub.add_parser("repository-identity")
identity.add_argument("--repo", required=True)

show = sub.add_parser("show-target")
show.add_argument("--run", required=True)
```

Use strict UTF-8 for JSON, exact bytes for patches/archives, and append only after complete
validation. `cmd_materialize_target` permits only a run history consisting of `run_start` followed
by exactly one `target_loaded`; any `dag_loaded`, `builder_output`, verdict, terminal,
`node_status_change`, approval, prior `source_materialized`, or other downstream event rejects before
touching `RUN/source`. Register `tests/test_target.py` in structural validation.

- [ ] **Step 10: Run Task 1 tests and commit**

Run:

```bash
/Users/sri/personal/crucible/.venv/bin/python -m pytest \
  tests/test_target.py tests/test_cli.py -q
PYTHONPATH=scripts /Users/sri/personal/crucible/.venv/bin/python tests/validate_structure.py
git diff --check
```

Commit:

```bash
git add scripts/crucible/target.py scripts/crucible/cli.py \
  tests/test_target.py tests/test_cli.py tests/validate_structure.py
git commit -m "feat(cli): add immutable pr-review targets"
```

---

### Task 2: Bind every pr-review gate to the target

**Files:**
- Modify: `scripts/crucible/integrity.py`
- Modify: `scripts/crucible/symmetric.py`
- Modify: `scripts/crucible/workflow.py`
- Modify: `scripts/crucible/cli.py`
- Modify: `scripts/crucible/report.py`
- Modify: `tests/test_integrity.py`
- Modify: `tests/test_symmetric.py`
- Modify: `tests/test_workflow.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_report.py`
- Modify: `tests/test_symmetric_consensus.py`

**Interfaces:**
- Consumes Task 1 `ReviewTarget`, `target_from_events`, `target_sha256`, `target_event_issues`.
- Produces:
  - binding field `target_sha256`;
  - `PeerAttestation.target_sha256`;
  - workflow/result target integrity;
  - report `## Review target`.

- [ ] **Step 1: Write failing binding tests**

Add tests asserting:

```python
def test_pr_review_bindings_include_target_sha(run_with_target):
    bindings = current_bindings(run_with_target, "plan", 1).to_dict()
    assert bindings["target_sha256"] == target_sha256(target_from_events(run_with_target.read_events()))

def test_build_and_deep_dive_bindings_do_not_gain_target_sha(...):
    assert "target_sha256" not in current_bindings(build_run, "plan", 1).to_dict()
    assert "target_sha256" not in current_bindings(deep_dive_run, "plan", 1).to_dict()
```

Cover PLAN, dependency, and FINAL; missing/mismatched target hashes; both peer files; accepted sets;
terminals; post-load target duplication; and byte-for-shape compatibility for existing workflows.

- [ ] **Step 2: Run binding tests and verify RED**

Run:

```bash
/Users/sri/personal/crucible/.venv/bin/python -m pytest \
  tests/test_integrity.py tests/test_symmetric.py tests/test_cli.py -q
```

Expected: `target_sha256` absent from bindings/peer schema.

- [ ] **Step 3: Extend binding and peer schemas**

In `integrity.py`:

```python
BINDING_KEYS = ("artifact_sha256", "dag_sha256", "node_sha256", "target_sha256")

@dataclass(frozen=True)
class BindingSet:
    artifact_sha256: str
    dag_sha256: str | None = None
    node_sha256: str | None = None
    target_sha256: str | None = None
```

`current_bindings` reads workflow kind. For pr-review, require exactly one valid target and set its
hash; other workflows omit the field.

In `PeerAttestation`, add `target_sha256: str | None`, parse with `_optional_hash`, include in
`to_dict`, and let existing exact expected-binding validation reject missing/extra values.

- [ ] **Step 4: Write failing workflow/result integrity tests**

Add tests for:

- init-only pr-review with no target -> `missing`;
- DAG/PLAN/protocol work without target -> `invalid`;
- target in build/deep-dive -> `invalid`;
- duplicate/malformed/late target -> `invalid`;
- target hash missing/mismatched on verdict, accepted set, or terminal -> `invalid`;
- all guarded CLI commands reject a missing target before append;
- status/next/report on an init-only pr-review run return usable in-progress output without a DAG;
- `accepted-findings`/`review-result` reject every invalid/missing target state;
- replacing `RUN/target.json` does not alter authoritative event state;
- source materialization event with the wrong target hash -> `invalid`.

- [ ] **Step 5: Implement workflow and Finish validation**

At the start of `workflow_issues`, append target issues based on workflow and event order. For
pr-review, require a target before the first DAG/PLAN/review event. Because `event_bindings` now
includes `target_sha256`, existing terminal/peer/accepted-set exact comparisons enforce target
identity.

Extend `require_complete_symmetric_run` with pr-review target validation before accepted findings are
published. Thread the workflow/config or expected target hash explicitly rather than importing
workflow code into `symmetric.py`.

Add one CLI helper:

```python
def _require_pr_review_target(run: RunLog) -> None:
    events = run.read_events()
    if workflow_kind(events) != "pr-review":
        return
    try:
        target_from_events(events)
    except ValueError as exc:
        raise SystemExit(f"crucible: invalid pr-review target: {exc}")
    if target_from_events(events) is None:
        raise SystemExit("crucible: pr-review target is missing; run load-target before this command")
```

Call it after `_require_run_integrity` in `load-dag`, `log`, `set-status`, `bindings`,
`symmetric-verdict`, `accepted-findings`, `review-result`, `approve-plan`, and `show-plan`.
`load-target` is the only pr-review mutation allowed without it. Make `status` catch a missing DAG
and return zero counts; make `next` catch it and exit nonzero with a clear message; report already has
a fail-closed rendering path.

- [ ] **Step 6: Write failing report tests**

Add exact report tests for all variants:

```python
def test_report_renders_github_target_identity(...):
    md = render_markdown(run)
    target = section(md, "Review target")
    assert "base/repo#7" in target
    assert "1" * 40 in target
    assert "fork/repo" in target
    assert "2" * 40 in target
    assert "cross-repository" in target

def test_report_diff_file_states_revision_unbound(...):
    assert "Revision:** unbound (patch identity only)" in render_markdown(run)
```

Also test sanitized title/body/path values, absent target before PLAN (`IN PROGRESS`), downstream work
without target (`INVALID`), and no target section for build/deep-dive.

- [ ] **Step 7: Implement report target section**

Read the authoritative target from `target_loaded`. Render `## Review target` before Summary with
sanitized labels and hashes. Render source snapshot status only from a valid `source_materialized`
event bound to the same target.

- [ ] **Step 8: Run Task 2 tests and commit**

Run:

```bash
/Users/sri/personal/crucible/.venv/bin/python -m pytest \
  tests/test_integrity.py tests/test_symmetric.py tests/test_workflow.py \
  tests/test_cli.py tests/test_report.py tests/test_symmetric_consensus.py -q
git diff --check
```

Commit:

```bash
git add scripts/crucible/integrity.py scripts/crucible/symmetric.py \
  scripts/crucible/workflow.py scripts/crucible/cli.py scripts/crucible/report.py \
  tests/test_integrity.py tests/test_symmetric.py tests/test_workflow.py \
  tests/test_cli.py tests/test_report.py tests/test_symmetric_consensus.py
git commit -m "feat(cli): bind pr-review gates to target"
```

---

### Task 3: Migrate pr-review normalization and execution protocol

**Files:**
- Modify: `skills/pr-review/SKILL.md`
- Modify: `skills/pr-review/references/platform-notes.md`
- Modify: `skills/pr-review/references/peer-prompt.md`
- Modify: `skills/pr-review/references/review-thread.md`
- Modify: `commands/pr-review.md`
- Modify: `README.md`
- Modify: `docs/cli.md`
- Modify: `SECURITY.md`
- Modify: `CHANGELOG.md`
- Modify: `docs/superpowers/specs/2026-07-21-pr-review-target-binding-design.md`
- Add: `docs/superpowers/plans/2026-07-21-pr-review-target-binding.md`
- Modify: `tests/test_pr_review_skill.py`
- Modify: `tests/test_pr_review_references.py`
- Modify: `tests/test_docs.py`
- Modify: `tests/test_target.py`

**Interfaces:**
- Consumes Task 1 normalization/load/materialization commands and Task 2 target-bound peer schema.
- Produces executable GitHub PR/local-range/diff-file protocols and public guidance.

- [ ] **Step 1: Write failing protocol guards**

Require the live skill/reference/command sections to contain:

- GitHub `gh pr view` fields `baseRefOid`, `headRefOid`, `headRepository`,
  `headRepositoryOwner`, `isCrossRepository`;
- `normalize-target`, `load-target`, and `materialize-target` before PLAN;
- pinned `RUN/source` reads for GitHub/local targets;
- no ambient source reads for diff-file targets;
- local merge-base normalization rather than raw `git diff <base>..<head>`;
- `target_sha256` in peer attestation examples;
- trusted-local execution checks clean checkout + exact recorded head SHA;
- GitHub PR and diff-file execution prohibition unchanged;
- target report/provenance and fresh-run rule for target changes.

Tests must scope assertions to the relevant sections and reject the old two-dot/branch-name-only
commands.

- [ ] **Step 2: Run protocol tests and verify RED**

Run:

```bash
/Users/sri/personal/crucible/.venv/bin/python -m pytest \
  tests/test_pr_review_skill.py tests/test_pr_review_references.py tests/test_docs.py -q
```

Expected: failures for immutable OIDs, target commands, merge-base semantics, and head verification.

- [ ] **Step 3: Rewrite input normalization**

Document this exact GitHub flow:

```bash
GH_JSON=number,url,title,body,files,baseRefName,baseRefOid,headRefName,headRefOid,headRepository,headRepositoryOwner,isCrossRepository
for ATTEMPT in 1 2 3; do
  ok=1
  gh pr view "$PR" --json "$GH_JSON" > "$RUN/pr-before.json" || ok=0
  BASE_REPOSITORY=; BASE_OID=; HEAD_REPOSITORY=; HEAD_OID=; MERGE_BASE_OID=
  [ "$ok" = 1 ] && { BASE_REPOSITORY=$(/Users/sri/personal/crucible/.venv/bin/python -c \
    'import json,sys,urllib.parse as u; print("/".join(u.urlsplit(json.load(open(sys.argv[1]))["url"]).path.strip("/").split("/")[:2]))' \
    "$RUN/pr-before.json") || ok=0; }
  [ "$ok" = 1 ] && { BASE_OID=$(/Users/sri/personal/crucible/.venv/bin/python -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["baseRefOid"])' "$RUN/pr-before.json") || ok=0; }
  [ "$ok" = 1 ] && { HEAD_REPOSITORY=$(/Users/sri/personal/crucible/.venv/bin/python -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["headRepository"]["nameWithOwner"])' "$RUN/pr-before.json") || ok=0; }
  [ "$ok" = 1 ] && { HEAD_OID=$(/Users/sri/personal/crucible/.venv/bin/python -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["headRefOid"])' "$RUN/pr-before.json") || ok=0; }
  [ "$ok" = 1 ] && { test -n "$BASE_REPOSITORY" && test -n "$BASE_OID" && test -n "$HEAD_REPOSITORY" && test -n "$HEAD_OID" || ok=0; }
  [ "$ok" = 1 ] && { gh api "repos/$BASE_REPOSITORY/compare/$BASE_OID...$HEAD_OID" > "$RUN/compare.json" || ok=0; }
  [ "$ok" = 1 ] && { MERGE_BASE_OID=$(/Users/sri/personal/crucible/.venv/bin/python -c \
    'import json,sys; print(json.load(open(sys.argv[1]))["merge_base_commit"]["sha"])' "$RUN/compare.json") || ok=0; }
  [ "$ok" = 1 ] && { test -n "$MERGE_BASE_OID" || ok=0; }
  [ "$ok" = 1 ] && { gh api "repos/$BASE_REPOSITORY/tarball/$MERGE_BASE_OID" > "$RUN/merge-base.tar.gz" || ok=0; }
  [ "$ok" = 1 ] && { gh api "repos/$HEAD_REPOSITORY/tarball/$HEAD_OID" > "$RUN/head.tar.gz" || ok=0; }
  [ "$ok" = 1 ] && { gh pr view "$PR" --json "$GH_JSON" > "$RUN/pr-after.json" || ok=0; }
  if [ "$ok" = 1 ] && PYTHONPATH=scripts python3 -m crucible normalize-target github \
    --metadata-before "$RUN/pr-before.json" --metadata-after "$RUN/pr-after.json" \
    --compare-metadata "$RUN/compare.json" \
    --merge-base-archive "$RUN/merge-base.tar.gz" --head-archive "$RUN/head.tar.gz" \
    --output "$RUN/target.json" --diff-output "$RUN/target.diff"; then
    break
  fi
  rm -f "$RUN/pr-before.json" "$RUN/pr-after.json" "$RUN/compare.json" "$RUN/merge-base.tar.gz" "$RUN/head.tar.gz" "$RUN/target.json" "$RUN/target.diff"
  [ "$ATTEMPT" -lt 3 ] || { echo "pr-review: GitHub target acquisition failed after 3 attempts" >&2; exit 1; }
done
PYTHONPATH=scripts python3 -m crucible load-target \
  --run "$RUN" --file "$RUN/target.json" --diff "$RUN/target.diff"
PYTHONPATH=scripts python3 -m crucible show-target --run "$RUN" > "$RUN/loaded-target.json"
```

Materialize the GitHub head snapshot by **reusing** the exact `head.tar.gz` codeload archive already
fetched during acquisition (never a re-fetch, which could observe a moved head), and never delete it:

```bash
SOURCE_AVAILABLE=no
if test -s "$RUN/head.tar.gz" \
  && PYTHONPATH=scripts python3 -m crucible materialize-target --run "$RUN" --archive "$RUN/head.tar.gz"
then
  SOURCE_AVAILABLE=yes
fi
```

The GitHub normalizer takes intent directly from stable before/after title/body metadata, and the
immutable patch is **derived** PR-style from the merge-base and head codeload snapshots — base
`repository@merge_base_commit.sha` (the fork point from the base repo's exact-OID
`compare/<baseRefOid>...<headRefOid>` endpoint) → head `repository@headRefOid` — never `gh pr diff`, a
base-tip two-dot diff, or a caller patch, so a base-only commit after the fork can never appear as a
reverse change. Both peers read `RUN/target.diff` and `RUN/source`, never ambient checkout files.
Acquisition fails **closed** (each `gh pr view` read, the `gh api` compare fetch, the merge-base parse,
both `gh api` archive fetches, and the second read explicitly checked; any failure discards every
partial artifact and retries). Materialization **reuses** the
acquired `head.tar.gz` and **fails closed / non-fatal**: `SOURCE_AVAILABLE` defaults to `no`,
`materialize-target` is explicitly checked (never a global `set -e`), any failure leaves
`SOURCE_AVAILABLE=no`, and only a clean materialize sets `SOURCE_AVAILABLE=yes`.

Local mode materializes on its own executable path: read the recorded `repository`/`head.sha` from the
same authoritative manifest, then gate the archive on **one** explicit compound `if` — the parses are
non-empty, `$LOCAL_REPO` is a directory whose `repository-identity` **equals** the recorded identity, and
its `git rev-parse HEAD` **equals** the recorded head SHA, every check `&&`-joined so a mismatch
**short-circuits past** the archive (no reliance on a global `set -e`; never `rev-parse`/archive in an
unrelated repo when `$LOCAL_REPO` is missing). Only then does an explicit `git -C "$LOCAL_REPO"` archive
(never ambient git) and materialize exactly that uncompressed `source.tar` — never the GitHub
`head.tar.gz`:

```bash
PYTHONPATH=scripts python3 -m crucible show-target --run "$RUN" > "$RUN/loaded-target.json"
RECORDED_REPOSITORY_IDENTITY=$(/Users/sri/personal/crucible/.venv/bin/python -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["repository"])' \
  "$RUN/loaded-target.json")
HEAD_SHA=$(/Users/sri/personal/crucible/.venv/bin/python -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["head"]["sha"])' \
  "$RUN/loaded-target.json")
OBSERVED_REPOSITORY=
if test -n "$RECORDED_REPOSITORY_IDENTITY" && test -n "$HEAD_SHA" && test -d "$LOCAL_REPO"
then
  OBSERVED_REPOSITORY=$(PYTHONPATH=scripts python3 -m crucible repository-identity \
    --repo "$LOCAL_REPO")
fi
rm -f "$RUN/source.tar"
SOURCE_AVAILABLE=no
if test -n "$RECORDED_REPOSITORY_IDENTITY" && test -n "$HEAD_SHA" && test -d "$LOCAL_REPO" \
  && test "$OBSERVED_REPOSITORY" = "$RECORDED_REPOSITORY_IDENTITY" \
  && test "$(git -C "$LOCAL_REPO" rev-parse HEAD)" = "$HEAD_SHA" \
  && git -C "$LOCAL_REPO" archive --format=tar --output "$RUN/source.tar" "$HEAD_SHA"
then
  if PYTHONPATH=scripts python3 -m crucible materialize-target \
    --run "$RUN" --archive "$RUN/source.tar"
  then
    SOURCE_AVAILABLE=yes
  else
    rm -f "$RUN/source.tar"
  fi
else
  rm -f "$RUN/source.tar"
fi
```

Document local and diff modes with the corresponding `normalize-target` subcommands. Local mode uses
the single `--range BASE..HEAD|BASE...HEAD` option, rejects separate base/head flags, always records
merge base, and emits the same patch for either spelling. Local materialization mirrors the same
fail-closed, non-fatal flow: the identity/head checks are `&&`-joined conjuncts of the one `if` that
gates the archive (a parse/identity/head mismatch short-circuits past the archive, never invokes
materialize, and leaves `SOURCE_AVAILABLE=no`), and a failed archive or rejected materialization discards
the partial `source.tar` and leaves `SOURCE_AVAILABLE=no`. Diff mode sets `SOURCE_AVAILABLE=no` and has no
source materialization. Whenever `SOURCE_AVAILABLE=no`, both peers get the same status and the review
continues patch-only with runtime-verified claims treated as unverified.

- [ ] **Step 4: Update peer and execution protocols**

Add `target_sha256` to every peer JSON example. Seed both peers with the canonical target manifest and
source availability status.

Execution stays available only for a **trusted local** checkout/range (a GitHub-PR or diff-file target
is static/CI-only and **never executes locally**, regardless of consent). For a **trusted local**
checkout/range, prove the checkout is the exact recorded head revision with **one explicit compound
gate** — never bare `test`s a missing global `set -e` would ignore — before asking exact-command
consent:

```bash
PYTHONPATH=scripts python3 -m crucible show-target --run "$RUN" > "$RUN/loaded-target.json"
RECORDED_REPOSITORY_IDENTITY=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["repository"])' "$RUN/loaded-target.json")
RECORDED_HEAD_SHA=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["head"]["sha"])' "$RUN/loaded-target.json")
OBSERVED_REPOSITORY=
if test -n "$RECORDED_REPOSITORY_IDENTITY" && test -n "$RECORDED_HEAD_SHA" && test -d "$LOCAL_REPO"
then
  OBSERVED_REPOSITORY=$(PYTHONPATH=scripts python3 -m crucible repository-identity --repo "$LOCAL_REPO")
fi
CHECKOUT_VERIFIED=no
if test -n "$RECORDED_REPOSITORY_IDENTITY" && test -n "$RECORDED_HEAD_SHA" && test -d "$LOCAL_REPO" \
  && test "$OBSERVED_REPOSITORY" = "$RECORDED_REPOSITORY_IDENTITY" \
  && STATUS=$(git -C "$LOCAL_REPO" status --porcelain) && test -z "$STATUS" \
  && HEAD_NOW=$(git -C "$LOCAL_REPO" rev-parse HEAD) && test "$HEAD_NOW" = "$RECORDED_HEAD_SHA"
then
  CHECKOUT_VERIFIED=yes
fi
```

Only when `CHECKOUT_VERIFIED=yes` may you show the exact commands and ask exact-command consent. If
`CHECKOUT_VERIFIED=no`, do not run commands and do not ask for execution consent. Offer static-only
continuation or an exact detached worktree-at-SHA command set that itself requires fresh consent. Save
observed repository identity, head SHA, clean status, and approved command list in the execution
evidence handed to both peers and rendered in the review provenance. The exact-command consent (with
its arbitrary-code warning) stays separate from posting consent.

- [ ] **Step 5: Add behavioral documentation integration test**

In `tests/test_target.py`, execute the documented local normalization path in the divergent fixture
and assert:

- manifest base/head/merge-base hashes;
- patch contains only feature changes;
- loaded target hash appears in PLAN bindings;
- report target section matches the manifest.

This prevents the protocol tests from regressing to token-only assertions.

- [ ] **Step 6: Update public docs/security/changelog**

Document:

- target commands and manifest variants in `docs/cli.md`;
- immutable PR/local target provenance in README;
- safe archive extraction, static-only source snapshots, and exact-head execution in SECURITY;
- the finding #4 fix under `[Unreleased]`;
- implementation status/link in the design spec.

Remove current `base..head` examples that imply two-dot semantics, while preserving historical
amendment-bannered documents.

- [ ] **Step 7: Run complete verification and commit**

Run:

```bash
/Users/sri/personal/crucible/.venv/bin/python -m pytest \
  tests/test_target.py tests/test_pr_review_skill.py \
  tests/test_pr_review_references.py tests/test_docs.py -q
PYTHONPATH=scripts /Users/sri/personal/crucible/.venv/bin/python tests/validate_structure.py
/Users/sri/personal/crucible/.venv/bin/python -m pytest -q
/Users/sri/personal/crucible/.venv/bin/python scripts/check.py
git diff --check
```

Commit:

```bash
git add skills/pr-review commands/pr-review.md README.md docs/cli.md SECURITY.md CHANGELOG.md \
  docs/superpowers/specs/2026-07-21-pr-review-target-binding-design.md \
  docs/superpowers/plans/2026-07-21-pr-review-target-binding.md \
  tests/test_target.py tests/test_pr_review_skill.py \
  tests/test_pr_review_references.py tests/test_docs.py
git commit -m "docs(pr-review): pin reviewed revisions"
```

---

## FINAL verification

After every dependency reaches Crucible consensus:

```bash
/Users/sri/personal/crucible/.venv/bin/python -m pytest -q
/Users/sri/personal/crucible/.venv/bin/python scripts/check.py
git diff --check "$(git merge-base main HEAD)"..HEAD
```

Run the configured Crucible FINAL gate over the whole branch. The final reviewer must specifically
test:

- fork PR identity;
- target replacement and late/missing target state;
- target hash on PLAN/thread/FINAL peers and terminals;
- local divergence/two-dot regression;
- archive traversal/link/resource rejection;
- exact-head consented execution;
- build/deep-dive compatibility;
- report/result fail-closed behavior.
