"""Ticket write-back, inbound pick-up, mid-run changes and a person's own identity (BOSSAPP-1047
Phase B2), against the fake Jira on the test DB.

The run-to-ticket sequence (doing + comment, PR link + review, decision comment, done, preview
comment, blocked comment), every effect once (replays and the catch-up never comment twice), a
failed effect retried, plane_sync's runs left to plane_sync, the "Include it / Next ticket" card,
webhook verification with ask vs auto_start, one poller firing per lease, the ticket tools'
permissions and screening, and the email_matches flag."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text

os.environ.setdefault("BOSS_MASTER_KEY",
                      base64.b64encode(b"p29a-test-master-key-32-bytes!!!").decode())

from boss_engine.connectors.tickets import auth as ticket_auth  # noqa: E402
from boss_engine.connectors.tickets import http as ticket_http  # noqa: E402
from boss_engine.identity import vault  # noqa: E402
from boss_engine.services import connection_credentials as creds  # noqa: E402
from boss_engine.services import ticket_sync  # noqa: E402

from . import dataset as D  # noqa: E402
from .fake_jira import CLIENT_ID, CLIENT_SECRET, EMAIL, TOKEN, FakeJira  # noqa: E402

TEST_DB = os.environ.get("BOSS_TEST_DATABASE_URL", os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"))
STORY = f"KAN-{D.STORY}"
SECRET = "ticket-webhook-secret-for-tests"


class _Settings:
    """What the services read from settings here (nothing secret)."""
    boss_edition = "community"


SETTINGS = _Settings()


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM work_tracker_bindings LIMIT 1"))
            c.execute(text("SELECT 1 FROM extension_job_leases LIMIT 1"))
            c.execute(text("SELECT connection_id FROM mcp_oauth_pending LIMIT 1"))
    except Exception as error:  # pragma: no cover
        pytest.skip(f"test database unavailable or not migrated: {type(error).__name__}")
    vault.reset_master_key_cache()
    return engine


@pytest.fixture(autouse=True)
def _hook_off(monkeypatch):
    # Each test drives on_event itself; the post-commit worker has its own test below.
    monkeypatch.setenv("BOSS_TICKET_SYNC", "off")


@pytest.fixture()
def fake(monkeypatch):
    server = FakeJira()
    real = ticket_http._urllib_transport

    def route(method, url, headers, data, timeout, context):
        for host in ("https://auth.atlassian.com", "https://api.atlassian.com"):
            if url.startswith(host):
                url = server.base + url[len(host):]
        return real(method, url, headers, data, timeout, context)

    monkeypatch.setattr(ticket_http, "TRANSPORT", route)
    monkeypatch.setattr(ticket_http, "SLEEP", lambda s: None)
    yield server
    server.close()


def _config(fake, **settings):
    return {"base_url": fake.base, "email": EMAIL, "project_key": "KAN", "api_token": TOKEN,
            "oauth_client_id": CLIENT_ID, "oauth_client_secret": CLIENT_SECRET,
            "settings": settings, "ticket_system": True}


@pytest.fixture()
def world(eng, fake):
    """A project with a connected (fake) Jira: {pid, conn_id, person}."""
    pid = f"p_tsync_{uuid.uuid4().hex[:8]}"
    conn_id = f"trk_{uuid.uuid4().hex[:12]}"
    person = f"usr_tsync_{uuid.uuid4().hex[:8]}"
    with eng.begin() as c:
        wid = c.execute(text("SELECT id FROM workspaces LIMIT 1")).scalar()
        if wid is None:
            pytest.skip("test database has no workspace")
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                       "(:i, :w, 'ticket sync', :i)"), {"i": pid, "w": wid})
        c.execute(text(
            "INSERT INTO work_tracker_providers (id, project_id, provider_key, display_name, "
            "status, is_default, configuration, created_by) VALUES (:id, :p, 'jira', 'Jira', "
            "'active', true, CAST(:c AS JSONB), :u)"),
            {"id": conn_id, "p": pid, "c": json.dumps(_config(fake)), "u": person})
        org = creds.org_of_project(c, pid)
        c.execute(text("INSERT INTO identity_principals (id, org_id, kind, status, display_name) "
                       "VALUES (:i, :o, 'user', 'active', :i)"), {"i": person, "o": org})
    yield {"pid": pid, "conn_id": conn_id, "person": person}
    with eng.begin() as c:
        items = list(c.execute(text("SELECT vault_item_id FROM connection_credentials WHERE "
                                    "project_id = :p"), {"p": pid}).scalars().all())
        for sql in (
            "DELETE FROM extension_job_leases WHERE extension_id = 'boss.tickets' "
            "AND job = 'poll:' || :c",
            "DELETE FROM decisions WHERE project_id = :p",
            "DELETE FROM receipts WHERE project_id = :p",
            "DELETE FROM work_tracker_bindings WHERE project_id = :p",
            "DELETE FROM stack_deployments WHERE project_id = :p",
            "DELETE FROM code_task_runs WHERE project_id = :p",
            "DELETE FROM workflow_runs WHERE project_id = :p",
            "DELETE FROM workflow_versions WHERE project_id = :p",
            "DELETE FROM workflow_definitions WHERE project_id = :p",
            "DELETE FROM work_items WHERE project_id = :p",
            "DELETE FROM connection_credentials WHERE project_id = :p",
            "DELETE FROM mcp_oauth_pending WHERE project_id = :p",
            "DELETE FROM work_tracker_providers WHERE project_id = :p",
            "DELETE FROM events WHERE project_id = :p",
            "DELETE FROM identity_users WHERE principal_id = :u",
        ):
            try:
                with c.begin_nested():
                    c.execute(text(sql), {"p": pid, "c": conn_id, "u": person})
            except Exception:  # noqa: BLE001 — a table this build lacks
                pass
        if items:
            c.execute(text("DELETE FROM identity_vault_items WHERE id = ANY(:i)"), {"i": items})
        for sql in ("DELETE FROM projects WHERE id = :p",
                    "DELETE FROM identity_principals WHERE id = :u"):
            try:
                with c.begin_nested():
                    c.execute(text(sql), {"p": pid, "u": person})
            except Exception:  # noqa: BLE001
                pass


def _settings(eng, world, **settings):
    """Settings as the API takes them (PATCH /v1/ticket-systems/{id}), stored the way B1 stores
    them (ticket_intake.merge_settings): write-back reads them back through public_settings."""
    from boss_engine.services import ticket_intake

    with eng.begin() as c:
        row = ticket_intake.get_row(c, world["conn_id"])
        ticket_intake.update_settings_row(c, world["conn_id"],
                                          ticket_intake.merge_settings(row["configuration"],
                                                                       settings))


def _import(eng, world, key=STORY):
    with eng.begin() as c:
        return ticket_sync.ticket_import_items(c, SETTINGS, project_id=world["pid"],
                                               items=[{"key": key}], actor=world["person"],
                                               connection_id=world["conn_id"])


def _snapshot(eng, world, key=STORY):
    from boss_engine.connectors.tickets import get_ticket_adapter

    with eng.begin() as c:
        conn = ticket_sync.open_connection(c, world["pid"], connection_id=world["conn_id"])
        ticket = get_ticket_adapter("jira").get_ticket(conn, key)
    return ticket_sync.snapshot(ticket, world["conn_id"])


def _workflow_run(eng, world, ticket, work_item_id=None):
    run_id = f"wfr_{uuid.uuid4().hex[:12]}"
    with eng.begin() as c:
        wdef = f"wfd_{uuid.uuid4().hex[:10]}"
        ver = f"wfv_{uuid.uuid4().hex[:10]}"
        c.execute(text("INSERT INTO workflow_definitions (id, project_id, workflow_key, name, "
                       "created_by) VALUES (:i, :p, :k, 'ticket test', 'test')"),
                  {"i": wdef, "p": world["pid"], "k": f"t_{wdef}"})
        c.execute(text("INSERT INTO workflow_versions (id, project_id, workflow_definition_id, "
                       "version, spec, content_hash, created_by) VALUES (:i, :p, :d, 1, "
                       "'{}'::jsonb, 'x', 'test')"), {"i": ver, "p": world["pid"], "d": wdef})
        c.execute(text("INSERT INTO workflow_runs (id, project_id, workflow_definition_id, "
                       "workflow_version_id, actor, status, input, work_item_id) VALUES "
                       "(:i, :p, :d, :v, :a, 'running', CAST(:in AS JSONB), :w)"),
                  {"i": run_id, "p": world["pid"], "d": wdef, "v": ver, "a": world["person"],
                   "in": json.dumps({"title": "Add Apple Pay", "ticket": ticket}),
                   "w": work_item_id})
    return run_id


def _code_task(eng, world, *, workflow_run_id=None, ticket=None, source=None, pr=None):
    run_id = f"crun_{uuid.uuid4().hex[:12]}"
    ctx = {"workflow_run_id": workflow_run_id}
    if ticket:
        ctx["ticket"] = ticket
    if source:
        ctx["source"] = source
    with eng.begin() as c:
        c.execute(text("INSERT INTO code_task_runs (id, project_id, title, finalize_ctx, "
                       "pull_request_url) VALUES (:i, :p, 'Add Apple Pay', CAST(:f AS JSONB), :u)"),
                  {"i": run_id, "p": world["pid"], "f": json.dumps(ctx), "u": pr})
    return run_id


def _comments(fake, key=STORY):
    return len(fake.comments[key])


# ── write-back ─────────────────────────────────────────────────────────────────────────────


def test_the_run_moves_its_ticket_through_every_event_once(eng, fake, world):
    item = _import(eng, world)["imported"][0]["work_item_id"]
    ticket = {**_snapshot(eng, world), "work_item_id": item}
    run = _workflow_run(eng, world, ticket, item)
    before = _comments(fake)

    started = ticket_sync.on_event(eng, SETTINGS, "workflow.run_started", "workflow_run", run)
    assert [e["op"] for e in started["effects"]] == ["transition", "comment"], started
    assert all(e["status"] == "succeeded" for e in started["effects"])
    assert fake.issues[STORY]["status"] == "In Progress"
    assert _comments(fake) == before + 1

    pr = "https://github.com/acme/shop/pull/7"
    task = _code_task(eng, world, workflow_run_id=run, pr=pr)
    opened = ticket_sync.on_event(eng, SETTINGS, "code_task.pr_opened", "code_task_run", task,
                                  {"pull_request_url": pr})
    assert [e["op"] for e in opened["effects"]] == ["link", "transition"], opened
    assert fake.issues[STORY]["status"] == "In Review"
    assert [link["object"]["url"] for link in fake.links[STORY]] == [pr]

    waiting = ticket_sync.on_event(eng, SETTINGS, "workflow.step_waiting_decision",
                                   "workflow_run", run, {"decision_id": "dec_1"})
    assert waiting["effects"][0]["status"] == "succeeded"
    merged = ticket_sync.on_event(eng, SETTINGS, "software_factory.pull_request_merged",
                                  "artifact", "art_1", {"pull_request_url": pr})
    assert merged["effects"][0]["op"] == "transition"
    assert fake.issues[STORY]["status"] == "Done"

    # deployed: driven through write_back (a stack_deployments row needs a whole deploy)
    deployed = ticket_sync.write_back(
        eng, SETTINGS, _subject(eng, run), "deployed", "deployment.stack_healthy",
        {"open_url": "https://preview.example/app"})
    assert deployed["effects"][0]["status"] == "succeeded"
    assert "https://preview.example/app" in _last_comment(fake)
    blocked = ticket_sync.on_event(eng, SETTINGS, "workflow.run_blocked", "workflow_run", run,
                                   {"reason": "waiting on a host"})
    assert "waiting on a host" in _last_comment(fake) and blocked["synced"]
    assert _comments(fake) == before + 4

    with eng.begin() as c:
        line = ticket_sync.timeline(c, world["pid"], item)
    assert line["sync_status"] == "synced" and line["failed"] == 0
    assert [(e["action"], e["op"]) for e in line["entries"]] == [
        ("started", "transition"), ("started", "comment"), ("pr_opened", "link"),
        ("pr_opened", "transition"), ("decision_pending", "comment"), ("merged", "transition"),
        ("deployed", "comment"), ("blocked", "comment")]

    # REPLAYED: the same events again (a redelivery, the catch-up sweep) write nothing.
    calls = len(fake.log)
    again = ticket_sync.on_event(eng, SETTINGS, "workflow.run_started", "workflow_run", run)
    assert all(e.get("replayed") for e in again["effects"])
    ticket_sync.on_event(eng, SETTINGS, "code_task.pr_opened", "code_task_run", task,
                         {"pull_request_url": pr})
    assert _comments(fake) == before + 4 and len(fake.links[STORY]) == 1
    assert not [r for r in fake.log[calls:] if r.method != "GET"]


def _subject(eng, run_id):
    with eng.begin() as c:
        return ticket_sync.resolve(c, "workflow_run", run_id)


def _last_comment(fake, key=STORY) -> str:
    body = fake.comments[key][-1]["body"]
    return json.dumps(body)


def test_a_code_task_inside_a_workflow_does_not_say_started_twice(eng, fake, world):
    run = _workflow_run(eng, world, _snapshot(eng, world))
    task = _code_task(eng, world, workflow_run_id=run)
    out = ticket_sync.on_event(eng, SETTINGS, "code_task.started", "code_task_run", task,
                               check_changes=False)
    assert out["synced"] is False and out["reason"] == "nothing to write"


def test_a_failed_effect_never_fails_the_run_and_retry_sends_it_once(eng, fake, world):
    item = _import(eng, world)["imported"][0]["work_item_id"]
    run = _workflow_run(eng, world, {**_snapshot(eng, world), "work_item_id": item}, item)
    before = _comments(fake)
    fake.fail_next(403, times=2)          # the transition and the comment are refused
    out = ticket_sync.on_event(eng, SETTINGS, "workflow.run_started", "workflow_run", run)
    assert out["synced"] is False
    assert {e["status"] for e in out["effects"]} == {"failed"}
    assert all(e["retryable"] for e in out["effects"])
    with eng.begin() as c:
        line = ticket_sync.timeline(c, world["pid"], item)
        status = c.execute(text("SELECT sync_status FROM work_tracker_bindings WHERE "
                                "work_item_id = :w"), {"w": item}).scalar()
    assert status == "failed" and line["sync_status"] == "failed" and line["failed"] == 2
    assert "refused" in line["entries"][0]["reason"]
    # a replay does not resend a failed effect; the retry does, once
    ticket_sync.on_event(eng, SETTINGS, "workflow.run_started", "workflow_run", run)
    assert _comments(fake) == before
    retried = ticket_sync.retry_failed(eng, SETTINGS, world["pid"], item)
    assert retried["ok"] and len(retried["retried"]) == 2
    assert _comments(fake) == before + 1 and fake.issues[STORY]["status"] == "In Progress"
    assert ticket_sync.retry_failed(eng, SETTINGS, world["pid"], item)["retried"] == []
    with eng.begin() as c:
        assert ticket_sync.timeline(c, world["pid"], item)["sync_status"] == "synced"


def test_write_back_follows_the_connections_settings(eng, fake, world):
    _settings(eng, world, write_back={"comment": False}, status_map={"doing": "In Review"})
    run = _workflow_run(eng, world, _snapshot(eng, world))
    before = _comments(fake)
    out = ticket_sync.on_event(eng, SETTINGS, "workflow.run_started", "workflow_run", run)
    assert [(e["op"], e["status"]) for e in out["effects"]] == [("transition", "succeeded"),
                                                                ("comment", "off")]
    assert _comments(fake) == before
    assert fake.issues[STORY]["status"] == "In Review"     # status_map: doing -> In Review


def test_plane_sync_runs_are_never_written_twice(eng, fake, world):
    task = _code_task(eng, world, ticket=_snapshot(eng, world), source="plane",
                      pr="https://github.com/acme/shop/pull/9")
    calls = len(fake.log)
    out = ticket_sync.on_event(eng, SETTINGS, "code_task.pr_opened", "code_task_run", task,
                               {"pull_request_url": "https://github.com/acme/shop/pull/9"})
    assert out == {"synced": False, "reason": "plane_sync handles this run"}
    assert len(fake.log) == calls


def test_a_run_not_started_from_a_ticket_is_left_alone(eng, fake, world):
    task = _code_task(eng, world)
    assert ticket_sync.on_event(eng, SETTINGS, "code_task.failed", "code_task_run", task) == \
        {"synced": False, "reason": "not a ticket run"}


def test_a_builds_cockpit_names_its_ticket(eng, fake, world):
    """Phase C: the cockpit's ticket badge. A build carrying its own ticket, a build inside a
    ticket's workflow run, and a build from nothing (None)."""
    snap = _snapshot(eng, world)
    own = _code_task(eng, world, ticket=snap)
    inside = _code_task(eng, world, workflow_run_id=_workflow_run(eng, world, snap))
    plain = _code_task(eng, world)
    with eng.connect() as c:
        mine = ticket_sync.ticket_of_code_task(c, own)
        via = ticket_sync.ticket_of_code_task(c, inside)
        nothing = ticket_sync.ticket_of_code_task(c, plain)
    for got in (mine, via):
        assert got["key"] == STORY and got["provider"] == "jira"
        assert got["connection_id"] == world["conn_id"] and got["url"]
        assert got["status_category"] in ("todo", "doing", "review", "done")
    assert nothing is None


