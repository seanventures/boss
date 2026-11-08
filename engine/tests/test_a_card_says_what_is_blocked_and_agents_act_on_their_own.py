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
                 need="grant log access", need_kind="access", grant="read_logs"),
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
            need="grant log access", need_kind="access", grant="read_logs")]})
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
        _ceo(c, project)
        a, b = _restarts_item(c, project), _restarts_item(c, project)
        entries = [asks.question_entry(c, i, actor=INFRA, reason=REASON, squad_title="Squad",
                                       blocked="read the logs", need="send the restart times")
                   for i in (a, b)]
        card_id = asks.raise_card(c, project, entries)
        assert _card(c, card_id)["options"][0]["label"] == "Close all: expected"
        # b moves on (a person took it elsewhere): the card is narrowed to a's question.
        c.execute(text("UPDATE work_items SET needs_you_decision_id = NULL WHERE id = :w"),
                  {"w": b["id"]})
        cos.reconsider(c, project, mode="propose")
        card = _card(c, card_id)
    assert card["status"] == "pending" and len(card["recommendation"]["batch"]) == 1
    # Nothing to grant: the card offers only what a person can do, and never "Let it go on".
    assert card["question"] == ("Infra Steward's squad can't read the logs (needs: send the "
                                "restart times) — close it as expected, or take it?")
    assert card["options"] == [{"key": "approve", "label": "Close it: expected"},
                               {"key": "reject", "label": "I'll take it"}]
    _approve(eng, card_id)
    with eng.begin() as c:
        closed = board.get(c, a["id"])
        said = [m["body"]["summary"] for m in collab.messages(c, a["id"], kinds=("finding",))]
    assert closed["lifecycle"] == "resolved", "Close it: expected closes it"
    assert any("closed it as expected" in s for s in said)


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


# ── an item put back on the board gets its owners' next turn, or the reason is recorded ──────
#
# After cc8b6e8fb the live card withdrew ("granted") and work_3c4d… went back on the board — then
# sat an hour with no turn: five higher-scored helpdesk CVE items filled the three owner slots of
# every tick. An item owed a turn now goes first; one that still gets none says why, on the ledger.

def _handed(c, item):
    collab.post(c, item["id"], kind="handoff", sender=cos.PARTY,
                body={"to": INFRA, "from": cos.PARTY, "summary": "Propose"})


def _returned(c, pid):
    """The live item's path: a stuck question, withdrawn because the squad can now read."""
    item = _restarts_item(c, pid)
    _handed(c, item)
    _grant(c, pid, "infra_steward")
    legacy = {"squad": "Squad", "words": "squad is stuck: this install", "detail": REASON,
              "ask_kind": "question", "asked_by": INFRA, "task_class": "system_health",
              "question": "Squad is stuck on this install — let it go on, or take it?",
              "work_item_id": item["id"]}
    asks.place(c, None, pid, [legacy], urgent=True)   # the project's one card
    return item


def test_an_item_put_back_on_the_board_goes_ahead_of_higher_priority_work(eng, project):  # noqa: F811
    from boss_engine.services.ambient import owners

    with eng.begin() as c:
        busier = []
        for n in range(3):
            other = open_item(c, project, task_class="system_health", fingerprint=f"fp:busy{n}",
                              title=f"Busier work {n}", subject_ref=f"repo:acme/b{n}")["item"]
            _handed(c, other)
            busier.append(other["id"])
        mine = _returned(c, project)
        c.execute(text("UPDATE work_items SET effective_score = 90 WHERE id = ANY(:b)"),
                  {"b": busier})
        c.execute(text("UPDATE work_items SET effective_score = 47 WHERE id = :w"),
                  {"w": mine["id"]})
        before = [d["id"] for d in owners.due_items(c, project_id=project, limit=3)]
        assert mine["id"] not in before, "waiting on the card: not the owners' to work"
        cos.reconsider(c, project, mode="propose")        # withdrawn: back on the board
        due = owners.due_items(c, project_id=project, limit=3)
    assert due[0]["id"] == mine["id"] and due[0].get("owed"), [d["id"] for d in due]


