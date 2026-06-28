import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(args):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "scripts")}
    return subprocess.run(
        [sys.executable, "-m", "crucible", *args],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )


def test_init_run_prints_run_dir(tmp_path):
    r = _run(["init-run", "--goal", "Add caching", "--base-dir", str(tmp_path)])
    assert r.returncode == 0, r.stderr
    run_dir = Path(r.stdout.strip())
    assert run_dir.exists()
    assert (run_dir / "config.json").exists()


def test_full_dry_run_flow(tmp_path):
    r = _run(["init-run", "--goal", "Add caching", "--base-dir", str(tmp_path)])
    run_dir = r.stdout.strip()

    dag = {
        "nodes": [
            {"id": "a", "title": "A", "description": "", "files": [], "test_plan": "", "status": "pending"},
            {"id": "b", "title": "B", "description": "", "files": [], "test_plan": "", "status": "pending"},
        ],
        "edges": [{"from": "b", "depends_on": "a"}],
    }
    dag_file = Path(tmp_path) / "dag.json"
    dag_file.write_text(json.dumps(dag))
    r = _run(["load-dag", "--run", run_dir, "--file", str(dag_file)])
    assert r.returncode == 0, r.stderr

    r = _run(["next", "--run", run_dir])
    assert r.stdout.strip() == "a"

    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({"gate": "dep:a", "round": 1, "verdict": "APPROVE", "summary": "ok", "findings": []}))
    r = _run(["verdict", "--run", run_dir, "--gate", "dep:a", "--round", "1", "--max-rounds", "5", "--file", str(vfile)])
    assert "CONSENSUS" in r.stdout

    r = _run(["set-status", "--run", run_dir, "--node", "a", "--status", "done"])
    assert r.returncode == 0, r.stderr
    r = _run(["next", "--run", run_dir])
    assert r.stdout.strip() == "b"

    r = _run(["report", "--run", run_dir])
    assert "Add caching" in r.stdout


def test_verdict_capped_outcome(tmp_path):
    r = _run(["init-run", "--goal", "g", "--base-dir", str(tmp_path)])
    run_dir = r.stdout.strip()
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({
        "gate": "plan", "round": 5, "verdict": "REQUEST_CHANGES", "summary": "still broken",
        "findings": [{"id": "F1", "severity": "blocker", "location": "x", "claim": "c", "suggestion": "s"}],
    }))
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "5", "--max-rounds", "5", "--file", str(vfile)])
    assert "CAPPED" in r.stdout


def test_verdict_proceed_with_flags_outcome(tmp_path):
    cfg = Path(tmp_path) / "c.json"
    cfg.write_text(json.dumps({"on_cap": "proceed_with_flags"}))
    r = _run(["init-run", "--goal", "g", "--base-dir", str(tmp_path), "--config", str(cfg)])
    run_dir = r.stdout.strip()
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({
        "gate": "plan", "round": 5, "verdict": "REQUEST_CHANGES", "summary": "unresolved",
        "findings": [{"id": "F1", "severity": "blocker", "location": "x", "claim": "c", "suggestion": "s"}],
    }))
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "5", "--max-rounds", "5", "--file", str(vfile)])
    assert r.returncode == 0, r.stderr
    assert "PROCEED_WITH_FLAGS" in r.stdout
    events = [json.loads(l) for l in (Path(run_dir) / "runlog.jsonl").read_text().splitlines() if l.strip()]
    proceeded = [e for e in events if e["event"] == "gate_proceeded_with_flags"]
    assert proceeded and proceeded[-1]["open_findings"] == ["F1"]
    assert not any(e["event"] == "gate_capped" for e in events)


def test_log_appends_full_payload(tmp_path):
    r = _run(["init-run", "--goal", "g", "--base-dir", str(tmp_path)])
    run_dir = r.stdout.strip()
    payload = Path(tmp_path) / "out.txt"
    payload.write_text("B" * 3000)
    r = _run(["log", "--run", run_dir, "--event", "builder_output", "--gate", "plan", "--round", "1", "--file", str(payload)])
    assert r.returncode == 0, r.stderr
    events = [json.loads(l) for l in (Path(run_dir) / "runlog.jsonl").read_text().splitlines() if l.strip()]
    assert events[-1]["payload"] == "B" * 3000


def test_verdict_cap_from_config_when_max_rounds_omitted(tmp_path):
    cfg = Path(tmp_path) / "c.json"
    cfg.write_text(json.dumps({"max_rounds_plan": 1}))
    r = _run(["init-run", "--goal", "g", "--base-dir", str(tmp_path), "--config", str(cfg)])
    run_dir = r.stdout.strip()
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({
        "gate": "plan", "round": 1, "verdict": "REQUEST_CHANGES", "summary": "x",
        "findings": [{"id": "F1", "severity": "blocker", "location": "x", "claim": "c", "suggestion": "s"}],
    }))
    # no --max-rounds: cap should come from config (max_rounds_plan=1) -> CAPPED at round 1
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "1", "--file", str(vfile)])
    assert "CAPPED" in r.stdout, r.stdout + r.stderr