def test_the_hook_writes_after_the_transaction_commits_and_never_for_a_rollback(
        eng, fake, world, monkeypatch):
    from boss_engine.services.core import emit_event

    monkeypatch.setenv("BOSS_TICKET_SYNC", "on")
    monkeypatch.setenv("DATABASE_URL", TEST_DB)
    run = _workflow_run(eng, world, _snapshot(eng, world))
    pr = "https://github.com/acme/shop/pull/11"
    task = _code_task(eng, world, workflow_run_id=run, pr=pr)
    # rolled back: the worker never sees the event row, so nothing is written
    monkeypatch.setattr(ticket_sync, "COMMIT_WAIT_SECONDS", 1.0)
    try:
        with eng.begin() as c:
            emit_event(c, world["pid"], "code_task.pr_opened", "boss", "code_task_run", task,
                       {"pull_request_url": pr})
            raise RuntimeError("roll back")
    except RuntimeError:
        pass
    ticket_sync._QUEUE.join()
    assert fake.links[STORY] == []
    with eng.begin() as c:
        emit_event(c, world["pid"], "code_task.pr_opened", "boss", "code_task_run", task,
                   {"pull_request_url": pr})
    ticket_sync._QUEUE.join()
    assert [link["object"]["url"] for link in fake.links[STORY]] == [pr]


