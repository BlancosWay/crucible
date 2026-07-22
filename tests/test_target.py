"""Behavioral tests for the immutable pr-review target model, normalization, and materialization.

These exercise the real schema/normalization/extraction logic in ``crucible.target`` against real
temporary Git repositories and real tarfile fixtures. Archive-limit tests stay resource-safe: they
shrink the module limits deterministically instead of allocating a gigabyte or a hundred thousand
files.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from crucible.target import (
    MAX_ARCHIVE_BYTES,
    MAX_ARCHIVE_MEMBERS,
    SHA1_RE,
    SHA256_RE,
    SOURCE_RECEIPT_SUFFIX,
    TARGET_KINDS,
    TARGET_VERSION,
    ReviewTarget,
    normalize_diff_target,
    normalize_github_target,
    normalize_local_target,
    normalized_repository_identity,
    read_source_receipt,
    safe_extract_source_archive,
    source_receipt,
    source_receipt_matches,
    source_receipt_path,
    target_event_issues,
    target_from_events,
    target_sha256,
    validate_source_materialization,
    write_source_receipt,
)


# --------------------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------------------

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
        "merge_base_sha": "3" * 40,
        "is_cross_repository": True,
        "diff_sha256": hashlib.sha256(b"patch").hexdigest(),
        "changed_files": ["src/a.py"],
        "intent": {"title": "Fix A", "body": "Details"},
    }


def local_target():
    return {
        "version": 1,
        "kind": "local-range",
        "revision_bound": True,
        "repository": "https://github.com/owner/repo.git",
        "base": {"ref": "main", "sha": "1" * 40},
        "head": {"ref": "feature", "sha": "2" * 40},
        "merge_base_sha": "3" * 40,
        "diff_sha256": hashlib.sha256(b"patch").hexdigest(),
        "changed_files": ["src/a.py"],
        "intent": {"title": "Local range review", "body": "..."},
    }


def diff_target():
    return {
        "version": 1,
        "kind": "diff-file",
        "revision_bound": False,
        "repository": None,
        "diff_sha256": hashlib.sha256(b"patch").hexdigest(),
        "changed_files": ["src/a.py"],
        "intent": {"title": "Patch review", "body": "..."},
    }


# --------------------------------------------------------------------------------------
# github-pr schema
# --------------------------------------------------------------------------------------

def test_module_constants():
    assert TARGET_VERSION == 1
    assert TARGET_KINDS == ("github-pr", "local-range", "diff-file")
    assert MAX_ARCHIVE_MEMBERS == 100_000
    assert MAX_ARCHIVE_BYTES == 1 << 30


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


@pytest.mark.parametrize("mutation", [
    lambda d: d.pop("pr_number"),
    lambda d: d.pop("url"),
    lambda d: d.pop("base"),
    lambda d: d.pop("head"),
    lambda d: d.pop("merge_base_sha"),
    lambda d: d.pop("is_cross_repository"),
    lambda d: d.update(pr_number=0),
    lambda d: d.update(pr_number=-1),
    lambda d: d.update(url="not a url"),
    lambda d: d["base"].update(sha="ABC"),
    lambda d: d["head"].update(extra=1),
    lambda d: d["intent"].update(extra=1),
    lambda d: d.update(intent={"title": "only title"}),
    lambda d: d.update(repository=None),
    lambda d: d.update(is_cross_repository="yes"),
    lambda d: d.update(merge_base_sha="not-a-sha"),  # github merge base must be 40 lowercase hex
])
def test_github_target_rejects_more_invalid_shapes(mutation):
    data = github_target()
    mutation(data)
    with pytest.raises(ValueError):
        ReviewTarget.from_dict(data)


def test_github_cross_repository_flag_must_match_repositories():
    data = github_target()
    # base==head repository but is_cross_repository True -> inconsistent
    data["head"]["repository"] = "base/repo"
    with pytest.raises(ValueError):
        ReviewTarget.from_dict(data)


def test_github_same_repo_pr_is_not_cross():
    data = github_target()
    data["head"]["repository"] = "base/repo"
    data["is_cross_repository"] = False
    target = ReviewTarget.from_dict(data)
    assert target.is_cross_repository is False
    assert target.to_dict() == data


def test_github_top_level_repository_must_equal_base_repository():
    data = github_target()
    data["repository"] = "other/repo"
    with pytest.raises(ValueError):
        ReviewTarget.from_dict(data)


# --------------------------------------------------------------------------------------
# local-range schema
# --------------------------------------------------------------------------------------

def test_local_target_round_trips_canonically():
    target = ReviewTarget.from_dict(local_target())
    assert target.to_dict() == local_target()
    assert target.merge_base_sha == "3" * 40
    # local revisions carry NO repository identity
    assert target.base.repository is None
    assert target.head.repository is None


@pytest.mark.parametrize("mutation", [
    lambda d: d.pop("merge_base_sha"),
    lambda d: d.pop("base"),
    lambda d: d.pop("head"),
    lambda d: d.update(revision_bound=False),
    lambda d: d.update(repository=None),
    lambda d: d["base"].update(repository="owner/repo"),  # local revisions must not name a repo
    lambda d: d.update(pr_number=1),  # not a local field
    lambda d: d.update(url="https://x"),  # not a local field
    lambda d: d.update(is_cross_repository=True),  # not a local field
    lambda d: d["base"].update(sha="zz"),
    lambda d: d.update(merge_base_sha="not-a-sha"),
    lambda d: d.update(extra=True),
])
def test_local_target_rejects_invalid_shape(mutation):
    data = local_target()
    mutation(data)
    with pytest.raises(ValueError):
        ReviewTarget.from_dict(data)


# --------------------------------------------------------------------------------------
# diff-file schema
# --------------------------------------------------------------------------------------

def test_diff_target_round_trips_canonically():
    target = ReviewTarget.from_dict(diff_target())
    assert target.to_dict() == diff_target()
    assert target.revision_bound is False
    assert target.base is None and target.head is None
    assert target.repository is None


@pytest.mark.parametrize("mutation", [
    lambda d: d.update(revision_bound=True),  # diff file must be revision-unbound
    lambda d: d.update(repository="owner/repo"),  # no source identity for a diff file
    lambda d: d.update(base={"ref": "m", "sha": "1" * 40}),  # no base/head source identity
    lambda d: d.update(head={"ref": "m", "sha": "1" * 40}),
    lambda d: d.update(merge_base_sha="1" * 40),
    lambda d: d.update(pr_number=1),
    lambda d: d.update(extra=True),
    lambda d: d.update(diff_sha256="short"),
])
def test_diff_target_rejects_invalid_shape(mutation):
    data = diff_target()
    mutation(data)
    with pytest.raises(ValueError):
        ReviewTarget.from_dict(data)


# --------------------------------------------------------------------------------------
# repository identity is canonical + credential-free (schema-level, shared validator)
# --------------------------------------------------------------------------------------

# Exactly the identities the normalizers actually emit (see normalized_repository_identity and
# normalize_github_target): owner/repo, a sanitized host/path or scheme://host[:port]/path URL, and
# local:<64 lowercase hex>. Each must load verbatim so target_sha256 stays stable.
_CANONICAL_REPO_IDENTITIES = [
    "owner/repo",
    "github.com/owner/repo.git",
    "https://github.com/owner/repo.git",
    "ssh://github.com/owner/repo",
    "git://github.com/owner/repo.git",
    "https://github.com:443/owner/repo",
    "local:" + "a" * 64,
]

# Non-empty but non-canonical strings a hand-written manifest could smuggle past a bare "non-empty
# string" check: URL credentials, query/fragment, filesystem paths, backslashes, unsanitized scp,
# malformed local fingerprints, whitespace/control, and single-segment junk.
_NONCANONICAL_REPO_IDENTITIES = [
    "https://user:pass@github.com/owner/repo.git",   # userinfo/credentials
    "https://token@github.com/owner/repo.git",       # token in userinfo
    "https://github.com/owner/repo.git?token=abc",   # query
    "https://github.com/owner/repo.git#frag",        # fragment
    "file:///home/user/repo",                        # file URL (no host)
    "file://localhost/home/user/repo",               # file URL WITH host (never a remote identity)
    "FILE://localhost/home/user/repo",               # file URL, uppercase scheme
    "File://localhost/home/user/repo",               # file URL, mixed-case scheme
    "file://localhost/home/us%20er/repo",            # file URL, percent-encoded local path
    "file:/home/user/repo",                          # file: single-slash local reference
    "file:home/user/repo",                           # file: opaque local reference
    "/home/user/secret/repo",                        # absolute path
    "./repo",                                         # relative path
    "../repo",                                        # parent-relative path
    "owner/../repo",                                  # traversal
    "C:\\Users\\me\\repo",                            # windows path / backslash
    "owner\\repo",                                    # backslash
    "github.com:owner/repo",                          # scp-style, not sanitized
    "local:not-a-real-fingerprint",                  # malformed local
    "local:" + "a" * 63,                             # local too short
    "local:" + "A" * 64,                             # local uppercase hex
    "local:" + "a" * 64 + "0",                       # local too long
    "owner repo",                                     # embedded whitespace
    "owner/repo\n",                                   # trailing control character
    "repo",                                           # single segment
]


@pytest.mark.parametrize("identity", _CANONICAL_REPO_IDENTITIES)
def test_top_level_repository_accepts_canonical_identities(identity):
    data = local_target()
    data["repository"] = identity
    target = ReviewTarget.from_dict(data)
    # Loaded verbatim — never silently sanitized, so target_sha256 stays stable.
    assert target.repository == identity
    assert target.to_dict()["repository"] == identity


@pytest.mark.parametrize("identity", _NONCANONICAL_REPO_IDENTITIES)
def test_top_level_repository_rejects_noncanonical(identity):
    data = local_target()
    data["repository"] = identity
    with pytest.raises(ValueError, match="repository"):
        ReviewTarget.from_dict(data)


@pytest.mark.parametrize("identity", _NONCANONICAL_REPO_IDENTITIES)
def test_nested_head_repository_rejects_noncanonical(identity):
    data = github_target()
    data["head"]["repository"] = identity  # fork/head slot; top-level stays canonical
    with pytest.raises(ValueError, match="repository"):
        ReviewTarget.from_dict(data)


@pytest.mark.parametrize("identity", _NONCANONICAL_REPO_IDENTITIES)
def test_nested_base_repository_rejects_noncanonical(identity):
    data = github_target()
    data["base"]["repository"] = identity  # base slot; validated before the base==top-level check
    with pytest.raises(ValueError, match="repository"):
        ReviewTarget.from_dict(data)


def test_github_head_repository_accepts_canonical_fork_slug():
    data = github_target()
    data["head"]["repository"] = "another-fork/repo"
    target = ReviewTarget.from_dict(data)
    assert target.head.repository == "another-fork/repo"
    assert target.to_dict()["head"]["repository"] == "another-fork/repo"


# A file: URL (or any local/opaque file reference) is never a canonical remote identity: it names a
# path on the local filesystem, so it must never survive sanitization or persist as a repository
# identity. urlsplit lowercases the scheme, so case variants collapse to the same rejection.
_FILE_LOCAL_REFERENCES = [
    "file://localhost/Users/foo/repo",               # authority form with a host
    "file:///Users/foo/repo",                        # empty authority (no host)
    "FILE://localhost/Users/foo/repo",               # uppercase scheme
    "File://localhost/Users/foo/repo",               # mixed-case scheme
    "file://localhost/Users/te%20st/repo",           # percent-encoded local path
    "file://LOCALHOST/Users/foo/repo",               # uppercase host
    "file:/Users/foo/repo",                          # single-slash local reference
    "file:Users/foo/repo",                           # opaque local reference
]


@pytest.mark.parametrize("ref", _FILE_LOCAL_REFERENCES)
def test_sanitize_remote_url_rejects_file_references(ref):
    from crucible.target import _sanitize_remote_url
    assert _sanitize_remote_url(ref) is None


@pytest.mark.parametrize("ref", _FILE_LOCAL_REFERENCES)
def test_canonical_identity_rejects_file_references(ref):
    from crucible.target import _is_canonical_repository_identity
    assert _is_canonical_repository_identity(ref) is False


def test_sanitize_remote_url_allows_only_network_schemes_with_host_and_path():
    from crucible.target import _sanitize_remote_url
    # Supported network schemes with a non-empty host and a safe path round-trip unchanged.
    for good in ["https://github.com/owner/repo.git", "ssh://github.com/owner/repo",
                 "git://github.com/owner/repo.git", "https://github.com:443/owner/repo"]:
        assert _sanitize_remote_url(good) == good
    # scp-like remotes still sanitize to host/path (credentials dropped).
    assert _sanitize_remote_url("git@github.com:owner/repo.git") == "github.com/owner/repo.git"


def test_sanitize_remote_url_requires_host_and_safe_path():
    from crucible.target import _sanitize_remote_url
    assert _sanitize_remote_url("https://") is None                     # no host
    assert _sanitize_remote_url("https://github.com") is None           # no path
    assert _sanitize_remote_url("https://github.com/") is None          # empty path
    assert _sanitize_remote_url("https://github.com/../secret") is None  # traversal in path
    # Only known network schemes pass; unknown or local schemes are rejected even with host+path.
    assert _sanitize_remote_url("unknownscheme://example.com/o/r") is None
    assert _sanitize_remote_url("file://example.com/o/r") is None


# --------------------------------------------------------------------------------------
# changed_files path safety (shared)
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    ["/abs/path.py"],
    ["../escape.py"],
    ["a/../b.py"],
    [""],
    ["a\\b.py"],
    ["dup.py", "dup.py"],
    "notalist",
    [123],
])
def test_changed_files_reject_unsafe_paths(bad):
    data = diff_target()
    data["changed_files"] = bad
    with pytest.raises(ValueError):
        ReviewTarget.from_dict(data)


def test_changed_files_allow_empty_list():
    data = diff_target()
    data["changed_files"] = []
    target = ReviewTarget.from_dict(data)
    assert target.changed_files == ()


# --------------------------------------------------------------------------------------
# target_sha256 + event round-trip
# --------------------------------------------------------------------------------------

def test_target_sha256_is_canonical_and_order_independent():
    a = ReviewTarget.from_dict(github_target())
    reordered = dict(reversed(list(github_target().items())))
    b = ReviewTarget.from_dict(reordered)
    assert target_sha256(a) == target_sha256(b)
    assert len(target_sha256(a)) == 64


def _loaded_event(manifest):
    target = ReviewTarget.from_dict(manifest)
    return {"event": "target_loaded", "target": target.to_dict(),
            "target_sha256": target_sha256(target)}


def test_target_from_events_zero_returns_none():
    assert target_from_events([{"event": "run_start"}]) is None


def test_target_from_events_one_valid_parses():
    events = [{"event": "run_start"}, _loaded_event(github_target())]
    target = target_from_events(events)
    assert target is not None
    assert target.to_dict() == github_target()


def test_target_from_events_duplicate_rejects():
    events = [{"event": "run_start"}, _loaded_event(diff_target()), _loaded_event(diff_target())]
    with pytest.raises(ValueError):
        target_from_events(events)


def test_target_from_events_hash_mismatch_rejects():
    ev = _loaded_event(diff_target())
    ev["target_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        target_from_events([{"event": "run_start"}, ev])


def test_target_from_events_malformed_manifest_rejects():
    ev = _loaded_event(diff_target())
    ev["target"]["kind"] = "unknown"
    with pytest.raises(ValueError):
        target_from_events([ev])


# --------------------------------------------------------------------------------------
# target_event_issues (consumed by workflow integrity in Task 2)
# --------------------------------------------------------------------------------------

def test_target_event_issues_clean_pr_review():
    events = [{"event": "run_start"}, _loaded_event(diff_target())]
    assert target_event_issues(events, "pr-review") == []


def test_target_event_issues_flags_target_in_build_run():
    events = [{"event": "run_start"}, _loaded_event(diff_target())]
    assert target_event_issues(events, "build")
    assert target_event_issues(events, "deep-dive")


def test_target_event_issues_flags_duplicate():
    events = [{"event": "run_start"}, _loaded_event(diff_target()), _loaded_event(diff_target())]
    assert target_event_issues(events, "pr-review")


def test_target_event_issues_flags_late_load():
    events = [{"event": "run_start"}, {"event": "dag_loaded", "gate": "plan"},
              _loaded_event(diff_target())]
    assert target_event_issues(events, "pr-review")


def test_target_event_issues_flags_work_without_target():
    events = [{"event": "run_start"}, {"event": "dag_loaded", "gate": "plan"}]
    assert target_event_issues(events, "pr-review")


def test_target_event_issues_init_only_pr_review_is_not_invalid():
    # init-only (no target, no work) is 'missing', not 'invalid' -> no issue string here
    assert target_event_issues([{"event": "run_start"}], "pr-review") == []


def test_target_event_issues_flags_malformed():
    ev = _loaded_event(diff_target())
    ev["target_sha256"] = "0" * 64
    assert target_event_issues([{"event": "run_start"}, ev], "pr-review")


# --------------------------------------------------------------------------------------
# validate_source_materialization — centralized fail-closed source-snapshot validation (F2/F3)
# --------------------------------------------------------------------------------------

def _source_event(manifest, **overrides):
    target = ReviewTarget.from_dict(manifest)
    ev = {"event": "source_materialized", "kind": target.kind,
          "target_sha256": target_sha256(target), "archive_sha256": "d" * 64}
    ev.update(overrides)
    return ev


def _materialized_run(manifest, **overrides):
    """run_start + target_loaded + one source_materialized (fields overridable) for ``manifest``."""
    return [{"event": "run_start"}, _loaded_event(manifest), _source_event(manifest, **overrides)]


def test_validate_source_materialization_absent_is_clean():
    events = [{"event": "run_start"}, _loaded_event(local_target())]
    result = validate_source_materialization(events, "pr-review")
    assert result.issues == []
    assert result.event is None


def test_validate_source_materialization_valid_local_range():
    result = validate_source_materialization(_materialized_run(local_target()), "pr-review")
    assert result.issues == []
    assert result.event is not None
    assert result.event["kind"] == "local-range"


def test_validate_source_materialization_valid_github_pr():
    result = validate_source_materialization(_materialized_run(github_target()), "pr-review")
    assert result.issues == []
    assert result.event is not None
    assert result.event["kind"] == "github-pr"


def test_validate_source_materialization_diff_file_target_is_invalid():
    # A revision-unbound diff-file target has no source snapshot; a source event is INVALID, not
    # merely missing, and never surfaces a validated event.
    result = validate_source_materialization(_materialized_run(diff_target()), "pr-review")
    assert result.issues
    assert result.event is None


def test_validate_source_materialization_without_target_is_invalid():
    # A source event with NO valid preceding target is INVALID (fail-closed), never merely missing.
    src = {"event": "source_materialized", "kind": "local-range",
           "target_sha256": "d" * 64, "archive_sha256": "d" * 64}
    result = validate_source_materialization([{"event": "run_start"}, src], "pr-review")
    assert result.issues
    assert result.event is None


def test_validate_source_materialization_after_malformed_target_is_invalid():
    ev = _loaded_event(local_target())
    ev["target_sha256"] = "0" * 64  # target payload/hash disagreement -> no valid preceding target
    events = [{"event": "run_start"}, ev, _source_event(local_target())]
    result = validate_source_materialization(events, "pr-review")
    assert result.issues
    assert result.event is None


def test_validate_source_materialization_wrong_kind_is_invalid():
    result = validate_source_materialization(
        _materialized_run(local_target(), kind="github-pr"), "pr-review")
    assert result.issues
    assert result.event is None


def test_validate_source_materialization_wrong_target_hash_is_invalid():
    result = validate_source_materialization(
        _materialized_run(local_target(), target_sha256="9" * 64), "pr-review")
    assert result.issues
    assert result.event is None


@pytest.mark.parametrize("bad", ["D" * 64, "d" * 63, "d" * 65, "xyz", "", "g" * 64, None, 123])
def test_validate_source_materialization_bad_archive_hash_is_invalid(bad):
    result = validate_source_materialization(
        _materialized_run(local_target(), archive_sha256=bad), "pr-review")
    assert result.issues, bad
    assert result.event is None, bad


def test_validate_source_materialization_duplicate_is_invalid():
    events = _materialized_run(local_target())
    events.append(_source_event(local_target(), archive_sha256="e" * 64))  # a second snapshot
    result = validate_source_materialization(events, "pr-review")
    assert result.issues
    assert result.event is None


def test_validate_source_materialization_before_target_is_invalid():
    # Recorded BEFORE the target_loaded event -> out of order.
    events = [{"event": "run_start"}, _source_event(local_target()), _loaded_event(local_target())]
    result = validate_source_materialization(events, "pr-review")
    assert result.issues
    assert result.event is None


def test_validate_source_materialization_after_protocol_work_is_invalid():
    # Recorded AFTER DAG/PLAN/review/status work began -> out of order.
    events = [{"event": "run_start"}, _loaded_event(local_target()),
              {"event": "dag_loaded", "gate": "plan"}, _source_event(local_target())]
    result = validate_source_materialization(events, "pr-review")
    assert result.issues
    assert result.event is None


def test_validate_source_materialization_non_pr_review_is_invalid():
    src = {"event": "source_materialized", "kind": "local-range",
           "target_sha256": "d" * 64, "archive_sha256": "d" * 64}
    for wf in ("build", "deep-dive"):
        result = validate_source_materialization([{"event": "run_start"}, src], wf)
        assert result.issues, wf
        assert result.event is None, wf


def test_validate_source_materialization_non_pr_review_absent_is_clean():
    events = [{"event": "run_start"}, {"event": "dag_loaded", "gate": "plan"}]
    for wf in ("build", "deep-dive"):
        result = validate_source_materialization(events, wf)
        assert result.issues == []
        assert result.event is None


# --------------------------------------------------------------------------------------
# Normalization: local merge-base semantics (real divergent Git repo)
# --------------------------------------------------------------------------------------

def _git(repo, *args, **kwargs):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True,
                          capture_output=True, **kwargs).stdout


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Tester")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def init_diverged_repo(tmp_path):
    """main and feature diverge: feature changes app.py; main adds an unrelated main-only.py."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "app.py").write_text("print('base')\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-q", "-m", "base")
    # feature branch changes app.py
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "app.py").write_text("print('feature change')\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-q", "-m", "feature: change app")
    # main advances with an unrelated file
    _git(repo, "checkout", "-q", "main")
    (repo / "main-only.py").write_text("print('main only')\n")
    _git(repo, "add", "main-only.py")
    _git(repo, "commit", "-q", "-m", "main: add main-only")
    return repo


