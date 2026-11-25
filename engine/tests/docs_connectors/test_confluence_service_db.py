"""Confluence end to end on a real database and recorded HTTP (BOSSAPP-1384): the org-wide
connection stored by reference, the agent tools (publish is a proposal), the approval that writes
a versioned page (and refuses a moved one), and the knowledge sync with provenance."""
from __future__ import annotations

import base64
import json
import os

import pytest
from sqlalchemy import text

os.environ.setdefault("BOSS_MASTER_KEY",
                      base64.b64encode(b"p29a-test-master-key-32-bytes!!!").decode())

from boss_engine.connectors.tickets import http as ticket_http  # noqa: E402
from boss_engine.identity import vault  # noqa: E402
from boss_engine.services import confluence_knowledge, confluence_publish, docs_connections as docs  # noqa: E402
from boss_engine.services.decisions import InvalidDecisionTransitionError  # noqa: E402

from .._ops2_support import decide, fresh_engine, make_project  # noqa: E402
from .fake_confluence import EMAIL, SITE, TOKEN, FakeConfluence  # noqa: E402


@pytest.fixture(scope="module")
def eng():
    engine, drop = fresh_engine("confl")
    vault.reset_master_key_cache()
    yield engine
    drop()


@pytest.fixture()
def fake(monkeypatch):
    transport = FakeConfluence()
    monkeypatch.setattr(ticket_http, "TRANSPORT", transport)
    monkeypatch.setattr(ticket_http, "SLEEP", lambda s: None)
    return transport


@pytest.fixture(autouse=True)
def _manual(monkeypatch):
    """Publish and sync run in the background in production; tests drive them themselves."""
    monkeypatch.setattr(confluence_publish, "DISPATCH", "manual")
    monkeypatch.setattr(confluence_knowledge, "DISPATCH", "manual")


def _reconcile(eng, project):
    return confluence_publish.reconcile_pending(eng, _settings(), project_id=project)


def _sync(eng, project, **kw):
    job = confluence_knowledge.start(eng, _settings(), project_id=project, actor="ceo", **kw)
    return confluence_knowledge.run(eng, _settings(), project, job["id"])


@pytest.fixture()
def project(eng):
    return make_project(eng)


def _settings():
    from boss_engine.config import get_settings

    return get_settings()


class Ctx:
    def __init__(self, engine):
        self.engine, self.settings = engine, _settings()

    def begin(self):
        return self.engine.begin()


def _connect(eng, project, token=TOKEN, **kw):
    with eng.begin() as c:
        config, tok = docs.prepare(c, _settings(), project_id=project, base_url=SITE,
                                   email=EMAIL, token=token, **kw)
    outcome = docs.run_test(config, tok)
    with eng.begin() as c:
        return docs.store(c, _settings(), project_id=project, config=config, token=tok,
                          outcome=outcome, actor="ceo", default_spaces=["ENG"])


def _tool(name):
    from boss_engine.tools import build_default_registry

    return build_default_registry().get(name).handler


def _decision(eng, decision_id):
    with eng.begin() as c:
        return c.execute(text("SELECT status, question, recommendation FROM decisions WHERE id = :i"),
                         {"i": decision_id}).mappings().one()


@pytest.fixture(autouse=True)
def _clean(eng):
    yield
    with eng.begin() as c:
        c.execute(text("DELETE FROM docs_connections"))


# ---- the connection -------------------------------------------------------------------------