# ── a ticket that changed mid-run ──────────────────────────────────────────────────────────


def test_a_ticket_changed_mid_run_raises_one_card_and_include_it_updates_the_run(
        eng, fake, world):
    snap = _snapshot(eng, world)
    run = _workflow_run(eng, world, snap)
    task = _code_task(eng, world, workflow_run_id=run)
    same = ticket_sync.on_event(eng, SETTINGS, "code_task.started", "code_task_run", task)
    assert same["change"] == {"changed": False}
    issue = fake.issues[STORY]
    from .fake_jira import adf

    issue["description"] = adf("We need Apple Pay.\n\n## Acceptance criteria\n\n- "
                               + D.AC_ITEMS[0] + "\n- Google Pay works too")
    issue["updated"] = "2026-11-13T12:00:00.000+0000"
    changed = ticket_sync.on_event(eng, SETTINGS, "code_task.started", "code_task_run", task)
    card = changed["change"]
    assert card["changed"] and card["added"] == ["Google Pay works too"]
    assert card["removed"] == [D.AC_ITEMS[1]]
    again = ticket_sync.on_event(eng, SETTINGS, "code_task.started", "code_task_run", task)
    assert again["change"]["duplicate"] and again["change"]["decision_id"] == card["decision_id"]
    with eng.begin() as c:
        row = dict(c.execute(text("SELECT id, project_id, recommendation FROM decisions "
                                  "WHERE id = :d"), {"d": card["decision_id"]}).mappings().one())
        from boss_engine.services import decision_actions as da

        labels = [a["label"] for a in da.allowed_actions({**row, "status": "pending"})]
        assert labels[:2] == ["Include it", "Next ticket"]
        ticket_sync.on_card_answered(c, SETTINGS, row, approved=True, actor=world["person"])
        new_input = c.execute(text("SELECT input FROM workflow_runs WHERE id = :r"),
                              {"r": run}).scalar()
    assert new_input["ticket"]["acceptance_criteria"] == [D.AC_ITEMS[0], "Google Pay works too"]
    assert new_input["ticket"]["changed_mid_run"] is True


