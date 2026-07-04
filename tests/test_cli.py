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


def test_init_run_default_base_is_crucible_home(tmp_path):
    # No --base-dir: runs land under ~/.crucible/runs, never the target repo.
    env = {**os.environ, "PYTHONPATH": str(ROOT / "scripts"), "HOME": str(tmp_path)}
    env.pop("CRUCIBLE_RUNS_DIR", None)
    r = subprocess.run([sys.executable, "-m", "crucible", "init-run", "--goal", "g"],
                       capture_output=True, text=True, env=env, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    run_dir = Path(r.stdout.strip())
    assert run_dir.exists()
    assert str(run_dir).startswith(str(tmp_path / ".crucible" / "runs"))


def test_init_run_env_override_base(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "scripts"), "CRUCIBLE_RUNS_DIR": str(tmp_path / "elsewhere")}
    r = subprocess.run([sys.executable, "-m", "crucible", "init-run", "--goal", "g"],
                       capture_output=True, text=True, env=env, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    assert str(Path(r.stdout.strip())).startswith(str(tmp_path / "elsewhere"))


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


def _events(run_dir):
    return [json.loads(l) for l in (Path(run_dir) / "runlog.jsonl").read_text().splitlines() if l.strip()]


def _two_node_dag():
    return {"nodes": [{"id": "a", "title": "A", "description": "", "files": [], "test_plan": "",
                       "status": "pending"}], "edges": []}


def test_log_rejects_reserved_event(tmp_path):
    # N1: `log` must not be able to forge a CLI-managed terminal/verdict event.
    run_dir = _init(tmp_path)
    before = len(_events(run_dir))
    r = _run(["log", "--run", run_dir, "--event", "gate_consensus", "--gate", "plan", "--round", "1"])
    assert r.returncode != 0
    assert "crucible:" in r.stderr and "builder_output" in r.stderr
    # nothing was appended
    assert not any(e["event"] == "gate_consensus" for e in _events(run_dir))
    assert len(_events(run_dir)) == before


def test_log_rejects_arbitrary_event(tmp_path):
    run_dir = _init(tmp_path)
    r = _run(["log", "--run", run_dir, "--event", "note", "--gate", "plan", "--round", "1"])
    assert r.returncode != 0
    assert "crucible:" in r.stderr


def test_log_critic_output_is_allowed(tmp_path):
    run_dir = _init(tmp_path)
    f = Path(tmp_path) / "critic.txt"
    f.write_text("the critic said things")
    r = _run(["log", "--run", run_dir, "--event", "critic_output", "--gate", "plan", "--round", "1", "--file", str(f)])
    assert r.returncode == 0, r.stderr
    assert _events(run_dir)[-1]["payload"] == "the critic said things"


def test_log_stores_json_file_as_raw_text(tmp_path):
    # N2: a JSON-shaped builder_output must be kept verbatim, not parsed + re-serialized.
    run_dir = _init(tmp_path)
    f = Path(tmp_path) / "out.json"
    raw = '{"b": 2,   "a": 1}\n'  # deliberate spacing/key-order
    f.write_text(raw)
    r = _run(["log", "--run", run_dir, "--event", "builder_output", "--gate", "plan", "--round", "1", "--file", str(f)])
    assert r.returncode == 0, r.stderr
    assert _events(run_dir)[-1]["payload"] == raw


def test_log_echoes_payload_to_terminal(tmp_path):
    run_dir = _init(tmp_path)
    f = Path(tmp_path) / "plan.md"
    f.write_text("# Final plan\nStep 1: do the thing.\n")
    r = _run(["log", "--run", run_dir, "--event", "builder_output",
              "--gate", "plan", "--round", "1", "--file", str(f)])
    assert r.returncode == 0, r.stderr
    assert "logged builder_output" in r.stdout
    assert "gate plan" in r.stdout and "round 1" in r.stdout
    # the actual plan details are visible on the terminal, not just a confirmation
    assert "# Final plan" in r.stdout and "Step 1: do the thing." in r.stdout


def test_log_without_file_reports_empty_payload(tmp_path):
    run_dir = _init(tmp_path)
    r = _run(["log", "--run", run_dir, "--event", "critic_output",
              "--gate", "plan", "--round", "1"])
    assert r.returncode == 0, r.stderr
    assert "empty payload" in r.stdout


def test_log_echoes_non_ascii_payload_under_ascii_locale(tmp_path):
    _require_ascii_locale()
    run_dir = _init(tmp_path)
    f = Path(tmp_path) / "plan.md"
    f.write_text("# Plan café ✅\n", encoding="utf-8")
    r = _run_ascii(["log", "--run", run_dir, "--event", "builder_output",
                    "--gate", "plan", "--round", "1", "--file", str(f)])
    assert r.returncode == 0, r.stderr                       # encoding-safe: no crash
    assert _events(run_dir)[-1]["payload"] == "# Plan café ✅\n"  # stored verbatim (UTF-8 runlog)


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
    # load-dag imports a *fresh* plan: every node must be `pending` (G2). To exercise next/
    # report against mixed statuses, load all-pending then transition via set-status — this
    # mirrors production, where statuses only ever change through set-status, never baked
    # into the imported plan.
    dag = {"nodes": [{"id": nid, "title": nid, "description": "", "files": [], "test_plan": "",
                      "status": "pending"} for nid in nodes],
           "edges": edges or []}
    f = Path(tmp_path) / "dag.json"
    f.write_text(json.dumps(dag))
    r = _run(["load-dag", "--run", run_dir, "--file", str(f)])
    if r.returncode == 0:
        for nid, st in nodes.items():
            if st != "pending":
                # Fixture scaffolding fabricates arbitrary states; --force bypasses the
                # done-gate requirement (H2). Assert it applied so setup can't silently no-op.
                sr = _run(["set-status", "--run", run_dir, "--node", nid, "--status", st, "--force"])
                assert sr.returncode == 0, sr.stderr
    return r


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


def test_cli_stdout_tolerates_non_ascii_under_ascii_locale(tmp_path):
    # main() reconfigures stdout so echoing non-ASCII cannot abort with UnicodeEncodeError under
    # an ASCII/C locale. Source the non-ASCII from a FILE (load-dag's tree) — NOT argv, which a
    # C locale decodes via surrogateescape and would fail the UTF-8 run-log write for reasons
    # unrelated to stdout. Assert it renders faithfully: raw if stdout is UTF-8, or backslash-
    # escaped (é -> \xe9, ✅ -> \u2705) if stdout is ASCII — never dropped or replaced with '?'.
    _require_ascii_locale()
    run_dir = _init(tmp_path)
    dagf = Path(tmp_path) / "dag.json"
    dagf.write_text(json.dumps({"nodes": [{"id": "a", "title": "Café ✅", "description": "",
                    "files": [], "test_plan": "", "status": "pending"}], "edges": []}),
                    encoding="utf-8")
    r = _run_ascii(["load-dag", "--run", run_dir, "--file", str(dagf)])
    assert r.returncode == 0, r.stderr
    assert ("Café ✅" in r.stdout) or (r"Caf\xe9 \u2705" in r.stdout)


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


# --- should-approve gate (human approval, default off) -----------------------

def test_should_approve_no_by_default(tmp_path):
    run_dir = _init(tmp_path)
    r = _run(["should-approve", "--run", run_dir])
    assert r.returncode == 1
    assert r.stdout.strip() == "no"


def test_should_approve_yes_when_enabled(tmp_path):
    cfg = Path(tmp_path) / "c.json"
    cfg.write_text(json.dumps({"human_approval": True}))
    run_dir = _run(["init-run", "--goal", "g", "--base-dir", str(tmp_path), "--config", str(cfg)]).stdout.strip()
    r = _run(["should-approve", "--run", run_dir])
    assert r.returncode == 0
    assert r.stdout.strip() == "yes"


def test_should_approve_missing_config_is_clean(tmp_path):
    bare = Path(tmp_path) / "bare_run"
    bare.mkdir()
    r = _run(["should-approve", "--run", str(bare)])
    assert r.returncode != 0
    assert "Traceback" not in r.stderr
    assert "crucible:" in r.stderr
    assert r.stdout.strip() not in ("yes", "no")


def test_should_reproduce_no_by_default(tmp_path):
    run_dir = _init(tmp_path)
    r = _run(["should-reproduce", "--run", run_dir])
    assert r.returncode == 1
    assert r.stdout.strip() == "no"


def test_should_reproduce_yes_when_enabled(tmp_path):
    cfg = Path(tmp_path) / "c.json"
    cfg.write_text(json.dumps({"reproduce_gate": True}))
    run_dir = _run(["init-run", "--goal", "g", "--base-dir", str(tmp_path), "--config", str(cfg)]).stdout.strip()
    r = _run(["should-reproduce", "--run", run_dir])
    assert r.returncode == 0
    assert r.stdout.strip() == "yes"


def test_should_reproduce_missing_config_is_clean(tmp_path):
    bare = Path(tmp_path) / "bare_repro"
    bare.mkdir()
    r = _run(["should-reproduce", "--run", str(bare)])
    assert r.returncode != 0
    assert "Traceback" not in r.stderr
    assert "crucible:" in r.stderr
    assert r.stdout.strip() not in ("yes", "no")


# --- show-plan: echo the approved plan + DAG to the terminal at consensus -----

def _approve_plan(tmp_path):
    run_dir = _init(tmp_path)
    df = Path(tmp_path) / "d.json"; df.write_text(json.dumps(_two_node_dag()))
    _run(["load-dag", "--run", run_dir, "--file", str(df)])
    plan = Path(tmp_path) / "plan.md"; plan.write_text("# Final plan\nDo the thing.")
    _run(["log", "--run", run_dir, "--event", "builder_output", "--gate", "plan", "--round", "1", "--file", str(plan)])
    v = Path(tmp_path) / "v.json"
    v.write_text(json.dumps({"gate": "plan", "round": 1, "verdict": "APPROVE", "summary": "ok", "findings": []}))
    _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "1", "--file", str(v)])
    return run_dir


