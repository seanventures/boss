"""BOSSAPP-881: one search over runs, builds, decisions, PRD panels, artifacts, work items and
knowledge.

* the content is searched, not only names: a PRD's markdown, a decision's summary, a build's
  instructions, an artifact's content;
* a title hit ranks before a body hit; every hit has a snippet and a console link;
* only the sources asked for (the caller's readable ones) are searched; another project's rows
  never come back; a restricted artifact is found by its title only;
* a one-character query searches nothing.
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from boss_engine.services import work_items, work_search
from boss_engine.services.core import create_blocking_decision

from ._ambient_support import eng, make_project, project, purge  # noqa: F401 — fixtures used by name


def _meeting(c, pid, title, prd):
    mid = f"mrun_{uuid.uuid4().hex[:8]}"
    c.execute(text("INSERT INTO meeting_runs (id, project_id, directive, title, state) VALUES "
                   "(:i, :p, :d, :t, CAST(:s AS JSONB))"),
              {"i": mid, "p": pid, "d": title, "t": title,
               "s": json.dumps({"prd_markdown": prd})})
    return mid


def _artifact(c, pid, title, content, boundary="internal"):
    aid = f"art_{uuid.uuid4().hex[:8]}"
    c.execute(text("INSERT INTO artifacts (id, project_id, artifact_type, title, content, "
                   "data_boundary) VALUES (:i, :p, 'doc', :t, CAST(:c AS JSONB), :b)"),
              {"i": aid, "p": pid, "t": title, "c": json.dumps(content), "b": boundary})
    return aid


def _build(c, pid, title, instructions):
    rid = f"crun_{uuid.uuid4().hex[:8]}"
    c.execute(text("INSERT INTO code_task_runs (id, project_id, title, spec) VALUES "
                   "(:i, :p, :t, CAST(:s AS JSONB))"),
              {"i": rid, "p": pid, "t": title, "s": json.dumps({"instructions": instructions})})
    return rid


def test_content_is_searched_across_sources_with_snippets_and_links(eng, project):
    with eng.begin() as c:
        mid = _meeting(c, project, "Checkout PRD",
                       "## Goals\nAccept Apple Pay at checkout, with a fallback to cards.")
        did = create_blocking_decision(c, project, None, "Ship the wallet flow?",
                                       {"action": "decide", "summary": "Apple Pay needs a merchant id"},
                                       {}, "test", risk_tier=1)
        bid = _build(c, project, "Wallet button", "Render the Apple Pay button on checkout")
        aid = _artifact(c, project, "Apple Pay notes", {"text": "merchant validation"})
        hid = _artifact(c, project, "Wallet secrets", {"text": "Apple Pay key"}, "restricted")
        item = work_items.create_item(c, project_id=project, title="Apple Pay rollout")
        got = work_search.search(c, project_id=project, q="apple pay")
    by_id = {h["id"]: h for h in got["items"]}
    assert {mid, did, bid, aid, item["id"]} <= set(by_id)
    assert hid not in by_id                           # restricted content is never matched
    assert by_id[mid]["kind"] == "meeting" and "Accept Apple Pay at checkout" in by_id[mid]["snippet"]
    assert by_id[did]["match"] == "body" and "merchant id" in by_id[did]["snippet"]
    assert by_id[item["id"]]["match"] == "title"
    assert by_id[bid]["href"].startswith("?project_id=") and "build=" in by_id[bid]["href"]
    # Title hits first.
    matches = [h["match"] for h in got["items"]]
    assert matches == sorted(matches, key=lambda m: m != "title")
    assert got["counts"]["meeting"] == 1


def test_only_the_asked_sources_and_only_this_project(eng, project):
    other = make_project(eng)
    try:
        with eng.begin() as c:
            _meeting(c, other, "Other PRD", "zebra crossing")
            mine = _meeting(c, project, "Mine", "zebra stripes")
            did = create_blocking_decision(c, project, None, "Zebra?", {"action": "decide"}, {},
                                           "test", risk_tier=1)
            got = work_search.search(c, project_id=project, q="zebra", kinds=["meeting"])
            assert [h["id"] for h in got["items"]] == [mine] and got["searched"] == ["meeting"]
            assert did not in {h["id"] for h in got["items"]}
            assert work_search.search(c, project_id=project, q="z")["items"] == []
    finally:
        purge(eng, other)


def test_a_snippet_is_cut_around_the_match():
    assert work_search.snippet("alpha " * 50 + "the quick brown fox", "brown fox").startswith("…")
    assert work_search.snippet("", "x") == ""
