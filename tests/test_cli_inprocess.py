"""In-process tests that call ``crucible.cli`` directly (the rest of the CLI suite runs via
subprocess, which leaves cli.py at 0% in-process coverage). These exercise ``main`` and the
small private helpers so a regression in a ``cmd_*`` error branch is caught fast, in-process."""

import json
from pathlib import Path

import pytest

from crucible.cli import _finding_line, _load_resolutions, _validate_gate, main
from crucible.dag import DAG
from crucible.integrity import artifact_sha256, current_bindings
from crucible.runlog import RunLog


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


def test_load_resolutions_requires_rationale_for_wontfix(tmp_path):
    # A bare wontfix clears a blocking finding without a recorded reason — now rejected.
    path = _write(tmp_path, "res.json", {"F1": "wontfix"})
    with pytest.raises(ValueError, match="rationale"):
        _load_resolutions(path)


def test_load_resolutions_requires_rationale_for_deferred(tmp_path):
    # Whitespace-only rationale does not count.
    path = _write(tmp_path, "res.json", {"F1": {"resolution": "deferred", "rationale": "  "}})
    with pytest.raises(ValueError, match="rationale"):
        _load_resolutions(path)


def test_load_resolutions_fixed_needs_no_rationale(tmp_path):
    path = _write(tmp_path, "res.json", {"F1": "fixed"})
    norm, _ = _load_resolutions(path)
    assert norm == {"F1": "fixed"}


def test_load_resolutions_wontfix_with_rationale_ok(tmp_path):
    path = _write(tmp_path, "res.json", {"F1": {"resolution": "wontfix", "rationale": "out of scope"}})
    norm, raw = _load_resolutions(path)
    assert norm == {"F1": "wontfix"} and raw["F1"]["rationale"] == "out of scope"


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
    _prepare_plan_binding(run, 1)
    binding = current_bindings(RunLog(run), "plan", 1).to_dict()
    capsys.readouterr()  # drain the init-run path print (setup above emits nothing to stdout)
    vfile = _write(tmp_path, "v.json", {"gate": "plan", "round": 1, "verdict": "APPROVE",
                                        "summary": "ok", "findings": [], **binding})
    rc = main(["verdict", "--run", run, "--gate", "plan", "--round", "1", "--file", vfile])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "CONSENSUS"


# --- verdict prints "will fix" + "unresolved blocking" finding lists (stderr) ----------------

def _init_cfg(tmp_path, cfg: dict) -> str:
    cfile = _write(tmp_path, "cfg.json", cfg)
    rc = main(["init-run", "--goal", "g", "--base-dir", str(tmp_path), "--config", cfile])
    assert rc == 0
    runs = [p for p in Path(tmp_path).iterdir() if p.is_dir()]
    assert len(runs) == 1
    return str(runs[0])


def _find(fid, severity, location="cli.py:1", claim="c", suggestion="s"):
    return {"id": fid, "severity": severity, "location": location, "claim": claim,
            "suggestion": suggestion}


def _prepare_plan_binding(run, round_index):
    """Set up a schema-2 plan binding directly via RunLog (no stdout to pollute capsys): a loaded
    DAG (round 1 only) plus a Builder artifact for this round, so `verdict`'s binding handshake has a
    non-empty artifact + DAG to bind the decision to."""
    r = RunLog(run)
    if round_index == 1:
        r.save_dag(DAG.from_dict({"nodes": [{"id": "a", "title": "A", "description": "",
                   "files": [], "test_plan": "", "status": "pending"}], "edges": []}).to_dict())
    body = f"plan body round {round_index}".encode("utf-8")
    r.append("builder_output", gate="plan", round=round_index, payload=body.decode("utf-8"),
             artifact_sha256=artifact_sha256(body))


def _run_verdict(tmp_path, run, findings, verdict_label, resolutions=None, round="1"):
    _prepare_plan_binding(run, int(round))
    binding = current_bindings(RunLog(run), "plan", int(round)).to_dict()
    vfile = _write(tmp_path, "v.json", {"gate": "plan", "round": int(round), "verdict": verdict_label,
                                        "summary": "s", "findings": findings, **binding})
    argv = ["verdict", "--run", run, "--gate", "plan", "--round", round, "--file", vfile]
    if resolutions is not None:
        rfile = _write(tmp_path, "res.json", resolutions)
        argv += ["--resolutions", rfile]
    return main(argv)


def test_verdict_fixed_blocker_lists_in_fix_section(tmp_path, capsys):
    run = _init(tmp_path)
    capsys.readouterr()
    rc = _run_verdict(tmp_path, run, [_find("F1", "blocker")], "REQUEST_CHANGES",
                      resolutions={"F1": "fixed"})
    out = capsys.readouterr()
    assert rc == 0
    assert out.out.strip() == "CHANGES"  # fixed does not clear -> still open, round < cap
    assert "Findings the Builder will fix (1):" in out.err
    assert "F1 [blocker]" in out.err
    assert "Unresolved blocking findings" not in out.err