def test_show_plan_prints_final_plan_and_dag(tmp_path):
    run_dir = _approve_plan(tmp_path)
    r = _run(["show-plan", "--run", run_dir])
    assert r.returncode == 0
    assert "Final plan" in r.stdout and "Do the thing." in r.stdout
    assert "a" in r.stdout and "Dependency tree" in r.stdout


def test_show_plan_requires_plan_consensus(tmp_path):
    run_dir = _init(tmp_path)
    df = Path(tmp_path) / "d.json"; df.write_text(json.dumps(_two_node_dag()))
    _run(["load-dag", "--run", run_dir, "--file", str(df)])
    r = _run(["show-plan", "--run", run_dir])
    assert r.returncode != 0
    assert "consensus" in r.stderr.lower()


def test_verdict_echoes_plan_and_dag_to_stderr_on_plan_consensus(tmp_path):
    # Bug repro: when the PLAN gate settles, `crucible verdict` must deterministically echo the
    # approved plan + dependency tree to the terminal (stderr) so the final plan/DAG is always
    # visible before implementation — not reliant on a separately-invoked `show-plan` (which the
    # orchestrator can skip). The outcome token must stay alone on stdout.
    run_dir = _init(tmp_path)
    df = Path(tmp_path) / "d.json"; df.write_text(json.dumps(_two_node_dag()))
    _run(["load-dag", "--run", run_dir, "--file", str(df)])
    plan = Path(tmp_path) / "plan.md"; plan.write_text("# Final plan\nDo the thing.")
    _run(["log", "--run", run_dir, "--event", "builder_output", "--gate", "plan",
          "--round", "1", "--file", str(plan)])
    v = Path(tmp_path) / "v.json"
    v.write_text(json.dumps({"gate": "plan", "round": 1, "verdict": "APPROVE",
                             "summary": "ok", "findings": []}))
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "1", "--file", str(v)])
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "CONSENSUS"          # outcome token stays alone on stdout
    assert "Approved plan" in r.stderr              # plan echoed to stderr at settlement
    assert "Final plan" in r.stderr and "Do the thing." in r.stderr
    assert "Dependency tree" in r.stderr            # dependency tree echoed to stderr


