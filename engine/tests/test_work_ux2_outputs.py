"""BOSSAPP-882: outputs and cross-references on a work item.

* mentions are parsed from the title and description (an item key, a PR URL, a BOSS id, any
  other link as a document) and replaced when the text changes; manual refs stay;
* an item's backlinks are the items that mention it;
* a run that completes fills its item's outputs (its PR, preview, deploy); the status is read
  live from the source on every read; a person may record one by hand.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from boss_engine.services import work_items, work_outputs

from ._ambient_support import eng, project  # noqa: F401 — fixtures used by name


def test_parse_mentions_finds_keys_prs_ids_and_links():
    found = work_outputs.parse_mentions(
        "Blocked on WI-12", "See https://github.com/acme/app/pull/7, the spec "
        "https://docs.acme.dev/checkout and dec_8b573b40; WI-12 again; crun_abcdef12.")
    assert found == [
        ("work_item", "WI-12", "WI-12"),          # the title first; WI-12 again is the same ref
        ("pull_request", "https://github.com/acme/app/pull/7", "https://github.com/acme/app/pull/7"),
        ("document", "https://docs.acme.dev/checkout", "https://docs.acme.dev/checkout"),
        ("decision", "dec_8b573b40", "dec_8b573b40"),
        ("code_task", "crun_abcdef12", "crun_abcdef12"),
    ]
    assert work_outputs.parse_mentions(None, "") == []


def test_mentions_become_refs_and_backlinks_and_follow_the_text(eng, project):
    with eng.begin() as c:
        api = work_items.create_item(c, project_id=project, title="Payments API")
        form = work_items.create_item(
            c, project_id=project, title="Card form",
            description=f"Waits for {api['human_key']}; PR https://github.com/acme/app/pull/7 "
                        "and NOPE-99 (no such item)")
        refs = work_outputs.refs_of(c, form["id"])
        assert [(r["to_kind"], r["to_ref"]) for r in refs] == [
            ("pull_request", "https://github.com/acme/app/pull/7"), ("work_item", api["id"])]
        assert refs[1]["title"] == "Payments API"
        assert [b["id"] for b in work_outputs.backlinks(c, api["id"])] == [form["id"]]
        work_outputs.add_ref(c, project_id=project, item_id=form["id"], to_kind="document",
                             to_ref="https://wiki/x", actor="person:ceo")
        # The text changes: mentions are read again, the manual ref stays.
        work_items.update_item(c, project_id=project, item_id=form["id"],
                               fields={"description": "nothing now"}, actor="person:ceo")
        assert [(r["to_kind"], r["source"]) for r in work_outputs.refs_of(c, form["id"])] == [
            ("document", "manual")]
        assert work_outputs.backlinks(c, api["id"]) == []
        with pytest.raises(work_outputs.OutputError):
            work_outputs.add_ref(c, project_id=project, item_id=form["id"], to_kind="planet",
                                 to_ref="x", actor="person:ceo")


def test_a_completed_run_fills_the_outputs_and_status_is_read_live(eng, project):
    with eng.begin() as c:
        item = work_items.create_item(c, project_id=project, title="Wallet button")
        rid = f"crun_{uuid.uuid4().hex[:8]}"
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, title, status, pull_request_url, "
            " pull_request_number, work_item_id, spec) VALUES (:i, :p, 'Wallet button', "
            " 'completed', 'https://github.com/acme/app/pull/9', 9, :w, CAST(:s AS JSONB))"),
            {"i": rid, "p": project, "w": item["id"], "s": json.dumps({})})
        pid = f"prev_{uuid.uuid4().hex[:8]}"
        c.execute(text("INSERT INTO app_previews (id, project_id, run_id, url, status) VALUES "
                       "(:i, :p, :r, 'http://preview.local:8001', 'running')"),
                  {"i": pid, "p": project, "r": rid})
        work_items.on_run_completed(c, kind="code_task", run_id=rid)
        got = work_outputs.view(c, project, item["id"])
        kinds = {o["kind"]: o for o in got["outputs"]}
        assert kinds["pull_request"]["title"] == "PR #9 · Wallet button"
        assert kinds["pull_request"]["status"] == "completed"
        assert kinds["preview"]["status"] == "running"
        # The preview stops: the list says so without collecting again.
        c.execute(text("UPDATE app_previews SET status = 'stopped' WHERE id = :i"), {"i": pid})
        assert {o["kind"]: o["status"] for o in work_outputs.outputs_of(c, item["id"])}[
            "preview"] == "stopped"
        # Collecting again is idempotent; a person adds a document by hand.
        assert work_outputs.collect(c, project_id=project, item_id=item["id"]) == 2
        work_outputs.record(c, project_id=project, item_id=item["id"], kind="document",
                            ref="https://docs/wallet", title="Design", actor="person:ceo")
        assert len(work_outputs.outputs_of(c, item["id"])) == 3
        with pytest.raises(work_outputs.OutputError):
            work_outputs.record(c, project_id=project, item_id=item["id"], kind="poem",
                                ref="x", actor="person:ceo")
