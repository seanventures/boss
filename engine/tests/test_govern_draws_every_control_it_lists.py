"""Every control Govern lists is drawn on the board, or names the screen it is on (v1.0-rc10.32).

The release stranger test looked for "Agents fix failures with your authority" — the release
notes and a needs-person card both pointed at it — and it was nowhere in the console:
govern_view listed it (ENFORCED, SWITCHES) but build() never put it on the board; the same was
true of `step_recovery` and `open_pr_when_checks_fail`. The class: a key listed in govern_view
is in BOARD (drawn by build(), and by the console from the `sections` it returns) or in
ELSEWHERE with the place it lives; and every text naming the switch's place names the real one.
The DB-backed half (build() returns a control for every BOARD key, on by default) is in
apps/api/tests/test_govern_view_tells_the_truth.py.
"""
from __future__ import annotations

from boss_engine.services import failure_guidance, govern_view
from boss_engine.services import project_settings as ps

BOARD_KEYS = [k for _s, _n, keys in govern_view.BOARD for k in keys]


def test_every_listed_key_is_on_the_board_or_says_where_it_is():
    listed = set(govern_view.ENFORCED) | set(govern_view.SWITCHES)
    missing = sorted(k for k in listed if k not in BOARD_KEYS and k not in govern_view.ELSEWHERE)
    assert not missing, f"listed in govern_view but drawn nowhere: {missing}"
    assert len(BOARD_KEYS) == len(set(BOARD_KEYS)), "a row is on the board twice"
    assert not set(BOARD_KEYS) & set(govern_view.ELSEWHERE)


def test_every_board_row_can_be_built_and_explained():
    budget_rows = {"daily_cap", "monthly_cap", "burst", "pace", "max_concurrent_jobs",
                   "max_workers_total"}
    for key in BOARD_KEYS:
        assert key in govern_view.ENFORCED, key
        assert key in ps.KNOBS or key in budget_rows, f"{key}: build() has no way to draw it"
    for key in govern_view.SWITCHES:
        assert ps.KNOBS[key]["kind"] == "int" and (ps.KNOBS[key]["min"], ps.KNOBS[key]["max"]) == (0, 1)
    for _s, _n, keys in govern_view.BOARD[2:]:
        for key in keys:
            assert govern_view.EXPLAIN.get(key), f"{key} has no visible one-line explanation"


def test_the_agent_switches_sit_under_each_failed_step_and_say_their_limits():
    sections = {s: keys for s, _n, keys in govern_view.BOARD}
    assert {"agents_fix_failures", "step_recovery"} <= set(sections["each_failed_step"])
    assert "open_pr_when_checks_fail" in sections["each_build"]
    said = govern_view.EXPLAIN["agents_fix_failures"]
    for word in ("starter's authority", "on by default", "Never merges", "production",
                 "approves", "secrets", "per project"):
        assert word in said, word


def test_every_text_naming_the_switch_names_its_real_place():
    place = govern_view.FIX_FAILURES_PLACE
    assert place == "Govern › More › Advanced limits › Each failed step"
    card = failure_guidance.build(cause_code="grant", cls="person", title="Build",
                                  cause="the grant ended", action="", reason="no authority")
    assert any(place in step for step in card["steps"]), card["steps"]
    assert not any("Open Settings" in step for step in card["steps"])
    assert failure_guidance.problems(card) == []
    from boss_engine.identity import fix_authority

    src = open(fix_authority.__file__, encoding="utf-8").read()
    assert "Settings › agents_fix_failures" not in src
    assert place in src
