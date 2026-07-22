import json

import pytest

from crucible.config import Config
from crucible.dag import DAG
from crucible.integrity import (
    RUN_SCHEMA_VERSION,
    BindingSet,
    artifact_sha256,
    current_bindings,
    dag_sha256,
    node_sha256,
    read_artifact,
    require_current_schema,
    run_schema_version,
)
from crucible.runlog import RunLog, init_run
from crucible.target import ReviewTarget, target_from_events, target_sha256


# A minimal revision-unbound diff-file target — the smallest valid pr-review target, enough to bind
# a run's gates without any repository/revision machinery.
_TARGET_MANIFEST = {
    "version": 1, "kind": "diff-file", "revision_bound": False, "repository": None,
    "diff_sha256": "0" * 64, "changed_files": ["a.py"],
    "intent": {"title": "t", "body": "b"},
}


def _load_target(run):
    """Append the one immutable ``target_loaded`` event and return its authoritative target hash."""
    target = ReviewTarget.from_dict(_TARGET_MANIFEST)
    sha = target_sha256(target)
    run.append("target_loaded", target=target.to_dict(), target_sha256=sha)
    return sha


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


def test_binding_set_to_dict_omits_none_fields():
    assert BindingSet(artifact_sha256="a").to_dict() == {"artifact_sha256": "a"}
    assert BindingSet(artifact_sha256="a", dag_sha256="d").to_dict() == {
        "artifact_sha256": "a", "dag_sha256": "d"}
    assert BindingSet(artifact_sha256="a", dag_sha256="d", node_sha256="n").to_dict() == {
        "artifact_sha256": "a", "dag_sha256": "d", "node_sha256": "n"}


def test_current_plan_bindings_require_builder_output_and_dag(tmp_path):
    run = init_run("g", Config.from_dict({}), base_dir=tmp_path)
    run.save_dag(_dag().to_dict())
    run.append("builder_output", gate="plan", round=1, payload="plan",
               artifact_sha256=artifact_sha256(b"plan"))
    b = current_bindings(run, "plan", 1)
    assert b.artifact_sha256 == artifact_sha256(b"plan")
    assert b.dag_sha256 == dag_sha256(_dag())
    assert b.node_sha256 is None


def test_dep_bindings_include_node(tmp_path):
    run = init_run("g", Config.from_dict({}), base_dir=tmp_path)
    dag = _dag()
    run.save_dag(dag.to_dict())
    run.append(
        "builder_output",
        gate="dep:a",
        round=1,
        payload="diff",
        artifact_sha256=artifact_sha256(b"diff"),
    )
    b = current_bindings(run, "dep:a", 1)
    assert b.artifact_sha256 == artifact_sha256(b"diff")
    assert b.dag_sha256 == dag_sha256(dag)
    assert b.node_sha256 == node_sha256(dag, "a")


def test_current_bindings_require_a_logged_builder_output(tmp_path):
    run = init_run("g", Config.from_dict({}), base_dir=tmp_path)
    run.save_dag(_dag().to_dict())
    with pytest.raises(SystemExit, match="builder"):
        current_bindings(run, "plan", 1)


def test_current_bindings_use_latest_nonempty_output_for_exact_round(tmp_path):
    run = init_run("g", Config.from_dict({}), base_dir=tmp_path)
    run.save_dag(_dag().to_dict())
    run.append("builder_output", gate="plan", round=1, payload="stale",
               artifact_sha256=artifact_sha256(b"stale"))
    run.append("builder_output", gate="plan", round=1, payload="",
               artifact_sha256=artifact_sha256(b""))
    run.append("builder_output", gate="plan", round=1, payload="latest",
               artifact_sha256=artifact_sha256(b"latest"))
    # a different round must not be selected
    run.append("builder_output", gate="plan", round=2, payload="other",
               artifact_sha256=artifact_sha256(b"other"))
    assert current_bindings(run, "plan", 1).artifact_sha256 == artifact_sha256(b"latest")


def test_reproduce_bindings_are_artifact_only(tmp_path):
    run = init_run("g", Config.from_dict({}), base_dir=tmp_path)
    run.append("builder_output", gate="reproduce", round=1, payload="repro",
               artifact_sha256=artifact_sha256(b"repro"))
    b = current_bindings(run, "reproduce", 1)
    assert b.to_dict() == {"artifact_sha256": artifact_sha256(b"repro")}