def test_an_owed_item_that_gets_no_turn_says_why_on_the_ledger(eng, project):  # noqa: F811
    from boss_engine.services.ambient import owners

    from ._squad_support import Launchers

    with eng.begin() as c:
        first = _returned(c, project)
        second = _returned(c, project)
        cos.reconsider(c, project, mode="propose")
        owed = [d["id"] for d in owners.due_items(c, project_id=project, limit=5)
                if d.get("owed")]
    assert sorted(owed) == sorted([first["id"], second["id"]])
    quiet = ScriptedSquadModel({})
    report = owners.work(eng, settings(), project_id=project, limit=1, model=quiet,
                         launchers=Launchers(), rounds=1)
    [turned] = [w["work_item_id"] for w in report["worked"] if w["kind"] == "squad"]
    [waiting] = [i for i in (first["id"], second["id"]) if i != turned]
    with eng.begin() as c:
        said = [r for r in ledger.list_decisions(c, project, work_item_id=waiting)
                if r["question"] == owners.Q_WHY_NO_TURN]
    assert len(said) == 1 and said[0]["chosen"] == "wait"
    assert "first in line for the next tick" in said[0]["rationale"]
    # The next tick gives it its turn, and the reason is not written twice.
    report = owners.work(eng, settings(), project_id=project, limit=1, model=quiet,
                         launchers=Launchers(), rounds=1)
    assert [w["work_item_id"] for w in report["worked"] if w["kind"] == "squad"] == [waiting]
    with eng.begin() as c:
        assert len([r for r in ledger.list_decisions(c, project, work_item_id=waiting)
                    if r["question"] == owners.Q_WHY_NO_TURN]) == 1


# ── parked while the person decides; aged so nothing waits forever; one card, every entry ────

def test_work_whose_build_waits_on_its_merge_card_is_parked_until_the_person_decides(
        eng, project):  # noqa: F811
    """helpdesk 2026-11-07: CVE items whose builds had finished and waited on their merge cards
    took a squad turn every 30 minutes and every owner slot. Parked now; due again once the
    person answers."""
    from boss_engine.services.ambient import owners

    with eng.begin() as c:
        item = _restarts_item(c, project)
        _handed(c, item)
        wfr = f"wfr_t_{uuid4().hex[:10]}"
        rid = _run(c, project, title="Bump esbuild past 0.21.5", ctx={"workflow_run_id": wfr})
        c.execute(text("INSERT INTO work_item_links (work_item_id, kind, ref, project_id, "
                       "created_by) VALUES (:w, 'workflow_run', :r, :p, 'test')"),
                  {"w": item["id"], "r": wfr, "p": project})
        assert [d["id"] for d in owners.due_items(c, project_id=project)] == [item["id"]]
        merge = _merge_card(c, project, rid)
        assert owners.waits_on_a_decision(c, item) == merge
        assert owners.due_items(c, project_id=project) == [], "parked: nothing it can change"
        c.execute(text("UPDATE decisions SET status = 'approved' WHERE id = :d"), {"d": merge})
        assert [d["id"] for d in owners.due_items(c, project_id=project)] == [item["id"]]


def test_a_low_priority_item_that_waited_too_long_gets_a_turn_whatever_its_score(
        eng, project, monkeypatch):  # noqa: F811
    from datetime import timedelta

    from boss_engine.services.ambient import owners

    with eng.begin() as c:
        old = open_item(c, project, task_class="system_health", fingerprint="fp:old",
                        title="Low but long waiting", subject_ref="repo:acme/old")["item"]
        _handed(c, old)                                   # handed over first: waited longest
        busier = []
        for n in range(3):
            other = open_item(c, project, task_class="system_health", fingerprint=f"fp:hot{n}",
                              title=f"Busier work {n}", subject_ref=f"repo:acme/h{n}")["item"]
            _handed(c, other)
            busier.append(other["id"])
        c.execute(text("UPDATE work_items SET effective_score = 90 WHERE id = ANY(:b)"),
                  {"b": busier})
        c.execute(text("UPDATE work_items SET effective_score = 10 WHERE id = :w"),
                  {"w": old["id"]})
        assert old["id"] not in [d["id"] for d in owners.due_items(c, project_id=project,
                                                                   limit=3)]
        # Past the aging threshold with no turn on any of them: the longest wait goes first.
        later = owners.now() + timedelta(minutes=owners.STARVE_MINUTES + 5)
        monkeypatch.setattr(owners, "now", lambda: later)
        due = [d["id"] for d in owners.due_items(c, project_id=project, limit=3)]
    assert due[0] == old["id"], due


