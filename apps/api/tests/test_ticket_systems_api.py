"""Ticket systems over HTTP, Phase B1 (BOSSAPP-1047; TICKET_API.md §1–§3).

End to end against the Phase A fake Jira and fake Plane (real HTTP servers on 127.0.0.1 speaking
the providers' shapes) and real Postgres: connect (test first, the token to the vault), re-test,
settings merged, browse, import an epic with its children, no second import, start from a
ticket, and the run carrying `input.ticket` into the panel's objective and the build's brief. A
bare key in New work resolves; `UTF-8` never does; a flagged ticket holds the next write; and
the three permissions (integration:read, work_tracker:write, integration:manage) each open only
their own doors. No model, no GitHub.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from apps.api.tests.project_experience_seed import REPO, seed
from boss_engine.connectors.tickets import http as ticket_http
from boss_engine.services import ticket_intake
from tests.ticket_connectors import dataset as D
from tests.ticket_connectors.fake_jira import EMAIL, TOKEN, FakeJira
from tests.ticket_connectors.fake_plane import SLUG, TOKEN as PLANE_TOKEN, FakePlane


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    w = IdentityWorld.create(engine)
    w.owner = w.person("tk-owner")
    w.bind(w.owner, "owner")
    return w


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def jira(monkeypatch):
    monkeypatch.setattr(ticket_http, "SLEEP", lambda seconds: None)
    fake = FakeJira()
    yield fake
    fake.close()


@pytest.fixture
def project(world, client, engine) -> str:
    slug = f"tk-{uuid.uuid4().hex[:8]}"
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "Lending", "slug": slug})
    assert made.status_code == 201, made.text
    with engine.begin() as db:
        seed(db, slug)
        db.execute(text(
            "INSERT INTO github_repos (id, org_id, full_name, language, default_branch, allowed, "
            "checks, checked_at) VALUES (:i, :o, :f, 'TypeScript', 'main', true, CAST(:c AS JSONB), "
            "now()) ON CONFLICT DO NOTHING"),
            {"i": f"ghr_{uuid.uuid4().hex[:8]}", "o": world.org, "f": REPO,
             "c": '{"read": {"ok": true}, "write": {"ok": true}, "pull_requests": {"ok": true}}'})
    return slug


def _connect(client, project_id: str, fake: FakeJira, *, token: str = TOKEN, **extra):
    return client.post("/v1/ticket-systems", json={
        "project_id": project_id, "provider": "jira", "base_url": fake.base,
        "fields": {"email": EMAIL, "project_key": "KAN"}, "secrets": {"api_token": token},
        **extra})


def _consent(client, refused) -> bool:
    body = refused.json() if refused.status_code == 409 else {}
    if body.get("error") != "consent_required":
        return False
    grant = {"agents": [a["id"] for a in body["agents"]], "permissions": body["permissions"],
             "projects": body["projects"], "kind": "standing", "days": 30, "purpose": body["task"]}
    if body.get("github"):
        grant["github"] = {"repos": body["github"]["repos"], **body["github"]["permissions"]}
    given = client.post("/v1/identity/me/delegations", json=grant)
    assert given.status_code in (200, 201), given.text
    return True


def _missing(refused) -> list:
    body = refused.json() if refused.status_code == 409 else {}
    detail = body.get("detail") if isinstance(body.get("detail"), dict) else body
    ready = detail.get("readiness") if isinstance(detail.get("readiness"), dict) else detail
    return [m["key"] for m in (ready.get("missing") or [])] if isinstance(ready, dict) else []


def _until_started(client, send, body: dict):
    """Answer the start door's consent and readiness sheets the way a person would."""
    r = send(body)
    for _ in range(4):
        if r.status_code == 200:
            return r
        if _consent(client, r):
            r = send(body)
            continue
        missing = _missing(r)
        assert missing, r.text
        body = {**body, "start_anyway": sorted({*(body.get("start_anyway") or []), *missing})}
        r = send(body)
    return r


def _run_input(engine, run_id: str) -> dict:
    with engine.connect() as db:
        return db.execute(text("SELECT input FROM workflow_runs WHERE id = :r"),
                          {"r": run_id}).scalar()


# ---- connect → test → settings → browse → import → dedupe → start ---------------------------

