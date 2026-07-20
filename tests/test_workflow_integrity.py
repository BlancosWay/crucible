import json
import os
import subprocess
import sys
from pathlib import Path

from crucible.config import Config
from crucible.dag import DAG
from crucible.integrity import artifact_sha256, dag_sha256, node_sha256
from crucible.report import render_markdown
from crucible.runlog import init_run


ROOT = Path(__file__).resolve().parents[1]


def _run(args):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "scripts")}
    return subprocess.run(
        [sys.executable, "-m", "crucible", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=ROOT,
    )


def _init(tmp_path, config=None):
    args = ["init-run", "--goal", "workflow integrity", "--base-dir", str(tmp_path)]
    if config is not None:
        cfg = tmp_path / "config-input.json"
        cfg.write_text(json.dumps(config))
        args.extend(["--config", str(cfg)])
    result = _run(args)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _dag_file(tmp_path, *, file_name="old.py"):
    path = tmp_path / f"dag-{file_name}.json"
    path.write_text(json.dumps({
        "nodes": [{
            "id": "x",
            "title": "X",
            "description": f"Review {file_name}",
            "files": [file_name],
            "test_plan": "pytest tests/x -q",
            "status": "pending",
        }],
        "edges": [],
    }))
    return path


def _log_builder(run_dir, tmp_path, gate, round_index, text):
    artifact = tmp_path / f"{gate.replace(':', '-')}-{round_index}.txt"
    artifact.write_text(text)
    return _run([
        "log", "--run", run_dir, "--event", "builder_output",
        "--gate", gate, "--round", str(round_index), "--file", str(artifact),
    ])


def _start(run_dir, node):
    """Move a node pending -> in_progress (the legal precondition for reviewing its work)."""
    return _run(["set-status", "--run", run_dir, "--node", node, "--status", "in_progress"])


def _approve(run_dir, tmp_path, gate, round_index=1):
    # Schema-2 binding handshake: fetch the CLI-selected bindings for this gate/round and echo them
    # back in the verdict, exactly as a Critic must. Assumes the Builder artifact was already logged.
    bindings = _run(["bindings", "--run", run_dir, "--gate", gate, "--round", str(round_index)])
    assert bindings.returncode == 0, bindings.stderr
    verdict = tmp_path / f"{gate.replace(':', '-')}-verdict.json"
    verdict.write_text(json.dumps({
        "gate": gate,
        "round": round_index,
        "verdict": "APPROVE",
        "summary": "approved",
        "findings": [],
        **json.loads(bindings.stdout),
    }))
    return _run([
        "verdict", "--run", run_dir, "--gate", gate,
        "--round", str(round_index), "--file", str(verdict),
    ])


def _settle_plan(run_dir, tmp_path):
    assert _run([
        "load-dag", "--run", run_dir, "--file", str(_dag_file(tmp_path)),
    ]).returncode == 0
    assert _log_builder(run_dir, tmp_path, "plan", 1, "reviewed plan").returncode == 0
    result = _approve(run_dir, tmp_path, "plan")
    assert result.returncode == 0, result.stderr


def test_post_consensus_same_round_plan_output_is_rejected(tmp_path):
    run_dir = _init(tmp_path)
    _settle_plan(run_dir, tmp_path)

    late = _log_builder(run_dir, tmp_path, "plan", 1, "unreviewed replacement")

    assert late.returncode != 0
    assert "concluded" in late.stderr.lower() or "terminal" in late.stderr.lower()


def test_post_consensus_dag_replacement_is_rejected(tmp_path):
    run_dir = _init(tmp_path)
    _settle_plan(run_dir, tmp_path)

    replacement = _run([
        "load-dag", "--run", run_dir,
        "--file", str(_dag_file(tmp_path, file_name="new.py")),
    ])

    assert replacement.returncode != 0
    assert "plan" in replacement.stderr.lower() and "concluded" in replacement.stderr.lower()


def test_stale_same_id_review_cannot_authorize_replacement_node(tmp_path):
    run_dir = _init(tmp_path)
    _settle_plan(run_dir, tmp_path)
    assert _start(run_dir, "x").returncode == 0
    assert _log_builder(run_dir, tmp_path, "dep:x", 1, "reviewed old.py").returncode == 0
    assert _approve(run_dir, tmp_path, "dep:x").returncode == 0

    replacement = json.loads(_dag_file(tmp_path, file_name="different.py").read_text())
    (Path(run_dir) / "dag.json").write_text(json.dumps(replacement))
    done = _run(["set-status", "--run", run_dir, "--node", "x", "--status", "done"])

    assert done.returncode != 0
    assert "binding" in done.stderr.lower() or "reviewed" in done.stderr.lower()


def test_final_before_all_nodes_done_is_rejected(tmp_path):
    run_dir = _init(tmp_path)
    _settle_plan(run_dir, tmp_path)

    # The FIRST guarded FINAL operation — logging the Builder artifact — is rejected while the single
    # node is still pending; FINAL cannot begin before the implementation is complete.
    result = _log_builder(run_dir, tmp_path, "final", 1, "whole implementation")

    assert result.returncode != 0
    assert "done" in result.stderr.lower() or "unfinished" in result.stderr.lower()


def test_pending_node_cannot_be_reviewed(tmp_path):
    run_dir = _init(tmp_path)
    _settle_plan(run_dir, tmp_path)

    # The FIRST dependency operation — logging the Builder artifact — is rejected while node x is
    # still pending; a dependency can only be reviewed once its work is in progress.
    result = _log_builder(run_dir, tmp_path, "dep:x", 1, "not implemented")

    assert result.returncode != 0
    assert "pending" in result.stderr.lower() or "in_progress" in result.stderr


def test_dag_rejects_direct_pending_to_done_transition():
    dag = DAG.from_dict({
        "nodes": [{
            "id": "x",
            "title": "X",
            "description": "",
            "files": [],
            "test_plan": "",
            "status": "pending",
        }],
        "edges": [],
    })

    try:
        dag.set_status("x", "done")
    except ValueError as exc:
        assert "pending" in str(exc) and "done" in str(exc)
    else:
        raise AssertionError("pending -> done must be rejected")


def test_report_is_not_clean_when_configured_phases_are_omitted(tmp_path):
    cfg = Config.from_dict({
        "reproduce_gate": True,
        "human_approval": True,
        "final_review": True,
    })
    run = init_run("missing configured phases", cfg, base_dir=tmp_path)
    dag = DAG.from_dict({
        "nodes": [{
            "id": "x",
            "title": "X",
            "description": "do x",
            "files": ["x.py"],
            "test_plan": "pytest",
            "status": "done",
        }],
        "edges": [],
    })
    run.save_dag(dag.to_dict())
    # Valid schema-v2 PLAN and dependency bindings: the run is not-CLEAN because REPRODUCE,
    # approval, and FINAL are omitted (config-awareness), NOT because a binding is invalid.
    plan_artifact = artifact_sha256(b"reviewed plan")
    run.append("builder_output", gate="plan", round=1, payload="reviewed plan",
               artifact_sha256=plan_artifact)
    run.append("gate_consensus", gate="plan", round=1, artifact_sha256=plan_artifact,
               dag_sha256=dag_sha256(dag))
    dep_artifact = artifact_sha256(b"impl x")
    run.append("builder_output", gate="dep:x", round=1, payload="impl x",
               artifact_sha256=dep_artifact)
    run.append("gate_consensus", gate="dep:x", round=1, artifact_sha256=dep_artifact,
               dag_sha256=dag_sha256(dag), node_sha256=node_sha256(dag, "x"))

    report = render_markdown(run)

    assert "Status:** CLEAN" not in report
    assert "reproduce" in report.lower()
    assert "approval" in report.lower()
    assert "final" in report.lower()