def test_the_connection_is_org_wide_and_keeps_only_a_reference(eng, fake, project):
    row = _connect(eng, project)
    cfg = row["configuration"]
    assert cfg["secret_ref_id"] and cfg["last4"] == TOKEN[-4:] and cfg["acts_as"] == "BOSS Bot"
    assert TOKEN not in json.dumps(cfg, default=str)
    with eng.begin() as c:
        stored = c.execute(text("SELECT configuration FROM docs_connections")).scalar()
        events = c.execute(text("SELECT data FROM events WHERE project_id = :p"),
                           {"p": project}).scalars().all()
        sibling = make_project(eng)
        other_org = "org_other_confl"
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'x') ON CONFLICT DO NOTHING"),
                  {"o": other_org})
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_other_confl', :o, "
                       "'x') ON CONFLICT DO NOTHING"), {"o": other_org})
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                       "('p_other_confl', 'ws_other_confl', 'o', 'p_other_confl')"
                       " ON CONFLICT DO NOTHING"))
    assert TOKEN not in json.dumps(stored, default=str)
    assert TOKEN not in json.dumps(events, default=str)
    with eng.begin() as c:
        # another project of the SAME org reads through it; another org's project does not
        _row, client = docs.client_for(c, _settings(), sibling)
        assert client.search("x")
        with pytest.raises(docs.ConfluenceNotConnected):
            docs.client_for(c, _settings(), "p_other_confl")
        view = docs.public_view(c, docs.get_row(c, sibling), sibling, can_manage=False)
    assert view["reach"] == "org" and view["last4"] is None and view["home_project"]["id"] == project


def test_connect_refuses_in_words_and_stores_nothing(eng, fake, project):
    with eng.begin() as c:
        for kw, snippet in (
            ({"deployment": "datacenter", "base_url": SITE, "email": EMAIL, "token": TOKEN}, "Data Center"),
            ({"base_url": "http://x.atlassian.net", "email": EMAIL, "token": TOKEN}, "https"),
            ({"base_url": SITE, "email": "", "token": TOKEN}, "email"),
            ({"base_url": SITE, "email": EMAIL, "use_jira_credential": True}, "No Jira"),
        ):
            with pytest.raises(docs.DocsConnectionError) as caught:
                docs.prepare(c, _settings(), project_id=project, **kw)
            assert snippet in str(caught.value)
    with pytest.raises(docs.DocsConnectionError) as caught:
        docs.run_test({"base_url": SITE, "email": EMAIL, "auth_method": "basic"}, "wrong-token")
    assert caught.value.code == "auth_failed" and "Reconnect Confluence in Setup" in str(caught.value)
    with eng.begin() as c:
        assert c.execute(text("SELECT count(*) FROM docs_connections")).scalar() == 0


def test_the_jira_credential_can_be_reused_by_choice(eng, fake, project):
    from boss_engine.services.secret_backends import default_secret_backend
    from boss_engine.services.secrets_service import put_secret

    with eng.begin() as c:
        ref = put_secret(c, _settings(), project_id=project, name="ticket_jira_token", value=TOKEN,
                         backend=default_secret_backend(_settings()), actor="ceo")["secret_ref_id"]
        cfg = {"base_url": "https://acme.atlassian.net", "email": EMAIL, "ticket_system": True,
               "secret_ref_id": ref, "project_key": "KAN"}
        c.execute(text("INSERT INTO work_tracker_providers (id, project_id, provider_key, "
                       "display_name, status, is_default, configuration, created_by) VALUES "
                       "('trk_confl1', :p, 'jira', 'Jira', 'active', true, CAST(:c AS JSONB), 't')"),
                  {"p": project, "c": json.dumps(cfg)})
    row = _connect(eng, project, token="", use_jira_credential=True)
    assert row["configuration"]["credential_from"] == "jira"
    assert row["configuration"]["base_url"] == SITE and row["configuration"]["email"] == EMAIL
    with eng.begin() as c:   # its own vault entry: usable when Jira is gone
        c.execute(text("DELETE FROM work_tracker_providers WHERE id = 'trk_confl1'"))
        _r, client = docs.client_for(c, _settings(), project)
        assert client.read(page_id="101").title == "Deploy Runbook"


def test_disconnect_removes_the_reference_and_agents_are_told_to_connect(eng, fake, project):
    row = _connect(eng, project)
    with eng.begin() as c:
        ref = row["configuration"]["secret_ref_id"]
        docs.disconnect(c, row, "ceo", _settings())
        after = docs.get_row(c, project, include_disconnected=True)
        revoked = c.execute(text("SELECT status FROM secret_refs WHERE id = :i"), {"i": ref}).scalar()
    assert revoked == "revoked"      # the vault entry this connection stored is gone
    assert after["status"] == "disconnected" and "secret_ref_id" not in after["configuration"]
    assert after["configuration"]["default_spaces"] == ["ENG"]      # kept for a reconnect
    fake.requests.clear()
    got = _tool("confluence_search")(Ctx(eng), project_id=project, actor="agent:pm",
                                     args={"query": "deploy"})
    assert got["ok"] is False and got["error"] == "not_connected"
    assert "Connect Confluence in Setup › Connections" in got["message"]
    assert fake.requests == []