def test_verdict_rejects_gate_mismatch(tmp_path):
    r = _run(["init-run", "--goal", "g", "--base-dir", str(tmp_path)])
    run_dir = r.stdout.strip()
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({"gate": "plan", "round": 1, "verdict": "APPROVE", "summary": "", "findings": []}))
    r = _run(["verdict", "--run", run_dir, "--gate", "dep:a", "--round", "1", "--file", str(vfile)])
    assert r.returncode != 0
    assert "does not match" in r.stderr


def test_verdict_resolutions_and_raw_are_logged(tmp_path):
    r = _run(["init-run", "--goal", "g", "--base-dir", str(tmp_path)])
    run_dir = r.stdout.strip()
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({
        "gate": "plan", "round": 1, "verdict": "REQUEST_CHANGES", "summary": "x",
        "findings": [{"id": "F1", "severity": "major", "location": "x", "claim": "c", "suggestion": "s"}],
    }))
    res = Path(tmp_path) / "res.json"
    res.write_text(json.dumps({"F1": "wontfix"}))
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "1", "--file", str(vfile), "--resolutions", str(res)])
    assert "CONSENSUS" in r.stdout, r.stdout + r.stderr
    events = [json.loads(l) for l in (Path(run_dir) / "runlog.jsonl").read_text().splitlines() if l.strip()]
    kinds = [e["event"] for e in events]
    assert "builder_resolution" in kinds
    cv = [e for e in events if e["event"] == "critic_verdict"][-1]
    assert "raw" in cv and "REQUEST_CHANGES" in cv["raw"]  # full raw verdict text retained


def test_verdict_rejects_invalid_resolution(tmp_path):
    r = _run(["init-run", "--goal", "g", "--base-dir", str(tmp_path)])
    run_dir = r.stdout.strip()
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({"gate": "plan", "round": 1, "verdict": "APPROVE", "summary": "", "findings": []}))
    res = Path(tmp_path) / "res.json"
    res.write_text(json.dumps({"F1": "ignore-it"}))
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "1", "--file", str(vfile), "--resolutions", str(res)])
    assert r.returncode != 0
    assert "invalid resolution" in r.stderr


def test_verdict_rejects_contradictory_approve(tmp_path):
    r = _run(["init-run", "--goal", "g", "--base-dir", str(tmp_path)])
    run_dir = r.stdout.strip()
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({
        "gate": "plan", "round": 1, "verdict": "APPROVE", "summary": "lgtm",
        "findings": [{"id": "F1", "severity": "blocker", "location": "x", "claim": "c", "suggestion": "s"}],
    }))
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "1", "--file", str(vfile)])
    assert r.returncode != 0
    assert "inconsistent verdict" in r.stderr
    # A contradictory verdict is rejected before anything is logged.
    log = Path(run_dir) / "runlog.jsonl"
    kinds = [json.loads(l)["event"] for l in log.read_text().splitlines() if l.strip()]
    assert "critic_verdict" not in kinds


def test_verdict_rejects_request_changes_without_blocking(tmp_path):
    r = _run(["init-run", "--goal", "g", "--base-dir", str(tmp_path)])
    run_dir = r.stdout.strip()
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({
        "gate": "plan", "round": 1, "verdict": "REQUEST_CHANGES", "summary": "nits only",
        "findings": [{"id": "F1", "severity": "minor", "location": "x", "claim": "c", "suggestion": "s"}],
    }))
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "1", "--file", str(vfile)])
    assert r.returncode != 0
    assert "inconsistent verdict" in r.stderr


def test_verdict_consistency_uses_config_blocking_severities(tmp_path):
    # blocker-only policy: a `major`-only REQUEST_CHANGES is inconsistent (major non-blocking),
    # proving the boundary check reads cfg.blocking_severities, not a hardcoded {blocker,major}.
    cfg = Path(tmp_path) / "c.json"
    cfg.write_text(json.dumps({"blocking_severities": ["blocker"]}))
    r = _run(["init-run", "--goal", "g", "--base-dir", str(tmp_path), "--config", str(cfg)])
    run_dir = r.stdout.strip()
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({
        "gate": "plan", "round": 1, "verdict": "REQUEST_CHANGES", "summary": "x",
        "findings": [{"id": "F1", "severity": "major", "location": "x", "claim": "c", "suggestion": "s"}],
    }))
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "1", "--file", str(vfile)])
    assert r.returncode != 0
    assert "inconsistent verdict" in r.stderr


