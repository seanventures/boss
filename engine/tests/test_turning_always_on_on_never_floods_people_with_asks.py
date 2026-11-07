"""Turning the always-on agents on never floods people with asks (2026-11-07, laptop install).

THE CASE. At 08:51 UTC the owner switched Govern › Autonomy › Always-on agents to "Act within
levels" (mode `route`). Between 08:52 and 08:55 the Chief of Staff raised ~20 Needs-you cards,
one per item, across every project — "Hand to QA? — module:paramg/helpdesk-ai-demo:web/src/
pages/TicketDetailPage.jsx", "Nudge? — task:crun_da60…", "Is this roadmap item? —
subject:stage:solo" — and the bell, Approvals and the Projects tile read 22. Every routine
hand-off at A1 in route mode asked a person (only A2+ routed), every item got its own card, the
question carried the raw subject key, the Recommended roadmap's own `learn.*` signals were
triaged a second time, and nothing ever withdrew a card the policy no longer asked.

THE RULE, AS A CLASS (chief_of_staff.disposition / ask_batch / reconsider):
  * a routine hand-off follows the class's level: at A1+ in route mode it is handed straight to
    its owner (board + ledger + Agents at work), nobody is asked;
  * what needs a person is asked on ONE card per project per tick, in plain words;
  * a project whose classes only watch (A0) is never asked anything;
  * every tick re-judges the open cards with the same rule and withdraws what it no longer asks
    (with a note in words), so a mode switch — on, up or off — leaves nothing stale;
  * at most policy.ASKS_PER_PROJECT_HOUR cards an hour; the rest wait for the next card, and no
    new squad is seated while the hour is used up.
No test calls a model.
"""
from __future__ import annotations

import re
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services import incident_squad
from boss_engine.services.ambient import (board, chief_of_staff as cos, collab, ledger, owners,
                                          policy, signals)
from boss_engine.services.ambient import task_classes as tc

from ._ambient_support import eng, open_item, project, signal  # noqa: F401

REPO = "paramg/helpdesk-ai-demo"
#: Raw keys a person must never read on a card.
RAW = re.compile(r"module:|repo:|subject:|task:|run:|crun_|wfr_|host:")


def _settings(**kw):
    base = get_settings()
    return SimpleNamespace(**{**base.__dict__, "ambient_cos_model_calls": 0, **kw})


def _the_morning(c, pid):
    """The helpdesk-ai-demo signals of 08:52, in the shapes the real adapters write."""
    for path in ("web/src/pages/TicketDetailPage.jsx", "server/incidents/service.mjs",
                 "server/auth/routes.mjs"):
        signal(c, pid, source="scan", kind="scan.untested_hotspot",
               title=f"Hot file without tests: {path}", subject_kind="module",
               subject_id=f"{REPO}:{path}", app=None, severity="medium")
    signal(c, pid, source="scan", kind="scan.no_docs", title=f"{REPO} has no README",
           subject_kind="repo", subject_id=REPO, app=None, severity="low")
    signal(c, pid, source="scan", kind="scan.no_ci", title=f"{REPO} has no CI",
           subject_kind="repo", subject_id=REPO, app=None, severity="medium")
    signal(c, pid, source="shepherd", kind="shepherd.escalation",
           title="Finish the visual refresh the last change promised but did not deliver",
           subject_kind="task", subject_id="crun_da607756e6604bf09086", app=None,
           severity="medium")
    # The Recommended roadmap's own findings (roadmap.py records them as learn.* signals).
    signal(c, pid, source="build", kind="learn.slow_stage", title="Speed up the solo stage",
           subject_kind="subject", subject_id="stage:solo", app=None, severity="low")
    signal(c, pid, source="person", kind="learn.user_friction", title="Fewer rejects at decision",
           subject_kind="subject", subject_id="friction:reject:decision", app=None,
           severity="low")


def _open_cards(c, pid):
    return [dict(r) for r in c.execute(text(
        "SELECT id, question, status, recommendation, resolution_note FROM decisions "
        "WHERE project_id = :p AND status = 'pending' ORDER BY created_at"),
        {"p": pid}).mappings()]


