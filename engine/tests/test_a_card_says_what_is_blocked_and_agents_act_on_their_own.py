"""Only the compliance boundary halts for a person; a card says what is blocked and what to give.

Two always-on "Needs you" cards on the laptop, 2026-11-07, against the operator's rule ("it
should only halt for user input … provide the proper information and why it is not fixed"):

1. boss-dogfood, dec_47f9…: "Squad is stuck on this install — let it go on, or take it?" with
   "Approve all / Dismiss all". The Infra Steward's squad said it had "no tool access to
   host:install logs" — while holding `boss.fundamentals` (read_logs, system_health …): a squad
   turn offered no tools at all. Now
     * every squad turn reads with the tools its member's role was granted (role_skills);
     * an escalation claiming access the member already holds is refused (call it instead);
     * the card says what is blocked and what the person must give ("… can't read the
       install's logs to explain the restarts — grant access, or take it?"), names the squad,
       keeps the full reason behind it, and its answers say what they do — "Grant access"
       grants the skill (identity is on the boundary: a person approves it, and approving does
       it), "Let it go on" lets it go on; a card narrowed to one question says so;
     * a stuck question from before the squad could read is handed back to it, card withdrawn.
2. helpdesk, dec_6ae0…: "Chief of Staff suggests 2 hand-offs: 2 nudges" on two CVE builds that
   had FINISHED and waited on the person's own "Approve code change" merge cards. Now
     * work waiting on a person's open decision is not a stalled hand-off: not raised, and a
       card raised before withdraws itself on the next tick;
     * a nudge is the Chief of Staff's own act inside BOSS: done and recorded, never asked; one
       that does reach a person runs when approved (it used to be recorded and never done);
     * a card of nudges names the work: "Nudge 2 stalled builds: esbuild, minimatch bumps".
No test calls a model.
"""
from __future__ import annotations

import json
import re
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services.ambient import (asks, board, chief_of_staff as cos, collab, ledger,
                                          squad_turns, squads)
from boss_engine.services.core import create_blocking_decision

from ._ambient_support import eng, open_item, project, signal  # noqa: F401
from ._squad_support import ScriptedSquadModel, add_roles, move, settings
from .test_cards_name_the_work_and_withdraw_themselves import _quiet_build
from .test_stuck_work_is_explained_and_diagnosed_by_an_agent import _clean, _run

INFRA_ROLES = ("infra_steward", "software_engineer", "adversarial_reviewer")
INFRA = "agent:infra_steward"
REASON = ("Specialist has no tool access to host:install logs needed to verify restart cause "
          "(update-trigger vs OOM/watchdog crash-loop) per the agreed plan; need a person to "
          "grant log access or supply the timestamps so the squad can settle")


def _grant(c, pid, role, skill="boss.fundamentals"):
    from boss_engine.services.skill_definitions import assign_skill_to_role

    assign_skill_to_role(c, project_id=pid, actor="person:ceo", role_key=role, skill_id=skill)


def _restarts_item(c, pid):
    """The boss-dogfood item: the system doctor's "Updates restarted a build 2 times in an hour",
    with the general squad seated (the Infra Steward leads)."""
    add_roles(c, pid, INFRA_ROLES)
    item = open_item(c, pid, task_class="system_health", fingerprint=f"fp:{uuid4().hex[:8]}",
                     title="Updates restarted a build 2 times in an hour",
                     subject_ref="host:install")["item"]
    squads.assemble(c, item["id"])
    return item


def _ceo(c, pid):
    c.execute(text(
        "INSERT INTO project_members (id, project_id, member_id, display_name, member_type, "
        "role_keys, permissions) VALUES (:i, :p, 'ceo', 'ceo', 'human', '[\"ceo\"]'::jsonb, "
        "'[\"decision_resolve\"]'::jsonb) ON CONFLICT DO NOTHING"),
        {"i": f"pm_{pid}_ceo", "p": pid})


