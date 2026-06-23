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
    with pytest.raises(KeyError):
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