def _load_two_node_dag(tmp_path, run_dir):
    df = Path(tmp_path) / "d.json"; df.write_text(json.dumps(_two_node_dag()))
    _run(["load-dag", "--run", run_dir, "--file", str(df)])


def test_verdict_plan_changes_does_not_echo_plan(tmp_path):
    # A non-settling plan outcome (CHANGES) must NOT echo the approved plan/DAG.
    run_dir = _init(tmp_path)
    _load_two_node_dag(tmp_path, run_dir)
    v = Path(tmp_path) / "v.json"
    v.write_text(json.dumps({"gate": "plan", "round": 1, "verdict": "REQUEST_CHANGES",
                             "summary": "no", "findings": [{"id": "F1", "severity": "blocker",
                             "location": "x", "claim": "c", "suggestion": "s"}]}))
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "1", "--file", str(v)])
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "CHANGES"
    assert "Approved plan" not in (r.stdout + r.stderr)
    assert "Dependency tree" not in (r.stdout + r.stderr)


def test_verdict_plan_proceed_with_flags_echoes_plan(tmp_path):
    # PROCEED_WITH_FLAGS advances past the PLAN gate, so it MUST echo the approved plan + DAG.
    cfg = Path(tmp_path) / "c.json"; cfg.write_text(json.dumps({"on_cap": "proceed_with_flags"}))
    r0 = _run(["init-run", "--goal", "g", "--base-dir", str(tmp_path), "--config", str(cfg)])
    run_dir = r0.stdout.strip()
    _load_two_node_dag(tmp_path, run_dir)
    plan = Path(tmp_path) / "plan.md"; plan.write_text("# Final plan\nProceed body.")
    _run(["log", "--run", run_dir, "--event", "builder_output", "--gate", "plan",
          "--round", "5", "--file", str(plan)])
    v = Path(tmp_path) / "v.json"
    v.write_text(json.dumps({"gate": "plan", "round": 5, "verdict": "REQUEST_CHANGES",
                             "summary": "unresolved", "findings": [{"id": "F1", "severity": "blocker",
                             "location": "x", "claim": "c", "suggestion": "s"}]}))
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "5", "--max-rounds", "5",
              "--file", str(v)])
    assert r.returncode == 0, r.stderr
    assert "PROCEED_WITH_FLAGS" in r.stdout
    assert "Approved plan" in r.stderr and "Proceed body." in r.stderr
    assert "Dependency tree" in r.stderr
    assert "Approved plan" not in r.stdout          # echo is stderr-only


def test_verdict_plan_capped_does_not_echo_plan(tmp_path):
    # CAPPED (halt) does not advance past the gate, so it must NOT echo the approved plan/DAG.
    run_dir = _init(tmp_path)
    _load_two_node_dag(tmp_path, run_dir)
    v = Path(tmp_path) / "v.json"
    v.write_text(json.dumps({"gate": "plan", "round": 5, "verdict": "REQUEST_CHANGES",
                             "summary": "still broken", "findings": [{"id": "F1", "severity": "blocker",
                             "location": "x", "claim": "c", "suggestion": "s"}]}))
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "5", "--max-rounds", "5",
              "--file", str(v)])
    assert "CAPPED" in r.stdout
    assert "Approved plan" not in (r.stdout + r.stderr)
    assert "Dependency tree" not in (r.stdout + r.stderr)


def test_verdict_dep_gate_consensus_does_not_echo_plan(tmp_path):
    # The echo is PLAN-gate only: a dependency gate reaching consensus must not echo the plan.
    run_dir = _init(tmp_path)
    _load_two_node_dag(tmp_path, run_dir)          # node "a" exists so dep:a is a real gate
    v = Path(tmp_path) / "v.json"
    v.write_text(json.dumps({"gate": "dep:a", "round": 1, "verdict": "APPROVE",
                             "summary": "ok", "findings": []}))
    r = _run(["verdict", "--run", run_dir, "--gate", "dep:a", "--round", "1", "--file", str(v)])
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "CONSENSUS"
    assert "Approved plan" not in (r.stdout + r.stderr)


