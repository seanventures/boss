from boss_engine.services.org_assignments import apply_to


def test_planned_structure_preserves_observed_tree_without_conflating_edges():
    observed = {
        "nodes": [
            {"id": "pm", "name": "Product Manager", "role": "pm"},
            {"id": "cto", "name": "CTO", "role": "cto"},
        ],
        "tree": [
            {"id": "pm", "name": "Product Manager", "depth": 0},
            {"id": "cto", "name": "CTO", "depth": 1},
        ],
        "edges": [{"source": "pm", "target": "cto", "times": 3}],
    }
    chart = apply_to(observed, {"pm": {"reports_to": "cto"}, "cto": {"reports_to": None}})

    assert [item["id"] for item in chart["observed_tree"]] == ["pm", "cto"]
    assert [item["id"] for item in chart["tree"]] == ["cto", "pm"]
    assert chart["edges"] == observed["edges"]
    assert chart["declared"]["pm"]["reports_to"] == "cto"
    assert observed["tree"][0]["id"] == "pm"


def test_no_declared_structure_keeps_observed_view_available():
    observed = {"nodes": [], "tree": [{"id": "qa", "depth": 0}], "edges": []}
    chart = apply_to(observed, {})
    assert chart["has_declared"] is False
    assert chart["observed_tree"] == observed["tree"]
    assert chart["declared"] == {}