def test_connect_browse_import_and_start_from_a_ticket(world, client, engine, project, jira,
                                                       monkeypatch):
    from boss_engine.identity import grants

    monkeypatch.setattr(grants, "repo_guard_active", lambda *a, **k: False)
    with world.acting_as(world.ctx(world.owner)):
        # TEST FIRST: a wrong token stores nothing.
        refused = _connect(client, project, jira, token="not-the-token-0000")
        assert refused.status_code == 424, refused.text
        assert refused.json()["detail"]["error"] == "auth_failed"
        assert client.get("/v1/ticket-systems",
                          params={"project_id": project}).json()["connections"] == []

        made = _connect(client, project, jira)
        assert made.status_code == 201, made.text
        assert TOKEN not in made.text
        conn = made.json()["connection"]
        cid = conn["id"]
        assert conn["status"] == "connected" and conn["account"] == "BOSS bot"
        assert conn["last4"] == TOKEN[-4:] and conn["fingerprint"]
        assert conn["settings"]["pickup"] == {"label": "boss", "mode": "ask"}
        assert conn["settings"]["status_map"] == {"review": "In Review"}
        # Bare keys need the system's project keys: filled from list_projects on connect.
        assert set(conn["settings"]["project_keys"]) >= {"KAN", "OPS", "WEB"}
        with engine.connect() as db:
            stored = db.execute(text("SELECT configuration FROM work_tracker_providers "
                                     "WHERE id = :i"), {"i": cid}).scalar()
        assert stored["secret_ref_id"] and "api_token" not in stored
        assert TOKEN not in str(stored)

        tested = client.post(f"/v1/ticket-systems/{cid}/test", json={})
        assert tested.status_code == 200, tested.text
        assert tested.json()["test"]["ok"] and tested.json()["test"]["account"] == "BOSS bot"
        assert tested.json()["test"]["capabilities"]["read"] is True
        assert TOKEN not in tested.text

        # SETTINGS MERGE: each PATCH changes only what it names.
        one = client.patch(f"/v1/ticket-systems/{cid}", json={"pickup": {"mode": "auto_start"}})
        assert one.status_code == 200, one.text
        two = client.patch(f"/v1/ticket-systems/{cid}",
                           json={"write_back": {"comment": False},
                                 "inbound": {"mode": "poll", "poll_minutes": 10}})
        assert two.status_code == 200, two.text
        settings = two.json()["connection"]["settings"]
        assert settings["pickup"]["mode"] == "auto_start"
        assert settings["write_back"] == {"comment": False, "transition": True, "link_pr": True}
        assert settings["inbound"] == {"mode": "poll", "poll_minutes": 10}
        assert set(settings["project_keys"]) >= {"KAN"}
        with engine.connect() as db:
            stored = db.execute(text("SELECT configuration FROM work_tracker_providers "
                                     "WHERE id = :i"), {"i": cid}).scalar()
        assert stored["email"] == EMAIL and stored["secret_ref_id"]
        assert stored["settings"]["auto_start"] is True          # the contract's own key
        assert stored["settings"]["poll_interval_s"] == 600
        bad = client.patch(f"/v1/ticket-systems/{cid}", json={"pickup": {"mode": "yes"}})
        assert bad.status_code == 422 and bad.json()["detail"]["error"] == "invalid_settings"

        # BROWSE.
        projects = client.get(f"/v1/ticket-systems/{cid}/projects").json()["projects"]
        assert "KAN" in {p["key"] for p in projects}
        epics = client.get(f"/v1/ticket-systems/{cid}/projects/KAN/epics").json()["tickets"]
        assert [e["key"] for e in epics] == [f"KAN-{D.EPIC}"] and epics[0]["in_boss"] is None
        children = client.get(f"/v1/ticket-systems/{cid}/tickets/KAN-{D.EPIC}/children")
        assert {t["key"] for t in children.json()["tickets"]} == {f"KAN-{n}" for n in D.CHILDREN}
        story = client.get(f"/v1/ticket-systems/{cid}/tickets/KAN-{D.STORY}").json()["ticket"]
        assert story["acceptance_criteria"] == D.AC_ITEMS
        assert story["screen"]["verdict"] == "clean" and story["type"] == "story"
        bugs = client.get(f"/v1/ticket-systems/{cid}/search", params={"type": "bug"}).json()
        assert [t["key"] for t in bugs["tickets"]] == ["KAN-3"]
        unparented = client.get(f"/v1/ticket-systems/{cid}/projects/KAN/unparented").json()
        assert f"KAN-{D.EPIC}" not in {t["key"] for t in unparented["tickets"]}
        missing = client.get(f"/v1/ticket-systems/{cid}/tickets/KAN-9999")
        assert missing.status_code == 404 and missing.json()["detail"]["error"] == "not_found"

        # THE PICK-UP INBOX: open tickets carrying the pickup label, not yet in BOSS.
        inbox = client.get(f"/v1/projects/{project}/tickets/waiting")
        assert inbox.status_code == 200, inbox.text
        assert {f"KAN-{D.EPIC}", f"KAN-{D.STORY}"} <= {t["key"] for t in inbox.json()["tickets"]}

        # IMPORT an epic with its children: items under the epic's item, status Ready (todo).
        imported = client.post(f"/v1/projects/{project}/tickets/import", json={
            "connection_id": cid, "items": [{"key": f"KAN-{D.EPIC}", "with_children": True}]})
        assert imported.status_code == 200, imported.text
        got = imported.json()["imported"]
        assert [i["key"] for i in got][0] == f"KAN-{D.EPIC}"
        assert len(got) == 1 + len(D.CHILDREN)
        epic_item = got[0]["work_item_id"]
        assert all(i["parent_work_item_id"] == epic_item for i in got[1:])
        story_item = next(i["work_item_id"] for i in got if i["key"] == f"KAN-{D.STORY}")
        with engine.connect() as db:
            rows = db.execute(text(
                "SELECT w.status, w.parent_id, b.external_key, b.sync_status FROM work_items w "
                "JOIN work_tracker_bindings b ON b.work_item_id = w.id WHERE b.provider_id = :c"),
                {"c": cid}).mappings().all()
        assert len(rows) == len(got) and {r["status"] for r in rows} == {"todo"}
        assert {r["sync_status"] for r in rows} == {"synced"}

        # NO SECOND IMPORT: the binding dedupes.
        again = client.post(f"/v1/projects/{project}/tickets/import", json={
            "connection_id": cid, "items": [{"key": f"KAN-{D.EPIC}", "with_children": True}]})
        assert again.status_code == 200 and again.json()["imported"] == []
        assert len(again.json()["already_in_boss"]) == len(got)
        marked = client.get(f"/v1/ticket-systems/{cid}/projects/KAN/epics").json()["tickets"]
        assert marked[0]["in_boss"]["work_item_id"] == epic_item
        inbox = client.get(f"/v1/projects/{project}/tickets/waiting").json()["tickets"]
        assert not {f"KAN-{D.EPIC}", f"KAN-{D.STORY}"} & {t["key"] for t in inbox}

        # START from the story's work item: a story runs minor_feature, the ticket rides along.
        started = _until_started(
            client, lambda b: client.post(f"/v1/work-items/{story_item}/start-from-ticket",
                                          json=b), {})
    assert started.status_code == 200, started.text
    out = started.json()
    assert out["workflow_key"] == "minor_feature" and out["ticket"]["key"] == f"KAN-{D.STORY}"
    run_id = out["run"]["id"]
    run_input = _run_input(engine, run_id)
    ticket = run_input["ticket"]
    assert ticket["key"] == f"KAN-{D.STORY}" and ticket["connection_id"] == cid
    assert ticket["acceptance_criteria"] == D.AC_ITEMS
    assert ticket["title"] == "Add Apple Pay" and ticket["screen"]["verdict"] == "clean"
    assert {"provider", "url", "body", "comments", "parent", "screen"} <= set(ticket)
    assert run_input["work_item_id"] == story_item
    with engine.connect() as db:
        binding = db.execute(text(
            "SELECT work_item_id, metadata FROM work_tracker_bindings WHERE provider_id = :c "
            "AND external_key = :k"), {"c": cid, "k": f"KAN-{D.STORY}"}).mappings().one()
    assert binding["work_item_id"] == story_item and run_id in binding["metadata"]["run_ids"]

    # The panel's objective and the build's brief carry it, criteria as the done-when list.
    objective = ticket_intake.fold_meeting_objective("Add one small thing.", run_input)
    assert ticket_intake.BLOCK_START in objective and "Add Apple Pay" in objective
    assert "Done when" in objective and D.AC_ITEMS[0] in objective
    build = ticket_intake.fold_build_inputs({"instructions": "Do KAN-2"}, run_input)
    assert build["instructions"].startswith(ticket_intake.BLOCK_START)
    assert build["acceptance_criteria"] == D.AC_ITEMS
    assert ticket_intake.fold_build_inputs(build, run_input)["instructions"].count(
        ticket_intake.BLOCK_START) == 1

    # THE ENGINE'S START (B2's "Start it" card and auto_start pick-up): the same plan as the
    # route (ticket_intake.start_plan), started in the caller's transaction, run on the binding.
    from boss_engine.config import get_settings

    other = next(i for i in got[1:] if i["key"] != f"KAN-{D.STORY}")
    with engine.begin() as db:
        by_engine = ticket_intake.start_from_ticket(
            db, get_settings(), project_id=project, work_item_id=other["work_item_id"],
            actor=world.owner.id)
    assert by_engine["workflow_key"] in ("minor_feature", "fast_fix", "major_feature")
    assert by_engine["run"] and by_engine["run"]["id"], by_engine
    engine_input = _run_input(engine, by_engine["run"]["id"])
    assert engine_input["ticket"]["key"] == other["key"]
    assert engine_input["work_item_id"] == other["work_item_id"]
    with engine.connect() as db:
        meta = db.execute(text(
            "SELECT metadata FROM work_tracker_bindings WHERE work_item_id = :w"),
            {"w": other["work_item_id"]}).scalar()
    assert by_engine["run"]["id"] in meta["run_ids"]