def _approve(eng, card_id):
    from boss_engine.services.decisions import DecisionService

    with eng.begin() as c:
        DecisionService(get_settings()).transition(c, card_id, action="approve", actor="ceo",
                                                   note="Go ahead.")


def _card(c, card_id):
    row = c.execute(text("SELECT status, question, options, recommendation, resolution_note "
                         "FROM decisions WHERE id = :d"), {"d": card_id}).mappings().one()
    return dict(row)


class ReadingModel:
    """A model that calls one read tool, then answers with a finding citing what it read."""

    def __init__(self, tool):
        self.tool, self.offered, self.results = tool, [], []

    def create_chat_completion(self, messages, tools=None):
        self.offered.append([t["function"]["name"] for t in tools or []])
        seen = [m for m in messages if m.get("role") == "tool"]
        if not seen and tools:
            return {"content": "", "tool_calls": [{"id": "c1", "name": self.tool,
                                                   "arguments": "{}"}]}, {"model": "stub"}
        self.results = [m["content"] for m in seen]
        sig = re.search(r"signal:sig_[0-9A-Za-z]+",
                        " ".join(str(m.get("content")) for m in messages)).group(0)
        answer = {"action": "finding", "confidence": 0.9, "reason": "read the logs",
                  "arguments": {"summary": "The restarts line up with the update runs",
                                "evidence": [sig]}}
        return {"content": json.dumps(answer)}, {"model": "stub"}


# ── 1b. a squad member reads with the tools its role was granted ─────────────────────────────

def test_a_member_is_offered_the_reads_its_role_holds_and_uses_them(eng, project):  # noqa: F811
    with eng.begin() as c:
        item = _restarts_item(c, project)
        assert squad_turns.held_reads(c, None, project, INFRA) == []    # nothing granted yet
        _grant(c, project, "infra_steward")
        held = squad_turns.held_reads(c, None, project, INFRA)
        assert {"read_logs", "system_health", "inspect_self"} <= set(held)
        assert "file_platform_gap" not in held, "reads only: the bundle's write is not offered"
        model = ReadingModel("read_logs")
        turn = squad_turns.take_turn(c, settings(), item["id"], INFRA, model=model)
        posted = collab.messages(c, item["id"], kinds=("finding",))
    assert "read_logs" in model.offered[0], model.offered
    assert model.results, "the read ran and its result came back to the member"
    assert turn["status"] == "posted" and posted[-1]["sender"] == INFRA


def test_asking_a_person_for_access_it_already_holds_is_refused(eng, project):  # noqa: F811
    with eng.begin() as c:
        item = _restarts_item(c, project)
        _grant(c, project, "infra_steward")
        lead = squads.roster(c, item["id"])["lead"]
        assert lead == INFRA
        model = ScriptedSquadModel({INFRA: [
            move("escalate", reason=REASON, blocked="read the install's logs",
                 need="grant log access", grant="read_logs"),
            move("pass")]})
        turn = squad_turns.take_turn(c, settings(), item["id"], INFRA, model=model)
        after = board.get(c, item["id"])
    assert turn["status"] != "needs_you" and after["lifecycle"] != "needs_you"
    assert "you already hold read_logs" in model.prompts[-1][1]


# ── 1a / 1c. the card says what is blocked, what to give, and its answers do what they say ──