def _answer_open_cards(c, pid):
    """A person answered every open card (Dismiss): the next ask raises a new one."""
    c.execute(text("UPDATE decisions SET status = 'rejected' WHERE project_id = :p "
                   "AND status = 'pending'"), {"p": pid})


def _run(eng, pid, mode, settings=None, **kw):
    with eng.begin() as c:
        return _run_in(c, pid, mode, settings, **kw)


def _run_in(c, pid, mode, settings=None, **kw):
    return cos.triage_project(c, _settings(**(settings or {})), pid, mode=mode,
                              max_model_calls=0, **kw)


def test_switching_to_act_within_levels_hands_routine_work_over_and_asks_once(eng, project):
    with eng.begin() as c:
        _the_morning(c, project)
    report = _run(eng, project, "route")
    # Tests ×3, docs, CI: A1 hand-offs proceed. The nudge is the Chief of Staff's own action and
    # needs A2: it is the only thing a person is asked, on one card.
    assert report["counts"] == {"routed": 5, "proposed": 1}, report["counts"]
    with eng.begin() as c:
        [card] = _open_cards(c, project)
        assert card["question"].startswith("Nudge: “Finish the visual refresh")
        assert not RAW.search(card["question"]), card["question"]
        routed = [i for i in board.list_items(c, project, limit=50)
                  if collab.messages(c, i["id"], kinds=("handoff",))]
        owners_of = sorted(collab.messages(c, i["id"], kinds=("handoff",))[0]["body"]["to"]
                           for i in routed)
        assert owners_of == ["agent:docs_writer", "agent:operations_engineer", "agent:qa",
                             "agent:qa", "agent:qa"]
        for item in routed:
            [row] = ledger.list_decisions(c, project, work_item_id=item["id"])
            assert row["chosen"].startswith("route:") and row["action"] == "route"
            assert item["lifecycle"] == "proposed" and item["needs_you_decision_id"] is None
        # The roadmap's own signals are left to the roadmap: no item, no card, still new.
        learn = signals.list_signals(c, project_id=project, status=["new"])
        assert sorted(s["kind"] for s in learn) == ["learn.slow_stage", "learn.user_friction"]
        assert not [i for i in board.list_items(c, project, limit=50)
                    if i["task_class"] == "roadmap_item"]


def test_ask_first_puts_every_hand_off_on_one_card_in_plain_words(eng, project):
    with eng.begin() as c:
        _the_morning(c, project)
    report = _run(eng, project, "propose")
    assert report["counts"] == {"proposed": 6}
    with eng.begin() as c:
        [card] = _open_cards(c, project)
        q = card["question"]
        assert q.startswith("Chief of Staff suggests 6 hand-offs: tests for 3 hot files")
        assert "docs" in q and "a nudge" in q and "CI" in q
        assert not RAW.search(q), q
        entries = card["recommendation"]["batch"]
        assert len(entries) == 6 and card["recommendation"]["asked_by"] == cos.PARTY
        words = sorted(e["words"] for e in entries)
        assert "tests for TicketDetailPage.jsx" in words
        assert "docs for helpdesk-ai-demo" in words and "CI for helpdesk-ai-demo" in words
        assert not any(RAW.search(e["words"]) for e in entries)
        waiting = board.list_items(c, project, limit=50)
        assert {i["lifecycle"] for i in waiting} == {"needs_you"}
        assert {i["needs_you_decision_id"] for i in waiting} == {card["id"]}
    # One answer reaches every item on the card.
    from boss_engine.services.decisions import DecisionService

    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO project_members (id, project_id, member_id, display_name, member_type, "
            "role_keys, permissions) VALUES (:i, :p, 'ceo', 'ceo', 'human', '[\"ceo\"]'::jsonb, "
            "'[\"decision_resolve\"]'::jsonb) ON CONFLICT DO NOTHING"),
            {"i": f"pm_{project}_ceo", "p": project})
    with eng.begin() as c:
        DecisionService(get_settings()).transition(c, card["id"], action="approve", actor="ceo",
                                                note="Go ahead.")
    with eng.begin() as c:
        items = board.list_items(c, project, limit=50)
        handed = {i["id"]: collab.messages(c, i["id"], kinds=("handoff",)) for i in items
                  if i["task_class"] != "stale_handoff"}   # the nudge is the CoS's own
        assert len(handed) == 5 and all(handed.values()), "every hand-off on the card"
        outcomes = {ledger.list_decisions(c, project, work_item_id=i["id"])[0]["outcome"]
                    for i in items}
        assert outcomes == {"accepted"}