def test_imported_tickets_show_on_the_work_list_with_their_ticket_once(world, client, engine,
                                                                       project, jira,
                                                                       monkeypatch):
    """T9 from the API (Phase C): imported Ready items are work-list rows with origin
    ticket_import and their ticket; a re-import adds none; a started one leaves the Ready set
    and its run row carries the same ticket."""
    from boss_engine.identity import grants

    monkeypatch.setattr(grants, "repo_guard_active", lambda *a, **k: False)
    with world.acting_as(world.ctx(world.owner)):
        cid = _connect(client, project, jira).json()["connection"]["id"]

        def ready() -> list[dict]:
            got = client.get(f"/v1/projects/{project}/work")
            assert got.status_code == 200, got.text
            return [w for w in got.json()["items"] if w.get("origin") == "ticket_import"]

        assert ready() == []
        body = {"connection_id": cid, "items": [{"key": f"KAN-{D.EPIC}", "with_children": True}]}
        got = client.post(f"/v1/projects/{project}/tickets/import", json=body).json()["imported"]
        epic_item = got[0]["work_item_id"]
        rows = ready()
        assert sorted(r["work_item_id"] for r in rows) == sorted(i["work_item_id"] for i in got)
        by_key = {r["ticket"]["key"]: r for r in rows}
        assert set(by_key) == {i["key"] for i in got}
        epic = by_key[f"KAN-{D.EPIC}"]
        assert epic["state"] == "planned" and epic["ticket_type"] == "epic"
        assert epic["parent_work_item_id"] is None
        assert epic["ticket"]["provider"] == "jira" and epic["ticket"]["connection_id"] == cid
        assert epic["ticket"]["url"] and epic["ticket"]["sync_status"] == "synced"
        assert epic["ticket"]["status_category"] in ("todo", "doing", "review", "done")
        story = by_key[f"KAN-{D.STORY}"]
        assert story["parent_work_item_id"] == epic_item and story["ticket_type"] == "story"

        # A RE-IMPORT DUPLICATES NOTHING: the same rows, no more.
        again = client.post(f"/v1/projects/{project}/tickets/import", json=body)
        assert again.json()["imported"] == []
        assert sorted(r["id"] for r in ready()) == sorted(r["id"] for r in rows)

        # STARTED: the story leaves the Ready set; its run row carries the ticket.
        started = _until_started(
            client, lambda b: client.post(f"/v1/work-items/{story['work_item_id']}"
                                          "/start-from-ticket", json=b), {})
        assert started.status_code == 200, started.text
        run_id = started.json()["run"]["id"]
        assert story["work_item_id"] not in {r["work_item_id"] for r in ready()}
        items = client.get(f"/v1/projects/{project}/work").json()["items"]
        run_row = next(w for w in items if w["id"] == run_id)
        assert run_row["ticket"]["key"] == f"KAN-{D.STORY}"
        assert all("ticket" in w for w in items)


def test_the_runner_folds_the_ticket_into_meeting_and_build():
    """The meeting objective and the build instructions are built in one place each."""
    import inspect

    from boss_engine.services import workflow_capability_runner as runner

    body = inspect.getsource(runner._perform_capability)
    assert "ticket_intake.fold_meeting_objective(objective, context.get(\"input\"))" in body
    assert "ticket_intake.fold_build_inputs(inputs, context.get(\"input\"))" in body


# ---- references in text ---------------------------------------------------------------------