def test_local_normalization_uses_merge_base_not_tip_diff(tmp_path):
    repo = init_diverged_repo(tmp_path)
    target, patch = normalize_local_target(repo, "main..feature", {"title": "t", "body": "b"})
    names = _git(repo, "diff", "--name-only",
                 f"{target.merge_base_sha}..{target.head.sha}").splitlines()
    assert names == ["app.py"]
    assert "main-only.py" not in patch.decode()


def test_raw_two_dot_diff_includes_base_only_change(tmp_path):
    # The un-normalized two-dot diff DOES mention the base-only file — the defect we normalize away.
    repo = init_diverged_repo(tmp_path)
    raw = _git(repo, "diff", "main..feature")
    assert "main-only.py" in raw


def test_local_two_and_three_dot_resolve_identically(tmp_path):
    repo = init_diverged_repo(tmp_path)
    t2, p2 = normalize_local_target(repo, "main..feature", {"title": "t", "body": "b"})
    t3, p3 = normalize_local_target(repo, "main...feature", {"title": "t", "body": "b"})
    assert t2.to_dict() == t3.to_dict()
    assert p2 == p3
    assert t2.changed_files == ("app.py",)


def test_local_target_records_all_three_shas(tmp_path):
    repo = init_diverged_repo(tmp_path)
    target, _ = normalize_local_target(repo, "main..feature", {"title": "t", "body": "b"})
    assert SHA1_RE.match(target.base.sha)
    assert SHA1_RE.match(target.head.sha)
    assert SHA1_RE.match(target.merge_base_sha)
    assert target.base.ref == "main" and target.head.ref == "feature"
    assert target.base.repository is None and target.head.repository is None