def test_two_cards_raised_with_the_same_words_become_one_card_asking_both(eng, project):  # noqa: F811
    """create_blocking_decision folds a same-question pending decision into the open one; the
    second item used to wait on a card whose batch did not list it, so a withdrawal left it
    stuck on Needs you."""
    with eng.begin() as c:
        items, cards = [], []
        for _ in range(2):
            item = _restarts_item(c, project)
            items.append(item["id"])
            cards.append(asks.raise_card(c, project, [{
                "ask_kind": "question", "asked_by": INFRA, "task_class": "system_health",
                "squad": "Squad", "words": "squad is stuck", "detail": REASON,
                "question": "Squad is stuck on this install — let it go on, or take it?",
                "work_item_id": item["id"]}]))
        card = _card(c, cards[0])
        assert cards[0] == cards[1]
        assert sorted(e["work_item_id"] for e in card["recommendation"]["batch"]) == sorted(items)
        _grant(c, project, "infra_steward")
        cos.reconsider(c, project, mode="propose")
        after = [board.get(c, i) for i in items]
    assert {i["lifecycle"] for i in after} == {"proposed"}
    assert {i["needs_you_decision_id"] for i in after} == {None}


# ── BOSS reads its own upgrades and restarts; a claim of no tool is checked first ────────────
#
# Live, 23:05: the squad read with its tools, then asked again — "can't read host:install logs
# … grant host:install log access" — with nothing grantable, so the card offered a do-nothing
# "Let it go on". BOSS kept no record of which commit it ran when. Now every start is recorded,
# `install_restarts` classifies them, the infra squad holds it, and an ask for a tool that does
# not exist is refused with the reads it holds.

def test_every_start_is_recorded_and_classified_upgrade_or_crash(eng, monkeypatch):  # noqa: F811
    from boss_engine.services import install_history

    service = f"t{uuid4().hex[:8]}"
    host = {"name": "c1"}
    monkeypatch.setattr(install_history.socket, "gethostname", lambda: host["name"])
    try:
        monkeypatch.setenv("BOSS_SOURCE_SHA", "a" * 40)
        monkeypatch.setenv("BOSS_BUILT_AT", "2026-11-07T19:40:00Z")
        monkeypatch.setenv("BOSS_INSTALL_METHOD", "upgrade.sh")
        assert install_history.record_start(eng, service)["upgraded"] is False
        monkeypatch.setenv("BOSS_SOURCE_SHA", "b" * 40)       # upgrade.sh: a new commit
        monkeypatch.setenv("BOSS_BUILT_AT", "2026-11-07T19:43:00Z")
        host["name"] = "c2"
        up = install_history.record_start(eng, service)
        assert up["upgraded"] and up["previous_commit"] == "a" * 40
        install_history.record_start(eng, service)            # same container again: a crash
        with eng.connect() as c:
            got = install_history.restarts(c, hours=1)
        mine = [s for s in got["starts"] if s.get("service") == service]
        assert [s["kind"] for s in mine] == ["first", "upgrade", "restart"]
        assert mine[1]["from_commit"] == "a" * 12 and mine[1]["commit"] == "b" * 12
        assert mine[1]["by"] == "upgrade.sh" and "crash-loop" in mine[2]["says"]
        assert {"at": mine[1]["at"], "service": service, "from": "a" * 12, "to": "b" * 12,
                "by": "upgrade.sh"} in got["upgrades"]
    finally:
        with eng.begin() as c:
            c.execute(text("DELETE FROM boss_starts WHERE service = :s"), {"s": service})


def test_the_infra_squad_holds_install_restarts_through_its_granted_bundle(eng, project):  # noqa: F811
    from boss_engine.services.agent_baseline import FUNDAMENTALS_TOOLS
    from boss_engine.tools import build_discuss_read_registry

    assert "install_restarts" in FUNDAMENTALS_TOOLS
    assert build_discuss_read_registry().get("install_restarts").kind == "read"
    with eng.begin() as c:
        _restarts_item(c, project)
        _grant(c, project, "infra_steward")
        assert "install_restarts" in squad_turns.held_reads(c, None, project, INFRA)