def test_a_bare_key_in_new_work_is_resolved_and_utf8_is_not(world, client, engine, project,
                                                           jira, monkeypatch):
    from boss_engine.identity import grants

    monkeypatch.setattr(grants, "repo_guard_active", lambda *a, **k: False)
    with world.acting_as(world.ctx(world.owner)):
        assert _connect(client, project, jira).status_code == 201
        found = client.get("/v1/tickets/resolve", params={
            "project_id": project, "text": f"Do KAN-{D.STORY}, encode as UTF-8 (SHA-256)."})
        assert found.status_code == 200, found.text
        assert [t["key"] for t in found.json()["tickets"]] == [f"KAN-{D.STORY}"]
        assert [r["key"] for r in found.json()["refs"]] == [f"KAN-{D.STORY}"]
        none = client.get("/v1/tickets/resolve", params={
            "project_id": project, "text": "Store every name as UTF-8 and hash with SHA-256."})
        assert none.json()["refs"] == [] and none.json()["tickets"] == []
        url = client.get("/v1/tickets/resolve", params={
            "project_id": project, "text": f"see {jira.base}/browse/KAN-{D.EPIC}"})
        assert [t["key"] for t in url.json()["tickets"]] == [f"KAN-{D.EPIC}"]

        started = _until_started(
            client, lambda b: client.post(f"/v1/projects/{project}/work", json=b),
            {"kind": "ticket", "text": f"KAN-{D.STORY}"})
    assert started.status_code == 200, started.text
    out = started.json()
    assert out["ticket"]["key"] == f"KAN-{D.STORY}"
    assert out["issue"]["label"] == f"KAN-{D.STORY}" and out["issue"]["provider"] == "jira"
    run_input = _run_input(engine, out["run_id"])
    assert run_input["ticket"]["key"] == f"KAN-{D.STORY}"
    assert run_input["title"] == "Add Apple Pay"

    # A text with no ticket in it starts as before, with no ticket.
    with world.acting_as(world.ctx(world.owner)):
        plain = _until_started(
            client, lambda b: client.post(f"/v1/projects/{project}/work", json=b),
            {"kind": "feature", "text": "Store names as UTF-8"})
    assert plain.status_code == 200, plain.text
    assert "ticket" not in _run_input(engine, plain.json()["run_id"])


def test_new_work_recognises_jira_links_and_keys_through_the_connectors():
    from boss_engine.connectors.tickets import TicketConnection
    from boss_engine.services.project_experience import new_work

    link = "https://acme.atlassian.net/browse/LEND-12"
    assert new_work.ticket_link(f"please do {link}.") == link
    assert new_work.issue_of(source_url=link)["label"] == "LEND-12"
    assert new_work.issue_of(source_url=link)["provider"] == "jira"
    conn = TicketConnection("jira", {"base_url": "https://acme.atlassian.net",
                                     "settings": {"project_keys": ["LEND"]}})
    assert [r.key for r in new_work.ticket_refs("Fix LEND-12 in UTF-8", [conn])] == ["LEND-12"]
    assert new_work.ticket_refs("UTF-8 and SHA-256", [conn]) == []


# ---- a flagged ticket -----------------------------------------------------------------------

def test_a_flagged_ticket_is_outside_input_and_holds_the_next_write(world, client, engine,
                                                                     project, jira, monkeypatch):
    from boss_engine.identity import grants
    from boss_engine.services.tool_result_screen import next_write_must_ask

    monkeypatch.setattr(grants, "repo_guard_active", lambda *a, **k: False)
    with world.acting_as(world.ctx(world.owner)):
        assert _connect(client, project, jira).status_code == 201
        started = _until_started(
            client, lambda b: client.post(f"/v1/projects/{project}/work", json=b),
            {"kind": "feature", "text": f"Please do KAN-{D.INJECTED}"})
    assert started.status_code == 200, started.text
    assert started.json()["ticket"]["untrusted"] is True
    run_id = started.json()["run_id"]
    run_input = _run_input(engine, run_id)
    assert run_input["ticket"]["untrusted"] is True
    assert run_input["ticket"]["screen"]["verdict"] == "suspicious"
    block = ticket_intake.ticket_block(run_input["ticket"])
    assert "UNTRUSTED" in block and block.endswith(ticket_intake.BLOCK_END)

    with engine.begin() as db:
        held = ticket_intake.hold_writes_if_untrusted(
            db, {"agent_step": {"role_key": "forge", "workflow_run_id": run_id}})
        clean = ticket_intake.hold_writes_if_untrusted(db, {"agent_step": {"role_key": "forge"}})
    assert held["tool_screen_untrusted"] is True and "tool_screen_untrusted" not in clean
    # The agent loop reads exactly this flag: its next write's allow becomes ask.
    assert next_write_must_ask({"untrusted": held["tool_screen_untrusted"]}, write=True,
                               policy="allow") == "ask"
    import inspect

    from boss_engine.services import agent_loop

    assert "hold_writes_if_untrusted(connection, finalize_ctx)" in inspect.getsource(
        agent_loop.run_agent_loop)


# ---- mappings fan out; disconnect keeps bindings paused --------------------------------------

