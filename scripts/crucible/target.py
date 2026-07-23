"""Immutable pr-review target: schema, normalization, identity, and safe materialization.

This module is the single owner of the ``pr-review`` *review target* — the deterministic identity of
the change under review — kept strictly separate from the candidate/DAG/node bindings that identify
what a *gate decision* refers to. It is a pure data/normalization module: it never touches the CLI or
the run-log (Task 2 wires ``target_sha256`` into gate bindings; the CLI in ``crucible.cli`` records
and guards the one ``target_loaded`` event), and it adds no target state to ``build``/``deep-dive``
runs.

Three target kinds share a common core (exact ``diff_sha256`` patch identity, ``changed_files``, and
untrusted ``intent`` text):

- ``github-pr`` — a GitHub pull request pinned to base/head repository identity and immutable OIDs,
  normalized to PR-style ``merge_base..head`` semantics via the base repo's exact-OID compare endpoint
  (so a base-only commit after the fork point never leaks in as a reverse change);
- ``local-range`` — a local Git range normalized to PR-style ``merge_base..head`` semantics, with a
  credential-free repository fingerprint;
- ``diff-file`` — a bare patch that proves *patch bytes only* (``revision_bound: false``); it never
  borrows ambient repository context.

All parsing is strict (unknown fields reject at every nesting level, SHAs are validated) so a target
cannot be internally consistent while pointing at code other than the submitted change.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from crucible.integrity import canonical_json_sha256

TARGET_VERSION = 1
TARGET_KINDS = ("github-pr", "local-range", "diff-file")

# Confinement limits for one-shot source materialization (see ``safe_extract_source_archive``). Kept
# as module constants so resource-safe tests can shrink them deterministically instead of allocating
# a real gigabyte or a hundred thousand files.
MAX_ARCHIVE_MEMBERS = 100_000
MAX_ARCHIVE_BYTES = 1 << 30

SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Canonical, credential-free repository identity grammar (see ``_is_canonical_repository_identity``).
# ``LOCAL_IDENTITY_RE`` is the exact ``normalized_repository_identity`` fallback fingerprint; a slug
# segment is a GitHub ``owner``/``repo`` or a sanitized-remote path component (no ``.``/``..``, no
# separators, no ``:`` port/scp marker); ``_CONTROL_OR_SPACE_RE`` catches ASCII whitespace/control.
LOCAL_IDENTITY_RE = re.compile(r"^local:[0-9a-f]{64}$")
_SLUG_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_CONTROL_OR_SPACE_RE = re.compile(r"[\x00-\x20\x7f]")

# Git's network transport URL schemes (see ``_sanitize_remote_url``). ``file`` — and any unknown
# scheme — is deliberately excluded: a ``file:`` URL, or a bare/relative filesystem path, names a
# local checkout rather than a network identity, so it must fall back to the credential-free
# ``local:<sha256(real path)>`` fingerprint instead of persisting a local path as the repository
# identity.
_NETWORK_URL_SCHEMES = frozenset({"http", "https", "ssh", "git", "ftp", "ftps"})


# The common fields every target kind carries, plus the kind-specific field set. A target manifest
# must contain EXACTLY ``_COMMON_FIELDS | _VARIANT_FIELDS[kind]`` — no missing and no extra keys.
_COMMON_FIELDS = frozenset({
    "version", "kind", "revision_bound", "repository", "diff_sha256", "changed_files", "intent",
})
_VARIANT_FIELDS = {
    "github-pr": frozenset({"pr_number", "url", "base", "head", "merge_base_sha",
                            "is_cross_repository"}),
    "local-range": frozenset({"base", "head", "merge_base_sha"}),
    "diff-file": frozenset(),
}

# The run-log events a ``target_loaded`` must PRECEDE: target identity is established before any DAG,
# PLAN, or review-protocol work. ``source_materialized`` is deliberately excluded — it legitimately
# follows ``target_loaded`` (materialization happens immediately after the target loads).
_TARGET_MUST_PRECEDE = frozenset({
    "dag_loaded", "builder_output", "critic_output", "builder_resolution", "critic_verdict",
    "symmetric_verdict", "accepted_finding_set", "gate_consensus", "gate_proceeded_with_flags",
    "gate_capped", "node_status_change", "plan_approved",
})


# ---------------------------------------------------------------------------------------------------
# Strict field validators — each performs only the named check and raises ``ValueError`` naming the
# failing field, so ``main`` renders a clean ``crucible: ...`` message with no traceback.
# ---------------------------------------------------------------------------------------------------

def _require_exact_keys(data: dict[str, Any], allowed: frozenset[str] | set[str], *,
                        context: str) -> None:
    keys = set(data)
    extra = keys - set(allowed)
    if extra:
        raise ValueError(f"{context} has unknown field(s): {sorted(extra)}")
    missing = set(allowed) - keys
    if missing:
        raise ValueError(f"{context} is missing required field(s): {sorted(missing)}")


def _required_choice(data: dict[str, Any], key: str, choices: tuple[str, ...]) -> str:
    value = data.get(key)
    if value not in choices:
        raise ValueError(f"{key} must be one of {choices}, got {value!r}")
    return value


def _required_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _required_positive_int(data: dict[str, Any], key: str) -> int:
    value = _required_int(data, key)
    if value <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _required_bool(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _required_hash(data: dict[str, Any], key: str, pattern: re.Pattern[str]) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not pattern.match(value):
        raise ValueError(f"{key} must be a lowercase hex string matching {pattern.pattern}")
    return value


def _required_url(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"{key} must be an http(s) URL")
    return value


def _is_canonical_slug(value: str) -> bool:
    """A sanitized ``host/path`` or GitHub ``owner/repo`` slug: two or more ``/``-separated segments,
    each a non-empty ``[A-Za-z0-9._-]`` component that is neither ``.`` nor ``..``. Rejects absolute/
    relative paths (empty or dot segments), ``:`` (unsanitized scp/port), and anything else."""
    segments = value.split("/")
    if len(segments) < 2:
        return False
    return all(s not in (".", "..") and _SLUG_SEGMENT_RE.match(s) for s in segments)


def _is_canonical_repository_identity(value: str) -> bool:
    """Whether ``value`` is a repository identity in exactly the canonical, credential-free form the
    normalizers emit — the single source of truth reused for both the top-level ``repository`` and a
    nested ``Revision.repository``.

    Accepts only: ``local:<64 lowercase hex>`` (the ``normalized_repository_identity`` fingerprint), a
    sanitized ``scheme://host[:port]/path`` URL that is byte-identical to its own re-sanitization (so
    userinfo/query/fragment can never survive), or an ``owner/repo``-style slug (sanitized host/path
    or GitHub ``nameWithOwner``). Rejects absolute/relative filesystem paths, backslashes, credential-
    bearing/query/fragment URLs, malformed local fingerprints, whitespace/control, and any value that
    would normalize differently from its stored form.
    """
    if _CONTROL_OR_SPACE_RE.search(value) or "\\" in value:
        return False
    if LOCAL_IDENTITY_RE.match(value):
        return True
    if value.startswith("local:"):
        # A malformed local fingerprint is never a valid URL or slug — reject rather than reinterpret.
        return False
    if "://" in value:
        return _sanitize_remote_url(value) == value
    return _is_canonical_slug(value)


def _optional_repository(value: Any) -> str | None:
    """The top-level ``repository`` field: ``None`` (diff-file) or a canonical, credential-free
    repository identity (``owner/repo``, a sanitized host/path URL, or ``local:<64 hex>``)."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("repository must be null or a non-empty string")
    if not _is_canonical_repository_identity(value):
        raise ValueError(
            "repository must be a canonical credential-free identity (owner/repo, a sanitized "
            f"host/path URL, or local:<64 hex>), got {value!r}")
    return value