def test_local_normalization_neutralizes_ext_diff_driver(tmp_path):
    """F1a: a repo-configured external diff driver must NOT execute during normalization, and the
    recorded patch/diff_sha256 must be the real content diff, not the driver's output."""
    repo = _init_repo(tmp_path / "repo")
    sentinel = tmp_path / "driver_ran"
    driver = tmp_path / "drv.sh"
    driver.write_text(f"#!/bin/sh\ntouch {sentinel}\necho 'FORGED DRIVER OUTPUT'\n")
    driver.chmod(0o755)
    _git(repo, "config", "diff.forge.command", str(driver))
    (repo / "code.txt").write_text("v1\n")
    (repo / ".gitattributes").write_text("code.txt diff=forge\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "code.txt").write_text("v2-real-change\n")
    _git(repo, "commit", "-qam", "change code")
    target, patch = normalize_local_target(repo, "main..feature", {"title": "t", "body": "b"})
    assert not sentinel.exists(), "external diff driver executed during normalization"
    assert b"FORGED DRIVER OUTPUT" not in patch
    assert b"v2-real-change" in patch
    real = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.autocrlf=false", "diff", "--no-ext-diff",
         "--no-textconv", "--binary", f"{target.merge_base_sha}..{target.head.sha}"],
        check=True, capture_output=True).stdout
    assert target.diff_sha256 == hashlib.sha256(real).hexdigest()


def test_local_normalization_accepts_non_ascii_filename(tmp_path):
    """F1b: a changed file with a non-ASCII name is recorded with its exact UTF-8 path, not rejected
    for git C-quoting."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "base.txt").write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "caf\u00e9.py").write_text("y\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "add cafe")
    target, _ = normalize_local_target(repo, "main..feature", {"title": "t", "body": "b"})
    assert target.changed_files == ("caf\u00e9.py",)


def test_local_normalization_ascii_change_unchanged(tmp_path):
    """Regression: hardening must not change the recorded patch/changed_files for an ordinary ASCII,
    driver-free change."""
    repo = init_diverged_repo(tmp_path)
    target, patch = normalize_local_target(repo, "main..feature", {"title": "t", "body": "b"})
    assert target.changed_files == ("app.py",)
    real = subprocess.run(
        ["git", "-C", str(repo), "diff", "--no-ext-diff", "--no-textconv", "--binary",
         f"{target.merge_base_sha}..{target.head.sha}"],
        check=True, capture_output=True).stdout
    assert patch == real
    assert b"feature change" in patch


def test_parse_range_rejects_bad_inputs():
    from crucible.target import parse_range
    assert parse_range("main..feature") == ("main", "feature")
    assert parse_range("main...feature") == ("main", "feature")
    for bad in ["main", "a..b..c", "..feature", "main..", ""]:
        with pytest.raises(ValueError):
            parse_range(bad)


# --------------------------------------------------------------------------------------
# Normalization: repository identity (credential-free)
# --------------------------------------------------------------------------------------

def test_repository_identity_strips_credentials(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "remote", "add", "origin",
         "https://user:secret@github.com/owner/repo.git?token=abc#frag")
    identity = normalized_repository_identity(repo)
    assert identity == "https://github.com/owner/repo.git"
    assert "secret" not in identity and "user" not in identity
    assert "token" not in identity


def test_repository_identity_handles_scp_like_remote(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "remote", "add", "origin", "git@github.com:owner/repo.git")
    identity = normalized_repository_identity(repo)
    assert identity == "github.com/owner/repo.git"
    assert "git@" not in identity


def test_repository_identity_falls_back_to_local_hash(tmp_path):
    repo = _init_repo(tmp_path / "repo")  # no origin remote
    identity = normalized_repository_identity(repo)
    assert identity.startswith("local:")
    assert SHA256_RE.match(identity[len("local:"):])
    # never exposes the local filesystem path
    assert str(repo) not in identity
    assert os.path.realpath(str(repo)) not in identity


def test_local_normalization_identity_matches_repository_identity(tmp_path):
    repo = init_diverged_repo(tmp_path)
    _git(repo, "remote", "add", "origin", "https://user:pw@example.com/o/r.git")
    target, _ = normalize_local_target(repo, "main..feature", {"title": "t", "body": "b"})
    assert target.repository == normalized_repository_identity(repo)
    assert "pw" not in target.repository


# A file:// remote (or a bare filesystem path) names a local checkout, not a network identity. Its
# path must NEVER persist as the repository identity: normalization must fall back to the
# credential-free local fingerprint local:<sha256(real repo path)>.
@pytest.mark.parametrize("remote", [
    "file://localhost/Users/foo/checkout",           # file URL with a host
    "file:///Users/foo/checkout",                    # file URL, empty authority
    "FILE://localhost/Users/foo/checkout",           # file URL, uppercase scheme
    "File://localhost/Users/foo/checkout",           # file URL, mixed-case scheme
    "file://localhost/Users/te%20st/checkout",       # file URL, percent-encoded local path
    "file:/Users/foo/checkout",                      # file: single-slash local reference
    "/Users/foo/checkout",                           # absolute filesystem path
    "../sibling/checkout",                           # relative filesystem path
    "./checkout",                                     # relative filesystem path
])
def test_repository_identity_never_persists_local_paths(tmp_path, remote):
    repo = _init_repo(tmp_path / "repo")
    _git(repo, "remote", "add", "origin", remote)
    real = os.path.realpath(str(repo))
    expected = "local:" + hashlib.sha256(real.encode("utf-8")).hexdigest()
    identity = normalized_repository_identity(repo)
    # Falls back to the local fingerprint — never the remote path text or the file scheme.
    assert identity == expected, (remote, identity)
    assert "file" not in identity.lower()
    assert "checkout" not in identity and "Users" not in identity
    assert real not in identity and str(repo) not in identity


def test_local_normalization_with_file_remote_uses_local_fingerprint(tmp_path):
    repo = init_diverged_repo(tmp_path)
    _git(repo, "remote", "add", "origin", "file://localhost/Users/foo/checkout")
    target, _ = normalize_local_target(repo, "main..feature", {"title": "t", "body": "b"})
    real = os.path.realpath(str(repo))
    assert target.repository == "local:" + hashlib.sha256(real.encode("utf-8")).hexdigest()
    assert "file" not in target.repository.lower() and "checkout" not in target.repository


# --------------------------------------------------------------------------------------
# Normalization: GitHub PR metadata
# --------------------------------------------------------------------------------------

def _gh_metadata(**overrides):
    md = {
        "number": 7,
        "url": "https://github.com/base/repo/pull/7",
        "title": "Fix A",
        "body": "Details",
        "files": [{"path": "src/a.py"}],
        "baseRefName": "main",
        "baseRefOid": "1" * 40,
        "headRefName": "feature",
        "headRefOid": "2" * 40,
        "headRepository": {"nameWithOwner": "fork/repo"},
        "headRepositoryOwner": {"login": "fork"},
        "isCrossRepository": True,
    }
    md.update(overrides)
    return md


def _codeload_archive(path, wrapper, files):
    """A GitHub codeload-style tarball: every path nested under a single ``wrapper/`` directory (the
    ``owner-repo-<sha>/`` prefix ``gh api repos/.../tarball/<sha>`` emits), which the normalizer strips
    before deriving the snapshot patch. ``files`` maps a repository-relative path to its exact bytes."""
    import tarfile as _tf

    def build(tar):
        _add_dir(tar, f"{wrapper}/")
        seen = {""}
        for rel in sorted(files):
            parts = rel.split("/")
            for i in range(1, len(parts)):
                d = "/".join(parts[:i])
                if d not in seen:
                    _add_dir(tar, f"{wrapper}/{d}/")
                    seen.add(d)
            info = _tf.TarInfo(f"{wrapper}/{rel}")
            info.size = len(files[rel])
            tar.addfile(info, io.BytesIO(files[rel]))
    return _write_tar(path, build)


def _compare_metadata(*, base_sha="1" * 40, merge_base_sha="3" * 40, files=None, **overrides):
    """A GitHub ``repos/.../compare/BASE...HEAD`` payload (the exact-OID compare on the base repo).

    ``base_commit.sha`` is the compared base (== ``baseRefOid``); ``merge_base_commit.sha`` is the PR
    fork point the immutable patch is derived from; ``files[].filename`` is the three-dot (merge-base)
    changed-file view the derived patch must agree with. ``files`` defaults to ``["src/a.py"]`` to match
    ``_gh_metadata``'s default file list."""
    if files is None:
        files = ["src/a.py"]
    md = {
        "base_commit": {"sha": base_sha},
        "merge_base_commit": {"sha": merge_base_sha},
        "status": "ahead",
        "files": [{"filename": p, "status": "modified"} for p in files],
    }
    md.update(overrides)
    return md


def test_github_normalization_preserves_cross_fork_identity(tmp_path):
    merge_base = _codeload_archive(tmp_path / "merge-base.tar", "base-repo-333",
                                   {"src/a.py": b"print('base')\n"})
    head = _codeload_archive(tmp_path / "head.tar", "fork-repo-222",
                             {"src/a.py": b"print('head')\n"})
    target, diff = normalize_github_target(
        _gh_metadata(), _gh_metadata(), _compare_metadata(), merge_base, head)
    assert target.kind == "github-pr"
    assert target.repository == "base/repo"
    assert target.base.repository == "base/repo" and target.base.sha == "1" * 40
    assert target.head.repository == "fork/repo" and target.head.sha == "2" * 40
    assert target.base.ref == "main" and target.head.ref == "feature"
    assert target.merge_base_sha == "3" * 40  # recorded from the compare endpoint's merge_base_commit
    assert target.is_cross_repository is True
    assert target.pr_number == 7
    assert target.changed_files == ("src/a.py",)
    # The patch is DERIVED from merge-base -> head (not accepted from a caller), and its hash binds it.
    assert target.diff_sha256 == hashlib.sha256(diff).hexdigest()
    text = diff.decode()
    assert "a/src/a.py b/src/a.py" in text
    assert "-print('base')" in text and "+print('head')" in text