def test_a_mapping_gives_the_other_project_a_mirror_and_disconnect_pauses(world, client, engine,
                                                                          jira):
    owner = world.owner
    with world.acting_as(world.ctx(owner)):
        made = _connect(client, world.project_a, jira)
        assert made.status_code == 201, made.text
        cid = made.json()["connection"]["id"]
        mapped = client.patch(f"/v1/ticket-systems/{cid}", json={"mappings": [
            {"external_project": "KAN", "boss_project_id": world.project_a, "default": True},
            {"external_project": "OPS", "boss_project_id": world.project_b}]})
        assert mapped.status_code == 200, mapped.text
        mirror = mapped.json()["mirrors"][0]
        assert mirror["project_id"] == world.project_b
        listed = client.get("/v1/ticket-systems", params={"project_id": world.project_b}).json()
        theirs = listed["connections"][0]
        assert theirs["id"] == mirror["connection_id"] and theirs["status"] == "connected"
        assert theirs["managed_from"] == {"connection_id": cid, "project_id": world.project_a}
        assert theirs["settings"]["mappings"][1]["external_project"] == "OPS"
        # The mirror reads with its own (project-scoped) copy of the token.
        read = client.get(f"/v1/ticket-systems/{mirror['connection_id']}/tickets/KAN-2")
        assert read.status_code == 200, read.text
        refused = client.patch(f"/v1/ticket-systems/{mirror['connection_id']}",
                               json={"pickup": {"label": "x"}})
        assert refused.status_code == 409
        imported = client.post(f"/v1/projects/{world.project_b}/tickets/import", json={
            "connection_id": mirror["connection_id"], "items": [{"key": "KAN-4"}]})
        assert imported.status_code == 200, imported.text
        # Another project's connection id is not this project's to name (body ids are confined);
        # the org-level way in is the system's name, and KAN-4 is not imported twice.
        crossed = client.post(f"/v1/projects/{world.project_b}/tickets/import", json={
            "connection_id": cid, "items": [{"key": "KAN-4"}]})
        assert crossed.status_code == 404
        by_system = client.post(f"/v1/projects/{world.project_b}/tickets/import", json={
            "provider": "jira", "items": [{"key": "KAN-4"}]})
        assert by_system.status_code == 200, by_system.text
        assert [e["key"] for e in by_system.json()["already_in_boss"]] == ["KAN-4"]

        gone = client.delete(f"/v1/ticket-systems/{cid}")
        assert gone.status_code == 200, gone.text
        assert gone.json()["bindings_paused"] == 1
        assert set(gone.json()["disconnected"]) == {cid, mirror["connection_id"]}
    with engine.connect() as db:
        binding = db.execute(text("SELECT sync_status, metadata FROM work_tracker_bindings "
                                  "WHERE provider_id = :m"),
                             {"m": mirror["connection_id"]}).mappings().one()
        config = db.execute(text("SELECT status, configuration FROM work_tracker_providers "
                                 "WHERE id = :c"), {"c": cid}).mappings().one()
    assert binding["sync_status"] == "paused" and binding["metadata"]["paused_from"] == "synced"
    assert config["status"] == "disconnected" and "secret_ref_id" not in config["configuration"]


def test_plane_bare_keys_come_from_its_project_list(world, client, engine, monkeypatch):
    monkeypatch.setattr(ticket_http, "SLEEP", lambda seconds: None)
    plane = FakePlane()
    try:
        with world.acting_as(world.ctx(world.owner)):
            made = client.post("/v1/ticket-systems", json={
                "project_id": world.project_b, "provider": "plane", "base_url": plane.base,
                "fields": {"workspace_slug": SLUG, "project_id": plane.kan},
                "secrets": {"api_token": PLANE_TOKEN}})
            assert made.status_code == 201, made.text
            assert "KAN" in made.json()["connection"]["settings"]["project_keys"]
            found = client.get("/v1/tickets/resolve", params={
                "project_id": world.project_b, "text": f"Pick up KAN-{D.STORY} next (UTF-8)."})
        assert found.status_code == 200, found.text
        assert [t["key"] for t in found.json()["tickets"]] == [f"KAN-{D.STORY}"]
    finally:
        plane.close()


# ---- permissions ----------------------------------------------------------------------------

def test_read_write_and_manage_each_open_only_their_doors(world, client, engine, jira):
    reader = world.person("tk-reader")
    world.bind(reader, world.custom_role("tk-read", ["integration:read"]))
    writer = world.person("tk-writer")
    world.bind(writer, world.custom_role("tk-write", ["work_tracker:write", "work_item:read"]))
    manager = world.person("tk-manager")
    world.bind(manager, world.custom_role("tk-manage", ["integration:manage", "integration:read"]))
    with world.acting_as(world.ctx(world.owner)):
        made = _connect(client, world.project_a, jira)
        assert made.status_code == 201, made.text
        cid = made.json()["connection"]["id"]
    item = {"connection_id": cid, "items": [{"key": "KAN-5"}]}

    with world.acting_as(world.ctx(reader)):
        assert client.get("/v1/ticket-systems",
                          params={"project_id": world.project_a}).status_code == 200
        assert client.get(f"/v1/ticket-systems/{cid}/tickets/KAN-5").status_code == 200
        assert _connect(client, world.project_a, jira).status_code == 403
        assert client.patch(f"/v1/ticket-systems/{cid}",
                            json={"pickup": {"mode": "ask"}}).status_code == 403
        assert client.post(f"/v1/projects/{world.project_a}/tickets/import",
                           json=item).status_code == 403

    with world.acting_as(world.ctx(writer)):
        # A role that cannot even read the connection is answered 404 (the matrix's rule).
        assert client.get(f"/v1/ticket-systems/{cid}/tickets/KAN-5").status_code == 404
        assert client.get("/v1/ticket-systems",
                          params={"project_id": world.project_a}).status_code == 403
        assert client.patch(f"/v1/ticket-systems/{cid}",
                            json={"pickup": {"mode": "ask"}}).status_code == 404
        imported = client.post(f"/v1/projects/{world.project_a}/tickets/import", json=item)
        assert imported.status_code == 200, imported.text
        work_item = imported.json()["imported"][0]["work_item_id"]
        # Starting work is workflow_run:start as well; this role does not hold it.
        assert client.post(f"/v1/work-items/{work_item}/start-from-ticket",
                           json={}).status_code == 403

    with world.acting_as(world.ctx(manager)):
        assert client.patch(f"/v1/ticket-systems/{cid}",
                            json={"pickup": {"label": "ready-for-boss"}}).status_code == 200
        assert client.post(f"/v1/projects/{world.project_a}/tickets/import",
                           json=item).status_code == 403

    from boss_engine.identity import catalogue

    meta = catalogue.meta("work_tracker:write")
    assert meta.delegable
    from boss_engine.identity.permissions_gen import PASSPORT_PRESETS

    for preset in ("planner", "builder", "operator_agent"):
        held = set(PASSPORT_PRESETS[preset].permissions)
        assert {"integration:read", "work_tracker:write"} <= held, preset