def _init(tmp_path):
    return _run(["init-run", "--goal", "g", "--base-dir", str(tmp_path)]).stdout.strip()


def _load(run_dir, tmp_path, nodes, edges=None):
    dag = {"nodes": [{"id": nid, "title": nid, "description": "", "files": [], "test_plan": "", "status": st}
                     for nid, st in nodes.items()],
           "edges": edges or []}
    f = Path(tmp_path) / "dag.json"
    f.write_text(json.dumps(dag))
    return _run(["load-dag", "--run", run_dir, "--file", str(f)])


def test_load_dag_rejects_empty(tmp_path):
    run_dir = _init(tmp_path)
    r = _load(run_dir, tmp_path, {})
    assert r.returncode != 0
    assert "empty" in r.stderr.lower()


def test_next_all_done_exits_zero_empty(tmp_path):
    run_dir = _init(tmp_path)
    _load(run_dir, tmp_path, {"a": "done"})
    r = _run(["next", "--run", run_dir])
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_next_ready_node_exits_zero(tmp_path):
    run_dir = _init(tmp_path)
    _load(run_dir, tmp_path, {"a": "pending"})
    r = _run(["next", "--run", run_dir])
    assert r.returncode == 0
    assert r.stdout.strip() == "a"


def test_next_blocked_node_is_stuck(tmp_path):
    run_dir = _init(tmp_path)
    _load(run_dir, tmp_path, {"a": "blocked"})
    r = _run(["next", "--run", run_dir])
    assert r.returncode == 3
    assert r.stdout.strip() == ""
    assert "stuck" in r.stderr.lower() and "a" in r.stderr


def test_next_pending_on_blocked_is_stuck(tmp_path):
    run_dir = _init(tmp_path)
    _load(run_dir, tmp_path, {"a": "blocked", "b": "pending"}, edges=[{"from": "b", "depends_on": "a"}])
    r = _run(["next", "--run", run_dir])
    assert r.returncode == 3
    assert "b" in r.stderr and "a" in r.stderr


def test_next_in_progress_is_in_flight(tmp_path):
    run_dir = _init(tmp_path)
    _load(run_dir, tmp_path, {"a": "in_progress"})
    r = _run(["next", "--run", run_dir])
    assert r.returncode == 4
    assert "flight" in r.stderr.lower()


# --- text encoding under a non-UTF-8 locale (H4) -----------------------------
ASCII_ENV = {
    **os.environ,
    "PYTHONPATH": str(ROOT / "scripts"),
    "LC_ALL": "C", "LANG": "C",
    "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0", "PYTHONIOENCODING": "",
}


def _require_ascii_locale():
    """Skip unless the ASCII-forcing env actually yields an ASCII default encoding
    (so the test guards the fix instead of passing vacuously)."""
    import pytest
    probe = subprocess.run(
        [sys.executable, "-c", "import locale; print(locale.getpreferredencoding(False))"],
        capture_output=True, text=True, env=ASCII_ENV,
    )
    enc = probe.stdout.strip().lower()
    if not ("ascii" in enc or enc in ("ansi_x3.4-1968", "646")):
        pytest.skip(f"cannot force a non-UTF-8 locale on this platform (got {enc!r})")


def _run_ascii(args):
    return subprocess.run([sys.executable, "-m", "crucible", *args],
                          capture_output=True, text=True, env=ASCII_ENV, cwd=ROOT)


def test_load_dag_accepts_non_ascii_title_under_ascii_locale(tmp_path):
    _require_ascii_locale()
    run_dir = _init(tmp_path)
    dagf = Path(tmp_path) / "dag.json"
    dagf.write_text(json.dumps({"nodes": [{"id": "a", "title": "Café ✅", "description": "",
                    "files": [], "test_plan": "", "status": "pending"}], "edges": []}),
                    encoding="utf-8")
    r = _run_ascii(["load-dag", "--run", run_dir, "--file", str(dagf)])
    assert r.returncode == 0, r.stderr
    saved = json.loads((Path(run_dir) / "dag.json").read_text(encoding="utf-8"))
    assert saved["nodes"][0]["title"] == "Café ✅"


def test_init_run_preserves_non_ascii_config_under_ascii_locale(tmp_path):
    _require_ascii_locale()
    cfgf = Path(tmp_path) / "c.json"
    cfgf.write_text(json.dumps({"builder": {"model": "modèle", "effort": "max"}}, ensure_ascii=False),
                    encoding="utf-8")
    r = _run_ascii(["init-run", "--goal", "g", "--base-dir", str(tmp_path), "--config", str(cfgf)])
    assert r.returncode == 0, r.stderr
    saved = (Path(r.stdout.strip()) / "config.json").read_text(encoding="utf-8")
    assert "modèle" in saved


