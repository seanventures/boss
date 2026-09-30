"""VAG #108 — bounded generator↔verifier debate: the contested-case trigger, the debate
orchestration (rounds, convergence, resolution), and the panel glue. Pure — debaters are injected,
no models."""
from apps.api.app.services import verifier_debate as vd


# --- contested trigger -------------------------------------------------------
def test_is_contested_band():
    assert vd.is_contested(0.5) is True
    assert vd.is_contested(0.9) is False          # clear survive — trust the panel
    assert vd.is_contested(0.1) is False          # clear refute
    assert vd.is_contested(None) is False
    assert vd.is_contested(0.5, low=0.45, high=0.55) is True
    assert vd.is_contested(0.7, low=0.45, high=0.55) is False


# --- debate resolution -------------------------------------------------------
def _say(argument, **flags):
    def _fn(claim, transcript):
        return {"argument": argument, **flags}
    return _fn


def test_generator_concession_refutes_early():
    r = vd.run_debate("off-by-one on the boundary", _say("you're right", concede=True),
                      _say("press"), max_rounds=3)
    assert r["verdict"] == "refuted" and r["reason"] == "generator_conceded"
    assert r["rounds_used"] == 1 and r["confidence"] >= 0.9
    assert r["transcript"][0]["role"] == "generator"


def test_verifier_withdrawal_survives_early():
    r = vd.run_debate("claims a leak", _say("here's why it's fine"),
                      _say("fair, my mistake", withdraw=True), max_rounds=3)
    assert r["verdict"] == "survived" and r["reason"] == "verifier_withdrew"
    assert r["rounds_used"] == 1


def test_unresolved_fails_closed_without_judge():
    r = vd.run_debate("disputed", _say("defend"), _say("press"), max_rounds=2)
    assert r["verdict"] == "refuted" and r["reason"] == "unresolved_fail_closed"
    assert r["rounds_used"] == 2 and r["confidence"] < 0.6      # low confidence — a guess
    # both sides spoke twice
    assert sum(1 for t in r["transcript"] if t["role"] == "generator") == 2


def test_unresolved_goes_to_judge_when_provided():
    judge = lambda claim, tr: {"verdict": "survived", "rationale": "defense held"}  # noqa: E731
    r = vd.run_debate("disputed", _say("defend"), _say("press"), judge, max_rounds=1)
    assert r["verdict"] == "survived" and r["reason"] == "judge"
    assert r["transcript"][-1]["role"] == "judge"


def test_zero_rounds_goes_straight_to_resolution():
    r = vd.run_debate("x", _say("d"), _say("c"), max_rounds=0)
    assert r["rounds_used"] == 0 and r["reason"] == "unresolved_fail_closed"


# --- panel glue --------------------------------------------------------------
def test_resolve_contested_skips_debate_when_decisive():
    called = {"n": 0}

    def _debate():
        called["n"] += 1
        return {"verdict": "survived"}

    out = vd.resolve_contested("refuted", 0.95, _debate)        # decisive -> no debate
    assert out["debated"] is False and out["verdict"] == "refuted" and called["n"] == 0


def test_resolve_contested_runs_debate_when_split():
    out = vd.resolve_contested("refuted", 0.5,
                               lambda: vd.run_debate("d", _say("ok", concede=True), _say("p")))
    assert out["debated"] is True and out["verdict"] == "refuted"
    assert out["panel_confidence"] == 0.5