def test_verdict_plan_consensus_without_dag_does_not_crash_or_echo(tmp_path):
    # verdict is decoupled from the DAG: a plan gate may settle with no DAG loaded. The echo is
    # best-effort — it must skip silently (no crash, no masking placeholder) when absent.
    run_dir = _init(tmp_path)                       # deliberately NO load-dag
    v = Path(tmp_path) / "v.json"
    v.write_text(json.dumps({"gate": "plan", "round": 1, "verdict": "APPROVE",
                             "summary": "ok", "findings": []}))
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "1", "--file", str(v)])
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "CONSENSUS"
    assert "Approved plan" not in (r.stdout + r.stderr)
    assert "no dependency tree" not in (r.stdout + r.stderr).lower()  # no masking placeholder


# --- load-dag echoes the dependency tree on the terminal ---------------------

def test_load_dag_echoes_tree_in_build_order(tmp_path):
    run_dir = _init(tmp_path)
    # nodes listed OUT of build order: b before a, with b depends-on a
    dag = {"nodes": [{"id": "b", "title": "Second"}, {"id": "a", "title": "First"}],
           "edges": [{"from": "b", "depends_on": "a"}]}
    f = Path(tmp_path) / "d.json"; f.write_text(json.dumps(dag))
    r = _run(["load-dag", "--run", run_dir, "--file", str(f)])
    assert r.returncode == 0, r.stderr
    assert "loaded 2 nodes" in r.stdout
    assert "Dependency tree" in r.stdout
    # 'a' (a dependency of 'b') must be printed before 'b' — true build order, not input order
    assert r.stdout.index("a: First") < r.stdout.index("b: Second")
    assert "b: Second  [deps: a]" in r.stdout
    assert "a: First  [deps: —]" in r.stdout


def test_load_dag_echoes_tree_with_non_ascii_title_under_ascii_locale(tmp_path):
    _require_ascii_locale()
    run_dir = _init(tmp_path)
    dagf = Path(tmp_path) / "dag.json"
    dagf.write_text(json.dumps({"nodes": [{"id": "a", "title": "Café ✅", "description": "",
                    "files": [], "test_plan": "", "status": "pending"}], "edges": []}),
                    encoding="utf-8")
    r = _run_ascii(["load-dag", "--run", run_dir, "--file", str(dagf)])
    assert r.returncode == 0, r.stderr           # encoding-safe: no crash
    assert "Dependency tree" in r.stdout          # tree is echoed


# --- clean: delete a finished run's directory --------------------------------

def test_clean_removes_run_dir(tmp_path):
    run_dir = _init(tmp_path)
    assert Path(run_dir).is_dir()
    r = _run(["clean", "--run", run_dir])
    assert r.returncode == 0, r.stderr
    assert not Path(run_dir).exists()


def test_clean_refuses_non_run_dir(tmp_path):
    bogus = Path(tmp_path) / "not_a_run"; bogus.mkdir()
    (bogus / "keep.txt").write_text("important")
    r = _run(["clean", "--run", str(bogus)])
    assert r.returncode != 0
    assert "crucible:" in r.stderr
    assert bogus.exists()  # refused to delete a dir with no runlog.jsonl


def test_clean_missing_dir_is_clean_error(tmp_path):
    r = _run(["clean", "--run", str(Path(tmp_path) / "nope")])
    assert r.returncode != 0
    assert "Traceback" not in r.stderr


def test_clean_refuses_in_progress_run(tmp_path):
    run_dir = _init(tmp_path)
    df = Path(tmp_path) / "d.json"; df.write_text(json.dumps(_two_node_dag()))
    _run(["load-dag", "--run", run_dir, "--file", str(df)])  # node 'a' stays pending
    r = _run(["clean", "--run", run_dir])
    assert r.returncode != 0
    assert "progress" in r.stderr.lower() or "force" in r.stderr.lower()
    assert Path(run_dir).exists()


def test_clean_force_removes_in_progress_run(tmp_path):
    run_dir = _init(tmp_path)
    df = Path(tmp_path) / "d.json"; df.write_text(json.dumps(_two_node_dag()))
    _run(["load-dag", "--run", run_dir, "--file", str(df)])
    r = _run(["clean", "--run", run_dir, "--force"])
    assert r.returncode == 0, r.stderr
    assert not Path(run_dir).exists()


def test_clean_allows_finished_run(tmp_path):
    run_dir = _init(tmp_path)
    df = Path(tmp_path) / "d.json"; df.write_text(json.dumps(_two_node_dag()))
    _run(["load-dag", "--run", run_dir, "--file", str(df)])
    _run(["set-status", "--run", run_dir, "--node", "a", "--status", "done", "--force"])
    r = _run(["clean", "--run", run_dir])
    assert r.returncode == 0, r.stderr
    assert not Path(run_dir).exists()


def test_verdict_rejects_round_below_one(tmp_path):
    run_dir = _init(tmp_path)
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({"gate": "plan", "round": 0, "verdict": "APPROVE",
                                 "summary": "", "findings": []}))
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "0", "--file", str(vfile)])
    assert r.returncode != 0
    assert "must be >= 1" in r.stderr


