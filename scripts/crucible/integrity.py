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
from dataclasses import asdict, dataclass
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


@dataclass(frozen=True)
class BindingSet:
    """The deterministic content bindings that identify what a gate decision refers to: the exact
    Builder ``artifact`` plus the relevant ``dag``/``node`` definition. Only ``artifact_sha256`` is
    always present; ``dag_sha256``/``node_sha256`` are gate-specific (see ``current_bindings``).

    ``to_dict`` drops absent fields so the CLI emits exactly the bindings a gate requires — the same
    shape the Critic verdict must echo back — with no ``null`` placeholders for a plan's node hash.
    """

    artifact_sha256: str
    dag_sha256: str | None = None
    node_sha256: str | None = None

    def to_dict(self) -> dict[str, str]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def _latest_builder_artifact_sha256(run: RunLog, gate: str, round_index: int) -> str:
    """Return the recorded ``artifact_sha256`` of the LATEST non-empty ``builder_output`` for the
    exact ``gate``/``round_index``.

    "Latest" is load-bearing: multiple Builder outputs may be logged in a non-terminal round, and
    the newest is the candidate the Critic reviews. The lookup is keyed by exact gate AND round so a
    later round (or a different gate) can never be substituted. Raises ``SystemExit`` when nothing
    qualifies, when the recorded hash is absent, or when it disagrees with the stored payload bytes
    (a hand-edited log), so a binding can never rest on a missing or forged artifact identity.
    """
    outputs = [
        event
        for event in run.read_events()
        if event.get("event") == "builder_output"
        and event.get("gate") == gate
        and event.get("round") == round_index
        and isinstance(event.get("payload"), str)
        and event.get("payload")
    ]
    if not outputs:
        raise SystemExit(
            f"crucible: no non-empty builder artifact logged for gate {gate!r} round "
            f"{round_index}; log the Builder output before requesting bindings"
        )
    latest = outputs[-1]
    recorded = latest.get("artifact_sha256")
    if not isinstance(recorded, str) or not recorded:
        raise SystemExit(
            f"crucible: builder artifact for gate {gate!r} round {round_index} has no recorded "
            f"artifact_sha256; re-log it under the current schema"
        )
    # The payload is a strict UTF-8 decode of the original artifact bytes, so re-encoding it must
    # reproduce those bytes. A mismatch means the logged payload or hash was tampered with.
    if recorded != artifact_sha256(latest["payload"].encode("utf-8")):
        raise SystemExit(
            f"crucible: builder artifact for gate {gate!r} round {round_index} does not match its "
            f"recorded artifact_sha256; the run log was altered — start a fresh run"
        )
    return recorded


def current_bindings(run: RunLog, gate: str, round_index: int) -> BindingSet:
    """The exact content bindings the CLI would certify for ``gate``/``round_index`` right now.

    The artifact hash comes from the latest non-empty Builder output for that exact gate/round; the
    DAG/node hashes are recomputed from the *current* dependency tree. Fields are gate-specific:

    - ``reproduce`` → artifact only;
    - ``plan`` / ``final`` → artifact + DAG;
    - ``dep:<id>`` → artifact + DAG + that node.

    Because DAG/node hashes are recomputed live, a plan/DAG/node substituted after the Critic
    reviewed it yields different bindings — which the verdict handshake and ``set-status`` guard use
    to reject stale/substituted decisions. Raises ``SystemExit`` when a required DAG is absent or the
    ``dep:`` node is unknown.
    """
    from crucible.dag import DAG  # local import: dag.py has no crucible deps, so this cannot cycle

    artifact = _latest_builder_artifact_sha256(run, gate, round_index)
    dag_hash: str | None = None
    node_hash: str | None = None
    if gate in ("plan", "final") or gate.startswith("dep:"):
        try:
            dag = DAG.from_dict(run.load_dag())
        except FileNotFoundError:
            raise SystemExit(
                f"crucible: gate {gate!r} bindings require a loaded dependency tree; run load-dag "
                f"first"
            )
        dag_hash = dag_sha256(dag)
        if gate.startswith("dep:"):
            node_id = gate[len("dep:"):]
            if node_id not in dag.nodes:
                raise SystemExit(
                    f"crucible: gate {gate!r} targets unknown node {node_id!r}; known nodes: "
                    f"{sorted(dag.nodes)}"
                )
            node_hash = node_sha256(dag, node_id)
    return BindingSet(artifact_sha256=artifact, dag_sha256=dag_hash, node_sha256=node_hash)