def test_github_normalization_derives_patch_from_merge_base_head_snapshots(tmp_path):
    # F1: the immutable patch is DERIVED from the merge-base and head OID snapshots — never accepted from
    # `gh pr diff` or any caller-supplied patch. An unchanged file must not appear in the derived patch.
    merge_base = _codeload_archive(tmp_path / "merge-base.tar", "base-repo-aaa",
                                   {"src/app.py": b"print('base')\n", "README.md": b"same\n"})
    head = _codeload_archive(tmp_path / "head.tar", "fork-repo-bbb",
                             {"src/app.py": b"print('head')\n", "README.md": b"same\n"})
    md = _gh_metadata(files=[{"path": "src/app.py"}])
    cmp = _compare_metadata(files=["src/app.py"])
    target, diff = normalize_github_target(md, md, cmp, merge_base, head)
    assert target.changed_files == ("src/app.py",)
    text = diff.decode()
    assert "a/src/app.py b/src/app.py" in text
    assert "README.md" not in text  # an unchanged file is never in the derived patch
    # Deterministic: a re-derivation from the same snapshots yields byte-identical patch + hash.
    target2, diff2 = normalize_github_target(md, md, cmp, merge_base, head)
    assert diff2 == diff and target2.diff_sha256 == target.diff_sha256


def test_github_normalization_excludes_base_only_commit_after_fork(tmp_path):
    # F1 regression: the base branch advanced past the PR fork point with a base-only commit (a normal PR
    # case). Deriving base-tip -> head would show that base-only file as a REVERSE change; deriving
    # merge-base -> head shows ONLY the feature change. baseRefOid is the *current* base tip; the compare
    # endpoint reports the fork point as merge_base, and the merge-base/head archives are the fork/head
    # snapshots (the base-only file exists in NEITHER, so it can never enter the derived patch).
    base_tip_oid, fork_oid, head_oid = "a" * 40, "b" * 40, "2" * 40
    merge_base = _codeload_archive(tmp_path / "merge-base.tar", "base-repo-fork",
                                   {"feature.py": b"v1\n", "shared.py": b"shared\n"})
    head = _codeload_archive(tmp_path / "head.tar", "fork-repo-head",
                             {"feature.py": b"v2\n", "shared.py": b"shared\n"})
    md = _gh_metadata(baseRefOid=base_tip_oid, headRefOid=head_oid,
                      files=[{"path": "feature.py"}])
    cmp = _compare_metadata(base_sha=base_tip_oid, merge_base_sha=fork_oid,
                            files=["feature.py"])
    target, diff = normalize_github_target(md, md, cmp, merge_base, head)

    # Only the feature change is derived; the base-only commit never appears as a reverse change.
    assert target.changed_files == ("feature.py",)
    text = diff.decode()
    assert "a/feature.py b/feature.py" in text
    assert "-v1" in text and "+v2" in text
    assert "base_only" not in text and "shared.py" not in text
    # The recorded merge base is the fork point, NOT the advanced base tip.
    assert target.merge_base_sha == fork_oid
    assert target.base.sha == base_tip_oid and target.merge_base_sha != target.base.sha


def test_github_patch_is_pure_function_of_snapshots_not_caller(tmp_path):
    # F1 ABA regression: the derived target is a pure function of the merge-base/head archives. There is
    # NO caller diff parameter to smuggle an unrelated "B" patch through, so before/after/compare "A"
    # metadata with any B-flavoured prose can never change the patch — only the snapshots decide it.
    import inspect
    params = inspect.signature(normalize_github_target).parameters
    assert "diff" not in params, \
        "normalize_github_target must not accept a caller-supplied diff (derive from snapshots)"

    mb_a = _codeload_archive(tmp_path / "mbA.tar", "base-repo-a1",
                             {"src/app.py": b"print('A base')\n"})
    head_a = _codeload_archive(tmp_path / "headA.tar", "fork-repo-a2",
                               {"src/app.py": b"print('A head')\n"})
    head_b = _codeload_archive(tmp_path / "headB.tar", "fork-repo-b2",
                               {"src/app.py": b"print('B head is unrelated')\n"})
    md = _gh_metadata(files=[{"path": "src/app.py"}])
    cmp = _compare_metadata(files=["src/app.py"])

    target_a, diff_a = normalize_github_target(md, md, cmp, mb_a, head_a)
    # A known-A snapshot pair yields a stable A patch/hash regardless of metadata/compare prose.
    md_prose = _gh_metadata(files=[{"path": "src/app.py"}], title="totally different", body="B story")
    cmp_prose = _compare_metadata(files=["src/app.py"], status="diverged", ahead_by=99)
    target_a2, diff_a2 = normalize_github_target(md_prose, md_prose, cmp_prose, mb_a, head_a)
    assert diff_a2 == diff_a and target_a2.diff_sha256 == target_a.diff_sha256

    # Swapping in an unrelated B head snapshot is the ONLY way the patch changes — it tracks content.
    target_b, diff_b = normalize_github_target(md, md, cmp, mb_a, head_b)
    assert diff_b != diff_a and target_b.diff_sha256 != target_a.diff_sha256


def test_github_normalization_changed_files_come_from_snapshot_not_metadata(tmp_path):
    # Round-2 F1: changed_files is derived SOLELY from the merge-base->head snapshot patch. GitHub's own
    # `files` view (PR metadata + compare) is informational/untrusted — it paginates/truncates on large
    # PRs and applies rename detection — so a metadata/compare file list that DISAGREES with the snapshot
    # is TOLERATED (never a false rejection) and never becomes the authoritative changed-files set.
    merge_base = _codeload_archive(tmp_path / "merge-base.tar", "base-repo-x",
                                   {"src/app.py": b"a\n"})
    head = _codeload_archive(tmp_path / "head.tar", "fork-repo-y",
                             {"src/app.py": b"b\n"})
    # Both the metadata and the compare claim a completely different file; the snapshot is authoritative.
    lying = _gh_metadata(files=[{"path": "src/lies.py"}])
    target, _diff = normalize_github_target(
        lying, lying, _compare_metadata(files=["src/also-lies.py"]), merge_base, head)
    assert target.changed_files == ("src/app.py",)  # the snapshot content delta, not the lying lists


@pytest.mark.parametrize("mutate,match", [
    (lambda c: c.__setitem__("base_commit", {"sha": "9" * 40}), "base_commit"),
    (lambda c: c["base_commit"].__setitem__("sha", "not-a-sha"), "base_commit"),
    (lambda c: c.pop("base_commit"), "base_commit"),
    (lambda c: c.pop("merge_base_commit"), "merge_base"),
    (lambda c: c["merge_base_commit"].__setitem__("sha", ""), "merge_base"),
    (lambda c: c["merge_base_commit"].__setitem__("sha", "3" * 39), "merge_base"),
    (lambda c: c["merge_base_commit"].__setitem__("sha", "Z" * 40), "merge_base"),
])
def test_github_normalization_rejects_malformed_compare_metadata(tmp_path, mutate, match):
    # F1: the compare payload is validated fail-closed on the two IMMUTABLE facts it establishes — the
    # base_commit.sha must equal baseRefOid and the merge_base_commit.sha must be a valid 40 lowercase
    # hex fork point. Any drift there rejects the whole target so the orchestrator can retry. (The
    # compare `files` list is informational only and is covered by the tolerance tests, not here.)
    merge_base = _codeload_archive(tmp_path / "merge-base.tar", "base-repo-1", {"src/a.py": b"o\n"})
    head = _codeload_archive(tmp_path / "head.tar", "fork-repo-2", {"src/a.py": b"n\n"})
    cmp = _compare_metadata()
    mutate(cmp)
    with pytest.raises(ValueError, match=match):
        normalize_github_target(_gh_metadata(), _gh_metadata(), cmp, merge_base, head)


def test_github_normalization_captures_new_deleted_binary(tmp_path):
    merge_base = _codeload_archive(tmp_path / "merge-base.tar", "base-repo-1", {
        "keep.py": b"v1\n", "gone.py": b"bye\n", "img.bin": b"\x00\x01\x02BIN\xff",
    })
    head = _codeload_archive(tmp_path / "head.tar", "fork-repo-2", {
        "keep.py": b"v2\n", "new.py": b"hi\n", "img.bin": b"\x00\x01\x02CHANGED\xff",
    })
    files = ["gone.py", "img.bin", "keep.py", "new.py"]
    md = _gh_metadata(files=[{"path": p} for p in files])
    cmp = _compare_metadata(files=files)
    target, diff = normalize_github_target(md, md, cmp, merge_base, head)
    assert target.changed_files == ("gone.py", "img.bin", "keep.py", "new.py")
    text = diff.decode("utf-8", "replace")
    assert "new file mode" in text and "deleted file mode" in text and "GIT binary patch" in text


def test_github_normalization_same_repo_pr_is_not_cross(tmp_path):
    merge_base = _codeload_archive(tmp_path / "merge-base.tar", "base-repo-1", {"src/a.py": b"o\n"})
    head = _codeload_archive(tmp_path / "head.tar", "base-repo-2", {"src/a.py": b"n\n"})
    md = _gh_metadata(headRepository={"nameWithOwner": "base/repo"},
                      headRepositoryOwner={"login": "base"}, isCrossRepository=False)
    target, _diff = normalize_github_target(md, md, _compare_metadata(), merge_base, head)
    assert target.is_cross_repository is False
    assert target.head.repository == "base/repo"


@pytest.mark.parametrize("field,value", [
    ("baseRefOid", "9" * 40),
    ("headRefOid", "9" * 40),
    ("number", 8),
    ("url", "https://github.com/base/repo/pull/8"),
    ("title", "changed"),
    ("body", "changed"),
    ("baseRefName", "master"),
    ("headRefName", "other"),
    ("headRepository", {"nameWithOwner": "fork2/repo"}),
    ("isCrossRepository", False),
])
def test_github_normalization_rejects_before_after_mismatch(tmp_path, field, value):
    # Identity drift is detected BEFORE any archive is extracted, so the archives are never even read.
    # The changed-file list is deliberately NOT an identity field (see the files-drift tolerance test):
    # only PR number/URL/title/body, base/head refs+OIDs, head repository, and cross-repo flag gate here.
    merge_base = _codeload_archive(tmp_path / "merge-base.tar", "base-repo-1", {"src/a.py": b"o\n"})
    head = _codeload_archive(tmp_path / "head.tar", "fork-repo-2", {"src/a.py": b"n\n"})
    before = _gh_metadata()
    after = _gh_metadata(**{field: value})
    with pytest.raises(ValueError):
        normalize_github_target(before, after, _compare_metadata(), merge_base, head)


def test_github_normalization_accepts_stable_identity_reordered_files(tmp_path):
    # Same identity in a different `files` list order is still stable identity (files are not part of the
    # identity tuple at all), and the derived changed-files set comes from the snapshots — here both
    # src/a.py and src/b.py changed, so the snapshot-derived set is (src/a.py, src/b.py) regardless of
    # the metadata/compare file order.
    merge_base = _codeload_archive(tmp_path / "merge-base.tar", "base-repo-1",
                                   {"src/a.py": b"a1\n", "src/b.py": b"b1\n"})
    head = _codeload_archive(tmp_path / "head.tar", "fork-repo-2",
                             {"src/a.py": b"a2\n", "src/b.py": b"b2\n"})
    before = _gh_metadata(files=[{"path": "src/a.py"}, {"path": "src/b.py"}])
    after = _gh_metadata(files=[{"path": "src/b.py"}, {"path": "src/a.py"}])
    cmp = _compare_metadata(files=["src/b.py", "src/a.py"])
    target, _diff = normalize_github_target(before, after, cmp, merge_base, head)
    assert target.changed_files == ("src/a.py", "src/b.py")