def test_a_project_whose_classes_only_watch_is_never_asked(eng, project):
    with eng.begin() as c:
        for key in tc.TASK_CLASSES:
            policy.set_level(c, project, key, actor="person:ceo", level="A0",
                             reason="observe only here")
        _the_morning(c, project)
    for mode in ("route", "propose"):
        report = _run(eng, project, mode)
        assert set(report.get("counts") or {}) <= {"observed", "joined"}
    with eng.begin() as c:
        assert _open_cards(c, project) == []


def test_turning_to_route_withdraws_the_cards_it_no_longer_asks_and_hands_the_work_over(
        eng, project):
    with eng.begin() as c:
        _the_morning(c, project)
    _run(eng, project, "propose")
    with eng.begin() as c:
        [card] = _open_cards(c, project)
    # The owner switches to "Act within levels": the next tick re-judges the card.
    report = _run(eng, project, "route")
    [changed] = report["reconsidered"]
    assert changed["decision_id"] == card["id"] and changed.get("narrowed")
    assert sorted(s["verdict"] for s in changed["settled"]) == ["route"] * 5
    with eng.begin() as c:
        [left] = _open_cards(c, project)
        assert left["id"] == card["id"], "the card is narrowed to what still needs a person"
        assert left["question"].startswith("Nudge: “Finish")
        assert len(left["recommendation"]["batch"]) == 1
        routed = [i for i in board.list_items(c, project, limit=50)
                  if collab.messages(c, i["id"], kinds=("handoff",))]
        assert len(routed) == 5 and all(i["lifecycle"] == "proposed" for i in routed)
    # And turning the always-on agents off withdraws what is left, in words.
    off = _run(eng, project, "off")
    assert [r["withdrawn"] for r in off["reconsidered"]] == [True]
    with eng.begin() as c:
        assert _open_cards(c, project) == []
        status, note = c.execute(text(
            "SELECT status, resolution_note FROM decisions WHERE id = :d"),
            {"d": card["id"]}).one()
        assert status == "withdrawn" and "always-on agents are off" in note
        assert not RAW.search(note)
        # A withdrawn card is not an answer: nothing is carried, nothing scored as ignored.
        assert cos.follow_up(c, project) == []


def _legacy_card(c, pid, *, kind, subject_kind, subject_id, title, owner, task_class):
    """A card exactly as the code before this fix raised it: one item, one card, the raw key."""
    sig = signal(c, pid, source="scan", kind=kind, title=title, subject_kind=subject_kind,
                 subject_id=subject_id, app=None, severity="medium")
    ref = f"{subject_kind}:{subject_id}"
    opened = board.open_item(c, get_settings(), project_id=pid, fingerprint=f"fp:{ref}",
                             title=title, task_class=task_class, subject_ref=ref,
                             signal_ids=[sig["id"]], actor=cos.PARTY)["item"]
    held = board.claim(c, opened["id"], owner=cos.PARTY)
    act = ledger.record_decision(
        c, project_id=pid, decided_by=cos.PARTY, question="Who should own this?",
        chosen=f"propose:{owner}", action="propose",
        options=["observe", f"propose:{owner}", f"route:{owner}"], rationale="Legacy card.",
        reversibility="reversible", undo={"action": "dismiss"}, work_item_id=opened["id"],
        task_class=task_class, evidence=[{"kind": "signal", "ref": sig["id"]}])
    moved = board.needs_you(c, opened["id"], actor=cos.PARTY, token=held["lease_token"],
                            question=f"Hand to {owner.upper()}?",
                            recommendation={"action": "ambient_route", "owner": f"agent:{owner}",
                                            "owner_label": owner.upper(),
                                            "next_action": "open_draft_pr",
                                            "act_id": act["act_id"]})
    c.execute(text("UPDATE decisions SET question = :q WHERE id = :d"),
              {"q": f"Hand to {owner.upper()}? — {ref}", "d": moved["needs_you_decision_id"]})
    board.release(c, opened["id"], owner=cos.PARTY, token=held["lease_token"])
    return moved["needs_you_decision_id"], opened["id"]