def test_a_workflows_own_ticket_text_is_never_taken_for_a_reference():
    """`ticket` is also an ordinary input (a support workflow's customer words): only a link, a
    key, or {connection_id, key} names a ticket; anything else is left exactly as it was."""
    reference = ticket_intake._reference  # noqa: SLF001
    assert reference("My card was charged twice") is None
    assert reference("x") is None and reference({"text": "hi"}) is None
    assert reference("KAN-12") == "KAN-12"
    assert reference(" https://acme.atlassian.net/browse/KAN-12 ") == \
        "https://acme.atlassian.net/browse/KAN-12"
    assert reference({"connection_id": "tracker_1", "key": "KAN-12"})["key"] == "KAN-12"
    run_input = {"ticket": "My card was charged twice", "directive": "Triage KAN-2"}
    assert ticket_intake.resolve_for_start(None, "nowhere", run_input) == (None, None)
    assert run_input["ticket"] == "My card was charged twice"


# ---- shared with the whole org, by a person's choice (2026-11-15) ---------------------------
# "I added the Jira ticket system but it does not show up": it had been saved under another
# project. A ticket system lives on the project it was added from; a person who manages it there
# may SHARE it with the whole org. Unshared, another project is told so and never reads it;
# shared, a member of ONLY that other project reads (receipted) and imports (no token copied).

def test_a_ticket_system_is_used_from_another_project_only_once_shared(world, client, engine,
                                                                     jira):
    member_b = world.person("tk-member-b")
    world.bind(member_b, "operator", project=world.project_b)
    with world.acting_as(world.ctx(world.owner)):
        made = _connect(client, world.project_a, jira)
        assert made.status_code == 201, made.text
        cid = made.json()["connection"]["id"]
        assert made.json()["connection"]["settings"]["share_with_org"] is False
        # The catalog says a personal API token starts unshared.
        jira_item = next(c for c in client.get("/v1/ticket-systems/catalog").json()["items"]
                         if c["provider"] == "jira")
        assert jira_item["share_default"] is False

    with world.acting_as(world.ctx(member_b)):
        listed = client.get("/v1/ticket-systems", params={"project_id": world.project_b})
        assert listed.status_code == 200, listed.text
        hint = [c for c in listed.json()["connections"] if c["id"] == cid]
        assert hint and hint[0]["reach"] == "unshared" and "settings" not in hint[0]
        refused = client.get("/v1/tickets/resolve",
                             params={"project_id": world.project_b, "text": f"KAN-{D.STORY}"})
        assert refused.status_code == 200, refused.text
        assert refused.json()["tickets"] == []
        assert refused.json()["errors"][0]["error"] == "not_shared"
        blocked = client.post(f"/v1/projects/{world.project_b}/tickets/import", json={
            "provider": "jira", "items": [{"key": "KAN-5"}]})
        assert blocked.status_code == 403, blocked.text
        assert blocked.json()["detail"]["error"] == "not_shared"
        # Only its home's managers may share it.
        tried = client.patch(f"/v1/ticket-systems/{cid}", json={"share_with_org": True})
        assert tried.status_code in (403, 404), tried.text

    with world.acting_as(world.ctx(world.owner)):
        shared = client.patch(f"/v1/ticket-systems/{cid}", json={"share_with_org": True})
        assert shared.status_code == 200, shared.text
        assert shared.json()["connection"]["settings"]["share_with_org"] is True

    with world.acting_as(world.ctx(member_b)):
        listed = client.get("/v1/ticket-systems", params={"project_id": world.project_b})
        org_row = next(c for c in listed.json()["connections"] if c["id"] == cid)
        assert org_row["reach"] == "org" and org_row["status"] == "connected"
        # Project A is not one member B may read: it is "another project" (PR #140 review).
        assert org_row["home_project"]["id"] == "" and org_row["home_project"]["name"] == "another project"
        assert org_row["account"] is None and org_row["mirrors"] == []
        own_only = client.get("/v1/ticket-systems",
                              params={"project_id": world.project_b, "scope": "project"})
        assert cid not in {c["id"] for c in own_only.json()["connections"]}
        found = client.get("/v1/tickets/resolve",
                           params={"project_id": world.project_b, "text": f"KAN-{D.STORY}"})
        ticket = found.json()["tickets"][0]
        assert ticket["key"] == f"KAN-{D.STORY}" and ticket["title"] == "Add Apple Pay"
        assert ticket["maps_to"] == {"project_id": "", "name": "another project", "by": "home"}
        assert ticket["connection_reach"] == "org"
        absent = client.get("/v1/tickets/resolve",
                            params={"project_id": world.project_b, "text": "LEND-142"})
        error = absent.json()["errors"][0]
        assert error["error"] == "not_found", error
        assert "LEND-142 was not found in" in error["message"] and jira.base in error["message"]
        words = client.get("/v1/tickets/resolve", params={
            "project_id": world.project_b, "text": "Fix the UTF-8 bug in the export"})
        assert words.json()["errors"] == [] and words.json()["tickets"] == []
        # A body id is confined to its project, so the card names the system instead.
        foreign = client.post(f"/v1/projects/{world.project_b}/tickets/import", json={
            "connection_id": cid, "items": [{"key": "KAN-5"}]})
        assert foreign.status_code == 404
        imported = client.post(f"/v1/projects/{world.project_b}/tickets/import", json={
            "provider": "jira", "items": [{"key": "KAN-5"}]})
        assert imported.status_code == 200, imported.text
    with world.acting_as(world.ctx(world.owner)):
        mapped = client.patch(f"/v1/ticket-systems/{cid}", json={"mappings": [
            {"external_project": "KAN", "boss_project_id": world.project_a, "default": True}]})
        assert mapped.status_code == 200, mapped.text
    with engine.connect() as db:
        mirror = db.execute(text(
            "SELECT id, status, configuration FROM work_tracker_providers "
            "WHERE project_id = :p AND provider_key = 'jira'"),
            {"p": world.project_b}).mappings().one()
        bound = db.execute(text(
            "SELECT project_id FROM work_tracker_bindings WHERE provider_id = :m AND "
            "external_key = 'KAN-5'"), {"m": mirror["id"]}).scalar()
        reads = db.execute(text(
            "SELECT result FROM receipts WHERE project_id = :p AND "
            "effect_kind = 'ticket.cross_project_read'"), {"p": world.project_b}).scalars().all()
    config = mirror["configuration"]
    assert config["mirror_of"] == cid and mirror["status"] == "active"
    assert not {"secret_ref_id", "api_token", "last4", "fingerprint"} & set(config), config
    assert bound == world.project_b
    assert reads and {r["connection_id"] for r in reads} == {cid}
    assert all(r["project_id"] == world.project_b for r in reads)