def test_a_stuck_squads_card_names_the_block_and_grant_access_grants_it(eng, project):  # noqa: F811
    with eng.begin() as c:
        _ceo(c, project)
        item = _restarts_item(c, project)
        model = ScriptedSquadModel({INFRA: [move(
            "escalate", reason=REASON,
            blocked="read the install's logs to explain the restarts",
            need="grant log access", grant="read_logs")]})
        turn = squad_turns.take_turn(c, settings(), item["id"], INFRA, model=model)
        assert turn["status"] == "needs_you", turn
        card_id = board.get(c, item["id"])["needs_you_decision_id"]
        card = _card(c, card_id)
    q = card["question"]
    assert q == ("Infra Steward's squad can't read the install's logs to explain the restarts — "
                 "grant access, or take it?"), q
    assert "Squad is stuck" not in q and "this install" not in q
    [entry] = card["recommendation"]["batch"]
    assert entry["squad"] == "Infra Steward's squad" and entry["grant"] == "boss.fundamentals"
    assert "OOM/watchdog crash-loop" in entry["detail"], "the full reason rides behind it"
    assert card["options"] == [{"key": "approve", "label": "Grant access"},
                               {"key": "reject", "label": "I'll take it"}]
    _approve(eng, card_id)
    with eng.begin() as c:
        assert "read_logs" in squad_turns.held_reads(c, None, project, INFRA), "granted"
        after = board.get(c, item["id"])
        said = [m["body"]["summary"] for m in collab.messages(c, item["id"], kinds=("finding",))]
    assert after["lifecycle"] != "needs_you"
    assert any("granted boss.fundamentals to agent:infra_steward" in s for s in said)


def test_a_card_narrowed_to_one_question_answers_like_one(eng, project):  # noqa: F811
    with eng.begin() as c:
        a, b = _restarts_item(c, project), _restarts_item(c, project)
        entries = [asks.question_entry(c, i, actor=INFRA, reason=REASON, squad_title="Squad",
                                       blocked="read the logs", need="send the restart times")
                   for i in (a, b)]
        card_id = asks.raise_card(c, project, entries)
        assert _card(c, card_id)["options"][0]["label"] == "Let them go on"
        # b moves on (a person took it elsewhere): the card is narrowed to a's question.
        c.execute(text("UPDATE work_items SET needs_you_decision_id = NULL WHERE id = :w"),
                  {"w": b["id"]})
        cos.reconsider(c, project, mode="propose")
        card = _card(c, card_id)
    assert card["status"] == "pending" and len(card["recommendation"]["batch"]) == 1
    assert card["question"] == ("Infra Steward's squad can't read the logs — send the restart "
                                "times, or take it?")
    assert card["options"] == [{"key": "approve", "label": "Let it go on"},
                               {"key": "reject", "label": "I'll take it"}]


def test_a_stuck_question_from_before_the_squad_could_read_goes_back_to_it(eng, project):  # noqa: F811
    """The live card's shape: one legacy question entry ("Squad", no `reads`), the lead now
    holding the fundamentals. The next tick withdraws it and the squad looks for itself."""
    with eng.begin() as c:
        item = _restarts_item(c, project)
        _grant(c, project, "infra_steward")
        legacy = {"squad": "Squad", "words": "squad is stuck: this install", "detail": REASON,
                  "subject": "this install", "ask_kind": "question", "asked_by": INFRA,
                  "question": "Squad is stuck on this install — let it go on, or take it?",
                  "task_class": "system_health", "work_item_id": item["id"]}
        card_id = asks.raise_card(c, project, [legacy])
        done = cos.reconsider(c, project, mode="propose")
        card = _card(c, card_id)
        after = board.get(c, item["id"])
    assert [s["verdict"] for r in done for s in r.get("settled", [])] == ["granted"]
    assert card["status"] == "withdrawn" and "looks for itself" in card["resolution_note"]
    assert after["lifecycle"] == "proposed" and after["needs_you_decision_id"] is None


# ── 2a. work waiting on a person's decision is not a stalled hand-off ────────────────────────

def _merge_card(c, pid, rid, title="Bump esbuild past 0.21.5"):
    """The build finished and waits on the person's own "Approve code change" card."""
    did = create_blocking_decision(c, pid, None, f"Approve code change: {title} (PR #6)",
                                   {"action": "merge", "run_id": rid}, {}, "code_change",
                                   risk_tier=2, default_action="hold")
    c.execute(text("UPDATE code_task_runs SET status = 'completed', decision_id = :d "
                   "WHERE id = :r"), {"d": did, "r": rid})
    return did


def _shepherd_signal(c, pid, rid, title):
    return signal(c, pid, source="shepherd", kind="shepherd.escalation",
                  title=f"{title}: running 282 minutes against a median of 21. Let it continue, "
                        "or cancel it?", subject_kind="task", subject_id=rid, app=None,
                  severity="high")


