import json

import pytest

from crucible.config import Config
from crucible.dag import DAG
from crucible.integrity import (
    RUN_SCHEMA_VERSION,
    artifact_sha256,
    dag_sha256,
    node_sha256,
    read_artifact,
    require_current_schema,
    run_schema_version,
)
from crucible.runlog import RunLog, init_run


def _dag(status="pending", file_name="a.py"):
    return DAG.from_dict({
        "nodes": [{
            "id": "a", "title": "A", "description": "d",
            "files": [file_name], "test_plan": "pytest tests/a -q", "status": status,
        }],
        "edges": [],
    })


def test_new_run_records_schema_version(tmp_path):
    run = init_run("g", Config.from_dict({}), base_dir=tmp_path)
    assert run_schema_version(run.read_events()) == RUN_SCHEMA_VERSION == 2


def test_dag_digest_ignores_status_but_not_definition():
    assert dag_sha256(_dag("pending")) == dag_sha256(_dag("done"))
    assert dag_sha256(_dag(file_name="a.py")) != dag_sha256(_dag(file_name="b.py"))


def test_node_digest_includes_dependencies():
    base = {
        "nodes": [
            {"id": "a", "status": "pending"},
            {"id": "b", "status": "pending"},
        ],
        "edges": [],
    }
    with_dep = json.loads(json.dumps(base))
    with_dep["edges"] = [{"from": "b", "depends_on": "a"}]
    assert node_sha256(DAG.from_dict(base), "b") != node_sha256(DAG.from_dict(with_dep), "b")


def test_artifact_hash_preserves_crlf_bytes():
    assert artifact_sha256(b"a\r\n") != artifact_sha256(b"a\n")


def test_read_artifact_hashes_original_bytes(tmp_path):
    path = tmp_path / "artifact.txt"
    path.write_bytes(b"a\r\n")
    text, digest = read_artifact(path)
    assert text == "a\r\n"
    assert digest == artifact_sha256(b"a\r\n")


def test_legacy_run_is_not_mutable(tmp_path):
    path = tmp_path / "legacy"
    path.mkdir()
    run = RunLog(path)
    run.append("run_start", goal="old", config=Config.from_dict({}).to_dict())
    with pytest.raises(SystemExit, match="legacy.*fresh run"):
        require_current_schema(run)