# ── inbound: webhook and poller ────────────────────────────────────────────────────────────


def _delivery(key=STORY, event="jira:issue_created"):
    return json.dumps({"webhookEvent": event, "issue": {
        "id": str(10000 + D.STORY), "key": key, "self": "https://x.atlassian.net/rest/api/3/issue/1"},
        "changelog": {"items": [{"field": "labels"}]}}).encode()


def _signed(body: bytes, secret=SECRET) -> dict:
    return {"X-Hub-Signature": "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256)
            .hexdigest()}


def test_a_webhook_is_verified_by_its_connector_and_ask_raises_one_card(
        eng, fake, world, monkeypatch):
    monkeypatch.setenv("BOSS_JIRA_WEBHOOK_SECRET", SECRET)
    body = _delivery()
    assert ticket_sync.ingest_webhook(eng, SETTINGS, world["conn_id"],
                                      _signed(body, "wrong"), body)["status"] == "rejected"
    assert ticket_sync.ingest_webhook(eng, SETTINGS, world["conn_id"], {}, body)["status"] == \
        "rejected"
    assert ticket_sync.ingest_webhook(eng, SETTINGS, "trk_nope", _signed(body), body)["status"] \
        == "not_found"
    waiting = ticket_sync.ingest_webhook(eng, SETTINGS, world["conn_id"], _signed(body), body)
    assert waiting["status"] == "waiting" and waiting["decision_id"]
    assert ticket_sync.ingest_webhook(eng, SETTINGS, world["conn_id"], _signed(body),
                                      body)["status"] == "already_asked"
    unlabelled = _delivery(f"KAN-{D.INJECTED}")
    assert ticket_sync.ingest_webhook(eng, SETTINGS, world["conn_id"], _signed(unlabelled),
                                      unlabelled)["status"] == "ignored"
    with eng.begin() as c:
        row = dict(c.execute(text("SELECT id, project_id, recommendation FROM decisions "
                                  "WHERE id = :d"), {"d": waiting["decision_id"]})
                   .mappings().one())
        started = ticket_sync.on_card_answered(c, SETTINGS, row, approved=True,
                                               actor=world["person"])
    assert started["status"] in ("started", "imported") and started["work_item_id"]
    with eng.begin() as c:
        assert ticket_sync.binding_for(c, world["pid"], "jira", STORY) is not None