def test_github_normalization_rename_derives_old_and_new_paths(tmp_path):
    # Round-2 F1 regression: a rename the snapshot tree diff does NOT coalesce (dissimilar content, or —
    # in the field — a large diff where git skips inexact rename detection). It yields BOTH the deleted
    # old path and the added new path, while GitHub's rename-detected `files` view reports only the new
    # path. The earlier strict-equality gate FALSELY rejected this normal PR — now the snapshot-derived
    # old+new set is accepted and recorded, and the new-only file list never gates.
    merge_base = _codeload_archive(tmp_path / "merge-base.tar", "base-repo-r",
                                   {"src/old.py": b"def legacy():\n    return compute_old_value()\n"})
    head = _codeload_archive(tmp_path / "head.tar", "fork-repo-r",
                             {"src/new.py": b"class Rewritten:\n    field = 42\n"})
    md = _gh_metadata(files=[{"path": "src/new.py"}])       # GitHub rename detection -> new path only
    cmp = _compare_metadata(files=["src/new.py"])           # compare rename detection -> new path only
    target, diff = normalize_github_target(md, md, cmp, merge_base, head)
    assert target.changed_files == ("src/new.py", "src/old.py")  # both paths, from the snapshot
    text = diff.decode()
    assert "deleted file mode" in text and "new file mode" in text
    assert "a/src/old.py" in text and "b/src/new.py" in text


def test_github_normalization_tolerates_absent_compare_files(tmp_path):
    # Round-2 F1: the compare `files` list is informational — a compare payload with NO `files` key is
    # tolerated (large-PR compare responses omit/truncate it). The base_commit/merge_base validation and
    # the snapshot-derived patch still fully bind the target.
    merge_base = _codeload_archive(tmp_path / "merge-base.tar", "base-repo-a", {"src/a.py": b"o\n"})
    head = _codeload_archive(tmp_path / "head.tar", "fork-repo-b", {"src/a.py": b"n\n"})
    cmp = _compare_metadata()
    cmp.pop("files")
    target, _diff = normalize_github_target(_gh_metadata(), _gh_metadata(), cmp, merge_base, head)
    assert target.changed_files == ("src/a.py",)
    assert target.merge_base_sha == "3" * 40


def test_github_normalization_tolerates_truncated_file_lists(tmp_path):
    # Round-2 F1: GitHub paginates/truncates the compare + PR `files` view on large PRs. A file list that
    # is a strict SUBSET of the real change (truncation) must not gate — the full changed-files set comes
    # from the snapshot patch.
    merge_base = _codeload_archive(tmp_path / "merge-base.tar", "base-repo-a",
                                   {"a.py": b"1\n", "b.py": b"1\n", "c.py": b"1\n"})
    head = _codeload_archive(tmp_path / "head.tar", "fork-repo-b",
                             {"a.py": b"2\n", "b.py": b"2\n", "c.py": b"2\n"})
    md = _gh_metadata(files=[{"path": "a.py"}])            # truncated to 1 of 3
    cmp = _compare_metadata(files=["a.py"])                # truncated to 1 of 3
    target, _diff = normalize_github_target(md, md, cmp, merge_base, head)
    assert target.changed_files == ("a.py", "b.py", "c.py")   # the full snapshot set


def test_github_normalization_ignores_files_drift_between_reads(tmp_path):
    # Round-2 F1: the changed-file list is NOT part of the immutable identity tuple — GitHub's `files`
    # view can legitimately reorder/paginate/rename-detect differently between the two reads without the
    # review target changing. A files-only drift must NOT trigger a retry; an intent (title) drift still
    # does.
    merge_base = _codeload_archive(tmp_path / "merge-base.tar", "base-repo-a", {"src/a.py": b"o\n"})
    head = _codeload_archive(tmp_path / "head.tar", "fork-repo-b", {"src/a.py": b"n\n"})
    before = _gh_metadata(files=[{"path": "src/a.py"}])
    after = _gh_metadata(files=[{"path": "src/a.py"}, {"path": "src/late.py"}])  # moved between reads
    target, _diff = normalize_github_target(before, after, _compare_metadata(), merge_base, head)
    assert target.changed_files == ("src/a.py",)              # still derived from the snapshot
    with pytest.raises(ValueError, match="changed during normalization"):
        normalize_github_target(before, _gh_metadata(title="edited"), _compare_metadata(),
                                merge_base, head)


def test_github_normalization_large_paginated_file_list_uses_snapshot(tmp_path):
    # Round-2 F1: a large PR whose GitHub compare/`files` view is PAGINATED (only the first page is
    # present) must normalize from the snapshot, not the partial list. Shaped like pagination (many
    # entries) without huge data — tiny file bodies.
    base_bodies = {f"pkg/mod_{i:02d}.py": (str(i) + "\n").encode() for i in range(25)}
    head_bodies = {p: b"new-" + c for p, c in base_bodies.items()}
    merge_base = _codeload_archive(tmp_path / "merge-base.tar", "base-repo-a", base_bodies)
    head = _codeload_archive(tmp_path / "head.tar", "fork-repo-b", head_bodies)
    first_page = sorted(base_bodies)[:10]                    # GitHub returned only page 1 (10 of 25)
    md = _gh_metadata(files=[{"path": p} for p in first_page])
    cmp = _compare_metadata(files=first_page)
    target, _diff = normalize_github_target(md, md, cmp, merge_base, head)
    assert target.changed_files == tuple(sorted(base_bodies))  # all 25 from the snapshot, not 10
    assert len(target.changed_files) == 25


# --------------------------------------------------------------------------------------
# Raw snapshot tree construction (F1): the derived patch must reflect the EXACT archive bytes
# and modes, built with plumbing that never runs a clean/smudge/eol/working-tree-encoding filter
# (never `git add`), so an in-tree `.gitattributes` cannot rewrite the reviewed content.
# --------------------------------------------------------------------------------------

def _codeload_archive_modes(path, wrapper, files):
    """A GitHub codeload tarball where ``files`` maps a repo-relative path to ``(bytes, mode)`` so a
    fixture can carry an in-tree ``.gitattributes`` and per-file executable bits verbatim."""
    def build(tar):
        _add_dir(tar, f"{wrapper}/")
        seen = {""}
        for rel in sorted(files):
            data, mode = files[rel]
            parts = rel.split("/")
            for i in range(1, len(parts)):
                d = "/".join(parts[:i])
                if d not in seen:
                    _add_dir(tar, f"{wrapper}/{d}/")
                    seen.add(d)
            info = tarfile.TarInfo(f"{wrapper}/{rel}")
            info.size = len(data)
            info.mode = mode
            tar.addfile(info, io.BytesIO(data))
    return _write_tar(path, build)


def test_github_snapshot_patch_preserves_crlf_under_hostile_gitattributes(tmp_path):
    # F1 REGRESSION (RED under `git add`): an in-tree `.gitattributes` that requests EOL/text
    # normalization must NOT rewrite the reviewed bytes. Under `git add`, `* text=auto` runs the CRLF
    # clean filter on check-in, so the snapshot blobs (and the derived patch) lose their carriage
    # returns — the review would see phantom line-ending changes. The raw plumbing hashes the exact
    # archive bytes, so the CRLF survives in the derived patch on BOTH sides of the change.
    attrs = (b"* text=auto\n*.txt eol=crlf\n", 0o644)
    merge_base = _codeload_archive_modes(tmp_path / "merge-base.tar", "base-repo-1", {
        ".gitattributes": attrs, "data.txt": (b"alpha\r\nbeta\r\n", 0o644)})
    head = _codeload_archive_modes(tmp_path / "head.tar", "fork-repo-2", {
        ".gitattributes": attrs, "data.txt": (b"alpha\r\ngamma\r\n", 0o644)})
    md = _gh_metadata(files=[{"path": "data.txt"}])
    cmp = _compare_metadata(files=["data.txt"])
    target, diff = normalize_github_target(md, md, cmp, merge_base, head)
    assert b"\r" in diff, "the derived patch must preserve the archive's CRLF bytes exactly"
    assert b"beta\r" in diff and b"gamma\r" in diff  # both sides keep their carriage returns
    assert target.diff_sha256 == hashlib.sha256(diff).hexdigest()


def test_github_snapshot_patch_preserves_executable_bit(tmp_path):
    # F1 REGRESSION (RED under `git add`): the archive's executable mode must reach the Git tree. Under
    # `git add`, extraction dropped the exec bit and both trees were 100644, so an exec-only change
    # produced an EMPTY patch (the review missed a real permission change). The raw plumbing preserves
    # the archive mode: an exec-bit flip shows a mode change, and a new executable file is 100755.
    unchanged = b"#!/bin/sh\necho hi\n"
    merge_base = _codeload_archive_modes(tmp_path / "merge-base.tar", "base-repo-1", {
        "run.sh": (unchanged, 0o644)})
    head = _codeload_archive_modes(tmp_path / "head.tar", "fork-repo-2", {
        "run.sh": (unchanged, 0o755), "new-exe.sh": (b"#!/bin/sh\ntrue\n", 0o755)})
    files = ["new-exe.sh", "run.sh"]
    md = _gh_metadata(files=[{"path": p} for p in files])
    cmp = _compare_metadata(files=files)
    _target, diff = normalize_github_target(md, md, cmp, merge_base, head)
    text = diff.decode("utf-8", "replace")
    assert "old mode 100644" in text and "new mode 100755" in text  # run.sh exec-bit flip is visible
    assert "new file mode 100755" in text                            # new-exe.sh is executable


def test_github_snapshot_patch_handles_unusual_filenames_via_nul_plumbing(tmp_path):
    # F1: names with spaces/tabs/newlines are carried through the tree build with NUL-delimited plumbing
    # (never a whitespace-split or C-quoted parse). The only changed file has a space+tab+newline in its
    # name; its new content must appear in the derived patch AND the snapshot-derived changed-file set
    # must carry the name back verbatim — so a regression that whitespace-splits or C-quotes the path
    # (mis-parsing changed_files) is caught, not just one that drops the body.
    weird = "weird dir/a b\tc\nd.py"
    merge_base = _codeload_archive_modes(tmp_path / "merge-base.tar", "base-repo-1", {
        weird: (b"OLDBODY\n", 0o644)})
    head = _codeload_archive_modes(tmp_path / "head.tar", "fork-repo-2", {
        weird: (b"NEWBODY\n", 0o644)})
    md = _gh_metadata(files=[{"path": weird}])
    cmp = _compare_metadata(files=[weird])
    target, diff = normalize_github_target(md, md, cmp, merge_base, head)
    assert b"OLDBODY" in diff and b"NEWBODY" in diff
    assert target.changed_files == (weird,)  # NUL-delimited name carried back exactly, unquoted


def test_github_snapshot_patch_omits_empty_dirs_keeps_empty_and_binary_files(tmp_path):
    # F1: Git tracks only files, so an empty directory member is omitted (as `git add` did); an empty
    # (zero-byte) new file and a binary new file are both captured.
    merge_base = _codeload_archive_modes(tmp_path / "merge-base.tar", "base-repo-1", {
        "keep.py": (b"v1\n", 0o644)})
    def build(tar):
        _add_dir(tar, "fork-repo-2/")
        _add_dir(tar, "fork-repo-2/emptydir/")          # empty directory -> must not appear
        _add_file(tar, "fork-repo-2/keep.py", b"v1\n")   # unchanged
        _add_file(tar, "fork-repo-2/empty.txt", b"")     # new zero-byte file
        info = tarfile.TarInfo("fork-repo-2/img.bin"); info.size = 6
        tar.addfile(info, io.BytesIO(b"\x00\x01\x02\xff\xfeZ"))
    head = _write_tar(tmp_path / "head.tar", build)
    files = ["empty.txt", "img.bin"]
    md = _gh_metadata(files=[{"path": p} for p in files])
    cmp = _compare_metadata(files=files)
    target, diff = normalize_github_target(md, md, cmp, merge_base, head)
    text = diff.decode("utf-8", "replace")
    assert "emptydir" not in text and "emptydir" not in " ".join(target.changed_files)
    assert "empty.txt" in target.changed_files and "img.bin" in target.changed_files
    assert "keep.py" not in target.changed_files          # unchanged file absent
    assert "GIT binary patch" in text                     # binary new file captured