def _settings(**kw):
    return SimpleNamespace(**{**get_settings().__dict__, "ambient_cos_model_calls": 0, **kw})


def test_a_build_waiting_on_its_merge_card_is_never_raised_as_stalled(eng, project):  # noqa: F811
    with eng.begin() as c:
        rid = _run(c, project, status="running", title="Bump esbuild past 0.21.5")
        merge = _merge_card(c, project, rid)
        _shepherd_signal(c, project, rid, "Bump esbuild past 0.21.5")
        report = cos.triage_project(c, _settings(), project, mode="propose", max_model_calls=0)
        cards = c.execute(text("SELECT id FROM decisions WHERE project_id = :p AND status = "
                               "'pending' AND id <> :m"), {"p": project, "m": merge}).all()
        [item] = [i for i in board.list_items(c, project, limit=20)
                  if i["task_class"] == "stale_handoff"]
        linked = [l["ref"] for l in board.links(c, item["id"]) if l["kind"] == "decision"]
    assert report["counts"] == {"linked": 1}, report["counts"]
    assert cards == [] and item["needs_you_decision_id"] is None
    assert linked == [merge], "the item points at the card the person already has"


def _old_nudge_card(c, pid, rids_titles):
    """A card exactly as the code before this fix raised it: the Chief of Staff suggesting
    nudges (owner_may_act false), each item waiting on it."""
    entries = []
    for rid, title in rids_titles:
        sig = _shepherd_signal(c, pid, rid, title)
        item = open_item(c, pid, task_class="stale_handoff", fingerprint=f"fp:{rid}",
                         title=sig["title"], subject_ref=f"task:{rid}",
                         signal_ids=[sig["id"]])["item"]
        act = ledger.record_decision(
            c, project_id=pid, decided_by=cos.PARTY, work_item_id=item["id"],
            task_class="stale_handoff", question="Who should own this stalled handoff?",
            chosen="propose:chief_of_staff", action="propose", options=["observe", "ask"],
            reversibility="reversible", undo={"action": "dismiss"},
            evidence=[{"kind": "signal", "ref": sig["id"]}],
            rationale="Stalled handoff: a handoff is waiting on someone.")
        entries.append({"ask_kind": "handoff", "work_item_id": item["id"], "act_id": act["act_id"],
                        "task_class": "stale_handoff", "kind": "Stalled handoff",
                        "title": title, "words": f"a nudge on “{title}”", "asked": False,
                        "owner": cos.PARTY, "owner_label": "Chief of Staff",
                        "next_action": "nudge", "owner_may_act": False, "severity": "high"})
    return asks.raise_card(c, pid, entries, actor=cos.PARTY), [e["work_item_id"] for e in entries]


def test_a_nudge_card_withdraws_itself_once_the_builds_wait_on_their_merge_cards(eng, project):  # noqa: F811
    with eng.begin() as c:
        esbuild = _run(c, project, title="Bump esbuild past 0.21.5")
        minimatch = _run(c, project, title="Bump minimatch to 9.0.10")
        card_id, items = _old_nudge_card(c, project, [(esbuild, "Bump esbuild"),
                                                      (minimatch, "Bump minimatch")])
        # Since the card was raised, both builds finished and wait on their merge cards.
        merges = [_merge_card(c, project, esbuild), _merge_card(c, project, minimatch)]
        done = cos.reconsider(c, project, mode="propose")
        card = _card(c, card_id)
        ended = [board.get(c, i) for i in items]
        links = {i: [l["ref"] for l in board.links(c, i) if l["kind"] == "decision"]
                 for i in items}
    assert sorted(s["verdict"] for r in done for s in r["settled"]) == ["linked", "linked"]
    assert card["status"] == "withdrawn" and "waits on your own decision" in \
        card["resolution_note"]
    assert {i["lifecycle"] for i in ended} == {"resolved"}
    assert merges[0] in links[items[0]] and merges[1] in links[items[1]]