def test_verdict_unresolved_blocker_lists_in_unresolved_section(tmp_path, capsys):
    run = _init(tmp_path)
    capsys.readouterr()
    rc = _run_verdict(tmp_path, run, [_find("F1", "blocker")], "REQUEST_CHANGES")
    out = capsys.readouterr()
    assert rc == 0
    assert out.out.strip() == "CHANGES"
    assert "Unresolved blocking findings (1):" in out.err
    assert "F1 [blocker]" in out.err
    assert "Findings the Builder will fix" not in out.err


def test_verdict_mixed_fix_and_unresolved_are_disjoint(tmp_path, capsys):
    run = _init(tmp_path)
    capsys.readouterr()
    rc = _run_verdict(tmp_path, run, [_find("F1", "blocker"), _find("F2", "major")],
                      "REQUEST_CHANGES", resolutions={"F1": "fixed"})
    err = capsys.readouterr().err
    assert rc == 0
    assert "Findings the Builder will fix (1):" in err
    assert "Unresolved blocking findings (1):" in err
    fix_i = err.index("Findings the Builder will fix")
    unres_i = err.index("Unresolved blocking findings")
    assert fix_i < unres_i  # fix list printed first
    assert "F1 [blocker]" in err[fix_i:unres_i] and "F2" not in err[fix_i:unres_i]
    assert "F2 [major]" in err[unres_i:] and "F1" not in err[unres_i:]


def test_verdict_fixed_nonblocking_still_in_fix_list_at_consensus(tmp_path, capsys):
    run = _init(tmp_path)
    capsys.readouterr()
    rc = _run_verdict(tmp_path, run, [_find("F1", "minor")], "APPROVE",
                      resolutions={"F1": "fixed"})
    out = capsys.readouterr()
    assert rc == 0
    assert out.out.strip() == "CONSENSUS"  # a minor is non-blocking
    assert "Findings the Builder will fix (1):" in out.err
    assert "F1 [minor]" in out.err
    assert "Unresolved blocking findings" not in out.err


def test_verdict_wontfix_default_clears_prints_no_sections(tmp_path, capsys):
    run = _init(tmp_path)
    capsys.readouterr()
    rc = _run_verdict(tmp_path, run, [_find("F1", "blocker")], "REQUEST_CHANGES",
                      resolutions={"F1": {"resolution": "wontfix", "rationale": "r"}})
    out = capsys.readouterr()
    assert rc == 0
    assert out.out.strip() == "CONSENSUS"  # default strict_rebuttal=false -> wontfix clears
    # No finding-list sections (the plan-consensus plan echo to stderr is separate/expected).
    assert "Findings the Builder will fix" not in out.err
    assert "Unresolved blocking findings" not in out.err


def test_verdict_clean_approve_prints_no_sections(tmp_path, capsys):
    run = _init(tmp_path)
    capsys.readouterr()
    rc = _run_verdict(tmp_path, run, [], "APPROVE")
    out = capsys.readouterr()
    assert rc == 0
    assert out.out.strip() == "CONSENSUS"
    # regression guard: no finding-list noise (plan echo to stderr is separate/expected)
    assert "Findings the Builder will fix" not in out.err
    assert "Unresolved blocking findings" not in out.err


def test_verdict_strict_rebuttal_wontfix_shows_in_unresolved_with_tag(tmp_path, capsys):
    run = _init_cfg(tmp_path, {"strict_rebuttal": True})
    capsys.readouterr()
    rc = _run_verdict(tmp_path, run, [_find("F1", "blocker")], "REQUEST_CHANGES",
                      resolutions={"F1": {"resolution": "wontfix", "rationale": "r"}})
    out = capsys.readouterr()
    assert rc == 0
    assert out.out.strip() == "CHANGES"  # strict: wontfix stays open, round < cap
    assert "Unresolved blocking findings (1):" in out.err
    assert "F1 [blocker] (wontfix)" in out.err
    assert "Findings the Builder will fix" not in out.err


def test_finding_line_format():
    from crucible.verdict import Finding
    f = Finding(id="F1", severity="blocker", location="cli.py:9", claim="boom", suggestion="s")
    assert _finding_line(f) == "  F1 [blocker] cli.py:9: boom"
    assert _finding_line(f, "wontfix") == "  F1 [blocker] (wontfix) cli.py:9: boom"
    assert _finding_line(f, "fixed") == "  F1 [blocker] cli.py:9: boom"  # 'fixed' suppressed
    g = Finding(id="F2", severity="minor", location="", claim="", suggestion="")
    assert _finding_line(g) == "  F2 [minor]"


# --- _print_approved_plan renders a resolved payload + a given DAG to any stream ------