def test_snapshot_tree_construction_never_uses_git_add(tmp_path):
    # F1 guard: the snapshot tree is built from raw bytes with filter-free plumbing (hash-object
    # --no-filters + update-index/mktree), never `git add`, so no clean/smudge/eol filter can run.
    import inspect
    import crucible.target as t
    for fn in (t._write_snapshot_tree, t._git_snapshot_diff, t._derive_github_patch):
        src = inspect.getsource(fn)
        assert '"add"' not in src and "'add'" not in src, \
            f"{fn.__name__} must not shell out to `git add` (it runs check-in filters)"
    tree_src = inspect.getsource(t._write_snapshot_tree)
    assert "hash-object" in tree_src and "--no-filters" in tree_src
    assert "update-index" in tree_src or "mktree" in tree_src
    # The member is STREAMED into git via its stdin fd, never loaded whole into memory (a member may be
    # up to MAX_ARCHIVE_BYTES), so snapshot hashing must not read the entire file with read_bytes().
    assert "read_bytes(" not in tree_src, "snapshot hashing must stream the file, not read it whole"
    assert "stdin=" in tree_src, "snapshot hashing must stream the file descriptor into git"


# --------------------------------------------------------------------------------------
# Normalization: diff-file (patch only)
# --------------------------------------------------------------------------------------

def test_diff_normalization_is_revision_unbound():
    diff = (b"diff --git a/src/a.py b/src/a.py\n"
            b"--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-old\n+new\n")
    target = normalize_diff_target(diff, {"title": "Patch", "body": "b"})
    assert target.kind == "diff-file"
    assert target.revision_bound is False
    assert target.repository is None
    assert target.base is None and target.head is None
    assert target.diff_sha256 == hashlib.sha256(diff).hexdigest()
    assert target.changed_files == ("src/a.py",)


def test_diff_normalization_empty_patch_has_no_changed_files():
    target = normalize_diff_target(b"", {"title": "t", "body": "b"})
    assert target.changed_files == ()
    assert target.diff_sha256 == hashlib.sha256(b"").hexdigest()


def _diff_changed(diff: bytes):
    return normalize_diff_target(diff, {"title": "t", "body": "b"}).changed_files


def test_diff_changed_files_modified_non_ascii():
    # git C-quotes a non-ASCII path: cafe + U+00E9 -> \303\251 (UTF-8 octal).
    diff = (b'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
            b'index 111..222 100644\n'
            b'--- "a/caf\\303\\251.py"\n'
            b'+++ "b/caf\\303\\251.py"\n'
            b'@@ -1 +1 @@\n-old\n+new\n')
    assert _diff_changed(diff) == ("caf\u00e9.py",)


def test_diff_changed_files_deleted_non_ascii():
    # A deletion: +++ is /dev/null, so the path must come from --- (or the header), not +++.
    diff = (b'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
            b'deleted file mode 100644\nindex 111..0000000\n'
            b'--- "a/caf\\303\\251.py"\n+++ /dev/null\n'
            b'@@ -1 +0,0 @@\n-gone\n')
    assert _diff_changed(diff) == ("caf\u00e9.py",)


def test_diff_changed_files_added_non_ascii():
    # An addition: --- is /dev/null, path comes from +++.
    diff = (b'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
            b'new file mode 100644\nindex 0000000..111\n'
            b'--- /dev/null\n+++ "b/caf\\303\\251.py"\n'
            b'@@ -0,0 +1 @@\n+hi\n')
    assert _diff_changed(diff) == ("caf\u00e9.py",)


def test_diff_changed_files_path_with_space_unquoted():
    # git does NOT quote a space; the header is unquoted and --- / +++ carry the path.
    diff = (b'diff --git a/my file.py b/my file.py\n'
            b'index 1..2 100644\n--- a/my file.py\n+++ b/my file.py\n@@ -1 +1 @@\n-a\n+b\n')
    assert _diff_changed(diff) == ("my file.py",)


def test_diff_changed_files_path_with_tab_quoted():
    diff = (b'diff --git "a/a\\tb.py" "b/a\\tb.py"\n'
            b'index 1..2 100644\n--- "a/a\\tb.py"\n+++ "b/a\\tb.py"\n@@ -1 +1 @@\n-a\n+b\n')
    assert _diff_changed(diff) == ("a\tb.py",)


def test_diff_changed_files_mode_only_path_with_b_slash():
    # A pure mode change has NO ---/+++ lines; the only path source is the (unquoted, ambiguous)
    # header. Path 'a b/x' literally contains ' b/', so a naive first-' b/' split corrupts it.
    diff = (b'diff --git a/a b/x b/a b/x\nold mode 100644\nnew mode 100755\n')
    assert _diff_changed(diff) == ("a b/x",)


def test_diff_changed_files_rename_non_ascii():
    diff = (b'diff --git "a/old\\303\\251.py" "b/new\\303\\251.py"\n'
            b'similarity index 100%\n'
            b'rename from "old\\303\\251.py"\n'
            b'rename to "new\\303\\251.py"\n')
    assert _diff_changed(diff) == ("new\u00e9.py", "old\u00e9.py")


def test_diff_changed_files_ascii_unchanged():
    diff = (b"diff --git a/src/app.py b/src/app.py\n"
            b"index 1..2 100644\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-x\n+y\n")
    assert _diff_changed(diff) == ("src/app.py",)


def test_diff_changed_files_malformed_header_does_not_raise():
    diff = b"diff --git bogusheader with no valid paths\nnot a real diff body\n"
    # best-effort: must not raise; yields no bogus entry
    assert _diff_changed(diff) == ()


def test_diff_changed_files_raw_hunk_content_not_parsed_as_header():
    # A raw patch (no 'diff --git') whose hunk BODY contains lines that look like file headers
    # (`--- a/x` / `+++ b/x`) must NOT be recorded — they are removed/added CONTENT, not headers.
    diff = (b"--- a/real.txt\n+++ b/real.txt\n"
            b"@@ -1,3 +1,3 @@\n"
            b" context line\n"
            b"--- a/injected.txt\n"
            b"+++ b/injected.txt\n"
            b" tail context\n")
    assert _diff_changed(diff) == ("real.txt",)


def test_diff_changed_files_raw_multi_file_patch():
    # A raw multi-file patch: both file headers (delimited only by ---/+++ pairs, no 'diff --git')
    # must be captured once each hunk body is consumed.
    diff = (b"--- a/file1.txt\n+++ b/file1.txt\n@@ -1 +1 @@\n-a\n+b\n"
            b"--- a/file2.txt\n+++ b/file2.txt\n@@ -1 +1 @@\n-c\n+d\n")
    assert _diff_changed(diff) == ("file1.txt", "file2.txt")


# --------------------------------------------------------------------------------------
# Confined source materialization (real tarfile fixtures; limits shrunk, not exhausted)
# --------------------------------------------------------------------------------------

import io
import crucible.target as target_mod


def _add_file(tar, name, data=b"x", *, size=None):
    info = tarfile.TarInfo(name)
    info.size = len(data) if size is None else size
    tar.addfile(info, io.BytesIO(data))


def _add_dir(tar, name):
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    tar.addfile(info)


def _add_special(tar, name, typeflag, linkname=""):
    info = tarfile.TarInfo(name)
    info.type = typeflag
    info.linkname = linkname
    tar.addfile(info)


def _write_tar(path, build):
    with tarfile.open(path, "w") as tar:
        build(tar)
    return path


def _github_style_archive(path):
    def build(tar):
        _add_dir(tar, "owner-repo-abc123/")
        _add_file(tar, "owner-repo-abc123/README.md", b"# readme\n")
        _add_dir(tar, "owner-repo-abc123/src/")
        _add_file(tar, "owner-repo-abc123/src/a.py", b"print('a')\n")
    return _write_tar(path, build)


def test_extract_github_style_strips_top_level(tmp_path):
    archive = _github_style_archive(tmp_path / "src.tar")
    dest = tmp_path / "source"
    safe_extract_source_archive(archive, dest, strip_wrapper=True)
    assert (dest / "README.md").read_text() == "# readme\n"
    assert (dest / "src" / "a.py").read_text() == "print('a')\n"
    # the wrapper directory is stripped, not materialized
    assert not (dest / "owner-repo-abc123").exists()
    # staging is gone after the atomic rename
    assert not (tmp_path / "source.staging").exists()
    # extraction NEVER writes Crucible metadata into the reviewed source, nor an adjacent receipt
    assert sorted(p.name for p in dest.iterdir()) == ["README.md", "src"]
    assert not source_receipt_path(dest).exists()


def test_source_receipt_helpers_bind_triple():
    r = source_receipt("t" * 64, "a" * 64, "github-pr")
    assert r["target_sha256"] == "t" * 64 and r["archive_sha256"] == "a" * 64
    assert r["kind"] == "github-pr"
    assert source_receipt_matches(r, "t" * 64, "a" * 64, "github-pr")
    assert not source_receipt_matches(r, "0" * 64, "a" * 64, "github-pr")
    assert not source_receipt_matches(r, "t" * 64, "0" * 64, "github-pr")
    assert not source_receipt_matches(r, "t" * 64, "a" * 64, "local-range")
    assert not source_receipt_matches(None, "t" * 64, "a" * 64, "github-pr")


def test_source_receipt_path_is_adjacent_to_source(tmp_path):
    # F2: the crash-repair receipt is a SIBLING run-state file (`RUN/source.receipt.json`), never a
    # member of the reviewed source tree.
    dest = tmp_path / "run" / "source"
    p = source_receipt_path(dest)
    assert p == dest.with_name("source" + SOURCE_RECEIPT_SUFFIX)
    assert p.parent == dest.parent and p.name == "source.receipt.json"


def test_write_source_receipt_is_adjacent_not_inside_source(tmp_path):
    # F2: the receipt is written to the adjacent run-state path OUTSIDE RUN/source (so RUN/source stays
    # exactly the archive members), and read_source_receipt round-trips it from there.
    archive = _github_style_archive(tmp_path / "src.tar")
    dest = tmp_path / "source"
    safe_extract_source_archive(archive, dest, strip_wrapper=True)
    receipt = source_receipt("t" * 64, "a" * 64, "github-pr")
    write_source_receipt(dest, receipt)
    assert source_receipt_path(dest).exists()
    assert not (dest / "source.receipt.json").exists()      # not inside the reviewed tree
    assert sorted(p.name for p in dest.iterdir()) == ["README.md", "src"]  # source untouched
    assert read_source_receipt(dest) == receipt
    assert source_receipt_matches(read_source_receipt(dest), "t" * 64, "a" * 64, "github-pr")


def test_write_source_receipt_is_atomic(tmp_path, monkeypatch):
    # F2: a failed receipt write leaves no partial/visible receipt and no leftover staging file.
    dest = tmp_path / "source"
    dest.mkdir()
    real_replace = target_mod.os.replace

    def boom(src, dst, *a, **k):
        if str(dst).endswith("source.receipt.json"):
            raise OSError("injected receipt replace failure")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(target_mod.os, "replace", boom)
    with pytest.raises(OSError):
        write_source_receipt(dest, source_receipt("t" * 64, "a" * 64, "github-pr"))
    assert not source_receipt_path(dest).exists()
    assert not source_receipt_path(dest).with_name("source.receipt.json.staging").exists()


