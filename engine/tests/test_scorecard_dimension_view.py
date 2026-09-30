from boss_engine.services.benchmark_apps import scorecard_dimension_view


def test_scorecard_dimension_view_never_invents_null_score():
    card = {
        "dimensions": [
            {"id": "ui", "status": "scored", "score": 75, "evidence": ["states"]},
            {"id": "ux", "status": "not_scored", "score": None, "reason": "no jtbd"},
            {"id": "capability", "status": "scored", "score": None},
        ]
    }
    ui = scorecard_dimension_view(card, "ui")
    assert ui["status"] == "scored" and ui["score"] == 75
    ux = scorecard_dimension_view(card, "ux")
    assert ux["status"] == "not_scored" and ux["score"] is None
    cap = scorecard_dimension_view(card, "capability")
    assert cap["status"] == "not_scored" and cap["score"] is None
    missing = scorecard_dimension_view(card, "speed")
    assert missing["status"] == "not_scored" and missing["score"] is None
