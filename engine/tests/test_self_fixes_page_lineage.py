"""ONE FIX, FROM PROBLEM TO RELEASE (BOSSAPP-571 spec §10.3): the lineage BOSS hands the page —
problem → diagnosis → fix → tests → review → approved → installed → resumed → upstream — each
with a state and words, so a refused, rolled-back or sent fix reads true without the page
guessing."""
from __future__ import annotations

from boss_engine.services import self_fix as sf

KEYS = ["problem", "diagnosis", "fix", "tests", "review", "approved", "installed", "resumed",
        "upstream"]


def _states(sfx):
    steps = sf.lineage_steps(sfx)
    assert [s["key"] for s in steps] == KEYS
    return {s["key"]: s["state"] for s in steps}, {s["key"]: s["detail"] for s in steps}


def test_a_verified_fix_not_yet_sent():
    states, words = _states({"status": sf.VERIFIED, "signature": "deploy:port_held:8000",
                             "verification": {"outcome": "new_cause",
                                              "next_signature": "deploy:unwired:res-db"},
                             "red_green": {"tests": ["t.py"]}, "diagnosis": {
                                 "hypothesis": "h", "rejected": [{"why": "w"}]},
                             "upstream_state": "local_only"})
    assert [states[k] for k in KEYS[:8]] == ["done"] * 8 and states["upstream"] == "now"
    assert "new cause deploy:unwired:res-db" in words["resumed"]
    assert "first guess rejected" in words["diagnosis"] and words["upstream"] == "not sent"
    assert "✗→✓" in words["tests"]


def test_a_sent_fix_is_done_to_the_end():
    states, words = _states({"status": sf.VERIFIED, "upstream_state": "sent"})
    assert states["upstream"] == "done" and words["upstream"] == "sent"


def test_a_refused_fix_stops_where_its_refusal_is():
    states, words = _states({"status": sf.REFUSED, "refusal": "no_reproduction",
                             "refusal_detail": "the test passed on the base"})
    assert states["diagnosis"] == "done" and states["fix"] == "failed"
    assert states["installed"] == "skipped"
    assert "could not reproduce" in words["fix"]
    states, _ = _states({"status": sf.REFUSED, "refusal": "protected_path"})
    assert states["diagnosis"] == "failed"


def test_a_rolled_back_fix_fails_at_resumed_and_waiting_is_next():
    states, _ = _states({"status": sf.ROLLED_BACK})
    assert states["installed"] == "done" and states["resumed"] == "failed"
    states, _ = _states({"status": sf.AWAITING_APPROVAL})
    assert states["review"] == "done" and states["approved"] == "now"
    assert states["installed"] == "next"


def test_the_card_is_what_the_console_draws():
    """The approval card's shape (spec §10.2): paths as strings, the rejected first guess, each
    seat's verdict and findings, the build's minutes, and the self-fix it is for."""
    from boss_engine.services import self_fix_install as sfi

    card = sfi.card({
        "id": "sfx_1", "status": sf.AWAITING_APPROVAL, "tier": 3, "flags": ["host_effect"],
        "pr": {"title": "fix(deploy): retire"},
        "diff_stat": {"files": [{"path": "engine/boss_engine/services/stack_deploy.py",
                                 "added": 58, "removed": 1}], "added": 58, "removed": 1},
        "diagnosis": {"hypothesis": "h", "rejected": [{"hypothesis": "g", "why": "w"}]},
        "review": {"seats": {"security_check": {"verdict": "PASS", "ok": True,
                                                "engine": "codex",
                                                "findings": [{"severity": "MINOR",
                                                              "what": "quote it"}]}}},
        "started_at": "2026-10-17T08:00:00+00:00",
        "progress": {"at": "2026-10-17T08:14:00+00:00"}, "cost_micros": 3_120_000})
    assert card["self_fix_id"] == "sfx_1" and card["minutes"] == 14
    assert card["change"]["files"] == ["engine/boss_engine/services/stack_deploy.py"]
    assert card["rejected"] == [{"hypothesis": "g", "why": "w"}]
    assert card["review"]["security_check"] == {"verdict": "PASS", "engine": "codex",
                                                "ok": True, "findings": ["MINOR quote it"]}
    assert card["cost_usd"] == 3.12 and "Two people" in card["people"]
