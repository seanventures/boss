"""BOSSAPP-880: a person's watchdog on a work tree.

* conditions are evaluated over the root and every descendant: stopped with work open, blocked,
  the root closed, nothing moved for stall_hours;
* it fires ONCE per fingerprint (an event + one outbox message per contact point), never twice
  for the same stop, and again once the subtree moved on and stopped somewhere new;
* where it tells: the contact points chosen, else the project's Needs-you route (console);
* a bad condition or a contact point from elsewhere is refused.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from boss_engine.services import work_items, work_watchdogs

from ._ambient_support import eng, project  # noqa: F401 — fixtures used by name

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


def _row(c, wid):
    return dict(c.execute(text("SELECT * FROM work_item_watchdogs WHERE id = :i"),
                          {"i": wid}).mappings().one())


def _messages(c, wid):
    return c.execute(text("SELECT contact_point_id, subject, body FROM notifications "
                          "WHERE group_key = :g ORDER BY created_at"),
                     {"g": f"watchdog:{wid}"}).mappings().all()


def test_evaluate_is_pure_and_names_what_matched():
    t = NOW - timedelta(hours=2)
    items = [{"id": "r", "human_key": "WI-1", "status": "todo", "updated_at": t},
             {"id": "a", "human_key": "WI-2", "status": "blocked", "updated_at": t},
             {"id": "b", "human_key": "WI-3", "status": "done", "updated_at": t}]
    hits = work_watchdogs.evaluate(items, "r", work_watchdogs.CONDITIONS, stall_hours=1, now=NOW)
    assert [h["condition"] for h in hits] == ["stopped_unfinished", "blocked", "stalled"]
    assert [i["id"] for i in hits[1]["items"]] == ["a"]
    items[0]["status"] = "active"   # something in flight: not stopped, not blocked-only
    hits = work_watchdogs.evaluate(items, "r", ["stopped_unfinished", "stalled"], stall_hours=3,
                                   now=NOW)
    assert hits == []
    closed = [{"id": "r", "status": "done", "updated_at": t}]
    assert [h["condition"] for h in work_watchdogs.evaluate(
        closed, "r", ["closed", "stopped_unfinished"], stall_hours=1, now=NOW)] == ["closed"]


def test_it_fires_once_per_stop_and_again_after_the_tree_moves_on(eng, project):
    with eng.begin() as c:
        epic = work_items.create_item(c, project_id=project, title="Checkout epic")
        story = work_items.create_item(c, project_id=project, title="Card form",
                                       parent_id=epic["id"], status="active")
        dog = work_watchdogs.attach(c, project_id=project, root_item_id=epic["id"],
                                    watcher="person:ceo", conditions=["stopped_unfinished"],
                                    instructions="check the tests really ran")
        # Work in flight: nothing to tell.
        assert work_watchdogs.check(c, _row(c, dog["id"]), now=NOW) is None
        work_items.set_status(c, project_id=project, item_id=story["id"], status="todo",
                              actor="person:ceo")
        fired = work_watchdogs.check(c, _row(c, dog["id"]), now=NOW)
        assert fired and fired["conditions"] == ["stopped_unfinished"] and fired["messages"] == 1
        sent = _messages(c, dog["id"])
        assert len(sent) == 1 and "stopped with work still open" in sent[0]["subject"]
        assert "check the tests really ran" in sent[0]["body"]
        # The same stop is never told twice.
        assert work_watchdogs.check(c, _row(c, dog["id"]), now=NOW + timedelta(hours=1)) is None
        assert len(_messages(c, dog["id"])) == 1
        # It moves on (the fingerprint clears), then stops again: told again.
        work_items.set_status(c, project_id=project, item_id=story["id"], status="active",
                              actor="person:ceo")
        assert work_watchdogs.check(c, _row(c, dog["id"]), now=NOW) is None
        assert _row(c, dog["id"])["last_fingerprint"] is None
        work_items.set_status(c, project_id=project, item_id=story["id"], status="blocked",
                              actor="person:ceo")
        assert work_watchdogs.check(c, _row(c, dog["id"]), now=NOW)
        assert len(_messages(c, dog["id"])) == 2
        assert _row(c, dog["id"])["fired_count"] == 2
        events = c.execute(text("SELECT count(*) FROM events WHERE project_id = :p AND "
                                "event_type = 'work_item.watchdog_fired'"),
                           {"p": project}).scalar()
        assert events == 2


def test_the_sweep_checks_every_enabled_watchdog(eng, project):
    with eng.begin() as c:
        root = work_items.create_item(c, project_id=project, title="Done but not")
        work_items.set_status(c, project_id=project, item_id=root["id"], status="cancelled",
                              actor="person:ceo")
        dog = work_watchdogs.attach(c, project_id=project, root_item_id=root["id"],
                                    watcher="person:ceo", conditions=["closed"])
        paused = work_watchdogs.attach(c, project_id=project, root_item_id=root["id"],
                                       watcher="person:cfo", conditions=["closed"], enabled=False)
    got = work_watchdogs.sweep(eng, now=NOW)
    assert got["fired"] >= 1
    with eng.begin() as c:
        assert _row(c, dog["id"])["fired_count"] == 1
        assert _row(c, paused["id"])["fired_count"] == 0
        assert [w["watcher"] for w in work_watchdogs.of_item(c, project, root["id"])] == [
            "person:ceo", "person:cfo"]
        work_watchdogs.detach(c, project_id=project, watchdog_id=paused["id"], actor="ceo")
        assert len(work_watchdogs.of_item(c, project, root["id"])) == 1


def test_bad_watchdogs_are_refused(eng, project):
    with eng.begin() as c:
        root = work_items.create_item(c, project_id=project, title="x")
        with pytest.raises(work_watchdogs.WatchdogError):
            work_watchdogs.attach(c, project_id=project, root_item_id=root["id"],
                                  watcher="person:ceo", conditions=["explodes"])
        with pytest.raises(work_watchdogs.WatchdogError):
            work_watchdogs.attach(c, project_id=project, root_item_id=root["id"],
                                  watcher="person:ceo", contact_point_ids=["cp_elsewhere"])
        with pytest.raises(LookupError):
            work_watchdogs.attach(c, project_id=project, root_item_id="wi_nope",
                                  watcher="person:ceo")