# ---- agent tools ----------------------------------------------------------------------------

def test_search_and_read_tools_use_default_spaces_and_the_screen(eng, fake, project):
    _connect(eng, project)
    got = _tool("confluence_search")(Ctx(eng), project_id=project, actor="agent:pm",
                                     args={"query": "deploy", "limit": 3})
    assert got["ok"] and got["count"] == 3 and got["spaces"] == ["ENG"]
    assert got["pages"][0]["url"].startswith(SITE)
    assert 'space in ("ENG")' in [r for r in fake.requests if "search" in r["path"]][0]["query"]["cql"]
    page = _tool("confluence_read_page")(Ctx(eng), project_id=project, actor="agent:pm",
                                         args={"page_id": "101", "include": ["attachments", "tree"]})
    assert page["ok"] and page["page"]["version"] == 4 and "# Deploy Runbook" in page["page"]["body_markdown"]
    assert page["attachments"][0]["name"] == "diagram.png" and len(page["tree"]) == 2
    nothing = _tool("confluence_read_page")(Ctx(eng), project_id=project, actor="a", args={})
    assert nothing["ok"] is False and nothing["error"] == "invalid_request"


def test_publish_is_a_proposal_nothing_is_written_until_approved(eng, fake, project):
    from boss_engine.tools import build_default_registry

    spec = build_default_registry().get("confluence_publish_page")
    assert spec.kind == "write" and spec.queues_for_review and spec.reversibility == "safe"
    assert spec.permission == "docs:publish"
    _connect(eng, project)
    got = _tool("confluence_publish_page")(
        Ctx(eng), project_id=project, actor="agent:docs_writer",
        args={"space": "ENG", "title": "Deploy Runbook", "body": "# Deploy Runbook\n\nNew steps.\n",
              "note": "the steps changed"})
    assert got["ok"] and got["status"] == "pending" and got["operation"] == "update"
    assert "NOT published" in got["note"]
    assert fake.pages_created == [] and fake.pages_updated == []        # no write happened
    decision = _decision(eng, got["decision_id"])
    assert decision["recommendation"]["action"] == "apply_confluence_publish"
    assert "Update Confluence page 'Deploy Runbook'" in decision["question"]
    assert "the steps changed" in decision["question"] and decision["status"] == "pending"
    again = _tool("confluence_publish_page")(
        Ctx(eng), project_id=project, actor="agent:docs_writer",
        args={"space": "ENG", "title": "Deploy Runbook", "body": "# Deploy Runbook\n\nNew steps.\n"})
    assert again["decision_id"] == got["decision_id"] and again.get("duplicate")
    decide(eng, got["decision_id"], "approve")
    assert fake.pages_updated == []          # approving only records the intent and commits...
    with eng.begin() as c:
        assert c.execute(text("SELECT status FROM change_consents WHERE decision_id = :d"),
                         {"d": got["decision_id"]}).scalar() == "approved"
    done = _reconcile(eng, project)                   # ...the write follows, outside any transaction
    assert done[0]["ok"] and done[0]["version"] == 5
    assert _reconcile(eng, project) == []             # nothing left to do, and nothing done twice
    assert len(fake.pages_updated) == 1
    sent = fake.pages_updated[0]
    assert sent["version"]["number"] == 5 and "<p>New steps.</p>" in sent["body"]["value"]
    with eng.begin() as c:
        consent = c.execute(text("SELECT status FROM change_consents WHERE decision_id = :d"),
                            {"d": got["decision_id"]}).scalar()
        event = c.execute(text("SELECT data FROM events WHERE project_id = :p AND "
                               "event_type = 'docs.confluence_published'"), {"p": project}).scalar()
    assert consent == "consumed" and event["version"] == 5 and event["op"] == "update"