def test_init_run_bad_scalar_config_is_clean(tmp_path):
    # N3: a wrong-typed scalar config field is a clean 'crucible:' error, not a raw TypeError.
    cfg = Path(tmp_path) / "c.json"
    cfg.write_text(json.dumps({"max_rounds_plan": []}))
    r = _run(["init-run", "--goal", "g", "--base-dir", str(tmp_path), "--config", str(cfg)])
    assert r.returncode != 0
    assert "Traceback" not in r.stderr
    assert "crucible:" in r.stderr and "max_rounds_plan must be an integer" in r.stderr


def test_verdict_rejects_max_rounds_below_one(tmp_path):
    run_dir = _init(tmp_path)
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({"gate": "plan", "round": 1, "verdict": "APPROVE",
                                 "summary": "", "findings": []}))
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "1",
              "--max-rounds", "0", "--file", str(vfile)])
    assert r.returncode != 0
    assert "--max-rounds must be >= 1" in r.stderr


def test_report_html_cli_writes_file(tmp_path):
    run_dir = _init(tmp_path)
    _load(run_dir, tmp_path, {"a": "done"})
    r = _run(["report", "--run", run_dir, "--html"])
    assert r.returncode == 0, r.stderr
    assert "<!doctype html>" in r.stdout
    assert (Path(run_dir) / "report.html").exists()


def test_verdict_rejects_resolution_for_unknown_finding_id(tmp_path):
    # O1: a resolution id that is not a finding (e.g. a typo) must error, not be silently ignored.
    run_dir = _init(tmp_path)
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({
        "gate": "plan", "round": 1, "verdict": "REQUEST_CHANGES", "summary": "x",
        "findings": [{"id": "F1", "severity": "major", "location": "x", "claim": "c", "suggestion": "s"}]}))
    res = Path(tmp_path) / "res.json"
    res.write_text(json.dumps({"F2": "wontfix"}))  # F2 is not a finding
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "1",
              "--file", str(vfile), "--resolutions", str(res)])
    assert r.returncode != 0
    assert "crucible:" in r.stderr and "unknown finding id" in r.stderr and "F2" in r.stderr
    # nothing was logged for this rejected verdict
    kinds = [e["event"] for e in _events(run_dir)]
    assert "builder_resolution" not in kinds and "critic_verdict" not in kinds


def test_verdict_rejects_unknown_finding_id_dict_form(tmp_path):
    run_dir = _init(tmp_path)
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({
        "gate": "plan", "round": 1, "verdict": "REQUEST_CHANGES", "summary": "x",
        "findings": [{"id": "F1", "severity": "major", "location": "x", "claim": "c", "suggestion": "s"}]}))
    res = Path(tmp_path) / "res.json"
    res.write_text(json.dumps({"F2": {"resolution": "wontfix", "rationale": "r"}}))
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "1",
              "--file", str(vfile), "--resolutions", str(res)])
    assert r.returncode != 0
    assert "unknown finding id" in r.stderr


def test_log_requires_gate(tmp_path):
    # O2: a gateless log entry would be dropped from the report -> reject it at the CLI.
    run_dir = _init(tmp_path)
    f = Path(tmp_path) / "o.txt"
    f.write_text("x")
    r = _run(["log", "--run", run_dir, "--event", "builder_output", "--round", "1", "--file", str(f)])
    assert r.returncode != 0
    assert "gate" in r.stderr.lower()


def test_log_requires_round(tmp_path):
    run_dir = _init(tmp_path)
    f = Path(tmp_path) / "o.txt"
    f.write_text("x")
    r = _run(["log", "--run", run_dir, "--event", "builder_output", "--gate", "plan", "--file", str(f)])
    assert r.returncode != 0
    assert "round" in r.stderr.lower()

# --- G2: load-dag imports a fresh (all-pending) plan -------------------------

def test_load_dag_rejects_non_pending_initial_status(tmp_path):
    # G2: a freshly imported plan must have every node `pending`. A node pre-marked
    # `done` (or any non-pending status) would let `next` schedule its dependents and
    # silently skip its work. Reject it at import.
    run_dir = _init(tmp_path)
    dagf = Path(tmp_path) / "dag.json"
    dagf.write_text(json.dumps({"nodes": [
        {"id": "a", "title": "A", "description": "", "files": [], "test_plan": "", "status": "done"},
        {"id": "b", "title": "B", "description": "", "files": [], "test_plan": "", "status": "pending"}],
        "edges": [{"from": "b", "depends_on": "a"}]}))
    r = _run(["load-dag", "--run", run_dir, "--file", str(dagf)])
    assert r.returncode != 0
    assert "pending" in r.stderr.lower() and "a" in r.stderr
    # nothing was saved/logged for the rejected import
    assert "dag_loaded" not in [e["event"] for e in _events(run_dir)]


def test_load_dag_accepts_all_pending(tmp_path):
    # The normal case still works: an all-pending fresh plan imports fine.
    run_dir = _init(tmp_path)
    r = _load(run_dir, tmp_path, {"a": "pending", "b": "pending"},
              edges=[{"from": "b", "depends_on": "a"}])
    assert r.returncode == 0, r.stderr


# --- G3: gate names must be plan | final | dep:<id> --------------------------