def test_another_org_never_sees_the_connection_and_new_work_says_connect(engine, client, world,
                                                                       jira):
    other = IdentityWorld.create(engine)
    other.owner = other.person("tk-other-owner")
    other.bind(other.owner, "owner")
    with world.acting_as(world.ctx(world.owner)):
        made = _connect(client, world.project_a, jira, settings={"share_with_org": True})
        assert made.status_code == 201, made.text
    with other.acting_as(other.ctx(other.owner)):
        listed = client.get("/v1/ticket-systems", params={"project_id": other.project_a})
        assert listed.status_code == 200, listed.text
        assert [c for c in listed.json()["connections"] if c.get("status") == "connected"] == []
        said = client.get("/v1/tickets/resolve",
                          params={"project_id": other.project_a, "text": "KAN-7"})
        assert said.status_code == 200, said.text
        assert said.json()["tickets"] == [] and said.json()["connected"] == 0
        assert "Setup › Ticket systems" in said.json()["not_connected"]["next"]


# ---- the org's ticket systems, one global list (operator, 2026-11-15) ----------------------
# "I think we should show the jira irrespective of the project - inside the project we can filter
# for that project, this is the global setting." Setup › Ticket systems › All lists every
# connection of the org, wherever it was added; a project filters to what it can use. Another
# org sees none of it, and a person who cannot manage a connection's home is told whom to ask.

def test_the_org_list_shows_every_connection_and_another_org_none(world, client, engine, jira):
    member_b = world.person("tk-org-member-b")
    world.bind(member_b, "operator", project=world.project_b)
    with world.acting_as(world.ctx(world.owner)):
        made = _connect(client, world.project_a, jira)
        assert made.status_code == 201, made.text
        cid = made.json()["connection"]["id"]
        mine = client.get("/v1/ticket-systems/org", params={"project_id": world.project_a})
        assert mine.status_code == 200, mine.text
        row = next(c for c in mine.json()["connections"] if c["id"] == cid)
        assert row["usable_here"] is True and row["can_manage"] is True
        assert row["reach"] == "project" and row["settings"]["share_with_org"] is False
        assert row["home_project"]["id"] == world.project_a
        assert row["last4"]

    owner_name = world.owner.display_name
    with engine.connect() as db:
        a_name = db.execute(text("SELECT name FROM projects WHERE id = :p"),
                            {"p": world.project_a}).scalar()
    with world.acting_as(world.ctx(member_b)):
        listed = client.get("/v1/ticket-systems/org", params={"project_id": world.project_b})
        assert listed.status_code == 200, listed.text
        row = next(c for c in listed.json()["connections"] if c["id"] == cid)
        # Every member sees the provider, site, sharing and status (an org-wide setting) ...
        assert row["usable_here"] is False and row["can_manage"] is False
        assert row["reach"] == "unshared" and row["base_url"] == jira.base
        assert row["status"] == "connected" and row["settings"]["share_with_org"] is False
        # ... but never a project they cannot read: not its name, id, owners, the account whose
        # token it uses, the projects it maps to, or its counts (as /v1/projects hides it).
        assert row["home_project"]["id"] == "" and row["home_project"]["name"] == "another project"
        assert owner_name in row["home_project"]["owners"] and row["home_project"]["hidden"] is True
        assert row["project_id"] == "" and row["account"] is None and row["fields"] == {}
        assert row["settings"]["mappings"] == [] and row["project_names"] == {}
        assert row["mirrors"] == [] and row["bindings"] == {"total": 0, "paused": 0}
        assert row["last4"] is None and row["fingerprint"] is None and row["oauth_app"] is None
        body = str(listed.json())
        assert world.project_a not in body and a_name not in body and TOKEN not in body
        # The project list and the resolver say the same, naming whom to ask: an org owner.
        hint = next(c for c in client.get("/v1/ticket-systems", params={
            "project_id": world.project_b}).json()["connections"] if c["id"] == cid)
        assert hint["can_manage"] is False and hint["home_project"]["name"] == "another project"
        assert owner_name in hint["home_project"]["owners"]
        said = client.get("/v1/tickets/resolve",
                          params={"project_id": world.project_b, "text": f"KAN-{D.STORY}"})
        error = said.json()["errors"][0]
        assert error["error"] == "not_shared" and error["can_manage"] is False
        assert error["home_project"] == "" and error["home_project_name"] == "another project"
        assert owner_name in error["owners"] and a_name not in error["message"]
        assert error["connection_id"] == cid and error["label"] == "Jira"
        # A key inside a sentence says the same (its prefix is matched locally, nothing is read).
        sentence = client.get("/v1/tickets/resolve", params={
            "project_id": world.project_b, "text": f"Please implement KAN-{D.STORY} today"}).json()
        assert sentence["errors"][0]["error"] == "not_shared", sentence
        assert sentence["errors"][0]["ref"] == f"KAN-{D.STORY}"
        assert sentence["errors"][0]["home_project_name"] == "another project"
        # And plain words with a key-like token no connection knows say nothing.
        assert client.get("/v1/tickets/resolve", params={
            "project_id": world.project_b, "text": "Fix the UTF-8 bug"}).json()["errors"] == []

    # A member who CAN read project A sees it named, with its owners and whose token.
    reader_a = world.person("tk-org-reader-a")
    world.bind(reader_a, "viewer", project=world.project_a)
    world.bind(reader_a, "operator", project=world.project_b)
    with world.acting_as(world.ctx(reader_a)):
        row = next(c for c in client.get("/v1/ticket-systems/org", params={
            "project_id": world.project_b}).json()["connections"] if c["id"] == cid)
        assert row["home_project"]["id"] == world.project_a and row["home_project"]["owners"]
        assert row["account"] is not None and row["can_manage"] is False
        error = client.get("/v1/tickets/resolve", params={
            "project_id": world.project_b, "text": f"KAN-{D.STORY}"}).json()["errors"][0]
        assert error["home_project"] == world.project_a and error["home_project_name"] != "another project"

    with world.acting_as(world.ctx(world.owner)):
        shared = client.patch(f"/v1/ticket-systems/{cid}", json={"share_with_org": True})
        assert shared.status_code == 200, shared.text
    with world.acting_as(world.ctx(member_b)):
        row = next(c for c in client.get("/v1/ticket-systems/org", params={
            "project_id": world.project_b}).json()["connections"] if c["id"] == cid)
        assert row["usable_here"] is True and row["reach"] == "org"

    other = IdentityWorld.create(engine)
    other.owner = other.person("tk-org-other-owner")
    other.bind(other.owner, "owner")
    with other.acting_as(other.ctx(other.owner)):
        theirs = client.get("/v1/ticket-systems/org", params={"project_id": other.project_a})
        assert theirs.status_code == 200, theirs.text
        assert cid not in {c["id"] for c in theirs.json()["connections"]}
        # And they cannot ask about a project of another org.
        assert client.get("/v1/ticket-systems/org",
                          params={"project_id": world.project_a}).status_code in (403, 404)