def test_write_source_receipt_cleans_staging_on_write_failure(tmp_path, monkeypatch):
    # F2: a failure DURING the staging write (not only os.replace) must not leave a staging file behind —
    # the staging write is inside the same cleanup block as the rename.
    dest = tmp_path / "source"
    dest.mkdir()
    staging = source_receipt_path(dest).with_name("source.receipt.json.staging")
    real_write_text = target_mod.Path.write_text

    def boom(self, *a, **k):
        if str(self).endswith("source.receipt.json.staging"):
            real_write_text(self, "{partial")   # create a partial staging file, then fail mid-write
            raise OSError("injected receipt write failure")
        return real_write_text(self, *a, **k)

    monkeypatch.setattr(target_mod.Path, "write_text", boom)
    with pytest.raises(OSError):
        write_source_receipt(dest, source_receipt("t" * 64, "a" * 64, "github-pr"))
    assert not staging.exists(), "a failed staging write must be cleaned up"
    assert not source_receipt_path(dest).exists()


def test_extract_preserves_archive_member_named_like_the_legacy_receipt(tmp_path):
    # F2 COLLISION (RED before the move): a repository file literally named
    # `.crucible-source-receipt.json` is a legitimate archive member and must materialize UNCHANGED and
    # be visible to peers — the crash-repair receipt no longer lives inside RUN/source, so it can never
    # shadow or corrupt this file.
    payload = b'{"this":"is real repo content, not a crucible receipt"}\n'
    def build(tar):
        _add_dir(tar, "owner-repo-abc/")
        _add_file(tar, "owner-repo-abc/.crucible-source-receipt.json", payload)
        _add_file(tar, "owner-repo-abc/README.md", b"# readme\n")
    archive = _write_tar(tmp_path / "src.tar", build)
    dest = tmp_path / "source"
    receipt = source_receipt("t" * 64, "a" * 64, "github-pr")
    write_source_receipt(dest, receipt)
    safe_extract_source_archive(archive, dest, strip_wrapper=True)
    assert (dest / ".crucible-source-receipt.json").read_bytes() == payload  # unchanged, visible
    # the authoritative crash-repair receipt is the adjacent run-state file, not this repo member
    assert read_source_receipt(dest) == receipt
    assert source_receipt_path(dest) != (dest / ".crucible-source-receipt.json")


def test_read_source_receipt_tolerates_missing_or_corrupt(tmp_path):
    dest = tmp_path / "source"
    dest.mkdir()
    assert read_source_receipt(dest) is None  # absent
    source_receipt_path(dest).write_text("{not json")
    assert read_source_receipt(dest) is None  # corrupt (never raises)


def test_extract_preserves_executable_mode_safely(tmp_path):
    # F1: safe extraction applies the archive's executable bit but masks off setuid/setgid/sticky, so a
    # `100755`/`100644` distinction survives for the tree build without ever writing a privileged bit.
    def build(tar):
        _add_dir(tar, "owner-repo-abc/")
        for name, mode in (("plain.txt", 0o644), ("run.sh", 0o755), ("setuid.sh", 0o4755)):
            info = tarfile.TarInfo(f"owner-repo-abc/{name}"); info.size = 3; info.mode = mode
            tar.addfile(info, io.BytesIO(b"abc"))
    archive = _write_tar(tmp_path / "src.tar", build)
    dest = tmp_path / "source"
    safe_extract_source_archive(archive, dest, strip_wrapper=True)
    assert not (os.stat(dest / "plain.txt").st_mode & 0o111)     # non-executable preserved
    assert os.stat(dest / "run.sh").st_mode & 0o111              # executable preserved
    setuid = os.stat(dest / "setuid.sh").st_mode
    assert setuid & 0o111                                        # exec bit kept
    assert not (setuid & 0o7000)                                 # setuid/setgid/sticky stripped


def test_extract_flat_archive_without_wrapper(tmp_path):
    def build(tar):
        _add_file(tar, "README.md", b"r\n")
        _add_dir(tar, "src/")
        _add_file(tar, "src/a.py", b"a\n")
    archive = _write_tar(tmp_path / "flat.tar", build)
    dest = tmp_path / "source"
    safe_extract_source_archive(archive, dest, strip_wrapper=True)
    assert (dest / "README.md").read_text() == "r\n"
    assert (dest / "src" / "a.py").read_text() == "a\n"


def test_extract_rejects_existing_destination(tmp_path):
    archive = _github_style_archive(tmp_path / "src.tar")
    dest = tmp_path / "source"
    dest.mkdir()
    with pytest.raises(ValueError, match="already exists"):
        safe_extract_source_archive(archive, dest, strip_wrapper=True)


def test_extract_rejects_parent_escape(tmp_path):
    def build(tar):
        _add_file(tar, "../../escape.py", b"x")
    archive = _write_tar(tmp_path / "bad.tar", build)
    dest = tmp_path / "source"
    with pytest.raises(ValueError, match=r"\.\."):
        safe_extract_source_archive(archive, dest, strip_wrapper=True)
    assert not dest.exists()
    assert not (tmp_path / "source.staging").exists()


def test_extract_rejects_absolute_path(tmp_path):
    def build(tar):
        _add_file(tar, "/etc/passwd", b"x")
    archive = _write_tar(tmp_path / "bad.tar", build)
    dest = tmp_path / "source"
    with pytest.raises(ValueError, match="absolute"):
        safe_extract_source_archive(archive, dest, strip_wrapper=True)
    assert not dest.exists()


def test_extract_rejects_symlink_member(tmp_path):
    def build(tar):
        _add_file(tar, "top/a.py", b"a")
        _add_special(tar, "top/link", tarfile.SYMTYPE, linkname="/etc/passwd")
    archive = _write_tar(tmp_path / "bad.tar", build)
    dest = tmp_path / "source"
    with pytest.raises(ValueError, match="symlink|regular file"):
        safe_extract_source_archive(archive, dest, strip_wrapper=True)
    assert not dest.exists()
    assert not (tmp_path / "source.staging").exists()


def test_extract_rejects_hardlink_member(tmp_path):
    def build(tar):
        _add_file(tar, "top/a.py", b"a")
        _add_special(tar, "top/hard", tarfile.LNKTYPE, linkname="top/a.py")
    archive = _write_tar(tmp_path / "bad.tar", build)
    with pytest.raises(ValueError, match="hardlink|regular file"):
        safe_extract_source_archive(archive, tmp_path / "source", strip_wrapper=True)
    assert not (tmp_path / "source").exists()


@pytest.mark.parametrize("typeflag", [tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE])
def test_extract_rejects_special_files(tmp_path, typeflag):
    def build(tar):
        _add_file(tar, "top/a.py", b"a")
        _add_special(tar, "top/dev", typeflag)
    archive = _write_tar(tmp_path / "bad.tar", build)
    with pytest.raises(ValueError, match="regular file"):
        safe_extract_source_archive(archive, tmp_path / "source", strip_wrapper=True)
    assert not (tmp_path / "source").exists()


def test_extract_rejects_duplicate_after_strip(tmp_path):
    # Two different wrappers collapse to the same relative path after stripping -> but a single
    # wrapper keeps duplicates distinct; construct a real duplicate under one wrapper.
    def build(tar):
        _add_dir(tar, "top/")
        _add_file(tar, "top/a.py", b"1")
        _add_file(tar, "top/./a.py", b"2")  # normalizes to the same relative path
    archive = _write_tar(tmp_path / "dup.tar", build)
    with pytest.raises(ValueError, match="duplicate"):
        safe_extract_source_archive(archive, tmp_path / "source", strip_wrapper=True)
    assert not (tmp_path / "source").exists()


def test_extract_rejects_case_colliding_parent_dir(tmp_path):
    # The confirmed collapse: Foo/a.txt + foo/b.txt have DISTINCT full paths but share a parent
    # directory in differing case (Foo vs foo). On a case-insensitive filesystem they silently merge
    # under one casing; rejected deterministically on EVERY platform, even with no explicit directory
    # members in the tar.
    def build(tar):
        _add_file(tar, "Foo/a.txt", b"1")
        _add_file(tar, "foo/b.txt", b"2")
    archive = _write_tar(tmp_path / "case.tar", build)
    with pytest.raises(ValueError, match="filesystem-ambiguous"):
        safe_extract_source_archive(archive, tmp_path / "source", strip_wrapper=False)
    assert not (tmp_path / "source").exists()


def test_extract_allows_same_cased_directory(tmp_path):
    # Reusing a directory with the SAME casing is fine; both files extract.
    def build(tar):
        _add_file(tar, "Foo/a.txt", b"1")
        _add_file(tar, "Foo/b.txt", b"2")
    archive = _write_tar(tmp_path / "ok.tar", build)
    safe_extract_source_archive(archive, tmp_path / "source", strip_wrapper=False)
    assert (tmp_path / "source" / "Foo" / "a.txt").read_bytes() == b"1"
    assert (tmp_path / "source" / "Foo" / "b.txt").read_bytes() == b"2"


def test_extract_rejects_case_colliding_same_basename(tmp_path):
    # A same-basename case collision (previously an extraction-time FileExistsError) is now caught
    # deterministically at validation as a filesystem-ambiguous path.
    def build(tar):
        _add_file(tar, "Foo/x.txt", b"1")
        _add_file(tar, "foo/x.txt", b"2")
    archive = _write_tar(tmp_path / "case2.tar", build)
    with pytest.raises(ValueError, match="filesystem-ambiguous"):
        safe_extract_source_archive(archive, tmp_path / "source", strip_wrapper=False)
    assert not (tmp_path / "source").exists()


def test_extract_rejects_too_many_members(tmp_path, monkeypatch):
    monkeypatch.setattr(target_mod, "MAX_ARCHIVE_MEMBERS", 2)
    def build(tar):
        _add_file(tar, "top/a.py", b"a")
        _add_file(tar, "top/b.py", b"b")
        _add_file(tar, "top/c.py", b"c")
    archive = _write_tar(tmp_path / "many.tar", build)
    with pytest.raises(ValueError, match="members"):
        safe_extract_source_archive(archive, tmp_path / "source", strip_wrapper=True)
    assert not (tmp_path / "source").exists()


def test_extract_rejects_too_many_declared_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(target_mod, "MAX_ARCHIVE_BYTES", 8)
    def build(tar):
        _add_file(tar, "top/a.py", b"x" * 5)
        _add_file(tar, "top/b.py", b"y" * 5)  # cumulative 10 > 8
    archive = _write_tar(tmp_path / "big.tar", build)
    with pytest.raises(ValueError, match="bytes"):
        safe_extract_source_archive(archive, tmp_path / "source", strip_wrapper=True)
    assert not (tmp_path / "source").exists()


def test_extract_no_partial_when_later_member_invalid(tmp_path):
    # A valid member precedes an invalid one; because validation precedes extraction, nothing is
    # written and no staging remains.
    def build(tar):
        _add_dir(tar, "top/")
        _add_file(tar, "top/good.py", b"good")
        _add_special(tar, "top/evil", tarfile.SYMTYPE, linkname="x")
    archive = _write_tar(tmp_path / "mixed.tar", build)
    with pytest.raises(ValueError):
        safe_extract_source_archive(archive, tmp_path / "source", strip_wrapper=True)
    assert not (tmp_path / "source").exists()
    assert not (tmp_path / "source.staging").exists()


def test_extract_atomic_replace_failure_leaves_no_source(tmp_path, monkeypatch):
    archive = _github_style_archive(tmp_path / "src.tar")
    dest = tmp_path / "source"

    def boom(src, dst):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(target_mod.os, "replace", boom)
    with pytest.raises(OSError, match="simulated rename failure"):
        safe_extract_source_archive(archive, dest, strip_wrapper=True)
    assert not dest.exists()
    assert not (tmp_path / "source.staging").exists()


# --------------------------------------------------------------------------------------
# Wrapper stripping is an EXPLICIT per-kind decision (F1): a github-pr codeload tarball
# nests everything under one wrapper dir that must be stripped; a local-range `git archive`
# emits repository-root-relative paths that must be preserved verbatim.
# --------------------------------------------------------------------------------------

