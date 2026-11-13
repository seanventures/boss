"""PRIVATE WORK ITEMS WITH INHERITED GRANTS AND AN AUDITED BREAK-GLASS READ (BOSSAPP-876).

* A private item and everything under it is readable only by its parties (creator, assignee,
  responsible person) and grant holders; a grant on the private epic reaches its stories; the
  assignee of a PUBLIC root above a private subtree gets nothing from that.
* One predicate (identity/authz.work_item_visible_sql) hides them from the tree, a direct read
  (404, like a missing item), the inbox and the document list; a blocker that is private stays
  an edge but says only "Private item".
* BOSS's own reads (no viewer) see everything.
* Break-glass: a reason, a recorded row and event, the item returned once, a notice on the item
  its parties see — and the reader's next ordinary read is refused again.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import documents, inbox, work_items

from ._ambient_support import eng, project  # noqa: F401 — fixtures used by name


def _walk(nodes):
    out = set()
    for n in nodes:
        out.add(n["id"])
        out |= _walk(n["children"])
    return out


def test_privacy_flows_down_and_grants_flow_with_it(eng, project):
    with eng.begin() as c:
        root = work_items.create_item(c, project_id=project, title="Company", assignee="person:pm",
                                      actor="person:pm")
        hr = work_items.create_item(c, project_id=project, title="HR case", parent_id=root["id"],
                                    actor="person:hr", visibility="private")
        story = work_items.create_item(c, project_id=project, title="Interview notes",
                                       parent_id=hr["id"], actor="person:hr")
        public = work_items.create_item(c, project_id=project, title="Ship v2", actor="person:pm")
        # BOSS itself sees everything.
        assert {hr["id"], story["id"]} <= _walk(work_items.tree(c, project)["items"])

        work_items.view_as(c, "pm")
        seen = _walk(work_items.tree(c, project)["items"])
        assert root["id"] in seen and public["id"] in seen
        assert hr["id"] not in seen and story["id"] not in seen
        with pytest.raises(LookupError):
            work_items.get_item(c, project, story["id"])
        with pytest.raises(LookupError):
            work_items.update_item(c, project_id=project, item_id=hr["id"],
                                   fields={"title": "x"}, actor="person:pm")
        assert work_items.get_item(c, project, root["id"])["children"] == []

        work_items.view_as(c, "hr")       # the creator
        assert {hr["id"], story["id"]} <= _walk(work_items.tree(c, project)["items"])
        work_items.grant_access(c, project_id=project, item_id=hr["id"], party="person:legal",
                                actor="person:hr")
        work_items.view_as(c, "legal")    # a grant on the private parent reaches the story
        assert work_items.get_item(c, project, story["id"])["restricted"] is True
        work_items.view_as(c, "hr")
        work_items.revoke_access(c, project_id=project, item_id=hr["id"], party="person:legal",
                                 actor="person:hr")
        work_items.view_as(c, "legal")
        with pytest.raises(LookupError):
            work_items.get_item(c, project, story["id"])


def test_a_private_blocker_is_an_edge_without_a_name(eng, project):
    with eng.begin() as c:
        secret = work_items.create_item(c, project_id=project, title="Acquisition due diligence",
                                        actor="person:cfo", visibility="private")
        launch = work_items.create_item(c, project_id=project, title="Launch", actor="person:pm")
        work_items.add_relation(c, project_id=project, item_id=launch["id"], other_id=secret["id"],
                                kind="blocked_by", actor="person:cfo")
        work_items.view_as(c, "pm")
        got = work_items.get_item(c, project, launch["id"])
        assert got["status"] == "blocked"
        assert got["blocked_by"] == [{"id": secret["id"], "human_key": None,
                                      "title": "Private item", "status": "todo", "private": True}]
        assert got["relations"][0]["item"]["title"] == "Private item"
        # Linking to an item you cannot see is refused like a missing one.
        with pytest.raises(LookupError):
            work_items.add_relation(c, project_id=project, item_id=launch["id"],
                                    other_id=secret["id"], kind="relates_to", actor="person:pm")


def test_inbox_and_documents_follow_the_same_predicate(eng, project):
    with eng.begin() as c:
        secret = work_items.create_item(c, project_id=project, title="Payroll", actor="person:cfo",
                                        visibility="private", responsible_person="person:cfo")
        did = f"dec_{uuid.uuid4().hex[:10]}"
        c.execute(text(
            "INSERT INTO decisions (id, project_id, question, status, recommendation) VALUES "
            "(:i, :p, 'Approve: Payroll', 'pending', CAST(:r AS JSONB))"),
            {"i": did, "p": project, "r": json.dumps({"work_item_id": secret["id"]})})
        doc = documents.create_document(c, project_id=project, title="Payroll plan", body="x",
                                        work_item_id=secret["id"], actor="person:cfo")
        work_items.view_as(c, "pm")
        assert did not in [e["ref_id"] for e in inbox.inbox(c, project, actor="pm", mine=False)["items"]]
        assert doc["id"] not in [d["id"] for d in documents.list_documents(c, project)]
        with pytest.raises(LookupError):
            documents.get_document(c, project, doc["id"])
        work_items.view_as(c, "cfo")
        assert did in [e["ref_id"] for e in inbox.inbox(c, project, actor="cfo", mine=False)["items"]]
        assert doc["id"] in [d["id"] for d in documents.list_documents(c, project)]


def test_break_glass_is_one_audited_read_with_a_visible_notice(eng, project):
    with eng.begin() as c:
        secret = work_items.create_item(c, project_id=project, title="Incident: data leak",
                                        actor="person:sec", visibility="private")
        work_items.view_as(c, "auditor")
        with pytest.raises(work_items.WorkItemError):
            work_items.break_glass_read(c, project_id=project, item_id=secret["id"],
                                        actor="auditor", reason="short")
        got = work_items.break_glass_read(c, project_id=project, item_id=secret["id"],
                                          actor="auditor", reason="Regulator request REF-221")
        assert got["break_glass"] and got["title"] == "Incident: data leak"
        # Only once: the auditor's next ordinary read is refused again.
        with pytest.raises(LookupError):
            work_items.get_item(c, project, secret["id"])
        row = c.execute(text(
            "SELECT actor, reason FROM work_item_break_glass_reads WHERE work_item_id = :w"),
            {"w": secret["id"]}).mappings().one()
        assert row["actor"] == "auditor" and "REF-221" in row["reason"]
        event = c.execute(text(
            "SELECT data FROM events WHERE entity_id = :w AND event_type = 'work_item.break_glass_read'"),
            {"w": secret["id"]}).scalar()
        assert event is not None and "Incident" not in json.dumps(event)
        # The item's own people see who read it and why.
        work_items.view_as(c, "sec")
        notice = work_items.get_item(c, project, secret["id"])["break_glass_reads"]
        assert notice[0]["actor"] == "auditor" and "REF-221" in notice[0]["reason"]