def test_a_page_that_moved_since_the_proposal_is_not_overwritten(eng, fake, project):
    _connect(eng, project)
    got = _tool("confluence_publish_page")(
        Ctx(eng), project_id=project, actor="agent:docs_writer",
        args={"page_id": "101", "space": "ENG", "body": "mine"})
    assert got["status"] == "pending"
    fake.versions["101"] = 7                 # a person edited the page in Confluence meanwhile
    decide(eng, got["decision_id"], "approve")
    done = _reconcile(eng, project)
    assert done[0]["ok"] is False and "version 7" in done[0]["error"] and "new proposal" in done[0]["error"]
    assert fake.pages_updated == []
    with eng.begin() as c:
        assert c.execute(text("SELECT status FROM change_consents WHERE decision_id = :d"),
                         {"d": got["decision_id"]}).scalar() == "rejected"
        failed = c.execute(text("SELECT data FROM events WHERE project_id = :p AND "
                                "event_type = 'docs.confluence_publish_failed'"), {"p": project}).scalar()
    assert "version 7" in failed["error"]


def test_the_write_is_not_made_inside_the_decisions_transaction(eng, fake, project, monkeypatch):
    """The approval commits first; a failure to reach Confluence cannot roll the decision back."""
    _connect(eng, project)
    got = _tool("confluence_publish_page")(
        Ctx(eng), project_id=project, actor="agent:docs_writer",
        args={"page_id": "101", "space": "ENG", "body": "mine"})
    fake.requests.clear()
    decide(eng, got["decision_id"], "approve")
    assert fake.requests == []               # no HTTP at all during the approval
    assert _decision(eng, got["decision_id"])["status"] == "approved"


def test_an_unfinished_claim_is_taken_over_after_it_goes_stale(eng, fake, project):
    _connect(eng, project)
    got = _tool("confluence_publish_page")(
        Ctx(eng), project_id=project, actor="a", args={"page_id": "101", "space": "ENG", "body": "mine"})
    decide(eng, got["decision_id"], "approve")
    cid = got["consent_id"]
    with eng.begin() as c:       # a worker claimed it and died
        c.execute(text("UPDATE change_consents SET proposed = jsonb_set(proposed, '{_meta,claimed_at}', "
                       "to_jsonb(now()::text)) WHERE id = :i"), {"i": cid})
    assert _reconcile(eng, project) == []
    with eng.begin() as c:
        c.execute(text("UPDATE change_consents SET proposed = jsonb_set(proposed, '{_meta,claimed_at}', "
                       "to_jsonb((now() - interval '1 hour')::text)) WHERE id = :i"), {"i": cid})
    assert _reconcile(eng, project)[0]["ok"] is True


def test_a_rename_shows_old_and_new_title_in_the_diff(eng, fake, project):
    _connect(eng, project)
    got = _tool("confluence_publish_page")(
        Ctx(eng), project_id=project, actor="a",
        args={"page_id": "101", "space": "ENG", "title": "Deploy Handbook", "body": "# Deploy Runbook\n\nx"})
    with eng.begin() as c:
        q = c.execute(text("SELECT question FROM decisions WHERE id = :i"), {"i": got["decision_id"]}).scalar()
    assert "title: 'Deploy Runbook' -> 'Deploy Handbook'" in q


def test_create_a_new_page_under_a_parent_on_approval(eng, fake, project):
    _connect(eng, project)
    got = _tool("confluence_publish_page")(
        Ctx(eng), project_id=project, actor="agent:docs_writer",
        args={"space": "ENG", "title": "Brand New", "body": "Hello", "parent_id": "101"})
    assert got["operation"] == "create" and fake.pages_created == []
    decide(eng, got["decision_id"], "approve")
    assert _reconcile(eng, project)[0]["ok"]
    assert fake.pages_created[0]["parentId"] == "101" and fake.pages_created[0]["title"] == "Brand New"


def test_a_refused_proposal_writes_nothing_and_cannot_be_spent(eng, fake, project):
    _connect(eng, project)
    got = _tool("confluence_publish_page")(
        Ctx(eng), project_id=project, actor="agent:docs_writer",
        args={"page_id": "101", "space": "ENG", "body": "mine"})
    decide(eng, got["decision_id"], "reject")
    with eng.begin() as c:
        assert c.execute(text("SELECT status FROM change_consents WHERE decision_id = :d"),
                         {"d": got["decision_id"]}).scalar() == "rejected"
    assert fake.pages_updated == [] and fake.pages_created == []


