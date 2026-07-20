"""Schema-v2 canonical integrity primitives: content digests and run-schema guards.

This module owns the deterministic bindings that let the CLI prove a gate decision refers to the
exact artifact/DAG/node identity that was reviewed:

- ``artifact_sha256`` / ``read_artifact`` hash the *exact bytes* of a Builder artifact, so CRLF and
  LF payloads stay distinct — never a universal-newline text read before hashing.
- ``canonical_json_sha256`` hashes a value as canonical UTF-8 JSON (sorted keys, tight separators),
  so structurally equal values digest identically across processes.
- ``dag_sha256`` / ``node_sha256`` digest the status-free canonical definition of the tree / node,
  so a digest is stable as work progresses yet changes if any immutable field or dependency changes.
- ``run_schema_version`` / ``require_current_schema`` read and enforce the run schema, so legacy
  (pre-v2) runs stay readable but can never be mutated or certified.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # imported only for annotations — keeps this module runtime-decoupled (no cycles)
    from crucible.dag import DAG
    from crucible.runlog import RunLog

# The append-only run-log schema version. New runs record it on ``run_start``; older runs are
# treated as legacy/unverified. It is not user configuration and cannot be overridden.
RUN_SCHEMA_VERSION = 2


def canonical_json_sha256(value: Any) -> str:
    """SHA-256 over canonical UTF-8 JSON: sorted keys and tight separators so two structurally
    equal values always digest identically. List order is preserved (it is semantically
    meaningful); callers sort set-like collections (e.g. dependencies) before hashing."""
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def artifact_sha256(data: bytes) -> str:
    """SHA-256 over the exact artifact bytes, with no newline normalization."""
    return hashlib.sha256(data).hexdigest()


def read_artifact(path: str | Path) -> tuple[str, str]:
    """Read an artifact once as raw bytes and return ``(text, artifact_sha256)``.

    The digest is over the original bytes and the text is a *strict* UTF-8 decode of those same
    bytes — never a universal-newline text read — so CRLF/LF differences survive into both the
    digest and the run-log payload.
    """
    data = Path(path).read_bytes()
    return data.decode("utf-8"), artifact_sha256(data)


def dag_sha256(dag: DAG) -> str:
    """SHA-256 binding of the tree's canonical, status-free definition."""
    return canonical_json_sha256(dag.definition_dict())


def node_sha256(dag: DAG, node_id: str) -> str:
    """SHA-256 binding of one node's canonical, status-free definition (immutable fields + deps)."""
    return canonical_json_sha256(dag.node_definition_dict(node_id))


def run_schema_version(events: list[dict[str, Any]]) -> int | None:
    """Return the schema version recorded on the run's first ``run_start`` event, or ``None`` when
    it is absent or malformed (a legacy / unverified run)."""
    for event in events:
        if event.get("event") == "run_start":
            version = event.get("schema_version")
            # bool is an int subclass; a JSON ``true`` is not a valid schema version.
            if isinstance(version, bool) or not isinstance(version, int):
                return None
            return version
    return None


def require_current_schema(run: RunLog) -> None:
    """Guard mutation/certification: reject a run that does not record the current schema version.

    A run whose ``run_start`` predates the current schema (missing or a lower version) is legacy:
    it stays readable, but its provenance is unverified, so this raises ``SystemExit`` with a clear
    instruction to start a fresh run rather than mutate or certify unbindable history.
    """
    version = run_schema_version(run.read_events())
    if version is None or version < RUN_SCHEMA_VERSION:
        found = "none" if version is None else str(version)
        raise SystemExit(
            f"crucible: legacy run at {run.path} (schema version {found}, current is "
            f"{RUN_SCHEMA_VERSION}); its provenance is unverified and cannot be mutated or "
            f"certified — start a fresh run"
        )