def test_print_approved_plan_renders_to_given_stream():
    import io
    from crucible.cli import _print_approved_plan
    from crucible.dag import DAG

    dag = DAG.from_dict({"nodes": [{"id": "a", "title": "Node A"},
                                   {"id": "b", "title": "Node B"}],
                         "edges": [{"from": "b", "depends_on": "a"}]})
    buf = io.StringIO()
    _print_approved_plan("PLAN BODY TEXT", dag, buf)
    out = buf.getvalue()
    assert "=== Approved plan ===" in out
    assert "PLAN BODY TEXT" in out
    assert "=== Dependency tree (build order) ===" in out
    assert out.index("a: Node A") < out.index("b: Node B")  # build order


def test_print_approved_plan_without_plan_artifact_uses_placeholder():
    import io
    from crucible.cli import _print_approved_plan
    from crucible.dag import DAG

    dag = DAG.from_dict({"nodes": [{"id": "a", "title": "Node A"}], "edges": []})
    buf = io.StringIO()
    _print_approved_plan(None, dag, buf)
    out = buf.getvalue()
    assert "(no plan artifact logged)" in out
    assert "a: Node A" in out


# --- _bound_plan_payload selects the approved artifact by content hash, never by round ------

def test_bound_plan_payload_selects_by_artifact_hash():
    from crucible.cli import _bound_plan_payload
    events = [
        {"gate": "plan", "event": "builder_output", "round": 1, "payload": "OLD",
         "artifact_sha256": "a" * 64},
        {"gate": "plan", "event": "builder_output", "round": 1, "payload": "REVIEWED",
         "artifact_sha256": "b" * 64},
        {"gate": "plan", "event": "gate_consensus", "round": 1,
         "artifact_sha256": "b" * 64, "dag_sha256": "d" * 64},
    ]
    assert _bound_plan_payload(events) == "REVIEWED"


def test_bound_plan_payload_is_none_without_advance_terminal():
    from crucible.cli import _bound_plan_payload
    events = [{"gate": "plan", "event": "builder_output", "round": 1, "payload": "X",
               "artifact_sha256": "a" * 64}]
    assert _bound_plan_payload(events) is None


# --- symmetric-verdict routing + atomic decision (in-process) ----------------

def _init_workflow(tmp_path, workflow) -> str:
    rc = main(["init-run", "--goal", "g", "--base-dir", str(tmp_path), "--workflow", workflow])
    assert rc == 0
    runs = [p for p in Path(tmp_path).iterdir() if p.is_dir()]
    assert len(runs) == 1
    return str(runs[0])


def _peer(tmp_path, slot, gate, round_index, binding, name, verdict="APPROVE", objections=None):
    return _write(tmp_path, name, {
        "peer": slot, "gate": gate, "round": round_index, "verdict": verdict,
        "summary": "s", "objections": objections or [], **binding,
    })


def test_verdict_rejects_symmetric_run_inprocess(tmp_path):
    run = _init_workflow(tmp_path, "deep-dive")
    vfile = _write(tmp_path, "v.json", {"gate": "plan", "round": 1, "verdict": "APPROVE",
                                        "summary": "", "findings": []})
    with pytest.raises(SystemExit, match="symmetric-verdict"):
        main(["verdict", "--run", run, "--gate", "plan", "--round", "1", "--file", vfile])


def test_symmetric_verdict_rejects_build_inprocess(tmp_path):
    run = _init(tmp_path)  # build workflow
    a = _peer(tmp_path, "A", "plan", 1, {}, "a.json")
    b = _peer(tmp_path, "B", "plan", 1, {}, "b.json")
    with pytest.raises(SystemExit, match="build"):
        main(["symmetric-verdict", "--run", run, "--gate", "plan", "--round", "1",
              "--peer-a", a, "--peer-b", b])


def test_symmetric_verdict_plan_consensus_inprocess(tmp_path, capsys):
    run = _init_workflow(tmp_path, "pr-review")
    r = RunLog(run)
    r.save_dag(DAG.from_dict({"nodes": [{"id": "a", "title": "A", "description": "",
               "files": [], "test_plan": "", "status": "pending"}], "edges": []}).to_dict())
    body = b"# investigation plan"
    r.append("builder_output", gate="plan", round=1, payload=body.decode("utf-8"),
             artifact_sha256=artifact_sha256(body))
    binding = current_bindings(r, "plan", 1).to_dict()
    capsys.readouterr()
    a = _peer(tmp_path, "A", "plan", 1, binding, "a.json")
    b = _peer(tmp_path, "B", "plan", 1, binding, "b.json")
    rc = main(["symmetric-verdict", "--run", run, "--gate", "plan", "--round", "1",
               "--peer-a", a, "--peer-b", b])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "CONSENSUS"
    events = r.read_events()
    sym = [e for e in events if e["event"] == "symmetric_verdict"]
    assert len(sym) == 1 and sym[0]["peers"]["A"]["attestation"]["verdict"] == "APPROVE"
    assert any(e["event"] == "gate_consensus" and e.get("gate") == "plan" for e in events)