def test_publish_refuses_without_a_connection_and_unchanged_bodies(eng, fake, project):
    got = _tool("confluence_publish_page")(Ctx(eng), project_id=project, actor="a",
                                           args={"space": "ENG", "title": "T", "body": "x"})
    assert got["ok"] is False and got["error"] == "not_connected"
    _connect(eng, project)
    page = _tool("confluence_read_page")(Ctx(eng), project_id=project, actor="a",
                                         args={"page_id": "101", "max_chars": 60000})
    same = _tool("confluence_publish_page")(Ctx(eng), project_id=project, actor="a", args={
        "page_id": "101", "body": page["page"]["body_markdown"]})
    assert same["status"] == "unchanged"


# ---- knowledge ------------------------------------------------------------------------------

def test_sync_indexes_pages_with_provenance_and_resyncs_only_what_changed(eng, fake, project):
    _connect(eng, project)
    with eng.begin() as c:
        docs.update_settings(c, docs.get_row(c, project), actor="ceo", knowledge_spaces=["ENG"])
    first = _sync(eng, project)
    # the fake's search returns pages 101, 102, 103; only 101 has a page body fixture
    space = first["spaces"]["ENG"]
    assert first["status"] == "done" and space["status"] == "done"
    assert space["indexed"] == 1 and len(space["failed"]) == 2 and space["seen"] == 3
    with eng.begin() as c:
        node = c.execute(text("SELECT name, attrs, source, trust_tier FROM kg_nodes WHERE "
                              "project_id = :p AND node_key = 'confluence:acme.atlassian.net:101'"),
                         {"p": project}).mappings().one()
        capture = c.execute(text("SELECT source_kind FROM kg_captures WHERE project_id = :p"),
                            {"p": project}).mappings().all()
    attrs = node["attrs"]
    assert node["name"] == "Deploy Runbook" and node["source"] == "confluence"
    assert attrs["space"] == "ENG" and attrs["page_id"] == "101" and attrs["version"] == 4
    assert attrs["url"] == f"{SITE}/spaces/ENG/pages/101/Deploy+Runbook" and attrs["labels"] == ["runbook", "prod"]
    assert attrs["last_modified"] and [c["source_kind"] for c in capture] == ["confluence"]
    again = _sync(eng, project, full=True)
    assert again["spaces"]["ENG"]["unchanged"] == 1 and again["spaces"]["ENG"]["indexed"] == 0
    fake.versions["101"] = 5
    third = _sync(eng, project, full=True)
    with eng.begin() as c:
        v = c.execute(text("SELECT attrs->>'version' FROM kg_nodes WHERE project_id = :p AND "
                           "node_key = 'confluence:acme.atlassian.net:101'"), {"p": project}).scalar()
    assert third["spaces"]["ENG"]["updated"] == 1 and v == "5"


def test_the_sync_is_one_job_with_progress_and_it_resumes(eng, fake, project):
    _connect(eng, project)
    with eng.begin() as c:
        docs.update_settings(c, docs.get_row(c, project), actor="ceo", knowledge_spaces=["ENG", "OPS"])
    job = confluence_knowledge.start(eng, _settings(), project_id=project, actor="ceo")
    assert job["status"] == "running" and set(job["spaces"]) == {"ENG", "OPS"}
    second = confluence_knowledge.start(eng, _settings(), project_id=project, actor="ceo")
    assert second["already_running"] and second["id"] == job["id"]     # one job at a time
    with eng.begin() as c:
        assert confluence_knowledge.status(c, project)["status"] == "running"
    # the worker died after ENG finished: heartbeat goes stale; the next start RESUMES
    with eng.begin() as c:
        cfg = docs.get_row(c, project)["configuration"]["sync_jobs"][project]
        cfg["spaces"]["ENG"] = {"status": "done", "seen": 3, "indexed": 1, "updated": 0, "unchanged": 0, "failed": []}
        cfg["heartbeat"] = "2020-01-01T00:00:00+00:00"
        c.execute(text("UPDATE docs_connections SET configuration = jsonb_set(configuration, "
                       "ARRAY['sync_jobs', CAST(:p AS TEXT)], CAST(:j AS JSONB))"),
                  {"j": json.dumps(cfg), "p": project})
        assert confluence_knowledge.status(c, project)["status"] == "stalled"
    fake.requests.clear()
    resumed = confluence_knowledge.start(eng, _settings(), project_id=project, actor="ceo")
    assert resumed["resumed"] and resumed["spaces"]["ENG"]["status"] == "done" and resumed["id"] != job["id"]
    done = confluence_knowledge.run(eng, _settings(), project, resumed["id"])
    assert done["status"] == "done" and done["spaces"]["OPS"]["status"] == "done"
    spaces_searched = {r["query"]["cql"] for r in fake.requests if "search" in r["path"]}
    assert all('space in ("OPS")' in q for q in spaces_searched)       # ENG was not read again


