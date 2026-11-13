"""DOCUMENTS WITH REVISIONS, CONFLICT DETECTION AND ANCHORED COMMENTS (BOSSAPP-874).

* Re-anchoring is pure and says what happened to each passage: exact, moved, fuzzy, orphaned.
* Saves are append-only revisions from a named base: a stale base is a conflict carrying the
  revision that won; an unchanged save adds nothing; a restore is a new revision; a revision row
  can never be changed; someone else's edit lock refuses a save.
* A comment thread on a passage survives the rewrite around it, and the open threads are what
  the next writer is handed.
* The architecture path: each architect version is the next revision of one document; a person's
  edit and comment reach the architect's revision prompt; a person's save made WHILE the
  architect was writing is a conflict (the revision is written again next tick), never lost.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from boss_engine.config import get_settings
from boss_engine.services import architecture_docs as arch
from boss_engine.services import documents
from boss_engine.services.decisions import DecisionService

from .test_architecture_approval import (  # noqa: F401 — fixtures used by name
    _clean, _delivery, _panel_finished, _person, _review_of, _settle, architect, eng, project,
    tools,
)
from .test_workflow_capability_runner import Ctx  # noqa: F401


BODY = ("# Plan\n\nWe store tickets in Postgres.\n\nEach tenant gets its own schema.\n\n"
        "Search uses trigram indexes.\n")


# ── re-anchoring (pure) ──────────────────────────────────────────────────────────────────────

def test_a_quote_is_re_anchored_exact_moved_fuzzy_or_orphaned():
    start = BODY.index("Each tenant")
    quote = "Each tenant gets its own schema."
    pre, suf = BODY[max(0, start - 32):start], BODY[start + len(quote):start + len(quote) + 32]

    same = documents.locate(BODY, BODY, quote=quote, prefix=pre, suffix=suf, start=start)
    assert same["state"] == "exact" and same["start"] == start

    moved_body = "# Plan\n\nIntro paragraph added on top.\n\n" + BODY[len("# Plan\n\n"):]
    moved = documents.locate(BODY, moved_body, quote=quote, prefix=pre, suffix=suf, start=start)
    assert moved["state"] == "moved" and moved_body[moved["start"]:moved["end"]] == quote

    fuzzy_body = BODY.replace("Each tenant gets its own schema.", "Each tenant gets its own DB schema.")
    fuzzy = documents.locate(BODY, fuzzy_body, quote=quote, prefix=pre, suffix=suf, start=start)
    assert fuzzy["state"] == "fuzzy" and "own DB schema" in fuzzy["quote"]

    gone_body = BODY.replace("Each tenant gets its own schema.\n\n", "")
    gone = documents.locate(BODY, gone_body, quote=quote, prefix=pre, suffix=suf, start=start)
    assert gone["state"] == "orphaned" and gone["start"] is None


def test_of_two_identical_passages_the_one_with_the_same_context_wins():
    body = "alpha: retry later\n\nbeta: retry later\n"
    at = body.index("beta: ") + len("beta: ")
    pre = body[max(0, at - 32):at]
    new = "intro\n" + body
    spot = documents.locate(body, new, quote="retry later", prefix=pre, suffix="\n", start=at)
    assert new[spot["start"] - len("beta: "):spot["start"]] == "beta: "


# ── revisions ────────────────────────────────────────────────────────────────────────────────

def test_saves_need_their_base_and_history_is_append_only(eng, project):
    with eng.begin() as c:
        doc = documents.create_document(c, project_id=project, title="Plan", body=BODY,
                                        kind="plan", actor="person:ceo")
        r1 = doc["current_revision_id"]
        assert doc["current_revision_no"] == 1 and doc["current"]["body"] == BODY
        saved = documents.save_revision(c, project_id=project, document_id=doc["id"],
                                        body=BODY + "\nMore.\n", base_revision_id=r1,
                                        actor="person:ceo")
        assert saved["saved"] and saved["current_revision_no"] == 2
        # A save from revision 1 after revision 2 exists is a conflict carrying revision 2.
        with pytest.raises(documents.DocumentConflict) as stale:
            documents.save_revision(c, project_id=project, document_id=doc["id"], body="mine",
                                    base_revision_id=r1, actor="agent:pm")
        assert stale.value.current["revision_no"] == 2 and "More." in stale.value.current["body"]
        # The same body again is not a revision.
        again = documents.save_revision(c, project_id=project, document_id=doc["id"],
                                        body=BODY + "\nMore.\n",
                                        base_revision_id=saved["current_revision_id"],
                                        actor="person:ceo")
        assert not again["saved"] and again["current_revision_no"] == 2
        restored = documents.restore(c, project_id=project, document_id=doc["id"], revision_no=1,
                                     base_revision_id=saved["current_revision_id"], actor="person:ceo")
        assert restored["current_revision_no"] == 3 and restored["current"]["body"] == BODY
        assert restored["current"]["restored_from"] == r1
        change = documents.diff(c, project, doc["id"], from_no=2, to_no=3)
        assert change["removed"] == 2 and any(ln == "-More." for ln in change["lines"])
    with pytest.raises(DBAPIError, match="append-only"):
        with eng.begin() as c:
            c.execute(text("UPDATE document_revisions SET body = 'x' WHERE id = :r"), {"r": r1})


def test_someone_elses_lock_refuses_a_save_until_it_lapses(eng, project):
    with eng.begin() as c:
        doc = documents.create_document(c, project_id=project, title="PRD", body="v1", kind="prd",
                                        actor="person:ceo")
        documents.lock(c, project_id=project, document_id=doc["id"], actor="person:ceo")
        with pytest.raises(documents.DocumentLocked):
            documents.save_revision(c, project_id=project, document_id=doc["id"], body="v2",
                                    base_revision_id=doc["current_revision_id"], actor="agent:pm")
        own = documents.save_revision(c, project_id=project, document_id=doc["id"], body="v2",
                                      base_revision_id=doc["current_revision_id"], actor="person:ceo")
        assert own["locked_by"] == "person:ceo"
        c.execute(text("UPDATE documents SET locked_until = now() - interval '1 minute' WHERE id = :d"),
                  {"d": doc["id"]})
        later = documents.save_revision(c, project_id=project, document_id=doc["id"], body="v3",
                                        base_revision_id=own["current_revision_id"], actor="agent:pm")
        assert later["current_revision_no"] == 3 and later["locked_by"] is None


def test_a_thread_survives_the_rewrite_and_reaches_the_next_writer(eng, project):
    with eng.begin() as c:
        doc = documents.create_document(c, project_id=project, title="Plan", body=BODY,
                                        kind="plan", actor="person:ceo")
        at = BODY.index("Each tenant")
        thread = documents.add_thread(
            c, project_id=project, document_id=doc["id"], revision_id=doc["current_revision_id"],
            start=at, end=at + len("Each tenant gets its own schema."),
            body="Row-level security instead?", actor="person:ceo")
        assert thread["quote"] == "Each tenant gets its own schema." and thread["anchor_state"] == "exact"
        gone = documents.add_thread(
            c, project_id=project, document_id=doc["id"], revision_id=doc["current_revision_id"],
            start=BODY.index("Search"), end=BODY.index("Search") + len("Search uses trigram indexes."),
            body="Why trigram?", actor="person:ceo")
        with pytest.raises(documents.DocumentError):
            documents.add_thread(c, project_id=project, document_id=doc["id"],
                                 revision_id=doc["current_revision_id"], start=5, end=5,
                                 body="nothing selected", actor="person:ceo")
        rewrite = ("# Plan\n\nA new opening.\n\nWe store tickets in Postgres.\n\n"
                   "Each tenant gets its own schema.\n")
        documents.save_revision(c, project_id=project, document_id=doc["id"], body=rewrite,
                                base_revision_id=doc["current_revision_id"], actor="agent:architect",
                                author_kind="agent")
        now = {t["id"]: t for t in documents.get_document(c, project, doc["id"])["threads"]}
        assert now[thread["id"]]["anchor_state"] == "moved"
        assert rewrite[now[thread["id"]]["start"]:now[thread["id"]]["end"]] == thread["quote"]
        assert now[gone["id"]]["anchor_state"] == "orphaned"
        documents.reply(c, project_id=project, document_id=doc["id"], thread_id=thread["id"],
                        body="Agreed, RLS.", actor="person:cto")
        brief = documents.open_threads_brief(c, doc["id"])
        assert 'on "Each tenant gets its own schema."' in brief and "Agreed, RLS." in brief
        assert "the passage it was on is gone" in brief
        documents.set_thread_status(c, project_id=project, document_id=doc["id"],
                                    thread_id=gone["id"], resolved=True, actor="person:ceo")
        assert "Why trigram" not in documents.open_threads_brief(c, doc["id"])


# ── the architecture document ────────────────────────────────────────────────────────────────

def test_the_architect_revises_the_document_people_edited_and_answers_its_comments(
        eng, project, tools, architect):
    decisions = DecisionService(get_settings())
    with eng.begin() as c:
        _person(c, project)
        run, meeting_run = _delivery(c, project, tools, "arch_doc")
        from boss_engine.services import workflow_capability_runner as runner

        runner.tick(c, Ctx(eng), project_id=project)
        _panel_finished(c, project, meeting_run)
        _settle(c, eng, project, run["id"])
        first = _review_of(c, run["id"])
        doc = documents.for_source(c, project, f"architecture_review:{first['id']}")
        assert doc and doc["kind"] == "architecture" and "draft 1" in doc["body"]
        edited = doc["body"] + "\n\nPERSON-EDIT: queue writes through an outbox.\n"
        saved = documents.save_revision(c, project_id=project, document_id=doc["id"], body=edited,
                                        base_revision_id=doc["current_revision_id"], actor="ceo")
        at = edited.index("PERSON-EDIT")
        documents.add_thread(c, project_id=project, document_id=doc["id"],
                             revision_id=saved["current_revision_id"], start=at, end=at + 11,
                             body="THREAD-ASK: name the outbox table.", actor="ceo")
        decisions.transition(c, first["decision_id"], action="revise", actor="ceo",
                             note="Address the comments.")
        _settle(c, eng, project, run["id"], times=1)
        second = _review_of(c, run["id"])
        assert second["current_version"] == 2
        prompt = architect.prompts[1]
        assert "PERSON-EDIT: queue writes through an outbox." in prompt
        assert "THREAD-ASK: name the outbox table." in prompt and 'on "PERSON-EDIT"' in prompt
        after = documents.get_document(c, project, doc["id"])
        assert after["current_revision_no"] == 3
        assert after["current"]["author_kind"] == "agent" and "draft 2" in after["current"]["body"]
        assert after["current"]["base_revision_id"] == saved["current_revision_id"]


def test_a_person_saving_while_the_architect_writes_is_a_conflict_not_a_loss(
        eng, project, tools, architect, monkeypatch):
    decisions = DecisionService(get_settings())
    with eng.begin() as c:
        _person(c, project)
        run, meeting_run = _delivery(c, project, tools, "arch_doc_race")
        from boss_engine.services import workflow_capability_runner as runner

        runner.tick(c, Ctx(eng), project_id=project)
        _panel_finished(c, project, meeting_run)
        _settle(c, eng, project, run["id"])
        first = _review_of(c, run["id"])
        doc = documents.for_source(c, project, f"architecture_review:{first['id']}")
        decisions.transition(c, first["decision_id"], action="revise", actor="ceo", note="More detail.")
        real = architect.create_text_response

        def racing(prompt, system_prompt=None):
            # The person saves while the architect is still writing.
            documents.save_revision(c, project_id=project, document_id=doc["id"],
                                    body="RACING PERSON EDIT", base_revision_id=doc["current_revision_id"],
                                    actor="ceo")
            return real(prompt, system_prompt=system_prompt)

        monkeypatch.setattr(architect, "create_text_response", racing)
        out = arch.revise_sent_back(c, get_settings(), project_id=project, run_id=run["id"])
        assert out[0]["outcome"] == "architecture_revision_conflict"
        review = _review_of(c, run["id"])
        assert review["status"] == "revision_requested" and review["current_version"] == 1
        assert "edited while the architect wrote" in review["error"]
        current = documents.get_document(c, project, doc["id"])
        assert current["current"]["body"] == "RACING PERSON EDIT"
        monkeypatch.setattr(architect, "create_text_response", real)
        again = arch.revise_sent_back(c, get_settings(), project_id=project, run_id=run["id"])
        assert again[0]["outcome"] == "architecture_revised"
        assert "RACING PERSON EDIT" in architect.prompts[-1]
