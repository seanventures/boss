"""THE PERSONAL ATTENTION INBOX, RANKED BY BLOCKED WORK (BOSSAPP-875).

* One feed: decisions waiting on a person (delegated to them or naming them as a stage
  participant), work they answer for, and a failed run of that work.
* `mine` keeps what names you and what names nobody; another person's decision is not yours.
* Ranking: what blocks the most open work (transitively) comes first; an overdue deadline and a
  higher risk tier lift an entry; each says why in a few words.
* Read state, snooze (lapses on its own) and archive are the person's own.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from boss_engine.services import inbox, work_items

from ._ambient_support import eng, project  # noqa: F401 — fixtures used by name


def _decision(c, pid, question, *, delegated_to=None, participants=None, risk=1, deadline=None,
              work_item_id=None):
    did = f"dec_{uuid.uuid4().hex[:10]}"
    rec = {"action": "x", **({"participants": participants} if participants else {}),
           **({"work_item_id": work_item_id} if work_item_id else {})}
    c.execute(text(
        "INSERT INTO decisions (id, project_id, question, status, risk_tier, deadline_at, "
        "delegated_to, recommendation) VALUES (:i, :p, :q, :s, :r, :d, :dt, CAST(:rec AS JSONB))"),
        {"i": did, "p": pid, "q": question, "s": "delegated" if delegated_to else "pending",
         "r": risk, "d": deadline, "dt": delegated_to, "rec": json.dumps(rec)})
    return did


def test_rank_is_pure_and_says_why():
    now = datetime(2026, 11, 13, tzinfo=timezone.utc)
    score, why = inbox.rank({"risk_tier": 3, "deadline_at": now - timedelta(hours=1)},
                            blocked=4, mine=True, now=now)
    assert why == ["overdue", "high risk", "blocks 4", "for you"]
    low, _ = inbox.rank({"risk_tier": 0}, blocked=0, mine=False, now=now)
    assert score > low == 0


def test_the_feed_is_mine_and_ranked_by_what_it_blocks(eng, project):
    with eng.begin() as c:
        # The schema item blocks the API, which blocks the UI: finishing it unblocks two.
        schema = work_items.create_item(c, project_id=project, title="Schema",
                                        responsible_person="person:ceo")
        api = work_items.create_item(c, project_id=project, title="API")
        ui = work_items.create_item(c, project_id=project, title="UI")
        work_items.add_relation(c, project_id=project, item_id=schema["id"], other_id=api["id"],
                                kind="blocks", actor="person:ceo")
        work_items.add_relation(c, project_id=project, item_id=api["id"], other_id=ui["id"],
                                kind="blocks", actor="person:ceo")
        docs = work_items.create_item(c, project_id=project, title="Docs",
                                      assignee="person:ceo")
        other = work_items.create_item(c, project_id=project, title="Not mine",
                                       responsible_person="person:cfo")
        stage = _decision(c, project, "Approve: Schema", participants=["person:ceo"], risk=2,
                          work_item_id=schema["id"])
        mine_delegated = _decision(c, project, "Pick a vendor", delegated_to="ceo", risk=1)
        anyone = _decision(c, project, "Rotate keys?", risk=1)
        theirs = _decision(c, project, "Budget for Q1", delegated_to="cfo", risk=3)
        late = _decision(c, project, "Sign the DPA", participants=["person:ceo"], risk=1,
                         deadline=datetime.now(timezone.utc) - timedelta(hours=2))

        feed = inbox.inbox(c, project, actor="ceo")
        keys = [e["item_key"] for e in feed["items"]]
        assert f"decision:{theirs}" not in keys and f"work_item:{other['id']}" not in keys
        assert {f"decision:{mine_delegated}", f"decision:{anyone}", f"work_item:{docs['id']}",
                f"work_item:{schema['id']}"} <= set(keys)
        # The overdue one first; then the stage decision on the item that blocks two, ahead of
        # everything that blocks nothing.
        assert [e["item_key"] for e in feed["items"][:2]] == [f"decision:{late}", f"decision:{stage}"]
        assert "overdue" in feed["items"][0]["why"]
        top = feed["items"][1]
        assert top["blocked_count"] == 2 and "blocks 2" in top["why"] and "for you" in top["why"]
        rest = [e["item_key"] for e in feed["items"][2:]]
        assert rest.index(f"work_item:{schema['id']}") < rest.index(f"decision:{mine_delegated}")
        anyone_entry = next(e for e in feed["items"] if e["item_key"] == f"decision:{anyone}")
        assert anyone_entry["for_you"] is False
        # Without `mine`, the whole project's feed.
        everyone = [e["item_key"] for e in inbox.inbox(c, project, actor="ceo", mine=False)["items"]]
        assert f"decision:{theirs}" in everyone
        # A failed run of an item I answer for is in my feed.
        from .test_one_work_item_graph_with_execution_policy import _publish, _start

        run = _start(c, project, _publish(c, project)["id"], work_item_id=docs["id"])
        c.execute(text("UPDATE workflow_runs SET status = 'failed' WHERE id = :r"), {"r": run["id"]})
        failed = inbox.inbox(c, project, actor="ceo", kinds=["failed_run"])["items"]
        assert [e["ref_id"] for e in failed] == [run["id"]] and "failed" in failed[0]["why"]


def test_read_snooze_and_archive_are_mine(eng, project):
    with eng.begin() as c:
        a = _decision(c, project, "A?", delegated_to="ceo")
        b = _decision(c, project, "B?", delegated_to="ceo")
        d = _decision(c, project, "D?", delegated_to="ceo")
        inbox.mark_read(c, project, actor="ceo", item_keys=[f"decision:{a}"])
        feed = {e["item_key"]: e for e in inbox.inbox(c, project, actor="ceo")["items"]}
        assert feed[f"decision:{a}"]["read"] and not feed[f"decision:{b}"]["read"]
        # Someone else's read state is theirs.
        cto = {e["item_key"]: e for e in inbox.inbox(c, project, actor="cto", mine=False)["items"]}
        assert not cto[f"decision:{a}"]["read"]
        now = datetime.now(timezone.utc)
        inbox.snooze(c, project, actor="ceo", item_key=f"decision:{b}", minutes=60, now=now)
        inbox.snooze(c, project, actor="ceo", item_key=f"decision:{d}", archive=True)
        feed = inbox.inbox(c, project, actor="ceo")
        keys = [e["item_key"] for e in feed["items"]]
        assert f"decision:{b}" not in keys and f"decision:{d}" not in keys
        assert feed["snoozed"] == 1 and feed["archived"] == 1
        # The snooze lapses on its own; the archive stays until cleared.
        later = inbox.inbox(c, project, actor="ceo", now=now + timedelta(minutes=61))
        assert f"decision:{b}" in [e["item_key"] for e in later["items"]]
        assert f"decision:{d}" not in [e["item_key"] for e in later["items"]]
        inbox.snooze(c, project, actor="ceo", item_key=f"decision:{d}", clear=True)
        assert f"decision:{d}" in [e["item_key"] for e in inbox.inbox(c, project, actor="ceo")["items"]]
        import pytest

        with pytest.raises(inbox.InboxError):
            inbox.snooze(c, project, actor="ceo", item_key=f"decision:{a}", minutes=0)
        with pytest.raises(inbox.InboxError):
            inbox.snooze(c, project, actor="ceo", item_key="bogus:1", minutes=5)