def test_sync_needs_spaces_picked_first(eng, fake, project):
    _connect(eng, project)
    with pytest.raises(docs.DocsConnectionError) as caught:
        confluence_knowledge.start(eng, _settings(), project_id=project)
    assert "Pick the Confluence spaces" in str(caught.value)


def test_synced_text_stays_low_trust_when_read_back_from_knowledge(eng, fake, project):
    _connect(eng, project)
    with eng.begin() as c:
        docs.update_settings(c, docs.get_row(c, project), actor="ceo", knowledge_spaces=["ENG"])
    _sync(eng, project)
    from boss_engine.tools.knowledge_tools import _search as kg_search

    hit_result = kg_search(Ctx(eng), project_id=project, actor="a", args={"query": "rollback guide deploy runbook"})
    hits = [h for h in hit_result.get("hits", []) if str(h.get("name")) == "Deploy Runbook"]
    assert hits and hits[0]["untrusted"] is True and hits[0]["trust_origin"] == "document"
    from boss_engine.services import low_trust

    assert low_trust._marked(hit_result)


def test_the_jira_credential_needs_manage_at_jiras_home_and_forces_jiras_site(eng, fake, project):
    from boss_engine.services.secret_backends import default_secret_backend
    from boss_engine.services.secrets_service import put_secret

    with eng.begin() as c:
        ref = put_secret(c, _settings(), project_id=project, name="ticket_jira_token", value=TOKEN,
                         backend=default_secret_backend(_settings()), actor="ceo")["secret_ref_id"]
        cfg = {"base_url": "https://acme.atlassian.net", "email": EMAIL, "ticket_system": True,
               "secret_ref_id": ref, "project_key": "KAN"}
        c.execute(text("INSERT INTO work_tracker_providers (id, project_id, provider_key, "
                       "display_name, status, is_default, configuration, created_by) VALUES "
                       "('trk_confl2', :p, 'jira', 'Jira', 'active', true, CAST(:c AS JSONB), 't')"),
                  {"p": project, "c": json.dumps(cfg)})
        asked = []

        def deny(home):
            asked.append(home)
            raise PermissionError("no manage at the Jira home")

        with pytest.raises(PermissionError):
            docs.prepare(c, _settings(), project_id=project, base_url="https://evil.example",
                         use_jira_credential=True, may_use_jira=deny)
        assert asked == [project]
        config, token = docs.prepare(c, _settings(), project_id=project, base_url="https://evil.atlassian.net",
                                     email="someone@else.test", use_jira_credential=True,
                                     may_use_jira=lambda home: None)
        c.execute(text("DELETE FROM work_tracker_providers WHERE id = 'trk_confl2'"))
    assert config["base_url"] == "https://acme.atlassian.net/wiki" and config["email"] == EMAIL


# ---- review round 2 ---------------------------------------------------------------------------