def test_plan_bindings_require_a_loaded_dag(tmp_path):
    run = init_run("g", Config.from_dict({}), base_dir=tmp_path)
    run.append("builder_output", gate="plan", round=1, payload="plan",
               artifact_sha256=artifact_sha256(b"plan"))
    with pytest.raises(SystemExit, match="dependency tree"):
        current_bindings(run, "plan", 1)


def test_current_bindings_reject_non_string_payload_cleanly(tmp_path):
    # A hand-edited/foreign log with a non-string builder payload must fail closed with a clean
    # SystemExit (never an AttributeError traceback) — the binding never rests on a corrupt record.
    run = init_run("g", Config.from_dict({}), base_dir=tmp_path)
    run.save_dag(_dag().to_dict())
    run.append("builder_output", gate="plan", round=1, payload=12345, artifact_sha256="deadbeef")
    with pytest.raises(SystemExit, match="builder"):
        current_bindings(run, "plan", 1)


# --- Task 2: pr-review gate bindings carry the immutable target identity ------------------------

def test_pr_review_bindings_include_target_sha(tmp_path):
    run = init_run("g", Config.from_dict({}), base_dir=tmp_path, workflow="pr-review")
    sha = _load_target(run)
    run.save_dag(_dag().to_dict())
    run.append("builder_output", gate="plan", round=1, payload="plan",
               artifact_sha256=artifact_sha256(b"plan"))
    bindings = current_bindings(run, "plan", 1).to_dict()
    assert bindings["target_sha256"] == target_sha256(target_from_events(run.read_events()))
    assert bindings["target_sha256"] == sha


def test_pr_review_dep_bindings_include_target_sha(tmp_path):
    run = init_run("g", Config.from_dict({}), base_dir=tmp_path, workflow="pr-review")
    sha = _load_target(run)
    dag = _dag()
    run.save_dag(dag.to_dict())
    run.append("builder_output", gate="dep:a", round=1, payload="diff",
               artifact_sha256=artifact_sha256(b"diff"))
    b = current_bindings(run, "dep:a", 1).to_dict()
    assert b["target_sha256"] == sha
    assert b["dag_sha256"] == dag_sha256(dag)
    assert b["node_sha256"] == node_sha256(dag, "a")


def test_build_and_deep_dive_bindings_do_not_gain_target_sha(tmp_path):
    build = init_run("g", Config.from_dict({}), base_dir=tmp_path)
    build.save_dag(_dag().to_dict())
    build.append("builder_output", gate="plan", round=1, payload="plan",
                 artifact_sha256=artifact_sha256(b"plan"))
    assert "target_sha256" not in current_bindings(build, "plan", 1).to_dict()

    deep = init_run("g", Config.from_dict({}), base_dir=tmp_path, workflow="deep-dive")
    deep.save_dag(_dag().to_dict())
    deep.append("builder_output", gate="plan", round=1, payload="plan",
                artifact_sha256=artifact_sha256(b"plan"))
    assert "target_sha256" not in current_bindings(deep, "plan", 1).to_dict()


def test_pr_review_bindings_require_a_loaded_target(tmp_path):
    # A pr-review run whose gates are exercised before a target is loaded cannot produce bindings —
    # there is no target identity to bind to yet, so current_bindings fails closed.
    run = init_run("g", Config.from_dict({}), base_dir=tmp_path, workflow="pr-review")
    run.save_dag(_dag().to_dict())
    run.append("builder_output", gate="plan", round=1, payload="plan",
               artifact_sha256=artifact_sha256(b"plan"))
    with pytest.raises(SystemExit, match="target"):
        current_bindings(run, "plan", 1)


def test_pr_review_bindings_reject_malformed_target(tmp_path):
    # A duplicate/malformed target is not a single valid identity to bind to; bindings fail closed
    # rather than pick one.
    run = init_run("g", Config.from_dict({}), base_dir=tmp_path, workflow="pr-review")
    _load_target(run)
    _load_target(run)  # a second target_loaded — a target is immutable, so this is invalid
    run.save_dag(_dag().to_dict())
    run.append("builder_output", gate="plan", round=1, payload="plan",
               artifact_sha256=artifact_sha256(b"plan"))
    with pytest.raises(SystemExit, match="target"):
        current_bindings(run, "plan", 1)


def test_binding_set_to_dict_includes_target_when_set():
    assert BindingSet(artifact_sha256="a", dag_sha256="d", target_sha256="t").to_dict() == {
        "artifact_sha256": "a", "dag_sha256": "d", "target_sha256": "t"}
    # An omitted target stays omitted (build/deep-dive shape is unchanged).
    assert "target_sha256" not in BindingSet(artifact_sha256="a").to_dict()