def test_the_cards_raised_before_the_fix_are_withdrawn_on_the_next_tick(eng, project):
    """The ~20 rows on the laptop: one legacy card per item, raw keys, some from the roadmap's
    own signals. The first tick after the install withdraws each one the policy no longer asks
    and its work proceeds; the DB is never touched by hand."""
    with eng.begin() as c:
        tests, tests_item = _legacy_card(
            c, project, kind="scan.untested_hotspot", subject_kind="module",
            subject_id=f"{REPO}:server/auth/routes.mjs", title="Hot file without tests",
            owner="qa", task_class="missing_tests")
        road, road_item = _legacy_card(
            c, project, kind="learn.slow_stage", subject_kind="subject",
            subject_id="stage:solo", title="Speed up the solo stage", owner="pm",
            task_class="roadmap_item")
    report = _run(eng, project, "route")
    assert sorted(r["decision_id"] for r in report["reconsidered"] if r.get("withdrawn")) == \
        sorted([tests, road])
    with eng.begin() as c:
        assert _open_cards(c, project) == []
        notes = dict(c.execute(text(
            "SELECT id, resolution_note FROM decisions WHERE id = ANY(:ids)"),
            {"ids": [tests, road]}).all())
        assert "hands this over by itself" in notes[tests]
        assert "Recommended roadmap" in notes[road]
        assert [m["body"]["to"] for m in collab.messages(c, tests_item, kinds=("handoff",))] \
            == ["agent:qa"]
        assert board.get(c, road_item)["lifecycle"] == "dismissed"
        events = c.execute(text(
            "SELECT count(*) FROM events WHERE project_id = :p "
            "AND event_type = 'decision.withdrawn'"), {"p": project}).scalar()
        assert events == 2


def test_a_card_a_person_asked_for_stays_when_the_agents_are_off(eng, project):
    """"Triage now" while the always-on agents are off asks in `propose` (manual): turning them
    off — the state they are already in — must not withdraw what a person asked for."""
    with eng.begin() as c:
        _the_morning(c, project)
    _run(eng, project, "propose", manual=True)
    off = _run(eng, project, "off")
    assert off["reconsidered"] == []
    with eng.begin() as c:
        [card] = _open_cards(c, project)
        assert card["recommendation"]["manual"] is True


def test_the_rate_guard_holds_suggestions_for_the_next_card_and_seats_no_new_squad(eng,
                                                                                    project):
    cap = policy.ASKS_PER_PROJECT_HOUR
    roomy = {"ambient_cos_cards_per_class": 100}

    def note(n):
        with eng.begin() as c:
            signal(c, project, source="scan", kind="scan.no_docs", title=f"repo{n} has no README",
                   subject_kind="repo", subject_id=f"acme/repo{n}", app=None, severity="low")

    # While a card is open, every new suggestion JOINS it: one card, however many ticks.
    for n in range(2):
        note(n)
        report = _run(eng, project, "propose", settings=roomy)
        assert report["card"]["asked"], n
    with eng.begin() as c:
        [card] = _open_cards(c, project)
        assert len(card["recommendation"]["batch"]) == 2
    # Each card answered, the next suggestion raises a new one — until the hour holds `cap`.
    for n in range(2, cap + 2):
        with eng.begin() as c:
            _answer_open_cards(c, project)
        note(n)
        report = _run(eng, project, "propose", settings=roomy)
    assert report["card"]["asked"] == [] and len(report["card"]["held"]) == 1
    with eng.begin() as c:
        assert _open_cards(c, project) == []
        assert policy.asks_last_hour(c, project) == cap
    with eng.begin() as c:
        assert policy.ask_room(c, project) == 0
        # An item already handed over gets no NEW squad while the hour of cards is used up:
        # its proposals would ask again.
        handed = open_item(c, project, fingerprint="fp:handed", task_class="missing_tests",
                           subject_ref="module:acme/x:a.py")["item"]
        collab.post(c, handed["id"], kind="handoff", sender=cos.PARTY,
                    body={"to": "agent:qa", "from": cos.PARTY, "summary": "Open draft PR"})
        assert owners.due_items(c, project_id=project) == []
        # An hour later: the held suggestion is asked on the next card, and squads may start.
        c.execute(text("UPDATE decisions SET created_at = created_at - interval '2 hours' "
                       "WHERE project_id = :p"), {"p": project})
    report = _run(eng, project, "propose", settings=roomy)
    assert len(report["card"]["asked"]) == 1
    with eng.begin() as c:
        assert [d["id"] for d in owners.due_items(c, project_id=project)] == [handed["id"]]


