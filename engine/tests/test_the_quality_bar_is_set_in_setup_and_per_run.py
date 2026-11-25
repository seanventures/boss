"""The quality bar, phase 2 (BOSSAPP-1362): Setup shows the project's bar in plain words and each
gate's level (Advanced); New work picks a bar for one run; what did not block is listed on the run
as well as the PR; a production release always waits for a person, whatever the preset.

  * Setup › Quality bar reads `setup_view.quality_bar_view`, built from `quality_bar.describe`:
    the presets' words, every gate with what each preset does with it, the project's own level.
  * Advanced › Each gate writes the project setting `quality_bar_gates`, cleaned by
    `quality_bar.clean_gate_map`: the floor always blocks, an unknown gate is refused.
  * The project's per-gate map is honoured by every build (resolve_for_run) after the run's and
    the workflow's own maps, and never counts as "lowered".
  * A run's bar: the same or stricter is anyone's; a lower one needs settings:update
    (`quality_bar.lowers`, checked at the start door).
"""
from __future__ import annotations

import contextlib
import inspect
from types import SimpleNamespace

import pytest

from boss_engine.services import autonomy_gate, autonomy_policy, project_settings as ps
from boss_engine.services import quality_bar as qb
from boss_engine.services import run_retrospective, setup_view

P, S, R = qb.PROTOTYPE, qb.STANDARD, qb.PRODUCTION


# ── each gate's level: the floor always blocks ─────────────────────────────────────────────────

def test_a_gate_map_keeps_block_and_follow_up_and_drops_default():
    assert qb.clean_gate_map({"code_review_passed": "follow_up", "consistent": "BLOCK",
                              "tests_strong_enough": "default"}) == {
        "code_review_passed": "follow_up", "consistent": "block"}
    assert qb.clean_gate_map('{"brief_tests_written": "block"}') == {"brief_tests_written": "block"}
    assert qb.clean_gate_map(None) == {} and qb.clean_gate_map("") == {}


@pytest.mark.parametrize("bad", [{"security_clean": "follow_up"}, {"app_serves": "follow_up"},
                                 {"no_such_gate": "block"}, {"consistent": "sometimes"},
                                 "not json", ["consistent"]])
def test_a_write_that_relaxes_the_floor_or_names_no_gate_is_refused(bad):
    with pytest.raises(ValueError):
        qb.clean_gate_map(bad)
    with pytest.raises(ValueError):
        ps.coerce(qb.OVERRIDES_KEY, bad)


def test_a_stored_map_is_read_leniently_and_never_relaxes_the_floor():
    assert qb.clean_gate_map({"security_clean": "follow_up", "consistent": "follow_up",
                              "nope": "block"}, strict=False) == {"consistent": "follow_up"}


def test_the_per_gate_map_is_a_project_setting_anyone_can_read_and_owners_write():
    spec = ps.KNOBS[qb.OVERRIDES_KEY]
    assert spec["kind"] == "gate_map" and spec["attr"] == qb.OVERRIDES_KEY
    assert ps.coerce(qb.OVERRIDES_KEY, {"consistent": "follow_up"}) == {"consistent": "follow_up"}
    assert "follow_up" in ps.allowed_values_said(spec)
    from boss_engine.config import Settings

    assert "quality_bar_gates" in Settings.model_fields


# ── the project's own map, under the run's and the workflow's ──────────────────────────────────

def test_the_projects_gate_map_is_honoured_and_is_not_a_lowering():
    found = qb.resolve_detail(project_value=S, project_gates={"code_review_passed": "follow_up"})
    assert found["overrides"] == {"code_review_passed": "follow_up"}
    assert found["lowered_by"] is None
    assert qb.gate_blocks("code_review_passed", found["bar"], found["overrides"]) is False


def test_a_runs_own_map_wins_over_the_projects():
    found = qb.resolve_detail(
        project_value=S, project_gates={"code_review_passed": "follow_up"},
        run_input={qb.OVERRIDES_KEY: {"code_review_passed": "block"}, qb.BY_KEY: "pat"})
    assert found["overrides"]["code_review_passed"] == "block"


def test_every_build_reads_the_projects_gate_map(monkeypatch):
    stored = {qb.SETTING: P, qb.OVERRIDES_KEY: {"tests_strong_enough": "block"}}
    monkeypatch.setattr(ps, "effective", lambda c, p, s, key: stored.get(key))

    class _Conn:
        def begin_nested(self):
            return contextlib.nullcontext()

    bar, over, info = qb.resolve_for_run(_Conn(), SimpleNamespace(), "proj")
    assert bar == P and over == {"tests_strong_enough": "block"}
    assert info["lowered_by"] is None


# ── one run's bar: the same or stricter is anyone's ────────────────────────────────────────────

def test_only_a_lower_bar_or_a_follow_up_gate_needs_the_override_permission():
    assert qb.lowers(P, None, S) is True
    assert qb.lowers(S, None, R) is True
    assert qb.lowers(S, None, S) is False
    assert qb.lowers(R, None, S) is False
    assert qb.lowers(None, {"consistent": "block"}, S) is False
    assert qb.lowers(None, {"consistent": "follow_up"}, R) is True
    assert qb.lowers(R, None, None) is False         # unset project bar = standard


def test_a_stricter_run_bar_is_stamped_but_never_reads_as_lowered():
    found = qb.resolve_detail(project_value=S, run_input={qb.SETTING: R, qb.BY_KEY: "pat"})
    assert found["bar"] == R and found["source"] == "run_input" and found["lowered_by"] is None


# ── Setup › Quality bar: plain words, every gate ───────────────────────────────────────────────

