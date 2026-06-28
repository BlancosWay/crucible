"""In-process tests that call ``crucible.cli`` directly (the rest of the CLI suite runs via
subprocess, which leaves cli.py at 0% in-process coverage). These exercise ``main`` and the
small private helpers so a regression in a ``cmd_*`` error branch is caught fast, in-process."""

import json
from pathlib import Path

import pytest

from crucible.cli import _load_resolutions, _validate_gate, main


def _init(tmp_path) -> str:
    rc = main(["init-run", "--goal", "g", "--base-dir", str(tmp_path)])
    assert rc == 0
    # init-run prints the run path; find the single run dir under base-dir.
    runs = [p for p in Path(tmp_path).iterdir() if p.is_dir()]
    assert len(runs) == 1
    return str(runs[0])


def _write(tmp_path, name, obj) -> str:
    f = Path(tmp_path) / name
    f.write_text(json.dumps(obj))
    return str(f)


# --- _validate_gate ----------------------------------------------------------

@pytest.mark.parametrize("gate", ["plan", "final", "dep:a", "dep:some-node"])
def test_validate_gate_accepts_valid(gate):
    _validate_gate(gate)  # does not raise


@pytest.mark.parametrize("gate", ["finale", "dep:", "dep: ", "dep:\t", "dep:a ", "DEP:a", "plan ", "", "deps:a", "dep"])
def test_validate_gate_rejects_invalid(gate):
    with pytest.raises(ValueError, match="invalid --gate"):
        _validate_gate(gate)


# --- _load_resolutions -------------------------------------------------------

def test_load_resolutions_none_path_is_empty():
    assert _load_resolutions(None) == ({}, {})


def test_load_resolutions_rejects_non_dict(tmp_path):
    path = _write(tmp_path, "res.json", ["F1"])
    with pytest.raises(ValueError, match="must be a JSON object"):
        _load_resolutions(path)


def test_load_resolutions_rejects_null_value(tmp_path):
    path = _write(tmp_path, "res.json", {"F1": None})
    with pytest.raises(ValueError, match="invalid resolution"):
        _load_resolutions(path)


def test_load_resolutions_dict_and_scalar_forms(tmp_path):
    path = _write(tmp_path, "res.json", {"F1": "fixed", "F2": {"resolution": "wontfix", "rationale": "r"}})
    norm, raw = _load_resolutions(path)
    assert norm == {"F1": "fixed", "F2": "wontfix"}
    assert raw["F1"] == {"resolution": "fixed"} and raw["F2"]["rationale"] == "r"


# --- main() error branches (caught -> exit 1; guards -> SystemExit) -----------

def test_main_load_dag_empty_exits(tmp_path):
    run = _init(tmp_path)
    dagf = _write(tmp_path, "dag.json", {"nodes": [], "edges": []})
    with pytest.raises(SystemExit):
        main(["load-dag", "--run", run, "--file", dagf])


def test_main_load_dag_non_pending_exits(tmp_path):
    run = _init(tmp_path)
    dagf = _write(tmp_path, "dag.json", {"nodes": [
        {"id": "a", "title": "A", "description": "", "files": [], "test_plan": "", "status": "done"}],
        "edges": []})
    with pytest.raises(SystemExit, match="pending"):
        main(["load-dag", "--run", run, "--file", dagf])


def test_main_verdict_invalid_gate_returns_1(tmp_path, capsys):
    run = _init(tmp_path)
    vfile = _write(tmp_path, "v.json", {"gate": "finale", "round": 1, "verdict": "APPROVE",
                                        "summary": "", "findings": []})
    rc = main(["verdict", "--run", run, "--gate", "finale", "--round", "1", "--file", vfile])
    assert rc == 1
    assert "invalid --gate" in capsys.readouterr().err


def test_main_verdict_gate_mismatch_exits(tmp_path):
    run = _init(tmp_path)
    vfile = _write(tmp_path, "v.json", {"gate": "plan", "round": 1, "verdict": "APPROVE",
                                        "summary": "", "findings": []})
    with pytest.raises(SystemExit, match="does not match"):
        main(["verdict", "--run", run, "--gate", "final", "--round", "1", "--file", vfile])


def test_main_init_run_non_object_config_returns_1(tmp_path, capsys):
    cfg = _write(tmp_path, "c.json", ["not", "an", "object"])
    rc = main(["init-run", "--goal", "g", "--base-dir", str(tmp_path), "--config", cfg])
    assert rc == 1
    assert "crucible:" in capsys.readouterr().err


def test_main_verdict_consensus_returns_0(tmp_path, capsys):
    run = _init(tmp_path)
    capsys.readouterr()  # drain the init-run path print
    vfile = _write(tmp_path, "v.json", {"gate": "plan", "round": 1, "verdict": "APPROVE",
                                        "summary": "ok", "findings": []})
    rc = main(["verdict", "--run", run, "--gate", "plan", "--round", "1", "--file", vfile])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "CONSENSUS"