def test_auto_start_imports_and_starts_and_dedupes_on_the_binding(eng, fake, world, monkeypatch):
    monkeypatch.setenv("BOSS_JIRA_WEBHOOK_SECRET", SECRET)
    _settings(eng, world, pickup={"label": "boss", "mode": "auto_start"})
    body = _delivery()
    first = ticket_sync.ingest_webhook(eng, SETTINGS, world["conn_id"], _signed(body), body)
    assert first["status"] in ("started", "imported"), first
    second = ticket_sync.ingest_webhook(eng, SETTINGS, world["conn_id"], _signed(body), body)
    assert second["status"] == "already_in_boss"
    assert second["work_item_id"] == first["work_item_id"]


def test_two_pollers_one_firing(eng, fake, world):
    _settings(eng, world, inbound={"mode": "poll", "poll_minutes": 5},
              pickup={"label": "boss", "mode": "ask"})
    now = datetime.now(timezone.utc)
    job = f"poll:{world['conn_id']}"
    a = [r for r in ticket_sync.tick(eng, SETTINGS, holder="holder-a", now=now)
         if r["job"] == job]
    b = [r for r in ticket_sync.tick(eng, SETTINGS, holder="holder-b", now=now)
         if r["job"] == job]
    assert len(a) + len(b) == 1
    fired = (a or b)[0]
    assert fired["ok"], fired
    assert {r["key"] for r in fired["result"] if r["status"] == "waiting"} >= {STORY}
    with eng.begin() as c:
        cards = c.execute(text("SELECT count(*) FROM decisions WHERE project_id = :p AND "
                               "recommendation->>'action' = 'ticket_pickup'"),
                          {"p": world["pid"]}).scalar()
    # once more after the interval: deduped, no second card for the same tickets
    later = ticket_sync.tick(eng, SETTINGS, holder="holder-b",
                             now=datetime.fromtimestamp(now.timestamp() + 301, timezone.utc))
    assert [r for r in later if r["job"] == job]
    with eng.begin() as c:
        assert c.execute(text("SELECT count(*) FROM decisions WHERE project_id = :p AND "
                              "recommendation->>'action' = 'ticket_pickup'"),
                         {"p": world["pid"]}).scalar() == cards