def _repository_name(value: Any) -> str:
    """A revision's repository identity (github-pr base/head), validated against the same canonical,
    credential-free grammar as the top-level ``repository``."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("revision.repository must be a non-empty string")
    if not _is_canonical_repository_identity(value):
        raise ValueError(
            "revision.repository must be a canonical credential-free identity (owner/repo, a "
            f"sanitized host/path URL, or local:<64 hex>), got {value!r}")
    return value


def _parse_intent(intent: Any) -> tuple[str, str]:
    if not isinstance(intent, dict):
        raise ValueError("intent must be a JSON object")
    _require_exact_keys(intent, {"title", "body"}, context="intent")
    title, body = intent["title"], intent["body"]
    if not isinstance(title, str) or not isinstance(body, str):
        raise ValueError("intent.title and intent.body must be strings")
    return title, body


def _parse_changed_files(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("changed_files must be a list")
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError("each changed_files entry must be a non-empty string")
        if item.startswith("/"):
            raise ValueError(f"changed_files entry must be repository-relative, got absolute {item!r}")
        if "\\" in item:
            raise ValueError(f"changed_files entry must use POSIX separators, got {item!r}")
        if item in (".", "..") or ".." in item.split("/"):
            raise ValueError(f"changed_files entry must not contain '..': {item!r}")
        if item in seen:
            raise ValueError(f"duplicate changed_files entry: {item!r}")
        seen.add(item)
        out.append(item)
    return tuple(out)


def _parse_revision(data: Any, *, require_repository: bool) -> "Revision":
    if not isinstance(data, dict):
        raise ValueError("revision must be a JSON object")
    allowed = {"ref", "sha"} | ({"repository"} if require_repository else set())
    _require_exact_keys(data, allowed, context="revision")
    ref = data.get("ref")
    if not isinstance(ref, str) or not ref:
        raise ValueError("revision.ref must be a non-empty string")
    sha = data.get("sha")
    if not isinstance(sha, str) or not SHA1_RE.match(sha):
        raise ValueError("revision.sha must be a 40-char lowercase hex sha")
    repository = _repository_name(data["repository"]) if require_repository else None
    return Revision(repository=repository, ref=ref, sha=sha)


# ---------------------------------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Revision:
    """One end of a revision-bound target: an optional ``owner/name`` repository plus a ref name and
    its resolved 40-hex commit sha. Local ranges omit the repository (``None``)."""

    repository: str | None
    ref: str
    sha: str

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.repository is not None:
            out["repository"] = self.repository
        out["ref"] = self.ref
        out["sha"] = self.sha
        return out


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
        _require_exact_keys(data, _COMMON_FIELDS | _VARIANT_FIELDS[kind], context="target")
        version = _required_int(data, "version")
        if version != TARGET_VERSION:
            raise ValueError(f"target.version must be {TARGET_VERSION}")
        revision_bound = _required_bool(data, "revision_bound")
        if revision_bound != (kind != "diff-file"):
            raise ValueError(
                "revision_bound must be true for revision targets and false for diff-file")
        title, body = _parse_intent(data["intent"])
        github = kind == "github-pr"
        target = cls(
            version=version,
            kind=kind,
            revision_bound=revision_bound,
            repository=_optional_repository(data.get("repository")),
            diff_sha256=_required_hash(data, "diff_sha256", SHA256_RE),
            changed_files=_parse_changed_files(data["changed_files"]),
            intent_title=title,
            intent_body=body,
            pr_number=_required_positive_int(data, "pr_number") if github else None,
            url=_required_url(data, "url") if github else None,
            base=_parse_revision(data["base"], require_repository=github)
            if kind != "diff-file" else None,
            head=_parse_revision(data["head"], require_repository=github)
            if kind != "diff-file" else None,
            merge_base_sha=(_required_hash(data, "merge_base_sha", SHA1_RE)
                            if kind in ("local-range", "github-pr") else None),
            is_cross_repository=(_required_bool(data, "is_cross_repository") if github else None),
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


def _validate_variant_relationships(target: "ReviewTarget") -> None:
    """Cross-field invariants a per-field parse cannot see, by kind."""
    if target.kind == "github-pr":
        if target.repository is None:
            raise ValueError("github-pr target requires a top-level repository")
        if target.repository != target.base.repository:
            raise ValueError(
                "github-pr repository must equal base.repository "
                f"({target.repository!r} != {target.base.repository!r})")
        cross = target.base.repository != target.head.repository
        if target.is_cross_repository != cross:
            raise ValueError(
                "is_cross_repository must reflect whether base and head repositories differ")
    elif target.kind == "local-range":
        if target.repository is None:
            raise ValueError("local-range target requires a repository identity")
    elif target.kind == "diff-file":
        if target.repository is not None:
            raise ValueError("diff-file target must not carry a repository identity")


def target_sha256(target: "ReviewTarget") -> str:
    """Canonical content digest of a target (sorted-key JSON), stable across processes."""
    return canonical_json_sha256(target.to_dict())


# ---------------------------------------------------------------------------------------------------
# Run-log target events
# ---------------------------------------------------------------------------------------------------

def _target_from_event(event: dict[str, Any]) -> "ReviewTarget":
    """Parse and hash-verify a single ``target_loaded`` event's canonical payload."""
    payload = event.get("target")
    if not isinstance(payload, dict):
        raise ValueError("target_loaded event is missing its canonical target payload")
    target = ReviewTarget.from_dict(payload)
    recorded = event.get("target_sha256")
    if recorded != target_sha256(target):
        raise ValueError("target_loaded event target_sha256 does not match its payload")
    return target


def target_from_events(events: list[dict[str, Any]]) -> "ReviewTarget | None":
    """The single loaded target, or ``None`` when none is loaded.

    Zero ``target_loaded`` events → ``None``; exactly one valid event → the parsed target; a
    duplicate, a malformed payload, or a payload/hash disagreement raises ``ValueError``.
    """
    loaded = [e for e in events if e.get("event") == "target_loaded"]
    if not loaded:
        return None
    if len(loaded) > 1:
        raise ValueError("multiple target_loaded events (a target is immutable; load exactly one)")
    return _target_from_event(loaded[0])


def _first_protocol_index(events: list[dict[str, Any]]) -> int | None:
    for i, e in enumerate(events):
        if e.get("event") in _TARGET_MUST_PRECEDE:
            return i
    return None


def protocol_work_started(events: list[dict[str, Any]]) -> bool:
    """True once any DAG/PLAN/review-protocol event has been recorded — a target must precede these,
    so ``load-target``/``materialize-target`` use this to reject a late load/materialization."""
    return _first_protocol_index(events) is not None


def target_event_issues(events: list[dict[str, Any]], workflow: str) -> list[str]:
    """Target-event integrity problems for a run, as human-readable ``invalid`` strings.

    Consumed by the schema-v2 workflow validator (Task 2) which owns the ``missing``/``in-progress``
    distinction. This reports only *invalid* states: a target recorded in a non-pr-review run; a
    duplicate, malformed, or late target; or pr-review DAG/PLAN/review work recorded with no target.
    An init-only pr-review run with no target yet is ``missing`` (not invalid) and returns ``[]``.
    """
    loaded = [e for e in events if e.get("event") == "target_loaded"]
    if workflow != "pr-review":
        if loaded:
            return [f"{workflow} runs must not record a target_loaded event"]
        return []

    issues: list[str] = []
    if len(loaded) > 1:
        issues.append("multiple target_loaded events (a target is immutable; load exactly one)")
    for e in loaded:
        try:
            _target_from_event(e)
        except ValueError as exc:
            issues.append(f"malformed target_loaded event: {exc}")
    first_protocol = _first_protocol_index(events)
    if loaded:
        first_target_idx = events.index(loaded[0])
        if first_protocol is not None and first_target_idx > first_protocol:
            issues.append("target loaded after DAG/PLAN/review work began")
    elif first_protocol is not None:
        issues.append("pr-review DAG/PLAN/review work recorded without a loaded target")
    return issues