def test_setup_describes_every_preset_in_plain_words():
    view = qb.describe(S)
    assert [p["key"] for p in view["presets"]] == [P, S, R]
    for preset in view["presets"]:
        assert preset["label"] and preset["line"] and preset["blocks"] and preset["follow_ups"]
        assert "_" not in preset["line"]
    assert "production" in view["always"].lower() and "person" in view["always"].lower()
    assert "mid-build" in view["always"]


def test_every_known_gate_has_a_row_and_the_floor_always_blocks():
    view = qb.describe(P, {"code_review_passed": "block"})
    rows = {r["gate"]: r for r in view["gates"]}
    assert set(rows) == set(qb._BLOCKS)  # noqa: SLF001
    for gate in qb.FLOOR & set(rows):
        assert rows[gate]["floor"] and rows[gate]["level"] == "block"
        assert rows[gate]["effective"] == "block"
    assert rows["code_review_passed"]["level"] == "block"
    assert rows["code_review_passed"]["effective"] == "block"            # overridden at prototype
    assert rows["consistent"]["effective"] == "follow_up"                # the prototype preset
    assert rows["code_review_passed"]["presets"] == {P: "follow_up", S: "block", R: "block"}
    assert all(r["label"] and r["label"] != r["gate"] for r in view["gates"])
    # floor rows first, so Advanced shows what never changes before what can
    assert [r["floor"] for r in view["gates"]] == sorted((r["floor"] for r in view["gates"]),
                                                         reverse=True)


def test_setups_view_says_where_the_bar_came_from(monkeypatch):
    own = {qb.SETTING: R, qb.OVERRIDES_KEY: {"consistent": "follow_up"}}
    monkeypatch.setattr(ps, "get_own", lambda c, p: own)
    monkeypatch.setattr(ps, "effective", lambda c, p, s, key: own.get(key))

    class _Conn:
        def begin_nested(self):
            return contextlib.nullcontext()

    view = setup_view.quality_bar_view(_Conn(), SimpleNamespace(), "proj")
    assert view["bar"] == R and view["source"] == "project"
    assert view["key"] == "quality_bar" and view["gates_key"] == "quality_bar_gates"
    assert view["overrides"] == {"consistent": "follow_up"}
    assert view["permission"] == "settings:update"
    own.clear()
    view = setup_view.quality_bar_view(_Conn(), SimpleNamespace(), "proj")
    assert view["bar"] == S and view["source"] == "default"


# ── what did not block is listed on the run, as on the PR ──────────────────────────────────────

_STATE = {
    qb.KEY: {"bar": S, "followups": {"acceptance_met": ["Must-have 2 (P1) missing: export CSV"],
                                     "tests_strong_enough": []},
             "flaky": ["test_sort > is stable"]},
    "deferred_findings": [{"severity": "major", "text": "N+1 query on the list page",
                           "path": "app/list.py", "line": 40},
                          {"severity": "minor", "text": "rename a helper"}],
}


def test_the_run_lists_every_follow_up_the_pr_lists():
    card = qb.run_followups(_STATE)
    texts = " | ".join(i["text"] for i in card["items"])
    for said in ("export CSV", "Tests catch bugs", "test_sort > is stable", "N+1 query",
                 "rename a helper"):
        assert said in texts, said
    pr = qb.pr_section(_STATE)
    for said in ("export CSV", "test_sort > is stable", "N+1 query", "rename a helper"):
        assert said in pr, said
    assert card["bar"] == S and card["count"] == len(card["items"]) == 5
    assert card["items"][0]["tier"] == "P1" and card["items"][-1]["tier"] == "P2"
    assert card["words"].endswith("listed for later")


def test_a_build_with_nothing_let_through_has_no_follow_ups_on_its_run():
    assert qb.run_followups({}) is None
    assert qb.run_followups({qb.KEY: {"bar": R, "followups": {}, "flaky": []}}) is None


def test_the_run_report_carries_the_follow_ups_and_prints_them():
    report = {"ok": True, "workflow": "Minor feature", "status": "succeeded", "total_usd": 0.0,
              "elapsed_minutes": 1.0, "working_minutes": 1.0, "waiting_minutes": 0.0,
              "product": {"build": {"followups": qb.run_followups(_STATE)}}, "steps": []}
    md = run_retrospective.workflow_markdown(report)
    assert "## Follow-ups (quality bar: standard)" in md and "export CSV" in md
    assert "_quality_followups(state)" in inspect.getsource(run_retrospective._product_and_build)  # noqa: SLF001


# ── production always waits for a person, whatever the preset ──────────────────────────────────

def test_a_production_release_asks_a_person_at_every_preset():
    # The autonomy choke point never reads the quality bar: no preset can answer a production card.
    assert "quality_bar" not in inspect.getsource(autonomy_gate)
    with pytest.raises(autonomy_policy.PolicyLooser):
        autonomy_policy.coerce({"prod": "act"})
    acts = autonomy_policy.coerce({"dev": "act"})
    found = autonomy_gate.route(None, {"project_id": "p", "recommendation": {
        "capability": "promote_config", "workflow_run_id": "wfr_1"}}, policy=acts)
    assert found.answer == "ask" and found.cls == autonomy_gate.ASK_PROD_RELEASE


def test_below_production_a_plan_card_is_answered_by_an_agent_not_a_person():
    acts = autonomy_policy.coerce({"dev": "act"})
    found = autonomy_gate.route(None, {"project_id": "p", "recommendation": {
        "action": "resume_code_task"}}, policy=acts)
    assert found.answer == "act"