def test_an_ask_for_a_tool_that_does_not_exist_is_refused_with_the_reads_it_holds(
        eng, project):  # noqa: F811
    with eng.begin() as c:
        item = _restarts_item(c, project)
        _grant(c, project, "infra_steward")
        model = ScriptedSquadModel({INFRA: [
            move("escalate", reason=REASON, blocked="read host:install logs",
                 need="grant host:install log access", need_kind="access",
                 grant="host:install logs"),
            move("pass")]})
        turn = squad_turns.take_turn(c, settings(), item["id"], INFRA, model=model)
        after = board.get(c, item["id"])
    assert turn["status"] != "needs_you" and after["lifecycle"] != "needs_you"
    refusal = model.prompts[-1][1]
    assert "no tool or skill named host:install logs exists" in refusal
    assert "install_restarts" in refusal, "the catalogue it holds is named"


# ── turn → ask → withdraw → the next tick gives the turn; a guard's stop is never silent ─────
#
# Live, 23:28-23:56: the withdrawn item kept the 60-minute lease of the turn that asked, its
# lead's next turn read past the read phase's cap and "passed" with no answer, and that turn
# took the squad past its $1.50 budget — after which every tick refused it without a word.

def test_turn_ask_withdraw_and_the_next_tick_gives_the_turn(eng, project):  # noqa: F811
    from boss_engine.services.ambient import owners

    from ._squad_support import Launchers

    with eng.begin() as c:
        item = _restarts_item(c, project)
        _handed(c, item)
        asked = squad_turns.take_turn(c, settings(), item["id"], INFRA, model=ScriptedSquadModel(
            {INFRA: [move("escalate", reason=REASON, blocked="read the install's logs",
                          need="grant log access", need_kind="access", grant="read_logs")]}))
        assert asked["status"] == "needs_you"
        assert board.get(c, item["id"])["lease_owner"] == INFRA, "the asking turn's lease"
        _grant(c, project, "infra_steward")                    # granted elsewhere: withdrawn
        cos.reconsider(c, project, mode="propose")
        back = board.get(c, item["id"])
    assert back["lifecycle"] == "proposed" and back["needs_you_decision_id"] is None
    assert back["lease_owner"] is None, "back on the board means free: the lease went with it"
    report = owners.work(eng, settings(), project_id=project, limit=1,
                         model=ScriptedSquadModel({}), launchers=Launchers(), rounds=1)
    [worked] = [w for w in report["worked"] if w["work_item_id"] == item["id"]]
    assert worked["kind"] == "squad" and worked["turns"] >= 1, report


def _spent(c, pid, *, finding=None):
    """The live item: its squad seated and handed over, $1.53 spent of $1.50."""
    item = _restarts_item(c, pid)
    _handed(c, item)
    if finding:
        collab.post(c, item["id"], kind="finding", sender=INFRA, body={"summary": finding})
    c.execute(text("UPDATE work_items SET cost_usd_micros = :m WHERE id = :w"),
              {"m": squad_turns.SQUAD_BUDGET_USD_MICROS + 26_945, "w": item["id"]})
    return item


def _tick(eng, pid):
    from boss_engine.services.ambient import owners

    from ._squad_support import Launchers

    return owners.work(eng, settings(), project_id=pid, model=ScriptedSquadModel({}),
                       launchers=Launchers(), rounds=1)


