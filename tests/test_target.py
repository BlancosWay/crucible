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
    TARGET_KINDS,
    TARGET_VERSION,
    ReviewTarget,
    normalize_diff_target,
    normalize_github_target,
    normalize_local_target,
    normalized_repository_identity,
    safe_extract_source_archive,
    target_event_issues,
    target_from_events,
    target_sha256,
    validate_source_materialization,
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
    lambda d: d.update(merge_base_sha="4" * 40),  # not a github field
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


def test_github_normalization_preserves_cross_fork_identity():
    diff = b"patch bytes"
    target = normalize_github_target(_gh_metadata(), _gh_metadata(), diff)
    assert target.kind == "github-pr"
    assert target.repository == "base/repo"
    assert target.base.repository == "base/repo" and target.base.sha == "1" * 40
    assert target.head.repository == "fork/repo" and target.head.sha == "2" * 40
    assert target.base.ref == "main" and target.head.ref == "feature"
    assert target.is_cross_repository is True
    assert target.pr_number == 7
    assert target.changed_files == ("src/a.py",)
    assert target.diff_sha256 == hashlib.sha256(diff).hexdigest()


def test_github_normalization_same_repo_pr_is_not_cross():
    md = _gh_metadata(headRepository={"nameWithOwner": "base/repo"},
                      headRepositoryOwner={"login": "base"}, isCrossRepository=False)
    target = normalize_github_target(md, md, b"p")
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
    ("files", [{"path": "src/b.py"}]),
])
def test_github_normalization_rejects_before_after_mismatch(field, value):
    before = _gh_metadata()
    after = _gh_metadata(**{field: value})
    with pytest.raises(ValueError):
        normalize_github_target(before, after, b"p")


def test_github_normalization_accepts_stable_identity_reordered_files():
    # Same file set in a different list order is still stable identity (files are order-normalized).
    before = _gh_metadata(files=[{"path": "src/a.py"}, {"path": "src/b.py"}])
    after = _gh_metadata(files=[{"path": "src/b.py"}, {"path": "src/a.py"}])
    target = normalize_github_target(before, after, b"p")
    assert target.changed_files == ("src/a.py", "src/b.py")


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