def test_sync_state_and_job_are_per_project(eng, fake, project):
    _connect(eng, project)
    other = make_project(eng)                         # a second project of the same org
    with eng.begin() as c:
        docs.update_settings(c, docs.get_row(c, project), actor="ceo", knowledge_spaces=["ENG"])
    _sync(eng, project)
    with eng.begin() as c:                            # A holds a cursor for ENG
        confluence_knowledge._put(c, docs.get_row(c, project)["id"], "sync_states", project,  # noqa: SLF001
                                  {"ENG": {"synced_at": "2026-11-17 10:00", "pages": 3}})
        row = docs.get_row(c, project)
        assert set(row["configuration"]["sync_states"]) == {project}
        assert confluence_knowledge.status(c, other) == {}           # B sees none of A's job
    job_b = confluence_knowledge.start(eng, _settings(), project_id=other, actor="ceo")
    assert job_b["project_id"] == other and not job_b.get("already_running")
    fake.requests.clear()
    done = confluence_knowledge.run(eng, _settings(), other, job_b["id"])
    # B starts from nothing: it is NOT incremental from A's cursor, so it reads the whole space
    assert all("lastmodified >=" not in r["query"]["cql"] for r in fake.requests if "search" in r["path"])
    assert done["spaces"]["ENG"]["indexed"] == 1
    with eng.begin() as c:
        assert confluence_knowledge.status(c, project)["project_id"] == project
        assert confluence_knowledge.status(c, other)["project_id"] == other
        view = docs.public_view(c, docs.get_row(c, other), other)
        assert view["sync_state"] == {}                      # B's own cursor, not A's
        assert set(docs.public_view(c, docs.get_row(c, project), project)["sync_state"]) == {"ENG"}


def test_a_job_that_was_taken_over_stops_writing(eng, fake, project):
    _connect(eng, project)
    with eng.begin() as c:
        docs.update_settings(c, docs.get_row(c, project), actor="ceo", knowledge_spaces=["ENG"])
    old = confluence_knowledge.start(eng, _settings(), project_id=project, actor="ceo")
    with eng.begin() as c:      # its heartbeat went stale and a newer job took the project over
        cfg = docs.get_row(c, project)["configuration"]["sync_jobs"][project]
        cfg["heartbeat"] = "2020-01-01T00:00:00+00:00"
        c.execute(text("UPDATE docs_connections SET configuration = jsonb_set(configuration, "
                       "ARRAY['sync_jobs', CAST(:p AS TEXT)], CAST(:j AS JSONB))"),
                  {"j": json.dumps(cfg), "p": project})
    new = confluence_knowledge.start(eng, _settings(), project_id=project, actor="ceo")
    assert new["id"] != old["id"]
    fake.requests.clear()
    confluence_knowledge.run(eng, _settings(), project, old["id"])               # the old worker wakes
    assert [r for r in fake.requests if "search" in r["path"]] == []            # and does nothing
    with eng.begin() as c:
        assert confluence_knowledge.status(c, project)["id"] == new["id"]       # not overwritten
    with pytest.raises(confluence_knowledge.JobOvertaken):
        confluence_knowledge._save(eng, docs.get_row(eng.connect(), project)["id"], project, dict(old))


def test_a_write_that_landed_but_timed_out_is_success_not_rejected(eng, fake, project):
    _connect(eng, project)
    got = _tool("confluence_publish_page")(
        Ctx(eng), project_id=project, actor="a",
        args={"page_id": "101", "space": "ENG", "body": "# Deploy Runbook\n\nlanded"})
    decide(eng, got["decision_id"], "approve")
    fake.apply_then_fail = True                     # the server applies the PUT, the client times out
    done = _reconcile(eng, project)
    assert done[0]["ok"] is True and done[0]["version"] == 5
    assert len(fake.pages_updated) == 1             # the write was NOT re-sent
    with eng.begin() as c:
        assert c.execute(text("SELECT status FROM change_consents WHERE decision_id = :d"),
                         {"d": got["decision_id"]}).scalar() == "consumed"


def test_a_create_that_landed_is_not_retried_into_a_duplicate_title_error(eng, fake, project):
    _connect(eng, project)
    got = _tool("confluence_publish_page")(
        Ctx(eng), project_id=project, actor="a",
        args={"space": "ENG", "title": "Fresh Page", "body": "Hello"})
    decide(eng, got["decision_id"], "approve")
    fake.apply_then_fail = True
    assert _reconcile(eng, project)[0]["ok"] is True
    assert len(fake.pages_created) == 1