# ── 2b. a nudge is the Chief of Staff's own act, and approving one runs it ───────────────────

def test_approving_a_nudge_runs_it(eng, project):  # noqa: F811
    try:
        with eng.begin() as c:
            _ceo(c, project)
            rid = _quiet_build(c, project)
            card_id, [item_id] = _old_nudge_card(c, project, [(rid, "Open draft PR bumping")])
        _approve(eng, card_id)
        with eng.begin() as c:
            item = board.get(c, item_id)
            rows = ledger.list_decisions(c, project, work_item_id=item_id)
            diag = c.execute(text("SELECT finalize_ctx->'diagnosis' FROM code_task_runs "
                                  "WHERE id = :r"), {"r": rid}).scalar()
        nudged = [r for r in rows if r["chosen"] == "nudge"]
        assert item["lifecycle"] == "resolved" and item["outcome"].startswith("Nudged")
        assert nudged and nudged[0]["decided_by"].startswith("person:"), rows
        assert diag, "the Shepherd looked at the build: the nudge ran"
    finally:
        _clean(eng, project)


def test_a_stalled_build_is_nudged_by_the_chief_of_staff_without_asking(eng, project):  # noqa: F811
    try:
        with eng.begin() as c:
            rid = _quiet_build(c, project)
            _shepherd_signal(c, project, rid, "Open draft PR bumping esbuild")
            report = cos.triage_project(c, _settings(), project, mode="propose",
                                        max_model_calls=0)
            cards = c.execute(text("SELECT count(*) FROM decisions WHERE project_id = :p "
                                   "AND status = 'pending' AND recommendation->>'card' = "
                                   "'ambient'"), {"p": project}).scalar()
            [item] = [i for i in board.list_items(c, project, limit=20)
                      if i["task_class"] == "stale_handoff"]
            nudged = [r for r in ledger.list_decisions(c, project, work_item_id=item["id"])
                      if r["chosen"] == "nudge"]
        assert report["counts"] == {"acted": 1}, report["counts"]
        assert cards == 0 and item["lifecycle"] == "resolved"
        assert nudged and nudged[0]["decided_by"] == cos.PARTY and nudged[0]["action"] == "nudge"
    finally:
        _clean(eng, project)


# ── 2c. a card of nudges names the work ──────────────────────────────────────────────────────

def test_a_card_of_nudges_names_the_stalled_work(eng, project):  # noqa: F811
    with eng.begin() as c:
        names = []
        for package in ("esbuild", "minimatch"):
            started = open_item(c, project, task_class="security_patch",
                                fingerprint=f"fp:{package}", title=f"{package} has a hole",
                                subject_ref=f"dep:{package}")["item"]
            rid = _run(c, project, title=f"Bump {package}",
                       ctx={"authority": {"work_item_id": started["id"]}})
            sig = _shepherd_signal(c, project, rid, f"Bump {package}")
            stalled = open_item(c, project, task_class="stale_handoff", fingerprint=f"fp:s{rid}",
                                title=sig["title"], subject_ref=f"task:{rid}",
                                signal_ids=[sig["id"]])["item"]
            names.append(cos.stalled_name(c, stalled))
    assert names == [("esbuild", "bump"), ("minimatch", "bump")]
    entries = [{"task_class": "stale_handoff", "owner": cos.PARTY, "next_action": "nudge",
                "asked": False, "stalled": n, "stalled_noun": noun, "ask_kind": "handoff"}
               for n, noun in names]
    assert cos.batch_question(entries) == "Nudge 2 stalled builds: esbuild, minimatch bumps"
    assert asks.card_options(entries)[0]["label"] == "Nudge all"
    mixed = entries[:1] + [{"task_class": "doc_drift", "owner": "agent:docs_writer",
                            "asked": False, "ask_kind": "handoff"}]
    assert cos.batch_question(mixed) == \
        "Chief of Staff suggests 2 hand-offs: a nudge (esbuild bump), docs"
