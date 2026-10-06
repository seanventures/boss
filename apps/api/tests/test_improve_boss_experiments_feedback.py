"""Experiments (guardrails, compare, promote) and the person's feedback (one tap, quiet, local).
docs/product/RUN_SCORECARDS.md."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from apps.api.tests.improve_fixtures import cards, project, reset_state, seed_build, uid


def _experiment(client, **over):
    body = {"project_id": project(), "actor": "ceo", "target_kind": "role",
            "target_key": "software_engineer", "knob": "harness", "arm_a": "claude_code",
            "arm_b": "codex", "split_b": 0.5, "target_units": 20, "arm_cost_cap_usd": 5}
    body.update(over)
    return client.post("/v1/experiments", json=body)


def test_an_arm_never_touches_what_needs_approval(engine):
    from boss_engine.services import experiments

    assert not (experiments.ARM_SPEC_KEYS & experiments.APPROVAL_KEYS)
    assert {k["setting"] for k in experiments.ARM_KNOBS.values()} <= {
        "code_agent_engine", "code_review_inrun_max_rounds"}
    reset_state(engine)
    with engine.begin() as c:
        experiments.create(c, project_id=project(), actor="ceo", name="", target_kind="role",
                           target_key="software_engineer", knob="review_rounds", arm_a=2, arm_b=3)
        spec = {"role_key": "software_engineer", "risk_tier": 3, "profile": "strict",
                "open_pr": True}
        before = dict(spec)
        got = experiments.apply_arm(c, project_id=project(), run_id="crun_x1", spec=spec,
                                    finalize_ctx={})
    assert got and got["arm"] in ("a", "b")
    changed = {k for k in spec if spec.get(k) != before.get(k)}
    assert changed <= experiments.ARM_SPEC_KEYS
    assert all(spec[k] == before[k] for k in experiments.APPROVAL_KEYS if k in before)


def test_assignment_is_stable_and_an_explicit_pin_wins(engine):
    from boss_engine.services import experiments

    reset_state(engine)
    with engine.begin() as c:
        experiments.create(c, project_id=project(), actor="ceo", name="", target_kind="role",
                           target_key="software_engineer", knob="harness", arm_a="claude_code",
                           arm_b="codex")
        a = experiments.apply_arm(c, project_id=project(), run_id="crun_same",
                                  spec={"role_key": "software_engineer"}, finalize_ctx={})
        b = experiments.apply_arm(c, project_id=project(), run_id="crun_same",
                                  spec={"role_key": "software_engineer"}, finalize_ctx={})
        pinned = experiments.apply_arm(
            c, project_id=project(), run_id="crun_pinned",
            spec={"role_key": "software_engineer", "model_config": {"default": "codex:gpt-5"}},
            finalize_ctx={})
        other = experiments.apply_arm(c, project_id=project(), run_id="crun_other",
                                      spec={"role_key": "designer"}, finalize_ctx={})
    assert a == b and pinned is None and other is None


def test_start_code_task_enrolls_the_build_and_the_scorecard_carries_the_arm(engine):
    from boss_engine.services import experiments, scorecards
    from boss_engine.services.code_task_service import start_code_task

    reset_state(engine)

    class Jobs:
        def create_job(self, *a, **k):
            return {"id": "job_fake"}

    with engine.begin() as c:
        exp = experiments.create(c, project_id=project(), actor="ceo", name="", target_kind="role",
                                 target_key="software_engineer", knob="harness",
                                 arm_a="claude_code", arm_b="codex")
        rid = start_code_task(c, project_id=project(), task_id=None, title="t", instructions="i",
                              acceptance_criteria=[], test_commands=[], owner="o", repo="r",
                              base_branch="main", finalize_ctx={"is_followup": True},
                              jobs=Jobs(), role_key="software_engineer")
        spec = c.execute(text("SELECT spec FROM code_task_runs WHERE id = :id"), {"id": rid}).scalar()
        assert spec["experiment"]["id"] == exp["id"]
        assert spec["model_config"]["default"]["engine"] in ("claude_code", "codex")
        assert "risk_tier" not in spec or spec["risk_tier"] == spec.get("risk_tier")
        c.execute(text("UPDATE code_task_runs SET status = 'completed' WHERE id = :id"), {"id": rid})
        card = scorecards.record_scorecard(c, "build", rid)
    assert card["experiment_id"] == exp["id"] and card["experiment_arm"] == spec["experiment"]["arm"]


def _scored(c, eid: str, arm: str, *, cost: float, gates_failed: int = 0, score: float = 0.8):
    rid = uid("crun")
    c.execute(text("INSERT INTO experiment_assignments (experiment_id, unit_kind, unit_id, arm) "
                   "VALUES (:e, 'build', :u, :a)"), {"e": eid, "u": rid, "a": arm})
    c.execute(text("""
        INSERT INTO run_scorecards (unit_kind, unit_id, project_id, terminal_state, cost_usd,
                                    gates_failed, score, score_version, experiment_id,
                                    experiment_arm, ended_at)
        VALUES ('build', :u, :p, 'completed', :c, :g, :s, '1', :e, :a, now())"""),
        {"u": rid, "p": project(), "c": cost, "g": gates_failed, "s": score, "e": eid, "a": arm})


def test_a_capped_arm_takes_no_more_units_and_two_caps_stop_it(engine):
    from boss_engine.services import experiments

    reset_state(engine)
    with engine.begin() as c:
        exp = experiments.create(c, project_id=project(), actor="ceo", name="", target_kind="role",
                                 target_key="software_engineer", knob="harness",
                                 arm_a="claude_code", arm_b="codex", arm_cost_cap_usd=2)
        _scored(c, exp["id"], "b", cost=2.5)
        experiments.after_scorecard(c, exp["id"])
        assert experiments.get(c, project(), exp["id"])["capped_arms"] == ["b"]
        for i in range(6):
            got = experiments.apply_arm(c, project_id=project(), run_id=f"crun_after_{i}",
                                        spec={"role_key": "software_engineer"}, finalize_ctx={})
            assert got["arm"] == "a"
        _scored(c, exp["id"], "a", cost=2.5)
        experiments.after_scorecard(c, exp["id"])
        state = experiments.get(c, project(), exp["id"])
    assert state["status"] == "stopped" and state["stop_reason"] == "both_arms_capped"


def test_an_arm_that_fails_gates_more_stops_the_experiment_early(engine):
    from boss_engine.services import experiments

    reset_state(engine)
    with engine.begin() as c:
        exp = experiments.create(c, project_id=project(), actor="ceo", name="", target_kind="role",
                                 target_key="software_engineer", knob="harness",
                                 arm_a="claude_code", arm_b="codex", arm_cost_cap_usd=100)
        for _ in range(4):
            _scored(c, exp["id"], "a", cost=0.1)
            _scored(c, exp["id"], "b", cost=0.1, gates_failed=2, score=0.2)
        experiments.after_scorecard(c, exp["id"])
        state = experiments.get(c, project(), exp["id"])
    assert state["status"] == "stopped" and state["stop_reason"] == "arm_b_fails_gates"


def test_compare_says_too_close_until_the_interval_clears_zero():
    from boss_engine.services.experiments import compare

    few = {"a": {"n": 3, "mean": 0.5, "sd": 0.1}, "b": {"n": 3, "mean": 0.9, "sd": 0.1}}
    assert compare(few)["verdict"] == "too_close"
    noisy = {"a": {"n": 8, "mean": 0.60, "sd": 0.3}, "b": {"n": 8, "mean": 0.65, "sd": 0.3}}
    assert compare(noisy)["label"] == "Too close to call"
    clear = {"a": {"n": 10, "mean": 0.50, "sd": 0.05}, "b": {"n": 10, "mean": 0.80, "sd": 0.05}}
    got = compare(clear)
    assert got["verdict"] == "b" and got["low"] > 0


def test_promote_is_one_press_and_lands_in_the_change_log(engine, client):
    reset_state(engine)
    r = _experiment(client)
    assert r.status_code == 201, r.text
    eid = r.json()["id"]
    assert _experiment(client).status_code == 422  # one per target and knob
    listed = client.get(f"/v1/experiments?project_id={project()}").json()
    assert listed["items"][0]["compare"]["label"] == "Too close to call"
    p = client.post(f"/v1/experiments/{eid}/promote",
                    json={"project_id": project(), "actor": "ceo", "arm": "b"})
    assert p.status_code == 200, p.text
    assert p.json()["status"] == "promoted" and p.json()["promoted_arm"] == "b"
    with engine.begin() as c:
        value = c.execute(text("SELECT value FROM project_settings WHERE project_id = :p AND "
                               "key = 'code_agent_engine'"), {"p": project()}).scalar()
        logged = c.execute(text("SELECT actor FROM events WHERE event_type = 'settings.changed' "
                                "AND entity_id = 'code_agent_engine'")).scalar()
        c.execute(text("DELETE FROM project_settings WHERE key = 'code_agent_engine'"))
    assert value == "codex" and logged == "ceo"


# ---------------------------------------------------------------------------- feedback


def test_one_tap_rating_changes_on_a_second_tap_and_reaches_the_scorecard(engine, client):
    from boss_engine.services import scorecards

    reset_state(engine)
    with engine.begin() as c:
        rid = seed_build(c)
        scorecards.record_scorecard(c, "build", rid)
    base = {"project_id": project(), "actor": "ceo", "subject_kind": "build", "subject_id": rid}
    assert client.post("/v1/feedback", json={**base, "rating": "yes"}).status_code == 200
    r = client.post("/v1/feedback", json={**base, "rating": "no", "chips": ["too_slow"]})
    assert r.status_code == 200 and r.json()["rating"] == "no"
    with engine.begin() as c:
        assert c.execute(text("SELECT COUNT(*) FROM person_feedback WHERE subject_id = :s"),
                         {"s": rid}).scalar() == 1
        assert cards(c, rid)[0]["rating"] == "no"
    assert client.post("/v1/feedback", json={**base, "rating": "meh"}).status_code == 422
    assert client.post("/v1/feedback", json={**base, "rating": "no",
                                             "chips": ["made_up"]}).status_code == 422


def test_reject_and_steer_chips_are_appended_and_optional(engine, client):
    reset_state(engine)
    base = {"project_id": project(), "actor": "ceo"}
    r = client.post("/v1/feedback", json={**base, "subject_kind": "reject", "subject_id": "dec_1",
                                          "decision_kind": "plan", "chips": ["wrong_scope"]})
    assert r.status_code == 200 and r.json()["chips"] == ["wrong_scope"]
    client.post("/v1/feedback", json={**base, "subject_kind": "steer", "subject_id": "crun_1",
                                      "chips": ["misread_brief"]})
    none = client.post("/v1/feedback", json={**base, "subject_kind": "steer",
                                             "subject_id": "crun_1", "chips": []})
    assert none.json()["id"] is None  # nothing said, nothing kept
    with engine.begin() as c:
        assert c.execute(text("SELECT COUNT(*) FROM person_feedback")).scalar() == 2


def test_edit_amount_is_a_number_and_never_the_text(engine):
    from boss_engine.services import person_feedback

    reset_state(engine)
    with engine.begin() as c:
        ratio = person_feedback.record_edit(c, project_id=project(), actor="ceo", doc_kind="prd",
                                            subject_id="art_1", before="Zqx a b c d",
                                            after="Zqx a b c d e f g h")
        row = c.execute(text("SELECT * FROM person_feedback WHERE subject_kind = 'edit'")
                        ).mappings().one()
    assert 0 < ratio < 1 and float(row["edit_ratio"]) == ratio
    assert "Zqx" not in str(dict(row))


def test_the_asks_stay_quiet(engine, client):
    reset_state(engine)
    with engine.begin() as c:
        c.execute(text("UPDATE projects SET created_at = now() - interval '40 days' WHERE id = :p"),
                  {"p": project()})
    url = f"/v1/feedback/prompts?project_id={project()}&actor=ceo"
    first = client.get(url).json()
    assert first["ask_open"] is True and first["checkin_due"] is True
    for _ in range(3):
        client.post("/v1/feedback/prompts/shown", json={"project_id": project(), "actor": "ceo",
                                                         "prompt": "rate"})
    assert client.get(url).json()["ask_open"] is False  # 3 a day, then a quiet "Rate" link
    client.post("/v1/feedback/prompts/shown", json={"project_id": project(), "actor": "ceo",
                                                     "prompt": "checkin"})
    assert client.get(url).json()["checkin_due"] is True  # still up for the rest of the day
    r = client.post("/v1/feedback/checkin", json={"project_id": project(), "actor": "ceo",
                                                  "dismiss": True})
    assert r.status_code == 200
    assert client.get(url).json()["checkin_due"] is False  # not again for 30 days
    with engine.begin() as c:
        c.execute(text("UPDATE person_prompt_state SET last_dismissed_at = :t, last_shown_at = :t "
                       "WHERE actor = 'ceo' AND prompt = 'checkin'"),
                  {"t": datetime.now(timezone.utc) - timedelta(days=31)})
    assert client.get(url).json()["checkin_due"] is True
    a = client.post("/v1/feedback/checkin", json={"project_id": project(), "actor": "ceo",
                                                  "answer": "very", "line": "faster deploys"})
    assert a.json() == {"answer": "very", "line_kept": True, "line_shared": False}


def test_an_edit_amount_from_the_console_is_a_number_only(engine, client):
    reset_state(engine)
    ok = client.post("/v1/feedback", json={"project_id": project(), "actor": "ceo",
                                           "subject_kind": "edit", "subject_id": "prop_1",
                                           "decision_kind": "plan", "edit_ratio": 0.42})
    assert ok.status_code == 200 and ok.json() == {"edit_ratio": 0.42}
    bad = client.post("/v1/feedback", json={"project_id": project(), "actor": "ceo",
                                            "subject_kind": "edit", "subject_id": "prop_1",
                                            "decision_kind": "novel", "edit_ratio": 0.1})
    assert bad.status_code == 422
