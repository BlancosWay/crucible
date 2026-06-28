import pytest

from crucible.dag import DAG, CycleError, VALID_STATUSES

SAMPLE = {
    "nodes": [
        {"id": "model", "title": "Model", "description": "d", "files": ["a.py"], "test_plan": "pytest", "status": "pending"},
        {"id": "routes", "title": "Routes", "description": "d", "files": ["b.py"], "test_plan": "pytest", "status": "pending"},
        {"id": "ui", "title": "UI", "description": "d", "files": ["c.py"], "test_plan": "pytest", "status": "pending"},
    ],
    "edges": [
        {"from": "routes", "depends_on": "model"},
        {"from": "ui", "depends_on": "routes"},
    ],
}


def test_parse_and_topological_order():
    dag = DAG.from_dict(SAMPLE)
    order = dag.topological_order()
    assert order.index("model") < order.index("routes") < order.index("ui")


def test_ready_nodes_initially_only_roots():
    dag = DAG.from_dict(SAMPLE)
    assert dag.ready_nodes() == ["model"]


def test_ready_nodes_advance_as_deps_complete():
    dag = DAG.from_dict(SAMPLE)
    dag.set_status("model", "done")
    assert dag.ready_nodes() == ["routes"]
    dag.set_status("routes", "done")
    assert dag.ready_nodes() == ["ui"]
    dag.set_status("ui", "done")
    assert dag.ready_nodes() == []


def test_cycle_detection_raises():
    data = {
        "nodes": [
            {"id": "a", "title": "A", "description": "", "files": [], "test_plan": "", "status": "pending"},
            {"id": "b", "title": "B", "description": "", "files": [], "test_plan": "", "status": "pending"},
        ],
        "edges": [{"from": "a", "depends_on": "b"}, {"from": "b", "depends_on": "a"}],
    }
    with pytest.raises(CycleError):
        DAG.from_dict(data)


def test_edge_referencing_unknown_node_raises():
    data = {
        "nodes": [{"id": "a", "title": "A", "description": "", "files": [], "test_plan": "", "status": "pending"}],
        "edges": [{"from": "a", "depends_on": "ghost"}],
    }
    with pytest.raises(ValueError, match="ghost"):
        DAG.from_dict(data)


def test_duplicate_node_id_raises():
    data = {
        "nodes": [
            {"id": "a", "title": "A", "description": "", "files": [], "test_plan": "", "status": "pending"},
            {"id": "a", "title": "A2", "description": "", "files": [], "test_plan": "", "status": "pending"},
        ],
        "edges": [],
    }
    with pytest.raises(ValueError, match="duplicate"):
        DAG.from_dict(data)


def test_set_status_rejects_unknown_status():
    dag = DAG.from_dict(SAMPLE)
    with pytest.raises(ValueError, match="status"):
        dag.set_status("model", "frobnicated")


def test_set_status_rejects_unknown_node():
    dag = DAG.from_dict(SAMPLE)
    with pytest.raises(ValueError, match="unknown node"):
        dag.set_status("nope", "done")


def test_progress_counts():
    dag = DAG.from_dict(SAMPLE)
    dag.set_status("model", "done")
    assert dag.progress() == {"total": 3, "done": 1, "pending": 2, "in_progress": 0, "in_review": 0, "blocked": 0}


def test_to_dict_round_trips_with_status():
    dag = DAG.from_dict(SAMPLE)
    dag.set_status("model", "done")
    again = DAG.from_dict(dag.to_dict())
    assert again.node("model").status == "done"
    assert again.topological_order() == dag.topological_order()


def test_valid_statuses_constant():
    assert set(VALID_STATUSES) == {"pending", "in_progress", "in_review", "done", "blocked"}


def _dag(statuses, edges=None):
    nodes = [{"id": nid, "title": nid, "description": "", "files": [], "test_plan": "", "status": st}
             for nid, st in statuses.items()]
    return DAG.from_dict({"nodes": nodes, "edges": edges or []})


def test_is_complete_true_only_when_all_done():
    assert _dag({"a": "done", "b": "done"}).is_complete() is True
    assert _dag({"a": "done", "b": "pending"}).is_complete() is False
    assert _dag({"a": "blocked"}).is_complete() is False


def test_is_complete_vacuously_true_for_empty():
    assert _dag({}).is_complete() is True


def test_in_flight_lists_active_nodes_in_order():
    d = _dag({"a": "done", "b": "in_progress", "c": "in_review", "d": "pending"})
    assert d.in_flight() == ["b", "c"]


def test_unfinished_lists_non_done_in_order():
    d = _dag({"a": "done", "b": "pending", "c": "blocked"})
    assert d.unfinished() == ["b", "c"]


def test_next_state_ready_returns_first_ready():
    assert _dag({"a": "pending", "b": "pending"}).next_state() == ("ready", "a")


def test_next_state_complete():
    assert _dag({"a": "done"}).next_state() == ("complete", None)


def test_next_state_stuck_on_blocked():
    assert _dag({"a": "blocked"}).next_state() == ("stuck", None)


def test_next_state_stuck_on_pending_depending_on_blocked():
    d = _dag({"a": "blocked", "b": "pending"}, edges=[{"from": "b", "depends_on": "a"}])
    assert d.next_state() == ("stuck", None)


def test_next_state_in_flight_when_only_active_work_remains():
    d = _dag({"a": "in_progress", "b": "pending"}, edges=[{"from": "b", "depends_on": "a"}])
    assert d.next_state() == ("in_flight", None)


def test_next_state_blocked_takes_priority_over_in_flight():
    # A node in flight AND a blocked node => the blocker must surface => stuck (exit 3).
    d = _dag({"a": "in_progress", "c": "blocked"})
    assert d.next_state() == ("stuck", None)


def test_unfinished_detail_reports_status_and_unmet_deps():
    d = _dag({"a": "blocked", "b": "pending"}, edges=[{"from": "b", "depends_on": "a"}])
    detail = d.unfinished_detail()
    by_id = {x["id"]: x for x in detail}
    assert by_id["a"]["status"] == "blocked" and by_id["a"]["waiting_on"] == []
    assert by_id["b"]["status"] == "pending" and by_id["b"]["waiting_on"] == ["a"]


# --- input type validation (M7) ----------------------------------------------

def test_from_dict_rejects_non_dict_top_level():
    with pytest.raises(ValueError, match="must be a JSON object"):
        DAG.from_dict([{"id": "a"}])


def test_from_dict_rejects_non_list_nodes():
    with pytest.raises(ValueError, match='"nodes" must be a list'):
        DAG.from_dict({"nodes": "oops"})


def test_from_dict_rejects_non_dict_node():
    with pytest.raises(ValueError, match="node at index 0"):
        DAG.from_dict({"nodes": ["oops"]})


def test_from_dict_rejects_files_as_string():
    with pytest.raises(ValueError, match="files"):
        DAG.from_dict({"nodes": [{"id": "a", "files": "src/a.py"}]})


def test_from_dict_rejects_non_string_file_element():
    with pytest.raises(ValueError, match="files"):
        DAG.from_dict({"nodes": [{"id": "a", "files": ["ok.py", 3]}]})


def test_from_dict_rejects_non_list_edges():
    with pytest.raises(ValueError, match='"edges" must be a list'):
        DAG.from_dict({"nodes": [{"id": "a"}], "edges": "oops"})


def test_from_dict_rejects_non_dict_edge():
    with pytest.raises(ValueError, match="edge at index 0"):
        DAG.from_dict({"nodes": [{"id": "a"}], "edges": ["oops"]})