def _single_directory_local_archive(path):
    """A local ``git archive`` whose entire tree lives under one real directory (``src/``).

    This is the F1 regression fixture: with unconditional top-level stripping the sole real
    directory looks like a codeload wrapper and the snapshot collapses ``src/a.py`` to ``a.py``.
    """
    def build(tar):
        _add_dir(tar, "src/")
        _add_file(tar, "src/a.py", b"print('a')\n")
        _add_file(tar, "src/b.py", b"print('b')\n")
    return _write_tar(path, build)


def test_extract_local_range_preserves_single_directory_layout(tmp_path):
    # F1: a local-range archive whose files all live under one directory must keep that directory.
    archive = _single_directory_local_archive(tmp_path / "local.tar")
    dest = tmp_path / "source"
    safe_extract_source_archive(archive, dest, strip_wrapper=False)
    assert (dest / "src" / "a.py").read_text() == "print('a')\n"
    assert (dest / "src" / "b.py").read_text() == "print('b')\n"
    # the directory is NOT mistaken for a wrapper and stripped away
    assert not (dest / "a.py").exists()
    assert not (tmp_path / "source.staging").exists()


def test_extract_local_range_preserves_wrapper_named_paths(tmp_path):
    # Even a local tree that happens to share one top component with a codeload-looking name is
    # preserved verbatim when stripping is off — the decision is the kind, never the path shape.
    def build(tar):
        _add_dir(tar, "owner-repo-abc123/")
        _add_file(tar, "owner-repo-abc123/a.py", b"a\n")
    archive = _write_tar(tmp_path / "localish.tar", build)
    dest = tmp_path / "source"
    safe_extract_source_archive(archive, dest, strip_wrapper=False)
    assert (dest / "owner-repo-abc123" / "a.py").read_text() == "a\n"


def test_extract_github_pr_strips_exactly_one_codeload_wrapper(tmp_path):
    # github-pr: strip exactly the one `owner-repo-<sha>/` wrapper and preserve the real tree —
    # including a nested directory — beneath it (only ONE level is removed).
    def build(tar):
        _add_dir(tar, "owner-repo-abc123/")
        _add_dir(tar, "owner-repo-abc123/src/")
        _add_file(tar, "owner-repo-abc123/src/a.py", b"print('a')\n")
        _add_file(tar, "owner-repo-abc123/README.md", b"# r\n")
    archive = _write_tar(tmp_path / "gh.tar", build)
    dest = tmp_path / "source"
    safe_extract_source_archive(archive, dest, strip_wrapper=True)
    assert (dest / "src" / "a.py").read_text() == "print('a')\n"
    assert (dest / "README.md").read_text() == "# r\n"
    # the wrapper is gone but the inner directory (one level down) survives
    assert not (dest / "owner-repo-abc123").exists()
    assert (dest / "src").is_dir()


@pytest.mark.parametrize("strip_wrapper", [True, False])
def test_extract_rejects_parent_escape_in_both_modes(tmp_path, strip_wrapper):
    # Traversal defense is independent of the stripping decision.
    def build(tar):
        _add_file(tar, "../../escape.py", b"x")
    archive = _write_tar(tmp_path / "bad.tar", build)
    with pytest.raises(ValueError, match=r"\.\."):
        safe_extract_source_archive(archive, tmp_path / "source", strip_wrapper=strip_wrapper)
    assert not (tmp_path / "source").exists()
    assert not (tmp_path / "source.staging").exists()


@pytest.mark.parametrize("strip_wrapper", [True, False])
def test_extract_rejects_duplicate_after_normalization_in_both_modes(tmp_path, strip_wrapper):
    # Duplicate detection (post-normpath) holds whether or not a wrapper is stripped.
    def build(tar):
        _add_dir(tar, "top/")
        _add_file(tar, "top/a.py", b"1")
        _add_file(tar, "top/./a.py", b"2")  # normalizes to the same relative path
    archive = _write_tar(tmp_path / "dup.tar", build)
    with pytest.raises(ValueError, match="duplicate"):
        safe_extract_source_archive(archive, tmp_path / "source", strip_wrapper=strip_wrapper)
    assert not (tmp_path / "source").exists()


# --------------------------------------------------------------------------------------
# Behavioral: the documented local pr-review protocol path (real CLI + run-log events)
# --------------------------------------------------------------------------------------

def _cli(capsys, *args):
    """Run ``crucible.cli.main`` in-process, assert success, and return its captured stdout."""
    from crucible.cli import main
    rc = main(list(args))
    assert rc == 0, f"crucible {args!r} exited {rc}"
    return capsys.readouterr().out


def test_documented_local_protocol_binds_and_reports_target(tmp_path, capsys):
    """Drive the documented local normalization path end-to-end through the real CLI and assert the
    manifest SHAs, the feature-only patch, `target_sha256` in the PLAN bindings, and a report
    `## Review target` section that matches the manifest — the behavioral guard the token-scoped
    protocol tests cannot regress past."""
    from crucible.runlog import RunLog

    repo = init_diverged_repo(tmp_path)
    intent = tmp_path / "intent.json"
    intent.write_text(json.dumps({"title": "Local range review", "body": "feature change"}))

    # init-run (pr-review) -> RUN path (last non-empty stdout line)
    out = _cli(capsys, "init-run", "--goal", "review main..feature",
               "--workflow", "pr-review", "--base-dir", str(tmp_path / "runs"))
    run = [ln.strip() for ln in out.splitlines() if ln.strip()][-1]

    # normalize-target local --range (single merge-base range) -> manifest + exact patch
    target_json = tmp_path / "target.json"
    target_diff = tmp_path / "target.diff"
    _cli(capsys, "normalize-target", "local", "--repo", str(repo),
         "--range", "main..feature", "--intent", str(intent),
         "--output", str(target_json), "--diff-output", str(target_diff))

    # load-target records the one immutable target_loaded event before any DAG/PLAN work
    _cli(capsys, "load-target", "--run", run,
         "--file", str(target_json), "--diff", str(target_diff))

    # Manifest base/head/merge-base SHAs match the real divergent repo
    manifest = json.loads(target_json.read_text())
    base_sha = _git(repo, "rev-parse", "--verify", "main^{commit}").strip()
    head_sha = _git(repo, "rev-parse", "--verify", "feature^{commit}").strip()
    merge_base = _git(repo, "merge-base", base_sha, head_sha).strip()
    assert manifest["base"]["sha"] == base_sha
    assert manifest["head"]["sha"] == head_sha
    assert manifest["merge_base_sha"] == merge_base
    assert manifest["changed_files"] == ["app.py"]

    # The patch is merge-base..head: only the feature change, never the base-only reverse change
    patch = target_diff.read_text()
    assert "app.py" in patch
    assert "main-only.py" not in patch

    # The loaded target hash appears in the PLAN bindings (recomputed from the real run-log events)
    dag = tmp_path / "dag.json"
    dag.write_text(json.dumps({"nodes": [{
        "id": "review", "title": "Review app.py",
        "description": "review the feature change to app.py against its callers",
        "files": ["app.py"],
        "test_plan": "static evidence (always allowed): rg -n feature app.py",
        "status": "pending"}], "edges": []}))
    _cli(capsys, "load-dag", "--run", run, "--file", str(dag))
    plan = tmp_path / "plan.md"
    plan.write_text("review plan: interrogate app.py's feature change and its callers\n")
    _cli(capsys, "log", "--run", run, "--event", "builder_output",
         "--gate", "plan", "--round", "1", "--file", str(plan))
    bindings = json.loads(_cli(capsys, "bindings", "--run", run, "--gate", "plan", "--round", "1"))
    expected_hash = target_sha256(target_from_events(RunLog(run).read_events()))
    assert bindings["target_sha256"] == expected_hash

    # The report renders a ## Review target section that matches the manifest identity
    report = _cli(capsys, "report", "--run", run)
    assert "## Review target" in report
    assert base_sha in report and head_sha in report and merge_base in report
    assert manifest["diff_sha256"] in report
    assert expected_hash in report


def test_documented_local_source_materialization_pins_head_snapshot(tmp_path, capsys):
    """Drive the documented *local* source-materialization sequence end-to-end with the real CLI and the
    real `git -C "$LOCAL_REPO" archive` command, proving the executable protocol pins the exact head:

      load-target -> show-target > loaded-target.json -> parse repository/head.sha -> verify
      repository-identity(--repo LOCAL_REPO) == recorded -> git -C LOCAL_REPO archive --output source.tar
      HEAD_SHA -> materialize-target --archive source.tar

    Asserts the snapshot is the feature head (only `app.py`, no base-only `main-only.py`), materialized
    from `source.tar` (never the GitHub `source.tar.gz`), and recorded as a valid `source_materialized`
    event. This is the local-materialization complement to the binding/report behavioral test above; it
    exercises the archive+materialize path that test does not."""
    from crucible.runlog import RunLog

    repo = init_diverged_repo(tmp_path)
    intent = tmp_path / "intent.json"
    intent.write_text(json.dumps({"title": "Local range review", "body": "feature change"}))

    out = _cli(capsys, "init-run", "--goal", "review main..feature",
               "--workflow", "pr-review", "--base-dir", str(tmp_path / "runs"))
    run = [ln.strip() for ln in out.splitlines() if ln.strip()][-1]

    target_json = tmp_path / "target.json"
    target_diff = tmp_path / "target.diff"
    _cli(capsys, "normalize-target", "local", "--repo", str(repo),
         "--range", "main..feature", "--intent", str(intent),
         "--output", str(target_json), "--diff-output", str(target_diff))
    _cli(capsys, "load-target", "--run", run,
         "--file", str(target_json), "--diff", str(target_diff))

    # show-target emits the authoritative loaded manifest; the head SHA / recorded identity come ONLY
    # from it — never an ambient archive variable.
    loaded = json.loads(_cli(capsys, "show-target", "--run", run))
    recorded_repository = loaded["repository"]
    head_sha = loaded["head"]["sha"]
    assert recorded_repository and head_sha, "loaded manifest must carry a non-empty repository/head.sha"

    # The caller's LOCAL_REPO must be proven to be the recorded repository before any archive.
    observed = _cli(capsys, "repository-identity", "--repo", str(repo)).strip()
    assert observed == recorded_repository, "the local checkout must be the recorded repository"

    # The documented archive command: an explicit `git -C "$LOCAL_REPO" archive` of the exact head.
    source_tar = Path(run) / "source.tar"
    subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar",
         "--output", str(source_tar), head_sha],
        check=True, text=True, capture_output=True)
    assert source_tar.exists() and not (Path(run) / "source.tar.gz").exists(), \
        "the local path writes source.tar (never the GitHub source.tar.gz)"

    _cli(capsys, "materialize-target", "--run", run, "--archive", str(source_tar))

    # The pinned snapshot is the feature head: only app.py (the base-only main-only.py never appears).
    materialized = Path(run) / "source"
    assert (materialized / "app.py").read_text() == "print('feature change')\n"
    assert not (materialized / "main-only.py").exists(), \
        "the head snapshot must not contain the base-only file"

    # A single valid source_materialized event is recorded with the target + archive hashes.
    events = RunLog(run).read_events()
    materializations = [e for e in events if e.get("event") == "source_materialized"]
    assert len(materializations) == 1
    assert materializations[0]["kind"] == "local-range"
    assert materializations[0]["target_sha256"] == target_sha256(target_from_events(events))
    assert materializations[0]["archive_sha256"] == hashlib.sha256(source_tar.read_bytes()).hexdigest()
    validated = validate_source_materialization(events, "pr-review")
    assert validated.issues == [] and validated.event is not None