def test_a_real_failure_tells_the_approver_with_a_card(eng, fake, project):
    _connect(eng, project)
    got = _tool("confluence_publish_page")(
        Ctx(eng), project_id=project, actor="a", args={"page_id": "101", "space": "ENG", "body": "mine"})
    decide(eng, got["decision_id"], "approve")
    fake.versions["101"] = 9
    assert _reconcile(eng, project)[0]["ok"] is False
    with eng.begin() as c:
        cards = c.execute(text("SELECT question, recommendation FROM decisions WHERE project_id = :p "
                               "AND recommendation->>'action' = 'confluence_publish_failed'"),
                          {"p": project}).mappings().all()
    assert len(cards) == 1 and "NOT published" in cards[0]["question"] and "version 9" in cards[0]["question"]
    from boss_engine.services import decision_actions

    assert "confluence_publish_failed" in decision_actions.REFUSALS


def test_only_the_current_claim_holder_may_record_the_outcome(eng, fake, project):
    _connect(eng, project)
    got = _tool("confluence_publish_page")(
        Ctx(eng), project_id=project, actor="a", args={"page_id": "101", "space": "ENG", "body": "mine"})
    decide(eng, got["decision_id"], "approve")
    cid = got["consent_id"]
    first = confluence_publish._claim(eng, cid)
    assert first and confluence_publish._claim(eng, cid) is None          # one claimant at a time
    with eng.begin() as c:       # the first writer stalls; its claim goes stale and is taken over
        c.execute(text("UPDATE change_consents SET proposed = jsonb_set(proposed, '{_meta,claimed_at}', "
                       "to_jsonb((now() - interval '1 hour')::text)) WHERE id = :i"), {"i": cid})
    second = confluence_publish._claim(eng, cid)
    assert second and second != first
    with eng.begin() as c:
        consent = confluence_publish.change_consent.for_decision(c, got["decision_id"])
    stale = confluence_publish._publish_one(eng, _settings(), consent, first)
    assert stale == {"ok": False, "skipped": "another writer holds this approval"}
    assert confluence_publish._publish_one(eng, _settings(), consent, second)["ok"] is True
    assert len(fake.pages_updated) == 1


def test_the_periodic_tick_finishes_an_approval_that_never_got_its_write(eng, fake, project):
    _connect(eng, project)
    got = _tool("confluence_publish_page")(
        Ctx(eng), project_id=project, actor="a", args={"page_id": "101", "space": "ENG", "body": "later"})
    decide(eng, got["decision_id"], "approve")            # "the process restarted" before the thread ran
    assert fake.pages_updated == []
    done = confluence_publish.tick(eng, _settings())
    assert any(d.get("ok") for d in done) and len(fake.pages_updated) == 1
    from boss_engine.services import monitor_scheduler

    assert "confluence_publish" in open(monitor_scheduler.__file__).read()


def test_the_jira_check_uses_the_home_row_not_a_mirror(eng, fake, project):
    from boss_engine.services.secret_backends import default_secret_backend
    from boss_engine.services.secrets_service import put_secret

    home = project
    mirror_project = make_project(eng)
    with eng.begin() as c:
        ref = put_secret(c, _settings(), project_id=home, name="ticket_jira_token", value=TOKEN,
                         backend=default_secret_backend(_settings()), actor="ceo")["secret_ref_id"]
        base = {"base_url": "https://acme.atlassian.net", "email": EMAIL, "ticket_system": True}
        c.execute(text("INSERT INTO work_tracker_providers (id, project_id, provider_key, display_name, "
                       "status, is_default, configuration, created_by) VALUES ('trk_home3', :p, 'jira', "
                       "'Jira', 'active', true, CAST(:c AS JSONB), 't')"),
                  {"p": home, "c": json.dumps({**base, "secret_ref_id": ref,
                                               "settings": {"share_with_org": True}})})
        c.execute(text("INSERT INTO work_tracker_providers (id, project_id, provider_key, display_name, "
                       "status, is_default, configuration, created_by) VALUES ('trk_mir3', :p, 'jira', "
                       "'Jira', 'active', true, CAST(:c AS JSONB), 't')"),
                  {"p": mirror_project, "c": json.dumps({**base, "secret_ref_id": ref, "mirror_of": "trk_home3"})})
        found = docs.jira_credential(c, _settings(), mirror_project)
        c.execute(text("DELETE FROM work_tracker_providers WHERE id IN ('trk_home3','trk_mir3')"))
    assert found and found["home_project"] == home          # never the mirror's own project