def test_verdict_rejects_invalid_gate_name(tmp_path):
    # G3: a typo'd gate (e.g. "finale") must be rejected, not silently logged under a
    # bogus gate section using the dependency round cap.
    run_dir = _init(tmp_path)
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({"gate": "finale", "round": 1, "verdict": "APPROVE",
                                 "summary": "", "findings": []}))
    r = _run(["verdict", "--run", run_dir, "--gate", "finale", "--round", "1", "--file", str(vfile)])
    assert r.returncode != 0
    assert "gate" in r.stderr.lower()
    assert "CONSENSUS" not in r.stdout
    assert "critic_verdict" not in [e["event"] for e in _events(run_dir)]


def test_verdict_accepts_reproduce_gate(tmp_path):
    run_dir = _init(tmp_path)
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({"gate": "reproduce", "round": 1, "verdict": "APPROVE",
                                 "summary": "bug reproduced", "findings": []}))
    r = _run(["verdict", "--run", run_dir, "--gate", "reproduce", "--round", "1", "--file", str(vfile)])
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "CONSENSUS"


def test_reproduce_gate_halts_even_with_proceed_with_flags(tmp_path):
    # An unconfirmed reproduction must HALT (CAPPED), never PROCEED_WITH_FLAGS, regardless of on_cap.
    cfg = Path(tmp_path) / "c.json"
    cfg.write_text(json.dumps({"on_cap": "proceed_with_flags", "max_rounds_plan": 1}))
    run_dir = _run(["init-run", "--goal", "g", "--base-dir", str(tmp_path), "--config", str(cfg)]).stdout.strip()
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({"gate": "reproduce", "round": 1, "verdict": "REQUEST_CHANGES", "summary": "no repro",
                                 "findings": [{"id": "F1", "severity": "blocker", "location": "x", "claim": "c", "suggestion": "s"}]}))
    r = _run(["verdict", "--run", run_dir, "--gate", "reproduce", "--round", "1", "--file", str(vfile)])
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "CAPPED"
    assert not any(e["event"] == "gate_proceeded_with_flags" for e in _events(run_dir))


def test_verdict_accepts_final_gate(tmp_path):
    # `final` is a valid gate (round cap = max_rounds_dep).
    run_dir = _init(tmp_path)
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({"gate": "final", "round": 1, "verdict": "APPROVE",
                                 "summary": "ok", "findings": []}))
    r = _run(["verdict", "--run", run_dir, "--gate", "final", "--round", "1", "--file", str(vfile)])
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "CONSENSUS"


def test_log_rejects_invalid_gate_name(tmp_path):
    # G3 also applies to `log`: an off-convention gate would create a bogus report section.
    run_dir = _init(tmp_path)
    f = Path(tmp_path) / "o.txt"
    f.write_text("x")
    r = _run(["log", "--run", run_dir, "--event", "builder_output", "--gate", "finale",
              "--round", "1", "--file", str(f)])
    assert r.returncode != 0
    assert "gate" in r.stderr.lower()


# --- G6: a null resolution is malformed --------------------------------------

def test_verdict_rejects_null_resolution(tmp_path):
    # G6: {"F1": null} is a malformed resolution (no fixed|deferred|wontfix). It was
    # previously logged but treated as unresolved; reject it as a shape error instead.
    run_dir = _init(tmp_path)
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({
        "gate": "plan", "round": 1, "verdict": "REQUEST_CHANGES", "summary": "x",
        "findings": [{"id": "F1", "severity": "major", "location": "x", "claim": "c", "suggestion": "s"}]}))
    res = Path(tmp_path) / "res.json"
    res.write_text(json.dumps({"F1": None}))
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "1",
              "--file", str(vfile), "--resolutions", str(res)])
    assert r.returncode != 0
    assert "crucible:" in r.stderr and "F1" in r.stderr
    assert "builder_resolution" not in [e["event"] for e in _events(run_dir)]


# --- G5: a non-object config is a clean error --------------------------------

def test_init_run_list_config_is_clean(tmp_path):
    # G5: a top-level JSON list (not an object) config must be a clean 'crucible:' error,
    # not a raw AttributeError traceback.
    cfg = Path(tmp_path) / "c.json"
    cfg.write_text(json.dumps(["max_rounds_plan", 3]))
    r = _run(["init-run", "--goal", "g", "--base-dir", str(tmp_path), "--config", str(cfg)])
    assert r.returncode != 0
    assert "Traceback" not in r.stderr
    assert "crucible:" in r.stderr and "object" in r.stderr.lower()


# --- C1: dep:<id> gate must reference a real node in the run's DAG --------------

def test_verdict_rejects_ghost_dep_node(tmp_path):
    # C1: dep:<id> for a node that isn't in the DAG is a typo'd/ghost gate; reject it so a
    # verdict isn't recorded (and a terminal outcome rendered) under a non-existent node.
    run_dir = _init(tmp_path)
    _load(run_dir, tmp_path, {"a": "pending"})
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({"gate": "dep:ghost", "round": 1, "verdict": "APPROVE",
                                 "summary": "", "findings": []}))
    r = _run(["verdict", "--run", run_dir, "--gate", "dep:ghost", "--round", "1", "--file", str(vfile)])
    assert r.returncode != 0
    assert "ghost" in r.stderr and ("unknown node" in r.stderr or "unknown" in r.stderr)
    assert "CONSENSUS" not in r.stdout
    assert "critic_verdict" not in [e["event"] for e in _events(run_dir)]