# --- clean error handling (M5) -----------------------------------------------

def _assert_clean_error(r, *needles):
    assert r.returncode != 0
    assert "Traceback" not in r.stderr, r.stderr
    assert "crucible:" in r.stderr, r.stderr
    for n in needles:
        assert n in r.stderr, r.stderr


def test_verdict_malformed_json_is_clean(tmp_path):
    run_dir = _init(tmp_path)
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text("{ not valid json")
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "1", "--file", str(vfile)])
    _assert_clean_error(r, "invalid JSON")


def test_verdict_missing_field_is_clean(tmp_path):
    run_dir = _init(tmp_path)
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({"gate": "plan", "round": 1, "findings": []}))  # no "verdict"
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "1", "--file", str(vfile)])
    _assert_clean_error(r, "missing required field", "verdict")


def test_load_dag_cycle_is_clean(tmp_path):
    run_dir = _init(tmp_path)
    dagf = Path(tmp_path) / "dag.json"
    dagf.write_text(json.dumps({"nodes": [{"id": "a"}, {"id": "b"}],
                                "edges": [{"from": "a", "depends_on": "b"},
                                          {"from": "b", "depends_on": "a"}]}))
    r = _run(["load-dag", "--run", run_dir, "--file", str(dagf)])
    _assert_clean_error(r, "cycle")


def test_next_missing_dag_is_clean(tmp_path):
    run_dir = _init(tmp_path)  # no dag loaded
    r = _run(["next", "--run", run_dir])
    _assert_clean_error(r)


def test_set_status_unknown_node_is_clean(tmp_path):
    run_dir = _init(tmp_path)
    _load(run_dir, tmp_path, {"a": "pending"})
    r = _run(["set-status", "--run", run_dir, "--node", "nope", "--status", "done"])
    _assert_clean_error(r, "unknown node")


def test_report_corrupt_runlog_is_clean(tmp_path):
    run_dir = _init(tmp_path)
    (Path(run_dir) / "runlog.jsonl").write_bytes(b'{"event": "run_start"}\nNOT_JSON\n')
    r = _run(["report", "--run", run_dir])
    _assert_clean_error(r)


def test_resolutions_non_dict_is_clean(tmp_path):
    run_dir = _init(tmp_path)
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({"gate": "plan", "round": 1, "verdict": "APPROVE",
                                 "summary": "", "findings": []}))
    res = Path(tmp_path) / "res.json"
    res.write_text(json.dumps(["F1", "F2"]))  # a list, not an object
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "1",
              "--file", str(vfile), "--resolutions", str(res)])
    _assert_clean_error(r)


def test_load_dag_files_as_string_is_clean(tmp_path):
    run_dir = _init(tmp_path)
    dagf = Path(tmp_path) / "dag.json"
    dagf.write_text(json.dumps({"nodes": [{"id": "a", "files": "src/a.py"}], "edges": []}))
    r = _run(["load-dag", "--run", run_dir, "--file", str(dagf)])
    _assert_clean_error(r, "files")


# --- should-final gate (M6) --------------------------------------------------

def test_should_final_yes_by_default(tmp_path):
    run_dir = _init(tmp_path)
    r = _run(["should-final", "--run", run_dir])
    assert r.returncode == 0
    assert r.stdout.strip() == "yes"


def test_should_final_no_when_disabled(tmp_path):
    cfg = Path(tmp_path) / "c.json"
    cfg.write_text(json.dumps({"final_review": False}))
    run_dir = _run(["init-run", "--goal", "g", "--base-dir", str(tmp_path), "--config", str(cfg)]).stdout.strip()
    r = _run(["should-final", "--run", run_dir])
    assert r.returncode == 1
    assert r.stdout.strip() == "no"


def test_should_final_missing_config_is_clean(tmp_path):
    # An error (e.g. missing config) must be distinguishable from "no": clean stderr, no yes/no token.
    bare = Path(tmp_path) / "bare_run"
    bare.mkdir()
    r = _run(["should-final", "--run", str(bare)])
    assert r.returncode != 0
    assert "Traceback" not in r.stderr
    assert "crucible:" in r.stderr
    assert r.stdout.strip() not in ("yes", "no")


def test_verdict_rejects_round_below_one(tmp_path):
    run_dir = _init(tmp_path)
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({"gate": "plan", "round": 0, "verdict": "APPROVE",
                                 "summary": "", "findings": []}))
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "0", "--file", str(vfile)])
    assert r.returncode != 0
    assert "must be >= 1" in r.stderr