def test_mapping_here_never_drops_or_moves_a_mapping_the_caller_cannot_see(world, client, engine,
                                                                          jira):
    """PR #140 re-review repro: the owner maps KAN → A and OPS → B; P (admin on A and C, nothing
    on B) presses "Map KAN here" from C. KAN moves to C; OPS → B stays, and B's mirror with it.
    Moving or removing B's mapping needs integration:manage on B."""
    with world.acting_as(world.ctx(world.owner)):
        made = client.post("/v1/projects", json={"name": "Ledger C", "slug": f"tk-c-{uuid.uuid4().hex[:6]}"})
        assert made.status_code == 201, made.text
        project_c = made.json().get("id") or made.json().get("project", {}).get("id") or made.json().get("slug")
        cid = _connect(client, world.project_a, jira).json()["connection"]["id"]
        mapped = client.patch(f"/v1/ticket-systems/{cid}", json={"mappings": [
            {"external_project": "KAN", "boss_project_id": world.project_a, "default": True},
            {"external_project": "OPS", "boss_project_id": world.project_b, "default": False}]})
        assert mapped.status_code == 200, mapped.text
    p = world.person("tk-admin-a-c")
    world.bind(p, "admin", project=world.project_a)
    world.bind(p, "admin", project=project_c)

    def b_mirror():
        with engine.connect() as db:
            return db.execute(text(
                "SELECT status FROM work_tracker_providers WHERE project_id = :p "
                "AND configuration->>'mirror_of' = :h"), {"p": world.project_b, "h": cid}).scalar()

    assert b_mirror() == "active"
    with world.acting_as(world.ctx(p)):
        seen = next(c for c in client.get("/v1/ticket-systems/org", params={
            "project_id": project_c}).json()["connections"] if c["id"] == cid)
        # P sees only the mapping onto A; B is not theirs to see.
        assert seen["settings"]["mappings"] == [
            {"external_project": "KAN", "boss_project_id": world.project_a, "default": True}]
        # What the console's "Map KAN here" sends: the visible list with KAN moved to C.
        moved = client.patch(f"/v1/ticket-systems/{cid}", json={"mappings": [
            {"external_project": "KAN", "boss_project_id": project_c, "default": True}]})
        assert moved.status_code == 200, moved.text
        # Moving OPS off B (which P cannot manage) is refused, and so is dropping it.
        refused = client.patch(f"/v1/ticket-systems/{cid}", json={"mappings": [
            {"external_project": "KAN", "boss_project_id": project_c, "default": True},
            {"external_project": "OPS", "boss_project_id": project_c, "default": False}]})
        assert refused.status_code in (403, 404), refused.text
    with engine.connect() as db:
        stored = ticket_intake._stored_settings(ticket_intake.get_row(db, cid)["configuration"])["mappings"]
    assert sorted((m["external_project"], m["boss_project_id"], m["default"]) for m in stored) == [
        ("KAN", project_c, True), ("OPS", world.project_b, False)]
    assert b_mirror() == "active"


def test_an_agent_never_tells_a_person_a_project_the_console_hides(world, client, engine, jira):
    """ticket_get (ticket_reach.lookup) answers for the person the agent acts for: a home or a
    mapped project they cannot read is "another project", as on their screen."""
    from boss_engine.services import ticket_reach

    member_b = world.person("tk-agent-member-b")
    world.bind(member_b, "operator", project=world.project_b)
    with world.acting_as(world.ctx(world.owner)):
        cid = _connect(client, world.project_a, jira).json()["connection"]["id"]
    with engine.connect() as db:
        a_name = db.execute(text("SELECT name FROM projects WHERE id = :p"),
                            {"p": world.project_a}).scalar()
        said = ticket_reach.lookup(db, world.project_b, f"KAN-{D.STORY}", person_id=member_b.id)
    assert said["error"] == "not_shared" and a_name not in said["message"]
    assert said["home_project"] == "" and said["home_project_name"] == "another project"
    with world.acting_as(world.ctx(world.owner)):
        assert client.patch(f"/v1/ticket-systems/{cid}", json={"share_with_org": True}).status_code == 200
    ticket_reach.clear_cache()
    with engine.connect() as db:
        found = ticket_reach.lookup(db, world.project_b, f"KAN-{D.STORY}", person_id=member_b.id)
        owner_sees = ticket_reach.lookup(db, world.project_b, f"KAN-{D.STORY}",
                                         person_id=world.owner.id)
    assert found["ok"] and found["maps_to"]["project_id"] == "" and found["maps_to"]["name"] == "another project"
    assert owner_sees["maps_to"]["project_id"] == world.project_a