def test_log_rejects_ghost_dep_node(tmp_path):
    run_dir = _init(tmp_path)
    _load(run_dir, tmp_path, {"a": "pending"})
    f = Path(tmp_path) / "o.txt"
    f.write_text("x")
    r = _run(["log", "--run", run_dir, "--event", "builder_output", "--gate", "dep:ghost",
              "--round", "1", "--file", str(f)])
    assert r.returncode != 0
    assert "ghost" in r.stderr


def test_verdict_dep_gate_requires_loaded_dag(tmp_path):
    # A dep:<id> gate before any DAG is loaded cannot be validated -> clean error.
    run_dir = _init(tmp_path)
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({"gate": "dep:a", "round": 1, "verdict": "APPROVE",
                                 "summary": "", "findings": []}))
    r = _run(["verdict", "--run", run_dir, "--gate", "dep:a", "--round", "1", "--file", str(vfile)])
    assert r.returncode != 0
    assert "crucible:" in r.stderr


def test_verdict_valid_dep_node_still_works(tmp_path):
    run_dir = _init(tmp_path)
    _load(run_dir, tmp_path, {"a": "pending"})
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({"gate": "dep:a", "round": 1, "verdict": "APPROVE",
                                 "summary": "ok", "findings": []}))
    r = _run(["verdict", "--run", run_dir, "--gate", "dep:a", "--round", "1", "--file", str(vfile)])
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "CONSENSUS"


# --- C2: set-status enforces dependency completion for work statuses -----------

def test_set_status_done_requires_deps_done(tmp_path):
    # C2: a node cannot be marked done while a dependency is unfinished (it would let `next`
    # schedule dependents and skip the dependency's work).
    run_dir = _init(tmp_path)
    _load(run_dir, tmp_path, {"a": "pending", "b": "pending"},
          edges=[{"from": "b", "depends_on": "a"}])
    r = _run(["set-status", "--run", run_dir, "--node", "b", "--status", "done"])
    assert r.returncode != 0
    assert "a" in r.stderr and ("dependenc" in r.stderr.lower())


def test_set_status_in_progress_requires_deps_done(tmp_path):
    run_dir = _init(tmp_path)
    _load(run_dir, tmp_path, {"a": "pending", "b": "pending"},
          edges=[{"from": "b", "depends_on": "a"}])
    r = _run(["set-status", "--run", run_dir, "--node", "b", "--status", "in_progress"])
    assert r.returncode != 0
    assert "dependenc" in r.stderr.lower()


def test_set_status_done_allowed_when_deps_done(tmp_path):
    run_dir = _init(tmp_path)
    _load(run_dir, tmp_path, {"a": "pending", "b": "pending"},
          edges=[{"from": "b", "depends_on": "a"}])
    assert _run(["set-status", "--run", run_dir, "--node", "a", "--status", "done", "--force"]).returncode == 0
    r = _run(["set-status", "--run", run_dir, "--node", "b", "--status", "done", "--force"])
    assert r.returncode == 0, r.stderr


def test_set_status_blocked_not_gated_by_deps(tmp_path):
    # `blocked`/`pending` are not work statuses; they can be set regardless of deps.
    run_dir = _init(tmp_path)
    _load(run_dir, tmp_path, {"a": "pending", "b": "pending"},
          edges=[{"from": "b", "depends_on": "a"}])
    r = _run(["set-status", "--run", run_dir, "--node", "b", "--status", "blocked"])
    assert r.returncode == 0, r.stderr


# --- H2: set-status done requires the node's own dep gate to have been accepted ----

def test_set_status_done_refused_without_accepted_gate(tmp_path):
    # A node cannot be marked done until its OWN dep:<node> gate reached consensus/proceed.
    run_dir = _init(tmp_path)
    _load(run_dir, tmp_path, {"a": "pending"})           # no deps, no gate yet
    r = _run(["set-status", "--run", run_dir, "--node", "a", "--status", "done"])
    assert r.returncode != 0
    assert "dep:a" in r.stderr and "consensus" in r.stderr.lower()


def test_set_status_done_allowed_after_gate_consensus(tmp_path):
    run_dir = _init(tmp_path)
    _load(run_dir, tmp_path, {"a": "pending"})
    v = Path(tmp_path) / "v.json"
    v.write_text(json.dumps({"gate": "dep:a", "round": 1, "verdict": "APPROVE",
                             "summary": "ok", "findings": []}))
    assert _run(["verdict", "--run", run_dir, "--gate", "dep:a", "--round", "1",
                 "--file", str(v)]).stdout.strip() == "CONSENSUS"
    assert _run(["set-status", "--run", run_dir, "--node", "a", "--status", "done"]).returncode == 0


def test_set_status_done_allowed_after_proceed_with_flags(tmp_path):
    cfg = Path(tmp_path) / "c.json"; cfg.write_text(json.dumps({"on_cap": "proceed_with_flags"}))
    r0 = _run(["init-run", "--goal", "g", "--base-dir", str(tmp_path), "--config", str(cfg)])
    run_dir = r0.stdout.strip()
    _load(run_dir, tmp_path, {"a": "pending"})
    v = Path(tmp_path) / "v.json"
    v.write_text(json.dumps({"gate": "dep:a", "round": 1, "verdict": "REQUEST_CHANGES",
                             "summary": "x", "findings": [{"id": "F1", "severity": "blocker",
                             "location": "x", "claim": "c", "suggestion": "s"}]}))
    assert "PROCEED_WITH_FLAGS" in _run(["verdict", "--run", run_dir, "--gate", "dep:a",
        "--round", "1", "--max-rounds", "1", "--file", str(v)]).stdout
    assert _run(["set-status", "--run", run_dir, "--node", "a", "--status", "done"]).returncode == 0