def test_a_spent_budget_asks_a_person_on_the_projects_one_card(eng, project):  # noqa: F811
    from boss_engine.services import decision_actions
    from boss_engine.services.ambient import owners

    with eng.begin() as c:
        other = _restarts_item(c, project)                     # the project's card is open
        card_id = asks.raise_card(c, project, [asks.question_entry(
            c, other, actor=INFRA, reason=REASON, blocked="read the logs",
            need="send the restart times")])
        item = _spent(c, project, finding="The restarts line up with the upgrades at 19:43")
        assert owners.due_items(c, project_id=project) == [], "it takes no slot"
    report = _tick(eng, project)
    assert not [w for w in report["worked"] if w["work_item_id"] == item["id"]]
    with eng.begin() as c:
        card = _card(c, card_id)
        waiting = board.get(c, item["id"])
        cards = c.execute(text("SELECT count(*) FROM decisions WHERE project_id = :p AND "
                               "status = 'pending'"), {"p": project}).scalar()
    assert cards == 1 and waiting["needs_you_decision_id"] == card_id, "joins the ONE card"
    [entry] = [e for e in card["recommendation"]["batch"] if e["work_item_id"] == item["id"]]
    assert entry["question"] == ("Infra Steward's squad used its $1.50 on “Updates restarted a "
                                 "build 2 times in an hour” — give it $1 more, or close it?")
    assert entry["detail"] == "Found so far: The restarts line up with the upgrades at 19:43"
    alone = asks.card_options([entry])
    assert [o["label"] for o in alone] == ["Give it $1 more", "Close it", "I'll take it"]
    shown = decision_actions.allowed_actions({**card, "options": alone, "status": "pending"})
    assert [(a["action"], a["label"]) for a in shown][:3] == [
        ("approve", "Give it $1 more"), ("reject", "Close it"), ("revise", "I'll take it")]
    # Asked once: the next tick neither asks again nor works it.
    _tick(eng, project)
    with eng.begin() as c:
        again = c.execute(text("SELECT count(*) FROM decisions WHERE project_id = :p AND "
                               "status = 'pending'"), {"p": project}).scalar()
    assert again == 1


def _budget_card(eng, pid):
    with eng.begin() as c:
        _ceo(c, pid)
        item = _spent(c, pid)
    _tick(eng, pid)
    with eng.begin() as c:
        return item, board.get(c, item["id"])["needs_you_decision_id"]


def test_give_it_more_raises_the_budget_and_the_squad_is_owed_its_turn(eng, project):  # noqa: F811
    from boss_engine.services.ambient import owners

    item, card_id = _budget_card(eng, project)
    _approve(eng, card_id)
    with eng.begin() as c:
        after = board.get(c, item["id"])
        due = owners.due_items(c, project_id=project)
    assert after["squad_budget_usd_micros"] == 2_500_000 and after["lifecycle"] != "needs_you"
    assert due and due[0]["id"] == item["id"] and due[0].get("owed"), "its next turn is owed"
    report = _tick(eng, project)
    assert [w for w in report["worked"] if w["work_item_id"] == item["id"]][0]["turns"] >= 1


def test_close_it_resolves_and_ill_take_it_hands_it_over(eng, project):  # noqa: F811
    from boss_engine.services.decisions import DecisionService

    item, card_id = _budget_card(eng, project)
    with eng.begin() as c:
        DecisionService(get_settings()).transition(c, card_id, action="reject", actor="ceo",
                                                   note="expected: our upgrades")
        closed = board.get(c, item["id"])
    assert closed["lifecycle"] == "resolved"
    other, other_card = _budget_card(eng, project)
    with eng.begin() as c:
        DecisionService(get_settings()).transition(c, other_card, action="revise", actor="ceo",
                                                   note="I'll look")
        taken = board.get(c, other["id"])
    assert taken["lease_kind"] == "person"


def test_a_spent_budget_card_withdraws_once_its_item_is_moot(eng, project):  # noqa: F811
    item, card_id = _budget_card(eng, project)
    with eng.begin() as c:
        c.execute(text("UPDATE work_items SET squad_budget_usd_micros = 3000000 WHERE id = :w"),
                  {"w": item["id"]})                           # funded elsewhere
        done = cos.reconsider(c, project, mode="propose")
        card = _card(c, card_id)
    assert [s["verdict"] for r in done for s in r.get("settled", [])] == ["funded"]
    assert card["status"] == "withdrawn"


def test_a_read_phase_past_its_spend_cap_still_answers(eng, project, monkeypatch):  # noqa: F811
    from boss_engine.services import workflow_precheck_agent

    monkeypatch.setattr(workflow_precheck_agent, "_turn_cost", lambda usage: 200_000)
    with eng.begin() as c:
        item = _restarts_item(c, project)
        _grant(c, project, "infra_steward")
        model = ReadingModel("install_restarts")
        turn = squad_turns.take_turn(c, settings(), item["id"], INFRA, model=model)
    assert model.results, "it read once"
    assert model.offered[-1] == [], "the turn past the cap is asked without tools"
    assert turn["status"] == "posted", turn