@dataclass(frozen=True)
class SourceMaterialization:
    """Result of validating the at-most-one ``source_materialized`` event against the loaded target.

    ``issues`` are human-readable ``invalid`` strings (empty when the run has no source event, or when
    exactly one fully-valid event is present); ``event`` is that single VALIDATED event, else
    ``None``. The workflow validator folds ``issues`` into the run's integrity verdict and the report
    renders a source snapshot ONLY from ``event`` — both consume this single result so they can never
    disagree about whether a snapshot is trustworthy.
    """
    issues: list[str]
    event: dict[str, Any] | None


def _source_event_issues(events: list[dict[str, Any]], event: dict[str, Any],
                         target: ReviewTarget) -> list[str]:
    """Field/ordering checks for the one source event against a valid revision-bound target."""
    issues: list[str] = []
    if event.get("kind") != target.kind:
        issues.append("source_materialized kind does not match the loaded review target kind")
    archive = event.get("archive_sha256")
    if not (isinstance(archive, str) and SHA256_RE.match(archive)):
        issues.append("source_materialized archive_sha256 is not a lowercase 64-hex digest")
    if event.get("target_sha256") != target_sha256(target):
        issues.append("source snapshot is bound to a different target than the loaded review target")
    idx = events.index(event)
    loaded_idx = next(i for i, e in enumerate(events) if e.get("event") == "target_loaded")
    if idx < loaded_idx:
        issues.append("source materialized before the review target was loaded (out of order)")
    first_protocol = _first_protocol_index(events)
    if first_protocol is not None and idx > first_protocol:
        issues.append("source materialized after DAG/PLAN/review work began (out of order)")
    return issues


def validate_source_materialization(events: list[dict[str, Any]],
                                    workflow: str) -> SourceMaterialization:
    """Centralized fail-closed validation of the source-snapshot (``source_materialized``) event.

    A source snapshot is downstream, target-dependent work: it is trustworthy ONLY as a snapshot of a
    single valid, revision-bound review target that precedes it. Any source event that is not is
    ``invalid`` history, never merely *missing*. Exactly these conditions make the one event valid
    (and eligible to render):

    - at most one ``source_materialized`` event;
    - the run is a pr-review run carrying exactly one valid ``target_loaded`` (a build/deep-dive run,
      or a pr-review run with no/duplicate/malformed target, can have no snapshot);
    - the target is revision-bound (``github-pr``/``local-range``); a ``diff-file`` target is
      revision-unbound and has no source snapshot;
    - the event's ``kind`` equals the target ``kind``;
    - ``archive_sha256`` is a lowercase 64-hex digest;
    - ``target_sha256`` equals the loaded target's authoritative hash; and
    - it is ordered AFTER the ``target_loaded`` event and BEFORE any DAG/PLAN/review/status work.

    Returns the validated event only when every condition holds; otherwise returns the accumulated
    invalid reasons and ``event=None`` (a forged/duplicate/malformed/wrong-kind/-hash/-order event
    never surfaces as a trustworthy snapshot).
    """
    materialized = [e for e in events if e.get("event") == "source_materialized"]
    if not materialized:
        return SourceMaterialization([], None)
    if workflow != "pr-review":
        return SourceMaterialization(
            [f"{workflow} runs must not record a source_materialized event"], None)

    issues: list[str] = []
    if len(materialized) > 1:
        issues.append(
            "multiple source_materialized events (a source snapshot is materialized at most once)")

    # A snapshot is meaningful only against a single valid revision-bound target that precedes it.
    try:
        target = target_from_events(events)
    except ValueError:
        # A duplicate/malformed target is separately reported by target_event_issues; the source
        # event cannot be validated against a target that does not cleanly exist.
        issues.append("source materialized without a valid loaded review target")
        return SourceMaterialization(issues, None)
    if target is None:
        issues.append("source materialized without a loaded review target")
        return SourceMaterialization(issues, None)
    if not target.revision_bound:
        issues.append(
            "source materialized for a revision-unbound diff-file target "
            "(only revision-bound github-pr/local-range targets have a source snapshot)")
        return SourceMaterialization(issues, None)

    issues.extend(_source_event_issues(events, materialized[0], target))
    if issues:
        return SourceMaterialization(issues, None)
    return SourceMaterialization(issues, materialized[0])


# ---------------------------------------------------------------------------------------------------
# Repository identity (credential-free)
# ---------------------------------------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    """Run ``git -C <repo> <args...>`` with an argument vector (never ``shell=True``)."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, text=True, capture_output=True,
    ).stdout


def _hardened_git_env() -> dict[str, str]:
    """Environment that neutralizes ambient (global/system) git config and attributes so a diff
    derivation is deterministic and can never be steered by the caller's ``~/.gitconfig`` or a system
    gitattributes file, and never blocks on a credential prompt. Mirrors the isolation
    :func:`_derive_github_patch` applies to its snapshot repo."""
    return dict(
        os.environ,
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_ATTR_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
    )


# Per-invocation ``-c`` overrides that make a diff derivation deterministic and filter-free: no EOL
# conversion, no path C-quoting, and the ambient global attributes file ignored. Combined with
# ``--no-ext-diff``/``--no-textconv`` on the diff itself, no attribute-driven external program can run
# and no clean/smudge/EOL filter can alter the recorded bytes. Same contract as ``_derive_github_patch``.
_HARDENED_GIT_FLAGS = (
    "-c", "core.autocrlf=false",
    "-c", "core.quotepath=false",
    "-c", "core.attributesFile=" + os.devnull,
)


def _is_safe_remote_path(path: str) -> bool:
    """Whether a URL ``path`` is the safe ``/owner/repo[.git]`` shape the network normalizers emit:
    absolute, non-empty beyond the leading slash, with no empty/``.``/``..`` segment and no ASCII
    control or whitespace character."""
    if not path.startswith("/") or _CONTROL_OR_SPACE_RE.search(path):
        return False
    return all(segment not in ("", ".", "..") for segment in path[1:].split("/"))


def _sanitize_remote_url(url: str) -> str | None:
    """A public host/path identity with userinfo, query, and fragment removed, or ``None`` when no
    safe network identity can be derived. Credentials are never persisted, and a local reference —
    a ``file:`` URL (any case, with or without an authority, including percent-encoded local paths)
    or a bare/relative filesystem path — is never accepted: it has no credential-free network
    identity, so callers fall back to the ``local:<hash>`` fingerprint."""
    url = url.strip()
    if not url:
        return None
    if "://" in url:
        parts = urlsplit(url)
        # Only known network schemes with a non-empty host and a safe path yield a remote identity.
        if parts.scheme.lower() not in _NETWORK_URL_SCHEMES:
            return None
        if not parts.hostname or not _is_safe_remote_path(parts.path):
            return None
        host = parts.hostname
        if parts.port:
            host = f"{host}:{parts.port}"
        # netloc rebuilt from hostname/port only -> any user:pass@ userinfo is dropped.
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    # scp-like syntax: [user@]host:path -- but never a ``file:path`` local reference.
    m = re.match(r"^(?:[^@/]+@)?([^/:]+):(.+)$", url)
    if m:
        host, path = m.group(1), m.group(2)
        if host.lower() == "file":
            return None
        return f"{host}/{path.lstrip('/')}"
    return None