def test_a_suggestion_held_by_the_rate_guard_is_judged_again_when_the_mode_changes(eng,
                                                                                   project):
    with eng.begin() as c:
        for n in range(policy.ASKS_PER_PROJECT_HOUR):
            # Each card answered before the next: the hour's cap counts the cards raised.
            _answer_open_cards(c, project)
            signal(c, project, source="scan", kind="scan.no_ci", title=f"r{n} has no CI",
                   subject_kind="repo", subject_id=f"acme/r{n}", app=None, severity="low")
            _run_in(c, project, "propose")
        _answer_open_cards(c, project)
        held = signal(c, project, source="scan", kind="scan.no_docs", title="z has no README",
                      subject_kind="repo", subject_id="acme/z", app=None, severity="low")
        assert _run_in(c, project, "propose")["card"]["held"]
    report = _run(eng, project, "route")
    assert report["card"]["settled"] == ["routed"]
    with eng.begin() as c:
        item = board.get(c, signals.get(c, held["id"])["work_item_id"])
        assert collab.messages(c, item["id"], kinds=("handoff",))[0]["body"]["to"] == \
            "agent:docs_writer"


def test_a_question_names_the_file_or_repo_never_its_key():
    assert board.plain_subject({"subject_ref": f"module:{REPO}:web/src/pages/A.jsx"}) == "A.jsx"
    assert board.plain_subject({"subject_ref": f"repo:{REPO}"}) == "helpdesk-ai-demo"
    assert board.plain_subject({"subject_ref": "app:Order API"}) == "Order API"
    assert board.plain_subject({"subject_ref": "pr:acme/shop#41"}) == "PR #41"
    assert board.plain_subject({"subject_ref": "host:install"}) == "this install"
    for raw in ("task:crun_da607756", "run:wfr_8447d53b", "subject:stage:solo"):
        assert board.plain_subject({"subject_ref": raw}) is None
    item = {"subject_ref": "task:crun_da607756", "title": "Finish the visual refresh the last "
            "change promised but did not deliver"}
    assert cos.what_words("stale_handoff", item) == "a nudge on “Finish the visual refresh the " \
                                                    "last change…”"
    entries = [{"task_class": "missing_tests", "asked": False}] * 3 + [
        {"task_class": "doc_drift", "asked": False}, {"task_class": "stale_handoff",
                                                      "asked": False}]
    assert cos.batch_question(entries) == \
        "Chief of Staff suggests 5 hand-offs: tests for 3 hot files, docs, a nudge"


def test_an_unrecovered_incident_says_what_was_done_and_seen():
    q = incident_squad.recovery_question
    assert q("Order API", "staging", rolled_back=False, unconfirmed=True) == (
        "Order API (staging): too little traffic to confirm it recovered — check it or close it?")
    assert q("Order API", "staging", rolled_back=True, unconfirmed=False) == (
        "Order API (staging) did not recover after the rollback — undo it or take over?")
    assert "rollback" not in q("Order API", "production", rolled_back=False, unconfirmed=False)


def test_each_tick_after_the_first_adds_no_card(eng, project):
    with eng.begin() as c:
        _the_morning(c, project)
    _run(eng, project, "propose")
    again = _run(eng, project, "propose")
    assert again["card"] == {"asked": []} and again["reconsidered"] == []
    with eng.begin() as c:
        assert len(_open_cards(c, project)) == 1
        recent = c.execute(text(
            "SELECT count(*) FROM decisions WHERE project_id = :p AND created_at > :t"),
            {"p": project, "t": board.now() - timedelta(minutes=5)}).scalar()
        assert recent == 1