def test_set_status_done_refused_after_gate_capped(tmp_path):
    run_dir = _init(tmp_path)
    _load(run_dir, tmp_path, {"a": "pending"})
    v = Path(tmp_path) / "v.json"
    v.write_text(json.dumps({"gate": "dep:a", "round": 1, "verdict": "REQUEST_CHANGES",
                             "summary": "x", "findings": [{"id": "F1", "severity": "blocker",
                             "location": "x", "claim": "c", "suggestion": "s"}]}))
    assert "CAPPED" in _run(["verdict", "--run", run_dir, "--gate", "dep:a", "--round", "1",
                             "--max-rounds", "1", "--file", str(v)]).stdout
    r = _run(["set-status", "--run", run_dir, "--node", "a", "--status", "done"])
    assert r.returncode != 0 and "consensus" in r.stderr.lower()


def test_set_status_force_overrides_and_is_recorded(tmp_path):
    run_dir = _init(tmp_path)
    _load(run_dir, tmp_path, {"a": "pending"})
    r = _run(["set-status", "--run", run_dir, "--node", "a", "--status", "done", "--force"])
    assert r.returncode == 0, r.stderr
    forced = [e for e in _events(run_dir)
              if e["event"] == "node_status_change" and e.get("forced")]
    assert forced and forced[-1]["node"] == "a"


def test_set_status_done_refused_when_last_terminal_is_capped(tmp_path):
    # Last-terminal semantics: an earlier gate_consensus followed by a later gate_capped for
    # dep:a must be treated as capped, so a legacy/hand-edited runlog can't sneak a node to done.
    run_dir = _init(tmp_path)
    _load(run_dir, tmp_path, {"a": "pending"})
    log = Path(run_dir) / "runlog.jsonl"
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": "gate_consensus", "gate": "dep:a", "round": 1}) + "\n")
        fh.write(json.dumps({"event": "gate_capped", "gate": "dep:a", "round": 2}) + "\n")
    r = _run(["set-status", "--run", run_dir, "--node", "a", "--status", "done"])
    assert r.returncode != 0 and "consensus" in r.stderr.lower()


def test_set_status_force_does_not_bypass_dependency_check(tmp_path):
    # --force overrides ONLY the node-gate requirement, never the C2 dependency check.
    run_dir = _init(tmp_path)
    _load(run_dir, tmp_path, {"a": "pending", "b": "pending"},
          edges=[{"from": "b", "depends_on": "a"}])
    r = _run(["set-status", "--run", run_dir, "--node", "b", "--status", "done", "--force"])
    assert r.returncode != 0
    assert "dependenc" in r.stderr.lower()


# --- C3: a concluded gate cannot be re-decided --------------------------------

def test_verdict_rejects_already_concluded_gate(tmp_path):
    # C3: once a gate logs a terminal outcome, a second verdict must not silently rewrite it.
    run_dir = _init(tmp_path)
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({"gate": "plan", "round": 1, "verdict": "APPROVE",
                                 "summary": "first", "findings": []}))
    assert _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "1",
                 "--file", str(vfile)]).stdout.strip() == "CONSENSUS"
    v2 = Path(tmp_path) / "v2.json"
    v2.write_text(json.dumps({"gate": "plan", "round": 2, "verdict": "APPROVE",
                              "summary": "second", "findings": []}))
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "2", "--file", str(v2)])
    assert r.returncode != 0
    assert "concluded" in r.stderr.lower() or "terminal" in r.stderr.lower()
    # only the original terminal event remains
    assert [e["event"] for e in _events(run_dir)].count("gate_consensus") == 1


# --- O5-B: deferring a blocking finding is rejected ---------------------------

def test_verdict_rejects_defer_of_blocking_finding(tmp_path):
    # O5-B: `deferred` is only valid for a deferrable severity; deferring a blocker is a
    # misuse that would otherwise be logged as a no-op resolution.
    run_dir = _init(tmp_path)
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({
        "gate": "plan", "round": 1, "verdict": "REQUEST_CHANGES", "summary": "x",
        "findings": [{"id": "F1", "severity": "major", "location": "x", "claim": "c", "suggestion": "s"}]}))
    res = Path(tmp_path) / "res.json"
    res.write_text(json.dumps({"F1": "deferred"}))
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "1",
              "--file", str(vfile), "--resolutions", str(res)])
    assert r.returncode != 0
    assert "defer" in r.stderr.lower() and "F1" in r.stderr
    assert "builder_resolution" not in [e["event"] for e in _events(run_dir)]


def test_verdict_defer_of_minor_finding_is_allowed(tmp_path):
    # The legitimate case: a minor (deferrable) finding can be deferred and clears the gate.
    run_dir = _init(tmp_path)
    vfile = Path(tmp_path) / "v.json"
    vfile.write_text(json.dumps({
        "gate": "plan", "round": 1, "verdict": "APPROVE", "summary": "x",
        "findings": [{"id": "F1", "severity": "minor", "location": "x", "claim": "c", "suggestion": "s"}]}))
    res = Path(tmp_path) / "res.json"
    res.write_text(json.dumps({"F1": "deferred"}))
    r = _run(["verdict", "--run", run_dir, "--gate", "plan", "--round", "1",
              "--file", str(vfile), "--resolutions", str(res)])
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "CONSENSUS"
