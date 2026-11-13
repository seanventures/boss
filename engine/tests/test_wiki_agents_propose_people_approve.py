"""BOSSAPP-891: the project wiki. People edit; agents propose and a person approves the exact diff;
the maintainer renders the knowledge graph and never writes over a person.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import text

from ._ops2_support import decide, fresh_engine, make_project
from boss_engine.services import change_consent, wiki
from boss_engine.services.decisions import InvalidDecisionTransitionError

AGENT = "agent:pm"


@pytest.fixture(scope="module")
def eng():
    engine, drop = fresh_engine("wiki")
    yield engine
    drop()


@pytest.fixture()
def project(eng):
    return make_project(eng)


def _save(eng, project, slug, title, body, actor="ceo"):
    with eng.begin() as c:
        return wiki.save_page(c, project_id=project, actor=actor, slug=slug, title=title,
                              body=body)


def _fact(eng, project, kind, key, name, body, tier="observed"):
    with eng.begin() as c:
        c.execute(text("INSERT INTO kg_nodes (id, project_id, kind, node_key, name, body, "
                       "trust_tier, source) VALUES (:i, :p, :k, :key, :n, :b, :t, 'test')"),
                  {"i": f"n_{project}_{key}", "p": project, "k": kind, "key": key, "n": name,
                   "b": body, "t": tier})


def test_a_person_edit_is_a_revision_and_the_same_content_is_not(eng, project):
    first = _save(eng, project, "runbook", "Runbook", "Restart the API.\n")
    again = _save(eng, project, "runbook", "Runbook", "Restart the API.\n")
    second = _save(eng, project, "runbook", "Runbook", "Restart the API, then the worker.\n")
    assert first["changed"] and not again["changed"] and second["changed"]
    with eng.begin() as c:
        revs = wiki.list_revisions(c, project, "runbook")
        assert [r["revision_no"] for r in revs] == [2, 1]
        assert revs[0]["author_kind"] == "person" and revs[0]["current"]
        restored = wiki.restore(c, project_id=project, actor="ceo", slug="runbook", revision=1)
    assert restored["revision"]["revision_no"] == 3
    assert restored["revision"]["body"] == "Restart the API.\n"
    assert restored["revision"]["restored_from"] == revs[1]["id"]


def test_an_agent_cannot_write_a_page_by_itself(eng, project):
    with pytest.raises(change_consent.ConsentRequired):
        _save(eng, project, "deploys", "Deploys", "Ship on Fridays.", actor=AGENT)
    with eng.begin() as c:
        assert c.execute(text("SELECT count(*) FROM wiki_revisions WHERE project_id = :p"),
                         {"p": project}).scalar() == 0


def test_an_approved_proposal_writes_exactly_that_page_once(eng, project):
    _save(eng, project, "deploys", "Deploys", "Deploy with boss upgrade.\nNever on Fridays.\n")
    with eng.begin() as c:
        proposal = wiki.propose_patch(
            c, project_id=project, actor=AGENT, slug="deploys",
            edits=[{"find": "Never on Fridays.", "replace": "Never after 4pm on Fridays."}],
            note="the team relaxed the rule")
        row = c.execute(text("SELECT question, recommendation FROM decisions WHERE id = :i"),
                        {"i": proposal["decision_id"]}).mappings().one()
    assert proposal["status"] == "pending"
    assert "Never after 4pm" in row["question"] and row["recommendation"]["action"] == \
        "apply_wiki_patch"
    with eng.begin() as c:   # nothing written yet
        assert wiki.get_page(c, project, "deploys")["revision"]["revision_no"] == 1
        assert len(wiki.get_page(c, project, "deploys")["pending"]) == 1
    decide(eng, proposal["decision_id"], "approve")
    with eng.begin() as c:
        page = wiki.get_page(c, project, "deploys")
        consent = change_consent.for_decision(c, proposal["decision_id"])
    assert page["body"] == "Deploy with boss upgrade.\nNever after 4pm on Fridays.\n"
    assert page["revision"]["author_kind"] == "agent" and page["revision"]["author_id"] == AGENT
    assert page["revision"]["consent_id"] == consent["id"] and consent["status"] == "consumed"
    # The same consent cannot be spent twice.
    with pytest.raises(change_consent.ConsentRequired):
        with eng.begin() as c:
            wiki.save_page(c, project_id=project, actor=AGENT, slug="deploys",
                           title="Deploys", body=page["body"] + "x")


def test_a_page_changed_after_the_proposal_refuses_the_approval(eng, project):
    _save(eng, project, "oncall", "On call", "Page Alice.\n")
    with eng.begin() as c:
        proposal = wiki.propose_patch(c, project_id=project, actor=AGENT, slug="oncall",
                                      body="Page Bob.\n")
    _save(eng, project, "oncall", "On call", "Page Carol.\n")   # a person moved the base
    with pytest.raises(InvalidDecisionTransitionError, match="changed after this was proposed"):
        decide(eng, proposal["decision_id"], "approve")
    with eng.begin() as c:
        assert wiki.get_page(c, project, "oncall")["body"] == "Page Carol.\n"


def test_a_rejected_proposal_can_never_be_spent(eng, project):
    with eng.begin() as c:
        proposal = wiki.propose_patch(c, project_id=project, actor=AGENT, slug="new-page",
                                      title="New page", body="Hello.\n")
    decide(eng, proposal["decision_id"], "reject", note="not needed")
    with eng.begin() as c:
        assert change_consent.for_decision(c, proposal["decision_id"])["status"] == "rejected"
        with pytest.raises(wiki.PageNotFound):
            wiki.get_page(c, project, "new-page")


def test_links_read_back_as_backlinks(eng, project):
    _save(eng, project, "deploys", "Deploys", "See [[runbook]] first.")
    _save(eng, project, "runbook", "Runbook", "Restart.")
    with eng.begin() as c:
        page = wiki.get_page(c, project, "runbook")
    assert page["backlinks"] == [{"slug": "deploys", "title": "Deploys"}]


def test_the_maintainer_renders_facts_and_proposes_over_a_person(eng, project):
    _fact(eng, project, "service", "api", "Orders API", "Takes orders.", tier="approved")
    _fact(eng, project, "service", "worker", "Worker", "Runs jobs.")
    _fact(eng, project, "symbol", "x.py:f", "f", "code is not rendered")
    with eng.begin() as c:
        first = wiki.maintain(c, project)
        page = wiki.get_page(c, project, "kg-service")
    assert set(first["written"]) == {"kg-service", "knowledge"}
    assert "**Orders API** (approved" in page["body"] and "Worker" in page["body"]
    assert page["body"].index("Orders API") < page["body"].index("Worker")
    assert page["maintained_by"] == "kg" and len(page["revision"]["sources"]) == 2
    with eng.begin() as c:
        assert wiki.maintain(c, project)["unchanged"] == ["kg-service", "knowledge"]
    # A person adds to the page; the next refresh is proposed, never written over them.
    _save(eng, project, "kg-service", "Knowledge: Service", page["body"] + "\nOwned by Ops.\n")
    _fact(eng, project, "service", "billing", "Billing", "Bills.")
    with eng.begin() as c:
        third = wiki.maintain(c, project)
        after = wiki.get_page(c, project, "kg-service")
    assert third["proposed"] == ["kg-service"]
    assert "Owned by Ops." in after["body"] and "Billing" not in after["body"]
    assert len(after["pending"]) == 1


def test_agent_tools_read_and_propose(eng, project):
    from boss_engine.tools.wiki_tools import _propose, _read

    _save(eng, project, "deploys", "Deploys", "Deploy.\n")
    ctx = SimpleNamespace(begin=eng.begin)
    listed = _read(ctx, project_id=project, actor=AGENT, args={})
    assert listed["ok"] and listed["pages"][0]["slug"] == "deploys"
    proposed = _propose(ctx, project_id=project, actor=AGENT,
                        args={"slug": "deploys", "body": "Deploy carefully.\n"})
    assert proposed["ok"] and proposed["status"] == "pending" and "NOT written" in proposed["note"]
    refused = _propose(ctx, project_id=project, actor=AGENT,
                       args={"slug": "deploys", "edits": [{"find": "nope", "replace": "x"}]})
    assert not refused["ok"] and "exactly once" in refused["error"]