def normalized_repository_identity(repo: str | Path) -> str:
    """A stable, credential-free identity for a local repository.

    Prefers the sanitized ``origin`` remote URL (userinfo/query/fragment stripped) when it is a
    supported network scheme with a non-empty host and safe path. When there is no remote, or the
    remote is a local reference (a ``file:`` URL or a bare/relative filesystem path), falls back to
    ``local:<sha256(real repository path)>`` — a fingerprint that never exposes the local filesystem
    path or a local remote's path text.
    """
    repo = Path(repo)
    try:
        url = _git(repo, "remote", "get-url", "origin").strip()
    except (subprocess.CalledProcessError, OSError):
        url = ""
    if url:
        identity = _sanitize_remote_url(url)
        if identity:
            return identity
    real = os.path.realpath(str(repo))
    return "local:" + hashlib.sha256(real.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------------------------------

def parse_range(range_text: str) -> tuple[str, str]:
    """Split ``BASE..HEAD`` or ``BASE...HEAD`` into two ref names.

    Both spellings normalize identically (the normalizer always diffs ``merge_base..head``). Exactly
    one separator is accepted and neither ref may contain ``..`` (Git forbids it in ref names).
    """
    if not isinstance(range_text, str) or not range_text:
        raise ValueError("range must be a non-empty string")
    sep = "..." if "..." in range_text else (".." if ".." in range_text else None)
    if sep is None:
        raise ValueError("range must be BASE..HEAD or BASE...HEAD")
    parts = range_text.split(sep)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError("range must name exactly one base and one head ref")
    base, head = parts
    if ".." in base or ".." in head:
        raise ValueError("ref names cannot contain '..'")
    return base, head


def _build_target(manifest: dict[str, Any]) -> "ReviewTarget":
    """Validate a freshly normalized manifest through the strict schema (single source of truth)."""
    return ReviewTarget.from_dict(manifest)


def normalize_local_target(repo: str | Path, range_text: str,
                           intent: dict[str, str]) -> tuple["ReviewTarget", bytes]:
    """Normalize a local Git range to a revision-bound ``local-range`` target + its exact patch.

    Resolves both refs to commit shas, computes their merge base, and always diffs
    ``merge_base..head`` (PR-style three-dot semantics) so base-only commits never appear as reverse
    changes. Uses argument-vector ``git`` subprocesses only. Returns ``(target, patch_bytes)``.
    """
    repo = Path(repo)
    base_ref, head_ref = parse_range(range_text)
    env = _hardened_git_env()
    base = ["git", "-C", str(repo), *_HARDENED_GIT_FLAGS]

    def run(*args: str, text: bool = False):
        return subprocess.run(base + list(args), check=True, capture_output=True, env=env,
                              text=text).stdout

    base_sha = run("rev-parse", "--verify", f"{base_ref}^{{commit}}", text=True).strip()
    head_sha = run("rev-parse", "--verify", f"{head_ref}^{{commit}}", text=True).strip()
    merge_base = run("merge-base", base_sha, head_sha, text=True).strip()
    # A driver-free, config-neutralized diff: --no-ext-diff/--no-textconv/--no-color so no
    # attribute-driven external program can execute or transform the recorded bytes (a hostile or
    # ambient diff.<driver> could otherwise replace the patch), and the changed-file set is read
    # NUL-delimited (--name-only -z) so a non-ASCII/space/newline name is carried verbatim and never
    # git C-quoted. Mirrors _derive_github_patch's guarantees for the local-range path.
    diff_args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color"]
    patch = run(*diff_args, "--binary", f"{merge_base}..{head_sha}")
    names = run(*diff_args, "--name-only", "-z", f"{merge_base}..{head_sha}")
    changed = [n for n in names.decode("utf-8", "surrogateescape").split("\0") if n]
    manifest = {
        "version": TARGET_VERSION,
        "kind": "local-range",
        "revision_bound": True,
        "repository": normalized_repository_identity(repo),
        "base": {"ref": base_ref, "sha": base_sha},
        "head": {"ref": head_ref, "sha": head_sha},
        "merge_base_sha": merge_base,
        "diff_sha256": hashlib.sha256(patch).hexdigest(),
        "changed_files": changed,
        "intent": _intent_dict(intent),
    }
    return _build_target(manifest), patch


def _intent_dict(intent: Any) -> dict[str, str]:
    if not isinstance(intent, dict):
        raise ValueError("intent must be a JSON object with title and body")
    title, body = _parse_intent(intent)
    return {"title": title, "body": body}


def _nested(md: dict[str, Any], *keys: str) -> Any:
    cur: Any = md
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _base_repository_from_url(url: Any) -> str:
    """Extract ``owner/repo`` from a GitHub PR URL (``https://github.com/owner/repo/pull/N``)."""
    if not isinstance(url, str) or not url:
        raise ValueError("github metadata 'url' must be a non-empty string")
    parts = urlsplit(url).path.strip("/").split("/")
    if len(parts) < 2:
        raise ValueError(f"cannot derive base repository from PR url {url!r}")
    return f"{parts[0]}/{parts[1]}"


def _github_identity(md: dict[str, Any]) -> dict[str, Any]:
    """The immutable identity tuple that must be byte-identical before and after patch acquisition.

    The PR's changed-file list is deliberately **not** part of this tuple: GitHub's ``files`` view is
    paginated/truncated on large PRs and rename-detected, so it can legitimately differ between two
    reads or from the snapshot-derived set without the review target changing. Source content is bound
    by the merge-base/head OID snapshots (the derived patch + ``diff_sha256``), not by any file list.
    Title/body stay in the tuple so an intent edit mid-acquisition still forces a retry."""
    return {
        "number": md.get("number"),
        "url": md.get("url"),
        "title": md.get("title"),
        "body": md.get("body"),
        "baseRefName": md.get("baseRefName"),
        "baseRefOid": md.get("baseRefOid"),
        "headRefName": md.get("headRefName"),
        "headRefOid": md.get("headRefOid"),
        "headRepository": _nested(md, "headRepository", "nameWithOwner"),
        "headRepositoryOwner": _nested(md, "headRepositoryOwner", "login"),
        "isCrossRepository": md.get("isCrossRepository"),
    }


def _parse_compare_metadata(compare: dict[str, Any], base_oid: str) -> str:
    """Validate a GitHub ``repos/<base>/compare/<baseRefOid>...<headRefOid>`` payload and return its
    ``merge_base_commit.sha`` (the PR fork point).

    The exact-OID compare is what turns a moving-branch PR into an immutable, PR-style merge-base view:
    its ``base_commit.sha`` must equal the pinned ``baseRefOid`` (so the payload really describes the
    recorded base), and its ``merge_base_commit.sha`` — the fork point — must be a valid 40 lowercase
    hex OID. Both are validated fail-closed. The compare ``files`` list is deliberately **not** read
    here: GitHub paginates/truncates it on large PRs and rename-detects it, so it is informational only
    and never gates the target — the authoritative changed-file set is derived from the snapshot patch."""
    if not isinstance(compare, dict):
        raise ValueError("github compare metadata must be a JSON object")
    base_commit = compare.get("base_commit")
    if not isinstance(base_commit, dict) or not SHA1_RE.match(str(base_commit.get("sha") or "")):
        raise ValueError("github compare metadata base_commit.sha must be a 40 lowercase hex OID")
    if base_commit["sha"] != base_oid:
        raise ValueError(
            f"github compare base_commit.sha {base_commit['sha']!r} does not equal the pinned "
            f"baseRefOid {base_oid!r}; the compare payload describes a different base — discard "
            f"artifacts and retry the acquisition")
    merge_base_commit = compare.get("merge_base_commit")
    if (not isinstance(merge_base_commit, dict)
            or not SHA1_RE.match(str(merge_base_commit.get("sha") or ""))):
        raise ValueError(
            "github compare metadata merge_base_commit.sha must be a 40 lowercase hex OID")
    return merge_base_commit["sha"]


def normalize_github_target(metadata_before: dict[str, Any], metadata_after: dict[str, Any],
                            compare_metadata: dict[str, Any],
                            merge_base_archive: str | Path,
                            head_archive: str | Path) -> tuple["ReviewTarget", bytes]:
    """Normalize a GitHub PR into a revision-bound ``github-pr`` target + its DERIVED exact patch,
    using immutable PR-style **merge-base** semantics.

    ``metadata_before``/``metadata_after`` are the JSON documents emitted by ``gh pr view --json ...``
    read around the archive fetches. Every field of the immutable identity tuple (PR number/URL/title/
    body, base repo/ref/OID, head repo/ref/OID, cross-repository flag) must be identical between the two
    reads; any difference means the PR changed mid-acquisition and the whole target is rejected (before
    any archive is read) so the orchestrator can retry. The changed-file list is **not** part of this
    tuple — GitHub's ``files`` view paginates/truncates and rename-detects, so it can differ between the
    reads without the target changing. Branch names are display metadata — SHAs and repository
    identities are authoritative.

    ``compare_metadata`` is the ``repos/<base>/compare/<baseRefOid>...<headRefOid>`` payload from the
    base repository (exact head OID; cross-fork exact OIDs are supported — never branch names). Its
    ``base_commit.sha`` must equal the recorded ``baseRefOid`` and its ``merge_base_commit.sha`` is the
    PR fork point recorded as ``merge_base_sha``. This is what makes the target immutable **and** correct
    when the base branch has advanced past the fork point: the patch is derived from the base repo's
    ``merge_base`` snapshot (``merge_base_archive``) → the head repo's ``headRefOid`` snapshot
    (``head_archive``) — a pure function of the pinned merge-base and head OIDs — so a base-only commit
    made after the fork never appears as a reverse change, and a server-recomputed ``gh pr diff`` against
    a moved branch can no longer leak in (the ABA race is eliminated). The ``changed_files`` set is
    derived **solely** from that snapshot patch. GitHub's own ``files`` views (the PR metadata ``files``
    and the compare ``files``) are informational/untrusted — they paginate/truncate on large PRs and
    apply rename detection (reporting a rename as one new path where the historyless snapshot diff, run
    without rename detection, shows the old+new pair) — so they are **never** required to equal the
    derived set and a disagreement is not a rejection. Returns ``(target, diff_bytes)``.
    """
    before, after = _github_identity(metadata_before), _github_identity(metadata_after)
    if before != after:
        changed = sorted(k for k in before if before[k] != after[k])
        raise ValueError(
            f"PR metadata changed during normalization (fields: {changed}); discard artifacts and "
            f"retry the complete acquisition")

    md = metadata_after
    base_repo = _base_repository_from_url(md.get("url"))
    head_repo = _nested(md, "headRepository", "nameWithOwner")
    if not isinstance(head_repo, str) or not head_repo:
        raise ValueError("github metadata is missing headRepository.nameWithOwner")
    base_oid = md.get("baseRefOid")
    head_oid = md.get("headRefOid")
    base_ref = md.get("baseRefName")
    head_ref = md.get("headRefName")
    is_cross = md.get("isCrossRepository")
    if not isinstance(is_cross, bool):
        raise ValueError("github metadata isCrossRepository must be a boolean")
    if not isinstance(base_oid, str) or not SHA1_RE.match(base_oid):
        raise ValueError("github metadata baseRefOid must be a 40 lowercase hex OID")

    # Validate the compare payload and record the fork point BEFORE any archive is read: the compare
    # base_commit must equal the pinned baseRefOid and the merge_base_commit is the fork point. The
    # compare `files` list is informational only (paginated/truncated/rename-detected) and never gates.
    merge_base_sha = _parse_compare_metadata(compare_metadata, base_oid)

    # Derive the immutable patch — and the authoritative changed_files set — SOLELY from the merge-base
    # and head OID snapshots (never a caller diff, never an external file list). The snapshots are
    # historyless single-commit trees (codeload tarballs), so the patch is the deterministic content
    # delta between `base@merge_base_sha` and `head@headRefOid` — the PR-style three-dot (merge-base)
    # view. Because the merge base is the fork point, a base-only commit made on the base branch after
    # the fork exists in NEITHER snapshot and can never enter the patch. GitHub's own `files` view is
    # NOT required to equal this set: it paginates/truncates on large PRs and applies rename detection
    # (reporting a rename as one new path where this diff, without rename detection, shows the old+new
    # pair), so requiring equality would falsely reject legitimate PRs. The changed-file set is read
    # NUL-delimited from the snapshot diff, so a name with a space/tab/newline is carried exactly.
    diff, changed_files = _derive_github_patch(merge_base_archive, head_archive)

    manifest = {
        "version": TARGET_VERSION,
        "kind": "github-pr",
        "revision_bound": True,
        "repository": base_repo,
        "pr_number": md.get("number"),
        "url": md.get("url"),
        "base": {"repository": base_repo, "ref": base_ref, "sha": base_oid},
        "head": {"repository": head_repo, "ref": head_ref, "sha": head_oid},
        "merge_base_sha": merge_base_sha,
        "is_cross_repository": is_cross,
        "diff_sha256": hashlib.sha256(diff).hexdigest(),
        "changed_files": changed_files,
        "intent": {"title": md.get("title") or "", "body": md.get("body") or ""},
    }
    return _build_target(manifest), diff


def _derive_github_patch(merge_base_archive: str | Path,
                         head_archive: str | Path) -> tuple[bytes, list[str]]:
    """The deterministic binary patch AND the changed-file set between the merge-base and head GitHub
    codeload snapshots, derived with no external diff/textconv execution.

    Both codeload archives are safely inspected/extracted (the same traversal/link/resource rules as
    ``safe_extract_source_archive``, stripping exactly one ``owner-repo-<sha>/`` wrapper) into confined
    temp trees, then diffed via isolated temporary Git objects so the output carries canonical
    ``a/<path>`` / ``b/<path>`` prefixes and correct new/deleted/binary behavior. Ambient/global Git
    configuration and attributes are neutralized and ``--no-ext-diff --no-textconv`` disable any
    attribute-driven external program; each snapshot tree is built from raw file bytes with a
    filter-free ``git hash-object --no-filters`` (never ``git add``), so a hostile in-tree
    ``.gitattributes``/``.gitignore`` in the snapshot can neither run code, drop a file, nor rewrite a
    reviewed byte. The changed-file list is read NUL-delimited (``--name-only -z``) so a name with a
    space/tab/newline is never mis-parsed. Nothing outside the temp directory is touched.
    """
    with tempfile.TemporaryDirectory(prefix="crucible-gh-diff-") as tmp:
        tmp_path = Path(tmp)
        merge_base_tree_dir = tmp_path / "merge-base"
        head_tree_dir = tmp_path / "head"
        safe_extract_source_archive(merge_base_archive, merge_base_tree_dir, strip_wrapper=True)
        safe_extract_source_archive(head_archive, head_tree_dir, strip_wrapper=True)
        return _git_snapshot_diff(tmp_path / "git", merge_base_tree_dir, head_tree_dir)


def _git_snapshot_diff(git_dir: Path, base_tree_dir: Path,
                       head_tree_dir: Path) -> tuple[bytes, list[str]]:
    """Binary patch and changed-file set between two extracted snapshot trees via an isolated temporary
    Git object store.

    Builds a base and a head tree object from the RAW bytes of the two directories (each file hashed
    with a filter-free ``git hash-object --no-filters``, never ``git add``, so an in-tree
    ``.gitattributes`` can neither hide a member nor rewrite its content), using a per-tree
    ``GIT_INDEX_FILE``, then ``git diff --exit-code --binary --no-ext-diff --no-textconv``. Exit ``1``
    => differences (the patch bytes), ``0`` => empty, ``>1`` => a real error is raised. The changed
    paths are read from a companion ``git diff --name-only -z`` (same rename-detection-off settings) so
    they are NUL-delimited and never subject to git's C-style path quoting (a rename shows as its
    deleted-old + added-new pair, matching the historyless snapshot delta).
    """
    env = dict(
        os.environ,
        GIT_CONFIG_GLOBAL=os.devnull,   # ignore ambient ~/.gitconfig (aliases, filters, textconv...)
        GIT_CONFIG_SYSTEM=os.devnull,   # ...and /etc/gitconfig
        GIT_CONFIG_NOSYSTEM="1",
        GIT_ATTR_NOSYSTEM="1",          # ...and the system /etc/gitattributes
        GIT_TERMINAL_PROMPT="0",
    )
    base_cmd = [
        "git", "--git-dir", str(git_dir),
        "-c", "core.autocrlf=false", "-c", "core.quotepath=false",
        "-c", "core.attributesFile=" + os.devnull,   # ignore the ambient global attributes file
    ]
    subprocess.run(base_cmd + ["init", "-q"], check=True, capture_output=True, env=env)
    base_tree = _write_snapshot_tree(base_cmd, env, git_dir / "index.base", base_tree_dir)
    head_tree = _write_snapshot_tree(base_cmd, env, git_dir / "index.head", head_tree_dir)
    diff_args = base_cmd + ["diff", "--no-ext-diff", "--no-textconv", "--no-color"]
    names = subprocess.run(
        diff_args + ["--name-only", "-z", base_tree, head_tree],
        check=True, capture_output=True, env=env)
    changed = sorted({
        n for n in names.stdout.decode("utf-8", "surrogateescape").split("\0") if n})
    proc = subprocess.run(
        diff_args + ["--exit-code", "--binary", base_tree, head_tree], capture_output=True, env=env)
    if proc.returncode == 0:
        return b"", changed
    if proc.returncode == 1:
        return proc.stdout, changed
    raise ValueError(
        "failed to derive the github patch from the base/head snapshots: "
        + proc.stderr.decode("utf-8", "replace").strip())


def _write_snapshot_tree(base_cmd: list[str], env: dict[str, str],
                         index_file: Path, work_tree: Path) -> str:
    """Build a Git tree object from the RAW bytes and archive modes of every file under ``work_tree``,
    using plumbing that can never invoke a clean/smudge/EOL/working-tree-encoding filter (never
    ``git add``); returns the tree OID.

    Each regular file's exact bytes become a blob via ``git hash-object -w --stdin --no-filters``
    (attribute-driven EOL/encoding conversion disabled and, with ``--stdin``, no path is even consulted
    for attributes); the file is **streamed** into git through its stdin file descriptor rather than
    read wholly into memory (a member may be up to ``MAX_ARCHIVE_BYTES``), matching
    ``safe_extract_source_archive``'s streaming extraction. Every blob is inserted into an isolated
    index under its canonical repository-relative name with its archive-derived mode (``100755`` when
    the extracted file carries an executable bit, else ``100644``) via a single NUL-delimited
    ``git update-index -z --index-info`` call, so names with spaces/tabs/newlines are carried literally
    (a NUL is impossible in a path). ``git write-tree`` then writes the nested tree; empty directories
    are naturally omitted (Git tracks only files) and an empty tree yields the canonical empty-tree OID.
    """
    tree_env = dict(env, GIT_INDEX_FILE=str(index_file))
    entries: list[bytes] = []
    for path in sorted(p for p in work_tree.rglob("*") if p.is_file() and not p.is_symlink()):
        with path.open("rb") as fh:
            blob = subprocess.run(
                base_cmd + ["hash-object", "-w", "--stdin", "--no-filters"],
                stdin=fh, check=True, capture_output=True, env=tree_env,
            ).stdout.decode("utf-8").strip()
        mode = "100755" if (path.stat().st_mode & 0o111) else "100644"
        rel = path.relative_to(work_tree).as_posix()
        entries.append(f"{mode} {blob}\t{rel}".encode("utf-8"))
    if entries:
        subprocess.run(
            base_cmd + ["update-index", "-z", "--index-info"],
            input=b"\0".join(entries) + b"\0", check=True, capture_output=True, env=tree_env)
    out = subprocess.run(base_cmd + ["write-tree"], check=True, capture_output=True, env=tree_env)
    return out.stdout.decode("utf-8").strip()


def normalize_diff_target(diff: bytes, intent: dict[str, str]) -> "ReviewTarget":
    """Normalize a bare patch into a revision-unbound ``diff-file`` target (patch identity only)."""
    manifest = {
        "version": TARGET_VERSION,
        "kind": "diff-file",
        "revision_bound": False,
        "repository": None,
        "diff_sha256": hashlib.sha256(diff).hexdigest(),
        "changed_files": _changed_files_from_diff(diff),
        "intent": _intent_dict(intent),
    }
    return _build_target(manifest)


def _unquote_git_path(token: str) -> str:
    """Decode a git C-quoted path token (as git emits for a path containing non-ASCII, control,
    space-adjacent, quote, or backslash bytes): a leading+trailing double quote wrapping C escapes
    ``\\a \\b \\t \\n \\v \\f \\r \\" \\\\`` and up-to-3-digit ``\\NNN`` octal byte escapes. A token
    that is not double-quoted is returned unchanged (its literal bytes are already the path). Octal
    escapes reassemble the original UTF-8 bytes, which are then decoded."""
    if len(token) < 2 or token[0] != '"' or token[-1] != '"':
        return token
    inner = token[1:-1]
    simple = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}
    out = bytearray()
    i = 0
    while i < len(inner):
        ch = inner[i]
        if ch == "\\" and i + 1 < len(inner):
            nxt = inner[i + 1]
            if nxt in simple:
                out.append(simple[nxt])
                i += 2
            elif nxt in "01234567":
                j = i + 1
                digits = ""
                while j < len(inner) and len(digits) < 3 and inner[j] in "01234567":
                    digits += inner[j]
                    j += 1
                out.append(int(digits, 8) & 0xFF)
                i = j
            else:  # unknown escape: keep the escaped character literally
                out.extend(nxt.encode("utf-8"))
                i += 2
        else:
            out.extend(ch.encode("utf-8"))
            i += 1
    return out.decode("utf-8", "replace")


