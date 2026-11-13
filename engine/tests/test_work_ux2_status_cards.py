"""BOSSAPP-883: status cards and stored summaries.

* a card's document is composed from BOSS's own rows (counts, blocked, in review, outputs) and
  stored as numbered revisions; an unchanged document writes no revision;
* the schedule refreshes a due card; an item closing refreshes the cards of it and its
  ancestors, and the item keeps its own closing summary;
* only deterministic cards are built (a model mode is refused, in words).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from boss_engine.services import status_cards, work_items

from ._ambient_support import eng, project  # noqa: F401 — fixtures used by name


def test_a_card_stores_revisions_only_when_its_document_changes(eng, project):
    with eng.begin() as c:
        epic = work_items.create_item(c, project_id=project, title="Checkout epic")
        story = work_items.create_item(c, project_id=project, title="Card form",
                                       parent_id=epic["id"])
        card = status_cards.create(c, project_id=project, title="Checkout", actor="person:ceo",
                                   work_item_id=epic["id"], refresh_minutes=30)
        assert card["revision"] == 1 and card["pinned"]
        assert "0 of 2 done; 2 open" in card["document"]
        assert status_cards.refresh(c, project_id=project, card_id=card["id"]) is None
        work_items.set_status(c, project_id=project, item_id=story["id"], status="blocked",
                              actor="person:ceo")
        assert status_cards.refresh(c, project_id=project, card_id=card["id"]) == 2
        latest = status_cards.get(c, project, card["id"])
        assert "Blocked: " in latest["document"] and latest["facts"]["blocked"] == [story["human_key"]]
        assert [r["revision"] for r in status_cards.revisions(c, project, card["id"])] == [2, 1]


def test_closing_refreshes_the_cards_above_and_keeps_a_closing_summary(eng, project):
    with eng.begin() as c:
        epic = work_items.create_item(c, project_id=project, title="Epic")
        story = work_items.create_item(c, project_id=project, title="Story", parent_id=epic["id"])
        card = status_cards.create(c, project_id=project, title="Epic status", actor="person:ceo",
                                   work_item_id=epic["id"], refresh_minutes=0)
        # Make the close strictly later than the first revision (one transaction, one now()).
        c.execute(text("UPDATE status_card_revisions SET created_at = created_at - interval '1 minute' "
                       "WHERE card_id = :c"), {"c": card["id"]})
        work_items.update_item(c, project_id=project, item_id=story["id"],
                               fields={"status": "done"}, actor="person:ceo")
        latest = status_cards.get(c, project, card["id"])
        assert latest["revision"] == 2 and latest["trigger"] == "close"
        assert "1 of 2 done" in latest["document"]
        assert "Closed since last time: " + story["human_key"] in latest["document"]
        mine = status_cards.list_cards(c, project, work_item_id=story["id"])
        assert [(x["kind"], x["pinned"]) for x in mine] == [("closing", False)]
        assert mine[0]["document"].startswith("### Closing summary:")


def test_the_schedule_refreshes_a_due_card(eng, project):
    with eng.begin() as c:
        card = status_cards.create(c, project_id=project, title="Project", actor="person:ceo",
                                   refresh_minutes=15)
        c.execute(text("UPDATE status_cards SET next_refresh_at = now() - interval '1 minute' "
                       "WHERE id = :i"), {"i": card["id"]})
        work_items.create_item(c, project_id=project, title="New thing")
    got = status_cards.sweep(eng, now=datetime.now(timezone.utc) + timedelta(seconds=1))
    assert got["due"] >= 1
    with eng.begin() as c:
        latest = status_cards.get(c, project, card["id"])
        assert latest["revision"] == 2 and latest["trigger"] == "schedule"
        assert "0 of 1 done" in latest["document"]


def test_only_deterministic_cards_are_built(eng, project):
    with eng.begin() as c:
        with pytest.raises(status_cards.CardError, match="model-written"):
            status_cards.create(c, project_id=project, title="x", actor="person:ceo",
                                mode="model")
        with pytest.raises(LookupError):
            status_cards.create(c, project_id=project, title="x", actor="person:ceo",
                                work_item_id="wi_nope")
