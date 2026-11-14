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
        # Another project's connection id is not this project's to import with.
        crossed = client.post(f"/v1/projects/{world.project_b}/tickets/import", json={
            "connection_id": cid, "items": [{"key": "KAN-4"}]})
        assert crossed.status_code == 404

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