def _strip_ab_prefix(path: str) -> str:
    """Strip a leading ``a/`` or ``b/`` diff prefix (present on ``diff --git``/``---``/``+++`` paths,
    absent on ``rename from``/``rename to`` paths)."""
    return path[2:] if path.startswith(("a/", "b/")) else path


def _read_quoted(s: str, start: int) -> tuple[str, int] | None:
    """Read one double-quoted token beginning at ``s[start]``; return ``(token_with_quotes, next)`` or
    ``None`` when ``start`` is not an unescaped opening quote / has no closing quote."""
    if start >= len(s) or s[start] != '"':
        return None
    i = start + 1
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == '"':
            return s[start:i + 1], i + 1
        i += 1
    return None


def _diff_header_paths(header: str) -> list[str]:
    """Paths from a ``diff --git <old> <new>`` header (the text after ``diff --git ``), used ONLY as a
    fallback for a section that carries no ``---``/``+++``/``rename`` path (a pure mode-only or binary
    change). Two quoted tokens split unambiguously on the quote boundary. An unquoted ``a/<old>
    b/<new>`` is ambiguous when a path contains ``" b/"``, so we enumerate the ``" b/"`` split
    candidates and accept only the one whose stripped old and new paths are EQUAL (a mode-only/binary
    change is same-path); if none is unambiguous we return nothing rather than record a bogus path."""
    if header.startswith('"'):
        first = _read_quoted(header, 0)
        if first is None or first[1] >= len(header) or header[first[1]] != " ":
            return []
        second = _read_quoted(header, first[1] + 1)
        if second is None:
            return []
        out = []
        for token in (first[0], second[0]):
            p = _strip_ab_prefix(_unquote_git_path(token))
            if p and p != "/dev/null":
                out.append(p)
        return out
    idx = header.find(" b/")
    while idx != -1:
        left, right = header[:idx], header[idx + 1:]
        if left.startswith("a/") and right.startswith("b/") and left[2:] == right[2:]:
            return [left[2:]]
        idx = header.find(" b/", idx + 1)
    return []