def test_a_mirror_reads_its_homes_settings_and_is_never_a_second_connection(
        eng, fake, world, monkeypatch):
    """B1 maps one connection onto many projects by MIRROR rows (configuration.mirror_of).
    Write-back reads the HOME's settings; the poller polls the site once (the home); a delivery
    to a mirror's URL is the home's (its secret, its mappings), so one ticket asks once."""
    monkeypatch.setenv("BOSS_JIRA_WEBHOOK_SECRET", SECRET)
    _settings(eng, world, inbound={"mode": "poll", "poll_minutes": 5},
              pickup={"label": "boss", "mode": "ask"}, status_map={"doing": "In Review"})
    pid2 = f"p_tsm_{uuid.uuid4().hex[:8]}"
    mirror_id = f"trk_{uuid.uuid4().hex[:12]}"
    stale = {**_config(fake, inbound_mode="poll", pickup_label="other",
                       status_mapping={"doing": "Nope"}), "mirror_of": world["conn_id"]}
    with eng.begin() as c:
        wid = c.execute(text("SELECT workspace_id FROM projects WHERE id = :p"),
                        {"p": world["pid"]}).scalar()
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                       "(:i, :w, 'ticket mirror', :i)"), {"i": pid2, "w": wid})
        c.execute(text(
            "INSERT INTO work_tracker_providers (id, project_id, provider_key, display_name, "
            "status, is_default, configuration, created_by) VALUES (:id, :p, 'jira', 'Jira', "
            "'active', true, CAST(:c AS JSONB), :u)"),
            {"id": mirror_id, "p": pid2, "c": json.dumps(stale), "u": world["person"]})
    try:
        with eng.begin() as c:
            mirror = ticket_sync.provider_row(c, mirror_id)
            assert ticket_sync.home_row(c, mirror)["id"] == world["conn_id"]
            conn = ticket_sync.open_connection(c, pid2, connection_id=mirror_id)
            assert ticket_sync.sync_settings(conn.configuration)["status_map"]["doing"] == \
                "In Review"
            by_provider = ticket_sync.open_connection(c, pid2, provider="jira")
            assert ticket_sync.sync_settings(by_provider.configuration)["pickup"]["label"] == \
                "boss"
        ran = ticket_sync.tick(eng, SETTINGS, holder="holder-mirror",
                               now=datetime.now(timezone.utc))
        assert f"poll:{mirror_id}" not in {r["job"] for r in ran}
        body = _delivery()
        asked = ticket_sync.ingest_webhook(eng, SETTINGS, mirror_id, _signed(body), body)
        assert asked["status"] in ("waiting", "already_asked"), asked
        again = ticket_sync.ingest_webhook(eng, SETTINGS, world["conn_id"], _signed(body), body)
        assert again["status"] == "already_asked"
        with eng.begin() as c:
            assert c.execute(text("SELECT count(*) FROM decisions WHERE project_id = :p"),
                             {"p": pid2}).scalar() == 0
    finally:
        with eng.begin() as c:
            for sql in ("DELETE FROM extension_job_leases WHERE extension_id = 'boss.tickets' "
                        "AND job = 'poll:' || :m",
                        "DELETE FROM decisions WHERE project_id = :p",
                        "DELETE FROM work_tracker_providers WHERE id = :m",
                        "DELETE FROM events WHERE project_id = :p",
                        "DELETE FROM projects WHERE id = :p"):
                with c.begin_nested():
                    c.execute(text(sql), {"p": pid2, "m": mirror_id})


# ── the tools: permissions and screening ───────────────────────────────────────────────────


def test_each_ticket_tool_declares_its_kind_permission_and_surfaces():
    from boss_engine.services.low_trust import outside_origin
    from boss_engine.tools import build_default_registry

    reg = build_default_registry()
    want = {"ticket_get": ("read", "integration:read"),
            "ticket_search": ("read", "integration:read"),
            "ticket_browse": ("read", "integration:read"),
            "ticket_import": ("write", "work_tracker:write"),
            "ticket_comment": ("write", "work_tracker:write"),
            "ticket_transition": ("write", "work_tracker:write")}
    for name, (kind, permission) in want.items():
        spec = reg.get(name)
        assert (spec.kind, spec.permission) == (kind, permission), name
        assert set(spec.exposure.surfaces) == {"chat", "agent_step", "mcp"}, name
        assert not spec.exposure.core_for, name
        if kind == "read":
            assert outside_origin(name) == "fetched_page", name
        else:
            assert spec.reversibility == "safe", name
    from boss_engine.tools.builtin import _DISCUSS_OPERATIONS_TOOLS

    assert {"ticket_import", "ticket_comment", "ticket_transition"} <= _DISCUSS_OPERATIONS_TOOLS


def test_ticket_reads_are_screened_and_a_tool_is_refused_without_its_permission(eng, fake, world):
    from boss_engine.tools import build_default_registry

    class Ctx:
        settings = SETTINGS
        engine = eng

        def begin(self):
            return eng.begin()

    reg = build_default_registry()
    got = reg.get("ticket_get").handler(Ctx(), project_id=world["pid"], actor=world["person"],
                                        args={"ref": f"KAN-{D.INJECTED}"})
    assert got["ok"] and got["untrusted"] is True and got["screen_warning"]
    clean = reg.get("ticket_get").handler(Ctx(), project_id=world["pid"], actor=world["person"],
                                          args={"ref": STORY})
    assert clean["ok"] and not clean.get("untrusted")
    assert clean["ticket"]["acceptance_criteria"] == D.AC_ITEMS
    found = reg.get("ticket_search").handler(Ctx(), project_id=world["pid"],
                                             actor=world["person"],
                                             args={"labels": ["boss"], "open_only": True})
    assert STORY in {t["key"] for t in found["tickets"]}
    moved = reg.get("ticket_transition").handler(Ctx(), project_id=world["pid"],
                                                 actor=world["person"],
                                                 args={"ref": STORY, "to": "review"})
    assert moved["ok"] and fake.issues[STORY]["status"] == "In Review"