def _changed_files_from_diff(diff: bytes) -> list[str]:
    """Best-effort deterministic list of changed repository-relative paths from a unified diff.

    Collects paths from the unambiguous ``---``/``+++`` hunk-header pairs and ``rename from``/``rename
    to`` lines (unquoting git's C-quoting and dropping ``/dev/null``), falling back to the
    ``diff --git`` header only for a section that names no path any other way (a pure mode-only or
    binary change). A hunk's body is consumed by the line counts declared in its ``@@ -l,s +l,s @@``
    header, so a removed/added CONTENT line that merely starts with ``--``/``++`` (even one shaped
    exactly like a ``--- a/x`` / ``+++ b/x`` header, in a raw multi-file patch) is never mistaken for a
    file header. Unsafe paths are rejected downstream by the strict ``changed_files`` schema, so a
    malicious patch can never smuggle an escape."""
    lines = diff.decode("utf-8", errors="replace").splitlines()
    found: set[str] = set()
    header: str | None = None
    section_got_path = False
    old_left = new_left = 0

    def flush() -> None:
        if header is not None and not section_got_path:
            found.update(_diff_header_paths(header))

    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if old_left > 0 or new_left > 0:
            # Inside a hunk body: consume exactly the declared number of old/new lines so header-shaped
            # content can never be re-parsed. A '\ No newline at end of file' marker counts for neither.
            marker = line[:1]
            if marker == "+":
                new_left -= 1
            elif marker == "-":
                old_left -= 1
            elif marker != "\\":  # context (' ') or a blank line counts against both sides
                old_left -= 1
                new_left -= 1
            i += 1
            continue
        if line.startswith("diff --git "):
            flush()
            header = line[len("diff --git "):]
            section_got_path = False
            i += 1
        elif line.startswith("@@ "):
            m = re.match(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@", line)
            if m:
                old_left = int(m.group(1)) if m.group(1) is not None else 1
                new_left = int(m.group(2)) if m.group(2) is not None else 1
            i += 1
        elif (line.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ ")):
            # A file-header PAIR (works for git and raw patches, and re-anchors a new raw-patch file).
            for hdr in (line, lines[i + 1]):
                p = _unquote_git_path(hdr[4:].split("\t", 1)[0])
                if p and p != "/dev/null":
                    found.add(_strip_ab_prefix(p))
                    section_got_path = True
            i += 2
        elif line.startswith("rename from "):
            p = _unquote_git_path(line[len("rename from "):].split("\t", 1)[0])
            if p:
                found.add(p)
                section_got_path = True
            i += 1
        elif line.startswith("rename to "):
            p = _unquote_git_path(line[len("rename to "):].split("\t", 1)[0])
            if p:
                found.add(p)
                section_got_path = True
            i += 1
        else:
            i += 1
    flush()
    return sorted(found)


# ---------------------------------------------------------------------------------------------------
# Confined one-shot source materialization
# ---------------------------------------------------------------------------------------------------

def _reject_unsafe_name(name: str) -> None:
    if not name or name in (".", "./"):
        raise ValueError("archive member has an empty name")
    if name.startswith("/") or (len(name) >= 2 and name[1] == ":"):
        raise ValueError(f"archive member has an absolute path: {name!r}")
    if "\\" in name:
        raise ValueError(f"archive member name contains a backslash: {name!r}")
    if ".." in name.split("/"):
        raise ValueError(f"archive member escapes with '..': {name!r}")


def _common_top_level(members: list[tarfile.TarInfo]) -> str | None:
    """The single wrapper directory GitHub codeload archives prefix every path with, or ``None``.

    Only returned when every member shares one first path component AND at least one member is nested
    beneath it (so a flat ``git archive`` tree — files already at the repository root — is never
    mis-stripped). This heuristic is only consulted when the caller has decided, from the target
    *kind*, that a codeload wrapper is expected (``strip_wrapper=True``); it is never used to guess.
    """
    names = [m.name.strip("/") for m in members if m.name.strip("/")]
    if not names:
        return None
    firsts = {n.split("/", 1)[0] for n in names}
    if len(firsts) != 1:
        return None
    if any("/" in n for n in names):
        return next(iter(firsts))
    return None


def _strip_top(name: str, top: str | None) -> str:
    n = name.rstrip("/")
    if top is None:
        return n
    if n == top:
        return ""
    prefix = top + "/"
    return n[len(prefix):] if n.startswith(prefix) else n


def _validated_members(
    tar: tarfile.TarFile, strip_wrapper: bool
) -> list[tuple[tarfile.TarInfo, str]]:
    """Validate EVERY member before any filesystem write and return ``(member, relative_path)``.

    Rejects (in one pass, so a later invalid member is caught before extraction begins): more than
    ``MAX_ARCHIVE_MEMBERS`` members; symlinks/hardlinks/devices/FIFOs; absolute/``..``/backslash
    names; duplicate normalized paths; and more than ``MAX_ARCHIVE_BYTES`` of declared regular-file
    data. Never follows a link.

    ``strip_wrapper`` is an explicit per-kind decision made by the caller: ``True`` (a github-pr
    codeload tarball) removes the single ``owner-repo-<sha>/`` wrapper directory; ``False`` (a
    local-range ``git archive``) preserves the archive's repository-root-relative paths verbatim, so
    a repo whose whole tree lives under one directory is never mistaken for a wrapper and flattened.
    """
    # Read headers lazily and stop as soon as the member cap is exceeded, so a pathological archive
    # with an enormous member count is rejected without materializing every TarInfo first.
    members: list[tarfile.TarInfo] = []
    for member in tar:
        members.append(member)
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ValueError(
                f"archive has more than the {MAX_ARCHIVE_MEMBERS}-member limit (too many members)")
    top = _common_top_level(members) if strip_wrapper else None
    total_bytes = 0
    seen: set[str] = set()
    # casefold(prefix) -> the exact spelling first seen, for every path prefix. Detects a
    # case-insensitive filesystem collision (including a shared parent directory in differing case)
    # that would silently merge distinct members on macOS/Windows.
    casefold_prefixes: dict[str, str] = {}
    validated: list[tuple[tarfile.TarInfo, str]] = []
    for member in members:
        if not (member.isfile() or member.isdir()):
            raise ValueError(
                f"archive member {member.name!r} is not a regular file or directory "
                f"(symlinks, hardlinks, devices, and FIFOs are rejected)")
        _reject_unsafe_name(member.name)
        relative = _strip_top(member.name, top)
        if relative == "":
            continue  # the wrapper directory itself
        # Normalize away redundant ``.``/separators so paths that resolve to the same file on disk
        # (e.g. ``x/./a`` and ``x/a``) collapse to one key for duplicate detection and writing. ``..``
        # was already rejected on the raw name, so normalization can never introduce an escape.
        relative = posixpath.normpath(relative)
        _reject_unsafe_name(relative)
        if relative in seen:
            raise ValueError(f"duplicate archive member after normalization: {relative!r}")
        seen.add(relative)
        # Reject a case-insensitive filesystem collision deterministically on EVERY platform: two
        # members whose paths — or a shared parent directory — differ only in case would silently
        # merge on a case-insensitive filesystem (macOS/Windows), diverging the extracted tree from
        # the archive (and its diff_sha256 across platforms). Check every prefix, since a colliding
        # parent directory may be carried only implicitly (no explicit directory member).
        parts = relative.split("/")
        for depth in range(1, len(parts) + 1):
            prefix = "/".join(parts[:depth])
            existing = casefold_prefixes.get(prefix.casefold())
            if existing is not None and existing != prefix:
                raise ValueError(
                    f"filesystem-ambiguous archive member {relative!r}: path component {prefix!r} "
                    f"collides case-insensitively with {existing!r}")
            casefold_prefixes.setdefault(prefix.casefold(), prefix)
        if member.isfile():
            if member.size < 0:
                raise ValueError(f"archive member {member.name!r} declares a negative size")
            total_bytes += member.size
            if total_bytes > MAX_ARCHIVE_BYTES:
                raise ValueError(
                    f"archive declares more than {MAX_ARCHIVE_BYTES} bytes of regular-file data")
        validated.append((member, relative))
    return validated


def _ensure_within(base: Path, path: Path) -> None:
    """Defense-in-depth: the resolved ``path`` must stay inside ``base`` (never follow a link out)."""
    base_real = os.path.realpath(base)
    full = os.path.realpath(path)
    if full != base_real and not full.startswith(base_real + os.sep):
        raise ValueError(f"archive member would escape the destination: {path}")


def safe_extract_source_archive(
    archive: str | Path, destination: str | Path, *, strip_wrapper: bool
) -> None:
    """Extract a source archive into an ABSENT ``destination`` atomically and confined.

    Validates every member first (see ``_validated_members``), then extracts regular files and
    directories into a same-parent ``<destination>.staging`` directory and ``os.replace``s it into the
    absent final path. ``TarFile.extract`` is never used (its link/device handling is unsafe); file
    bytes are streamed with ``extractfile``. A crash or a failed rename leaves no visible source and
    removes the staging directory, so ``destination`` is only ever the complete, validated tree.
    ``destination`` holds ONLY the reviewed archive members — no Crucible metadata is ever written into
    it (the crash-repair receipt is the adjacent ``source_receipt_path``), so a repository file named
    like the receipt materializes unchanged and is visible to peers.

    Each extracted regular file keeps the archive's executable bit, masked to ordinary permission bits
    (``& 0o777``, so setuid/setgid/sticky are never written), which the snapshot tree build reads back
    to encode ``100755`` vs ``100644``.

    ``strip_wrapper`` is a required, explicit decision the caller derives from the immutable target
    *kind* (never from a caller flag or the archive's path shape): a github-pr codeload tarball nests
    the whole tree under one ``owner-repo-<sha>/`` wrapper that is stripped (``True``); a local-range
    ``git archive`` emits repository-root-relative paths that are preserved verbatim (``False``).
    """
    archive = Path(archive)
    destination = Path(destination)
    if destination.exists():
        raise ValueError(f"source destination already exists: {destination}")
    staging = destination.with_name(destination.name + ".staging")
    with tarfile.open(archive, "r:*") as tar:
        members = _validated_members(tar, strip_wrapper)  # full validation BEFORE creating staging
        shutil.rmtree(staging, ignore_errors=True)
        try:
            staging.mkdir(parents=True)
            for member, relative in members:
                out = staging / relative
                if member.isdir():
                    out.mkdir(parents=True, exist_ok=True)
                    _ensure_within(staging, out)
                else:
                    out.parent.mkdir(parents=True, exist_ok=True)
                    _ensure_within(staging, out)
                    src = tar.extractfile(member)
                    if src is None:
                        raise ValueError(f"cannot read archive member: {member.name!r}")
                    with src, out.open("xb") as dst:
                        shutil.copyfileobj(src, dst)
                    # Preserve the archive's executable bit, masked to ordinary permission bits so no
                    # setuid/setgid/sticky bit is ever written to the reviewed snapshot.
                    perm = member.mode & 0o777
                    os.chmod(out, 0o755 if (perm & 0o111) else 0o644)
            os.replace(staging, destination)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise


SOURCE_RECEIPT_SUFFIX = ".receipt.json"
SOURCE_RECEIPT_VERSION = 1


def source_receipt_path(source_dir: str | Path) -> Path:
    """The adjacent run-state crash-repair receipt path for a source snapshot dir: a SIBLING of
    ``RUN/source`` named ``RUN/source.receipt.json`` (``<name>`` + ``SOURCE_RECEIPT_SUFFIX``), never a
    member of ``RUN/source``. Keeping the receipt outside the reviewed tree means ``RUN/source`` holds
    exactly the archive members, so a repository file that happens to be named like a receipt is never
    shadowed or overwritten by Crucible metadata."""
    source_dir = Path(source_dir)
    return source_dir.with_name(source_dir.name + SOURCE_RECEIPT_SUFFIX)


def source_receipt(target_sha: str, archive_sha: str, kind: str) -> dict[str, Any]:
    """The canonical crash-repair receipt written to the adjacent ``source_receipt_path`` (F2/F3): it
    binds the extracted snapshot to its exact archive (``archive_sha256``), the loaded target
    (``target_sha256``), and the wrapper-stripping ``kind``, so a crashed materialization can be
    repaired/idempotently completed without re-extracting or trusting a stale/ambiguous source tree."""
    return {
        "version": SOURCE_RECEIPT_VERSION,
        "kind": kind,
        "target_sha256": target_sha,
        "archive_sha256": archive_sha,
    }


def write_source_receipt(source_dir: str | Path, receipt: dict[str, Any]) -> None:
    """Atomically write the crash-repair ``receipt`` to the adjacent ``source_receipt_path``, OUTSIDE
    ``RUN/source``. Written **before** the source staging rename, so a crash between the receipt write
    and the rename leaves a matching receipt with no source (a same-input retry reuses it; a different
    archive/target is rejected). A partial write is never visible: a same-parent ``.staging`` temp is
    written and ``os.replace``d into place, and removed if either the write or the rename fails."""
    path = source_receipt_path(source_dir)
    staging = path.with_name(path.name + ".staging")
    try:
        staging.write_text(
            json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8")
        os.replace(staging, path)
    except BaseException:
        try:
            staging.unlink()
        except OSError:
            pass
        raise


def read_source_receipt(source_dir: str | Path) -> dict[str, Any] | None:
    """The parsed adjacent receipt (``source_receipt_path``), or ``None`` when it is absent or
    unparseable. Never raises on a corrupt receipt — the caller treats a missing/unreadable receipt as
    an unverifiable source (reject rather than trust or silently delete)."""
    path = source_receipt_path(source_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def source_receipt_matches(receipt: dict[str, Any] | None,
                           target_sha: str, archive_sha: str, kind: str) -> bool:
    """Whether ``receipt`` binds exactly this ``(target_sha256, archive_sha256, kind)`` triple."""
    if not isinstance(receipt, dict):
        return False
    return (receipt.get("target_sha256") == target_sha
            and receipt.get("archive_sha256") == archive_sha
            and receipt.get("kind") == kind)