# ── a person's own account: email_matches ──────────────────────────────────────────────────


def _sign_in(eng, fake, world):
    with eng.begin() as c:
        row = ticket_sync.provider_row(c, world["conn_id"])
        conn = ticket_sync.connection_from_row(c, row)
        started = ticket_auth.begin_oauth(conn, world["person"],
                                          redirect_uri="https://boss.example/v1/ticket-systems/"
                                                       "oauth/callback")
    code, state = fake.approve(started["authorize_url"])
    with eng.begin() as c:
        conn = ticket_sync.connection_from_row(c, ticket_sync.provider_row(c, world["conn_id"]))
        done = ticket_auth.complete_oauth(conn, state, code, completed_by=world["person"])
        identity = ticket_sync.provider_identity(conn, done["person"])
        ticket_sync.remember_identity(c, world["pid"], conn.connection_id, world["person"],
                                      identity)
    return identity


@pytest.mark.parametrize("case,matches", [
    ("same", True), ("same_other_case", True), ("different", False), ("unverified", False)])
def test_my_ticket_identities_flag_whether_the_email_is_mine(eng, fake, world, case, matches):
    fake.me_email = f"ada+{uuid.uuid4().hex[:8]}@example.com"
    mine_email = {"same": fake.me_email, "same_other_case": fake.me_email.upper(),
                  "different": f"grace+{uuid.uuid4().hex[:8]}@example.com",
                  "unverified": fake.me_email}[case]
    with eng.begin() as c:
        c.execute(text("INSERT INTO identity_users (principal_id, email, email_verified_at) "
                       "VALUES (:p, :e, :v)"),
                  {"p": world["person"], "e": mine_email,
                   "v": None if case == "unverified" else datetime.now(timezone.utc)})
    identity = _sign_in(eng, fake, world)
    assert identity["email"] == fake.me_email
    with eng.begin() as c:
        mine = ticket_sync.my_identities(c, world["person"], world["pid"])
    assert len(mine) == 1 and mine[0]["provider"] == "jira"
    assert mine[0]["connection_id"] == world["conn_id"] and mine[0]["account"] == fake.me_email
    assert mine[0]["email_matches"] is matches and mine[0]["needs_confirmation"] is (not matches)
    raw = json.dumps(mine)
    assert not any(token in raw for token in fake.access)
    with eng.begin() as c:
        conn = ticket_sync.connection_from_row(c, ticket_sync.provider_row(c, world["conn_id"]))
        assert ticket_auth.disconnect_person(conn, world["person"]) is True
        assert ticket_sync.my_identities(c, world["person"], world["pid"]) == []


def test_sync_settings_read_both_vocabularies():
    old = ticket_sync.sync_settings({"settings": {"pickup_label": "ai", "auto_start": True,
                                                  "poll_interval_s": 600,
                                                  "status_mapping": {"review": "QA"}}})
    assert old["pickup"] == {"label": "ai", "mode": "auto_start"}
    assert old["inbound"] == {"mode": "webhook", "poll_minutes": 10}
    assert old["status_map"]["review"] == "QA"
    from boss_engine.services import ticket_intake

    # What PATCH stores (B1's merge_settings), read back through B1's public_settings.
    new = ticket_sync.sync_settings({"settings": ticket_intake.merge_settings({}, {
        "pickup": {"label": "boss", "mode": "ask"}, "inbound": {"mode": "poll", "poll_minutes": 2},
        "write_back": {"link_pr": False}, "status_map": {"done": "Shipped"}})})
    assert new["write_back"] == {"comment": True, "transition": True, "link_pr": False}
    assert new["status_map"]["done"] == "Shipped" and new["inbound"]["mode"] == "poll"


def test_a_stored_status_name_reads_as_its_category():
    """The work list's chip (Phase C): the connection's own status names first, then the common
    words; nothing stored is None."""
    from boss_engine.services.ticket_intake import category_of_state

    assert category_of_state("Building", {"doing": "Building"}) == "doing"
    assert category_of_state("In Progress") == "doing"
    assert category_of_state("In Review") == "review"
    assert category_of_state("Done") == "done" and category_of_state("closed") == "done"
    assert category_of_state("To Do") == "todo" and category_of_state("Backlog") == "todo"
    assert category_of_state("") is None and category_of_state(None) is None
