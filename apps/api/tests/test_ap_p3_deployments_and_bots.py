"""AP-P3 acceptance on a real database (docs/design/AGENT_PLATFORM.md §5 Phase 3; BOSSAPP-1054).

(a) A cron bot fires on schedule; with skip_if_active it never overlaps itself; pausing it stops
    it within one tick.
(b) An external client with a service-account token creates a thread, streams a run, approves an
    interrupt through the Decision API and cancels a second run; every call without the
    catalogue permission is denied.
(c) Promoting v8 to prod while v7 runs leaves the in-flight v7 run pinned to v7 (its resume
    after an approval included).
(d) Every previous focused-agent and wakeup behaviour passes its existing tests on the new
    primitive (the suites, re-run with the migration flags on), and with the flags on the work
    really is a bot run (trigger → admission → run row).

Plus: promotion checks (A3), triggers' words, preview and the webhook (A4), health with the
plain cause and the doctor's auto-pause (A5), the A1 list, worker registration and the scaling
signal, the MCP tool per deployed agent, fork.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from boss_engine.kernel.interrupts import interrupt
from boss_engine.kernel.scripted import Call, Say, ScriptedEngine
from boss_engine.kernel.toolset import FunctionToolSet, Tool
from boss_engine.kernel.types import ToolSchema

PROJECT = "boss-dogfood"
OBJ = {"type": "object", "properties": {"x": {"type": "string"}}, "additionalProperties": True}
ROOT = Path(__file__).resolve().parents[3]


# ── helpers ────────────────────────────────────────────────────────────────────────────────────

def _key(prefix: str = "triage") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:6]}"


def _versions(engine, project: str, key: str, count: int) -> None:
    """`count` versions of `key`, each with its own system prompt ("I am v<N>")."""
    from boss_engine.agents import versions

    with engine.begin() as c:
        for n in range(1, count + 1):
            versions.save(c, project, {
                "apiVersion": "boss.agent/v1", "kind": "Agent",
                "metadata": {"key": key, "name": "Triage bot"},
                "spec": {"mission": f"Triage issues (generation {n}).",
                         "prompts": {"system": f"I am v{n}."},
                         "limits": {"max_steps": 6}}}, actor="ceo", source="operator")


def _deploy(engine, project: str, key: str, env: str, version: int):
    from boss_engine.services import agent_deployments

    with engine.begin() as c:
        return agent_deployments.deploy(c, project, key, env, actor="ceo", version=version)


class Parts:
    """The deployed runner's engine and tools, scripted: what each run's model says depends on
    its input, and a resumed run (it has a checkpoint) says it finished. Records the version
    each build compiled (the pin)."""

    def __init__(self):
        self.built: list = []
        self.effects: list = []

    def __call__(self, connection, settings, run, version_row):
        resumed = bool(connection.execute(text(
            "SELECT 1 FROM agent_checkpoints WHERE run_id = :r LIMIT 1"), {"r": run["id"]}
        ).scalar())
        self.built.append((run["id"], version_row["version"], resumed))
        said = next((str(m.get("content") or "") for m in reversed(run.get("messages") or [])
                     if m.get("role") == "user"), "")
        if resumed:
            script = [Say(f"v{version_row['version']}: closed after approval")]
        elif "close" in said:
            script = [Call("close_issue", {"x": "#311"}, id="c1"), Say("unreachable")]
        elif "slow" in said:
            script = [Call("slow", {"x": "1"}, id="s1"), Say("slow done")]
        else:
            script = [Say(f"v{version_row['version']}: swept")]

        def close(args):
            return interrupt("approval", {"summary": f"close {args.get('x')} as duplicate"})

        def close_approved(args):
            self.effects.append(("close_issue", args.get("x")))
            return {"ok": True, "closed": args.get("x")}

        tools = FunctionToolSet([
            Tool(ToolSchema("close_issue", "close an issue", OBJ, kind="write",
                            idempotency="keyed"), close, close_approved),
            Tool(ToolSchema("slow", "a slow read", OBJ), lambda a: {"ok": True})])
        return {"engine": ScriptedEngine(script, cost_usd_micros=1_000, model="scripted-v1"),
                "tools": tools}


@pytest.fixture
def parts(monkeypatch):
    from boss_engine.services import bots

    p = Parts()
    monkeypatch.setattr(bots, "PARTS_HOOK", p)
    return p


def _drain(engine, project: str, *, rounds: int = 10) -> list:
    """A worker: lease and execute every queued agent run / resume of the project."""
    from boss_engine.config import get_settings
    from boss_engine.services import agent_run_worker
    from boss_engine.services.jobs import JobLeaseService

    jobs = JobLeaseService(get_settings())
    done = []
    for _ in range(rounds):
        with engine.begin() as c:
            ids = [r[0] for r in c.execute(text(
                "SELECT id FROM jobs WHERE project_id = :p AND kind IN ('agent.run', "
                "'agent.resume') AND status = 'queued' ORDER BY created_at"),
                {"p": project}).all()]
        if not ids:
            return done
        for job_id in ids:
            with engine.begin() as c:
                row = c.execute(text("SELECT kind, payload FROM jobs WHERE id = :j"),
                                {"j": job_id}).mappings().first()
                try:
                    leased = jobs.lease_owned_job(c, project_id=project, job_id=job_id,
                                                  worker_id="test-worker", lease_ttl_seconds=60)
                except Exception:  # noqa: BLE001 — leased by another round
                    continue
            payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(
                row["payload"])
            out = agent_run_worker.execute(engine, get_settings(), kind=row["kind"],
                                           job_id=job_id, run_id=payload["run_id"],
                                           worker_id="test-worker",
                                           lease_token=leased["lease_token"], jobs=jobs)
            if not out.get("job_deferred"):
                with engine.begin() as c:
                    jobs.complete(c, "test-worker", job_id, leased["lease_token"],
                                  {"status": out.get("status")})
            done.append(out)
    return done


def _run(engine, run_id: str) -> dict:
    with engine.connect() as c:
        return dict(c.execute(text("SELECT * FROM agent_runs WHERE id = :r"),
                              {"r": run_id}).mappings().one())


def _due_now(engine, trigger_id: str) -> None:
    with engine.begin() as c:
        c.execute(text("UPDATE agent_triggers SET next_fire_at = now() - interval '1 second' "
                       "WHERE id = :t"), {"t": trigger_id})


# ── (a) a cron bot: on schedule, never overlapping, paused within one tick ────────────────────

def test_a_cron_bot_fires_on_schedule_never_overlaps_and_pause_stops_it(client, engine, parts):
    from boss_engine.services import bots

    key = _key("sweeper")
    _versions(engine, PROJECT, key, 1)
    _deploy(engine, PROJECT, key, "dev", 1)
    made = client.post(f"/v1/agents/{key}/triggers", json={
        "project_id": PROJECT, "actor": "ceo", "environment": "dev", "kind": "cron",
        "config": {"every_seconds": 60}, "input": {"goal": "sweep issues older than 7 days"},
        "concurrency": "skip_if_active"})
    assert made.status_code == 201, made.text
    trigger = made.json()
    assert trigger["kind_label"] == "Schedule" and trigger["what"].startswith("Every minute")
    assert len(trigger["next_fires"]) == 3, "A4 shows the next three firings"

    # ON SCHEDULE: not before it is due, then exactly once when it is.
    assert bots.tick(engine)["fired"] == []
    _due_now(engine, trigger["id"])
    before = datetime.now(timezone.utc)
    fired = bots.tick(engine)["fired"]
    assert [f["trigger_id"] for f in fired] == [trigger["id"]]
    first = fired[0]["run_id"]
    run = _run(engine, first)
    assert run["deployment_id"] and run["agent_version"] == 1 and run["status"] == "queued"
    assert run["trigger"] == f"cron:{trigger['id']}"
    with engine.connect() as c:
        nxt = c.execute(text("SELECT next_fire_at FROM agent_triggers WHERE id = :t"),
                        {"t": trigger["id"]}).scalar()
    assert nxt > before, "re-armed for the next minute after firing"

    # NEVER OVERLAPS: due again while the first run has not finished → skipped, not admitted.
    _due_now(engine, trigger["id"])
    out = bots.tick(engine)
    assert out["fired"] == [] and out["skipped"][0]["outcome"] == "skipped"
    assert "skip if active" in out["skipped"][0]["reason"]
    with engine.connect() as c:
        runs = c.execute(text("SELECT count(*) FROM agent_runs WHERE trigger_id = :t"),
                         {"t": trigger["id"]}).scalar()
    assert runs == 1

    _drain(engine, PROJECT)
    assert _run(engine, first)["status"] == "completed"
    _due_now(engine, trigger["id"])
    second = bots.tick(engine)["fired"][0]["run_id"]
    _drain(engine, PROJECT)
    a, b = _run(engine, first), _run(engine, second)
    assert b["created_at"] >= a["updated_at"], "the second run started after the first ended"
    assert b["status"] == "completed" and b["output_text"] == "v1: swept"

    # PAUSE STOPS IT WITHIN ONE TICK.
    paused = client.post(f"/v1/agent-triggers/{trigger['id']}/pause",
                         json={"project_id": PROJECT, "actor": "ceo", "reason": "holiday"})
    assert paused.status_code == 200 and paused.json()["status"] == "paused", paused.text
    _due_now(engine, trigger["id"])
    assert bots.tick(engine)["fired"] == []
    from boss_engine.services import agent_triggers

    with engine.begin() as c:
        assert agent_triggers.fire(c, trigger["id"])["outcome"] == "refused", \
            "a firing selected before the pause landed is refused under the row lock"
        counts = dict(c.execute(text(
            "SELECT outcome, count(*) FROM agent_trigger_fires WHERE trigger_id = :t "
            "GROUP BY outcome"), {"t": trigger["id"]}).all())
    assert counts == {"admitted": 2, "skipped": 1, "refused": 1}
    resumed = client.post(f"/v1/agent-triggers/{trigger['id']}/resume",
                          json={"project_id": PROJECT, "actor": "ceo"})
    assert resumed.json()["status"] == "on" and resumed.json()["next_fires"]


def test_a4_preview_words_and_a_signed_webhook(client, engine, parts):
    from boss_engine.services.workflow_webhooks import sign

    preview = client.get("/v1/agent-triggers/preview", params={
        "project_id": PROJECT, "kind": "cron", "cron": "0 9 * * 1-5", "timezone": "UTC"})
    assert preview.status_code == 200, preview.text
    labels = [n["label"] for n in preview.json()["next"]]
    assert len(labels) == 3 and all(lbl.endswith("09:00") for lbl in labels)
    assert not any(lbl.startswith(("Sat", "Sun")) for lbl in labels)
    bad = client.get("/v1/agent-triggers/preview", params={
        "project_id": PROJECT, "kind": "cron", "cron": "61 9 * * *"})
    assert bad.status_code == 422

    key = _key("hooked")
    _versions(engine, PROJECT, key, 1)
    _deploy(engine, PROJECT, key, "staging", 1)
    made = client.post(f"/v1/agents/{key}/triggers", json={
        "project_id": PROJECT, "actor": "ceo", "environment": "staging", "kind": "webhook"})
    assert made.status_code == 201, made.text
    hook = made.json()
    assert hook["secret"].startswith("whsec_") and hook["secret_shown_once"]
    listed = client.get(f"/v1/agents/{key}/triggers", params={"project_id": PROJECT}).json()
    assert "secret" not in json.dumps(listed["triggers"]), "the secret is shown once"
    body = json.dumps({"issue": 311}).encode()
    stamp = str(int(time.time()))
    path = hook["url_path"]
    refused = client.post(path, content=body, headers={
        "Content-Type": "application/json", "X-BOSS-Timestamp": stamp,
        "X-BOSS-Signature": "v1=deadbeef"})
    assert refused.status_code == 401
    ok = client.post(path, content=body, headers={
        "Content-Type": "application/json", "X-BOSS-Timestamp": stamp,
        "X-BOSS-Signature": sign(hook["secret"], stamp, body), "X-BOSS-Delivery": "d-1"})
    assert ok.status_code == 202 and ok.json()["run_id"], ok.text
    again = client.post(path, content=body, headers={
        "Content-Type": "application/json", "X-BOSS-Timestamp": stamp,
        "X-BOSS-Signature": sign(hook["secret"], stamp, body), "X-BOSS-Delivery": "d-1"})
    assert again.status_code == 200 and again.json()["status"] == "duplicate"
    run = _run(engine, ok.json()["run_id"])
    assert run["finalize_ctx"]["payload"]["issue"] == 311, "the delivery is the run's input"
    assert "311" in run["input"][0]["content"]


def test_an_event_trigger_fires_through_event_dispatch(client, engine, parts):
    from boss_engine.services.event_dispatch import dispatch_event

    key = _key("eventbot")
    _versions(engine, PROJECT, key, 1)
    _deploy(engine, PROJECT, key, "dev", 1)
    made = client.post(f"/v1/agents/{key}/triggers", json={
        "project_id": PROJECT, "actor": "ceo", "environment": "dev", "kind": "event",
        "config": {"event_type": "github.issue_opened"}})
    assert made.status_code == 201, made.text
    assert made.json()["what"] == "A new GitHub issue opens"
    with engine.begin() as c:
        out = dispatch_event(c, event_id="evt_p3_1", project_id=PROJECT,
                             event_type="github.issue_opened", actor="github",
                             entity_type="issue", entity_id="311", data={"number": 311})
        again = dispatch_event(c, event_id="evt_p3_1", project_id=PROJECT,
                               event_type="github.issue_opened", actor="github",
                               entity_type="issue", entity_id="311", data={"number": 311})
    mine = [e for e in out if e.get("trigger_id") == made.json()["id"]]
    assert mine and mine[0]["outcome"] == "admitted"
    assert [e["outcome"] for e in again if e.get("trigger_id") == made.json()["id"]] == \
        ["duplicate"], "one event is one firing"
    refused = client.post(f"/v1/agents/{key}/triggers", json={
        "project_id": PROJECT, "actor": "ceo", "environment": "dev", "kind": "event",
        "config": {"event_type": "agent.made_up"}})
    assert refused.status_code == 422


# ── (b) an external client with a service-account token ────────────────────────────────────────

def _sse_events(response) -> list:
    out = []
    for line in response.iter_lines():
        line = line.decode() if isinstance(line, bytes) else line
        if line.startswith("data: "):
            out.append(json.loads(line[len("data: "):]))
    return out


def test_b_a_service_account_threads_streams_approves_and_cancels(engine, parts):
    from apps.api.app.main import app
    from apps.api.tests.identity_fixtures import IdentityWorld
    from boss_engine.identity.principal import (AuthContext, AuthMethod, CredentialRef,
                                                PrincipalKind)

    world = IdentityWorld.create(engine)
    world.claimed()
    owner = world.person("Olive Owner")
    world.bind(owner, "owner")
    world.person("Second Person")
    project = world.project_a
    key = _key("triage")
    _versions(engine, project, key, 1)
    _deploy(engine, project, key, "prod", 1)
    client = TestClient(app, raise_server_exceptions=False)

    def sa_ctx(name: str, permissions: tuple) -> AuthContext:
        sa = world.principal(name, kind=PrincipalKind.SERVICE_ACCOUNT)
        world.bind(sa, world.custom_role(name.lower().replace(" ", "_"), permissions),
                   project=project)
        k = world.key_ctx(sa, permissions)
        with engine.begin() as db:
            db.execute(text("UPDATE identity_api_credentials SET kind = 'sak' WHERE id = :k"),
                       {"k": k.credential.id})
        return AuthContext(sa, AuthMethod.API_KEY, credential=CredentialRef(
            id=k.credential.id, kind="sak", principal_id=sa.id, scopes=k.credential.scopes))

    portal = sa_ctx("Support portal", ("agent:invoke",))
    reader = sa_ctx("Read only", ("agent:read",))

    # A WORKER beside the client: executes the runs the API admits.
    stop = threading.Event()

    def worker():
        while not stop.is_set():
            _drain(engine, project, rounds=1)
            time.sleep(0.1)

    w = threading.Thread(target=worker, daemon=True)
    w.start()
    try:
        with world.acting_as(portal):
            made = client.post(f"/v1/agents/{key}/threads", json={"project_id": project})
            assert made.status_code == 201, made.text
            tid = made.json()["thread_id"]
            with client.stream("POST", f"/v1/threads/{tid}/runs/stream", json={
                    "project_id": project, "input": "close #311 as a duplicate",
                    "timeout": 30}) as streamed:
                assert streamed.status_code == 200
                assert streamed.headers["content-type"].startswith("text/event-stream")
                events = _sse_events(streamed)
        kinds = [e["type"] for e in events]
        assert kinds[0] == "metadata" and kinds[-1] == "end", kinds
        assert {"updates", "messages", "custom", "values"} <= set(kinds), kinds
        custom = next(e for e in events if e["type"] == "custom")["data"]
        decision_id = custom["decisions"][0]["decision_id"]
        run_id = events[0]["data"]["run_id"]
        values = next(e for e in events if e["type"] == "values")["data"]
        assert values["status"] == "interrupted" and values["version"] == 1

        # The service account's own key cannot approve: approvals are people's.
        with world.acting_as(portal):
            denied = client.post(f"/v1/decisions/{decision_id}/approve",
                                 json={"actor": "x", "note": "ok"})
        assert denied.status_code in (403, 404), denied.text
        # The client's approval key (an Owner's approval delegation for agent pauses) does,
        # through the Decision API.
        with world.acting_as(world.ctx(owner)):
            granted = client.post("/v1/identity/me/approval-delegations", json={
                "kinds": ["agent_interrupt"], "projects": [project], "days": 30})
        assert granted.status_code == 201, granted.text
        data = granted.json()
        from boss_engine.identity.grants import principal as load_principal

        with engine.connect() as db:
            delegate = load_principal(db, data["approval_delegation"]["delegate"]["id"])
        approver = AuthContext(delegate, AuthMethod.API_KEY, credential=CredentialRef(
            id=data["key"]["id"], kind="sak", principal_id=delegate.id,
            scopes=frozenset({"decision:read", "decision:approve"}),
            project_ids=frozenset([project])))
        with world.acting_as(approver):
            approved = client.post(f"/v1/decisions/{decision_id}/approve",
                                   json={"actor": "x", "note": "duplicate of #298"})
        assert approved.status_code == 200, approved.text
        with world.acting_as(portal):
            joined = client.get(f"/v1/threads/{tid}/runs/{run_id}/join",
                                params={"project_id": project, "timeout": 30})
        assert joined.status_code == 200 and joined.json()["status"] == "success", joined.text
        assert joined.json()["output"] == "v1: closed after approval"
        assert parts.effects == [("close_issue", "#311")], "the approved call ran exactly once"

        # A SECOND RUN, cancelled before it finishes (the worker is held back).
        stop.set()
        w.join(5)
        with world.acting_as(portal):
            second = client.post(f"/v1/threads/{tid}/runs",
                                 json={"project_id": project, "input": "sweep"})
            assert second.status_code == 201, second.text
            rid = second.json()["run_id"]
            cancelled = client.post(f"/v1/threads/{tid}/runs/{rid}/cancel",
                                    json={"project_id": project})
            assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
            got = client.get(f"/v1/threads/{tid}/runs/{rid}", params={"project_id": project})
            history = client.get(f"/v1/threads/{tid}/history", params={"project_id": project})
        assert got.json()["status"] == "cancelled"
        assert [r["status"] for r in history.json()["runs"]] == ["success", "cancelled"]

        # EVERY CALL WITHOUT agent:invoke IS DENIED.
        calls = [
            ("POST", f"/v1/agents/{key}/threads", {"project_id": project}),
            ("GET", f"/v1/threads/{tid}", None),
            ("GET", f"/v1/threads/{tid}/history", None),
            ("POST", f"/v1/threads/{tid}/runs", {"project_id": project, "input": "x"}),
            ("POST", f"/v1/threads/{tid}/runs/stream", {"project_id": project, "input": "x"}),
            ("POST", f"/v1/threads/{tid}/runs/wait", {"project_id": project, "input": "x"}),
            ("GET", f"/v1/threads/{tid}/runs/{run_id}", None),
            ("GET", f"/v1/threads/{tid}/runs/{run_id}/join", None),
            ("GET", f"/v1/threads/{tid}/runs/{run_id}/stream", None),
            ("POST", f"/v1/threads/{tid}/runs/{run_id}/cancel", {"project_id": project}),
            ("POST", f"/v1/threads/{tid}/runs/{run_id}/fork", {"project_id": project}),
            ("POST", f"/v1/agents/{key}/runs/wait", {"project_id": project, "input": "x"}),
            ("POST", f"/v1/agents/{key}/runs/stream", {"project_id": project, "input": "x"}),
        ]
        with engine.connect() as c:
            runs_before = c.execute(text("SELECT count(*) FROM agent_runs WHERE agent_key = :a"),
                                    {"a": key}).scalar()
        with world.acting_as(reader):
            for method, path, body in calls:
                res = (client.post(path, json=body) if method == "POST"
                       else client.get(path, params={"project_id": project}))
                assert res.status_code == 403, f"{method} {path}: {res.status_code} {res.text}"
        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM agent_runs WHERE agent_key = :a"),
                             {"a": key}).scalar() == runs_before, "a denied call starts nothing"
    finally:
        stop.set()
        w.join(5)


# ── (c) promoting v8 while v7 runs ─────────────────────────────────────────────────────────────

def test_c_promoting_v8_leaves_the_in_flight_v7_run_on_v7(client, engine, parts):
    from boss_engine.services import agent_deployments, agent_protocol

    key = _key("triage")
    _versions(engine, PROJECT, key, 8)
    _deploy(engine, PROJECT, key, "staging", 8)
    _deploy(engine, PROJECT, key, "prod", 7)
    with engine.begin() as c:
        thread = agent_protocol.create_thread(c, project_id=PROJECT, agent_key=key, actor="ceo")
        run = agent_protocol.start_run(c, project_id=PROJECT, thread_id=thread["thread_id"],
                                       actor="ceo", input="close #311 as a duplicate")
    assert run["version"] == 7
    _drain(engine, PROJECT)
    assert _run(engine, run["run_id"])["status"] == "awaiting_approval"

    checks = client.get(f"/v1/agents/{key}/promotion-checks", params={
        "project_id": PROJECT, "from_environment": "staging", "to_environment": "prod"})
    assert checks.status_code == 200, checks.text
    body = checks.json()
    assert body["version"] == 8 and body["live_version"] == 7 and body["can_promote"]
    assert {c["key"]: c["ok"] for c in body["checks"]}["spec"] is True
    assert body["approval"]["required"] is True
    assert body["in_flight"]["runs"] == 1 and "stay on v7" in body["in_flight"]["note"]

    promoted = client.post(f"/v1/agents/{key}/promote", json={
        "project_id": PROJECT, "actor": "ceo", "from_environment": "staging",
        "to_environment": "prod"})
    assert promoted.status_code == 200 and promoted.json()["status"] == "awaiting_approval"
    decision = promoted.json()["decision_id"]
    with engine.connect() as c:
        assert agent_deployments.get(c, PROJECT, key, "prod")["version"] == 7, \
            "nothing moves before the approval"
    ok = client.post(f"/v1/decisions/{decision}/approve", json={"actor": "ceo", "note": "ship"})
    assert ok.status_code == 200, ok.text
    with engine.connect() as c:
        prod = agent_deployments.get(c, PROJECT, key, "prod")
    assert prod["version"] == 8 and prod["previous_version"] == 7

    # The v7 run's interrupt is answered AFTER the promotion: it resumes on v7.
    with engine.connect() as c:
        interrupt_decision = c.execute(text(
            "SELECT id FROM decisions WHERE recommendation->>'run_id' = :r"),
            {"r": run["run_id"]}).scalar()
    assert client.post(f"/v1/decisions/{interrupt_decision}/approve",
                       json={"actor": "ceo", "note": "yes"}).status_code == 200
    _drain(engine, PROJECT)
    finished = _run(engine, run["run_id"])
    assert finished["status"] == "completed" and finished["agent_version"] == 7
    assert finished["output_text"] == "v7: closed after approval"
    assert [v for r, v, resumed in parts.built if r == run["run_id"]] == [7, 7]

    # A new run on the same thread starts on v8.
    with engine.begin() as c:
        nxt = agent_protocol.start_run(c, project_id=PROJECT, thread_id=thread["thread_id"],
                                       actor="ceo", input="sweep")
    _drain(engine, PROJECT)
    assert nxt["version"] == 8
    assert _run(engine, nxt["run_id"])["output_text"] == "v8: swept"

    view = client.get(f"/v1/agents/{key}/deployments", params={"project_id": PROJECT}).json()
    prod_col = next(e for e in view["environments"] if e["environment"] == "prod")
    assert prod_col["version"] == 8 and prod_col["next"] == {"action": "rollback",
                                                             "to_version": 7}
    assert [h["action"] for h in view["history"]][:1] == ["promote"]
    rolled = client.post(f"/v1/agents/{key}/rollback", json={
        "project_id": PROJECT, "actor": "ceo", "environment": "prod"})
    assert rolled.status_code == 200 and rolled.json()["version"] == 7, rolled.text
    direct = client.post(f"/v1/agents/{key}/deployments", json={
        "project_id": PROJECT, "actor": "ceo", "environment": "prod", "version": 8})
    assert direct.status_code == 409, "prod takes a version only by an approved promotion"


def test_a_failing_eval_blocks_promotion_and_cost_delta_is_shown(client, engine, parts):
    key = _key("evald")
    _versions(engine, PROJECT, key, 2)
    _deploy(engine, PROJECT, key, "dev", 2)
    _deploy(engine, PROJECT, key, "staging", 1)
    with engine.begin() as c:
        c.execute(text("INSERT INTO datasets (id, project_id, name, agent_key, created_by) "
                       "VALUES (:d, :p, :n, :a, 'ceo')"),
                  {"d": f"ds_{key}", "p": PROJECT, "n": f"set {key}", "a": key})
        c.execute(text("INSERT INTO dataset_experiments (id, project_id, dataset_id, "
                       "dataset_version, name, agent_key, spec_version, status, created_by, "
                       "finished_at) VALUES (:e, :p, :d, 1, 'core', :a, 2, 'completed', 'ceo', "
                       "now())"), {"e": f"de_{key}", "p": PROJECT, "d": f"ds_{key}", "a": key})
        for i, passed in enumerate((True, True, False)):
            c.execute(text("INSERT INTO dataset_experiment_results (id, project_id, "
                           "experiment_id, example_id, passed) VALUES (:i, :p, :e, :x, :ok)"),
                      {"i": f"der_{key}_{i}", "p": PROJECT, "e": f"de_{key}", "x": f"ex{i}",
                       "ok": passed})
    checks = client.get(f"/v1/agents/{key}/promotion-checks", params={
        "project_id": PROJECT, "from_environment": "dev", "to_environment": "staging"}).json()
    by = {c["key"]: c for c in checks["checks"]}
    assert by["eval"]["label"] == "Eval set 2/3" and by["eval"]["ok"] is False
    assert not checks["can_promote"] and checks["approval"]["required"] is False
    refused = client.post(f"/v1/agents/{key}/promote", json={
        "project_id": PROJECT, "actor": "ceo", "from_environment": "dev",
        "to_environment": "staging"})
    assert refused.status_code == 409


# ── (d) the migrated mechanisms, on the new primitive ──────────────────────────────────────────

def test_d_with_the_flags_on_a_wakeup_and_a_focused_agent_are_bot_runs(client, engine,
                                                                        monkeypatch):
    import inspect

    from apps.api.app.config import get_settings
    from boss_engine.services import agent_wakeups as wake
    from boss_engine.services.agent_service import AgentService

    monkeypatch.setenv("BOSS_BOTS_WAKEUPS", "1")
    monkeypatch.setenv("BOSS_BOTS_FOCUSED_AGENTS", "1")
    real = inspect.signature(AgentService.chat)

    class Bound:
        def __init__(self, *a, **k):
            pass

        def chat(self, *args, **kwargs):
            real.bind(self, *args, **kwargs)
            return {"reply": "nothing changed"}

    monkeypatch.setattr("boss_engine.services.agent_service.AgentService", Bound)
    with engine.begin() as c:
        c.execute(text("DELETE FROM agent_wakeups"))
        armed = wake.arm_monitor(c, project_id=PROJECT, actor="ceo", thread_id="athr_p3_wake",
                                 prompt="check the build", exit_condition="it has a result",
                                 interval_seconds=60)
        c.execute(text("UPDATE agent_wakeups SET next_wake_at = now() - interval '1 min' "
                       "WHERE id = :i"), {"i": armed["id"]})
    out = wake.run_due(get_settings(), engine, project_id="", limit=5)
    assert out["primitive"] == "bots" and out["due"] == 1 and not out["ran"][0].get("error")
    with engine.connect() as c:
        trig = c.execute(text("SELECT * FROM agent_triggers WHERE legacy_ref = :r"),
                         {"r": f"wakeup:{armed['id']}"}).mappings().one()
        run = c.execute(text("SELECT * FROM agent_runs WHERE trigger_id = :t"),
                        {"t": trig["id"]}).mappings().one()
        assert c.execute(text("SELECT cycle FROM agent_wakeups WHERE id = :i"),
                         {"i": armed["id"]}).scalar() == 1
    assert trig["kind"] == "monitor" and trig["fires"] == 1
    assert run["status"] == "completed" and run["environment"] == "prod"
    assert (run["finalize_ctx"] or {}).get("runner") == "wakeup"

    created = client.post("/v1/focused-agents", json={
        "project_id": PROJECT, "actor": "ceo", "kind": "reviewer", "name": "Reviewer bot",
        "focus": "Review what is verify-green", "start": True})
    assert created.status_code == 201, created.text
    ticked = client.post("/v1/focused-agents/tick-all", json={"project_id": PROJECT,
                                                              "actor": "ceo"})
    assert ticked.status_code == 200, ticked.text
    body = ticked.json()
    assert body["primitive"] == "bots"
    mine = [r for r in body["ran"] if (r.get("agent") or {}).get("id") == created.json()["id"]]
    assert mine and "error" not in mine[0], body
    with engine.connect() as c:
        run = c.execute(text(
            "SELECT r.status, r.finalize_ctx FROM agent_runs r JOIN agent_triggers t "
            "ON t.id = r.trigger_id WHERE t.legacy_ref = :r"),
            {"r": f"focused_agent:{created.json()['id']}"}).mappings().one()
    assert run["status"] == "completed" and run["finalize_ctx"]["runner"] == "focused_agent"
    # Pausing the bot's trigger stops the agent's next duty cycle.
    a1 = client.get("/v1/agent-platform/agents", params={"project_id": PROJECT}).json()
    names = {a["key"]: a for a in a1["agents"]}
    assert names["focused_agents"]["kind"] == "bot" and names["boss_wakeups"]["kind"] == "bot"


_FLAGS = {"BOSS_BOTS_FOCUSED_AGENTS": "1", "BOSS_BOTS_WAKEUPS": "1", "BOSS_BOTS_SQUAD_OWNERS": "1"}
_API_SUITES = ["apps/api/tests/test_focused_agents.py",
               "apps/api/tests/test_a_wakeup_fires_wherever_it_was_armed.py",
               "apps/api/tests/test_work_that_starts_itself.py"]
_ENGINE_SUITES = ["engine/tests/test_focused_agents.py",
                  "engine/tests/test_role_run_policy_857.py",
                  "engine/tests/test_the_wake_tick_fix_is_verified_across_apps.py",
                  "engine/tests/test_the_demo_morning_shows_agents_working_together.py",
                  "engine/tests/test_agents_work_a_signal_together_from_triage_to_outcome.py",
                  "apps/worker/tests/test_the_wake_tick_covers_every_live_project.py"]


@pytest.mark.parametrize("suites", [_API_SUITES, _ENGINE_SUITES], ids=["api", "engine"])
def test_d_the_existing_focused_agent_wakeup_and_squad_suites_pass_on_the_new_primitive(suites):
    """(d): the suites that pin today's focused-agent, wakeup and squad-owner behaviour, run
    again with every migration flag on — the same assertions, on bots."""
    env = {**os.environ, **_FLAGS, "PYTHONPATH": f"{ROOT}:{ROOT / 'engine'}"}
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:warnings", "-x",
                           *suites], cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=900)
    tail = "\n".join((done.stdout or "").splitlines()[-15:])
    assert done.returncode == 0, f"the existing suites fail on the new primitive:\n{tail}"


# ── health, the doctor, A1, workers, MCP, fork ─────────────────────────────────────────────────

def test_health_names_the_cause_and_the_doctor_pauses_a_failing_bot(client, engine, parts,
                                                                    monkeypatch):
    from boss_engine.services import bots, system_doctor

    key = _key("oncall")
    _versions(engine, PROJECT, key, 2)
    _deploy(engine, PROJECT, key, "prod", 2)
    made = client.post(f"/v1/agents/{key}/triggers", json={
        "project_id": PROJECT, "actor": "ceo", "environment": "prod", "kind": "monitor",
        "config": {"every_seconds": 60, "check": "Grafana alert error rate > 2%"}})
    trigger = made.json()

    def broken(connection, settings, run, version_row):
        raise RuntimeError("Grafana API answered 401 Unauthorized: the token was rejected")

    monkeypatch.setattr(bots, "PARTS_HOOK", broken)
    for _ in range(3):
        _due_now(engine, trigger["id"])
        assert bots.tick(engine)["fired"]
        try:
            _drain(engine, PROJECT)
        except RuntimeError:
            pass
    health = client.get("/v1/agent-platform/health", params={"project_id": PROJECT})
    assert health.status_code == 200, health.text
    row = next(d for d in health.json()["deployments"] if d["agent_key"] == key)
    assert row["state"] == "failing" and row["failure_streak"] == 3 and row["ok_rate"] == 0
    assert row["last_failure"]["cause"] == "the Grafana token was rejected"
    assert row["last_failure"]["same_cause_count"] == 3
    alert = next(a for a in health.json()["alerts"] if a["agent_key"] == key)
    assert alert["detail"].startswith("Same cause each time: the Grafana token was rejected")
    p4 = client.get("/v1/observability/agent-health", params={"project_id": PROJECT}).json()
    assert any(d["agent_key"] == key for d in p4["deployments"]), "P4's endpoint extended"

    sig = system_doctor.collect(engine)
    mine = [b for b in sig.failing_bots if b["trigger_id"] == trigger["id"]]
    assert mine and mine[0]["cause"] == "the Grafana token was rejected"
    findings = [f for f in system_doctor.diagnose(sig)
                if f["kind"] == f"bot_failing:{trigger['id']}"]
    assert findings and findings[0]["auto"] == ["pause_failing_bot"]
    system_doctor.tick(engine, sig=sig)
    with engine.connect() as c:
        state = c.execute(text("SELECT status, paused_by, paused_reason FROM agent_triggers "
                               "WHERE id = :t"), {"t": trigger["id"]}).mappings().one()
    assert state["status"] == "paused" and state["paused_by"] == "boss"
    assert "the Grafana token was rejected" in state["paused_reason"]
    after = client.get("/v1/agent-platform/health", params={"project_id": PROJECT}).json()
    row = next(d for d in after["deployments"] if d["agent_key"] == key)
    assert row["state"] == "paused" and row["actions"][0] == "resume"
    services = {s["key"] for s in after["system_services"]}
    assert {"system_doctor", "bot_clock"} <= services, "D7: system services, one list"


def test_a1_workers_mcp_and_fork(client, engine, parts):
    from boss_engine.mcp.agent_tools import sync
    from boss_engine.services import agent_protocol
    from boss_engine.tools.registry import ToolRegistry

    key = _key("listed")
    _versions(engine, PROJECT, key, 3)
    _deploy(engine, PROJECT, key, "staging", 3)
    _deploy(engine, PROJECT, key, "prod", 2)
    a1 = client.get("/v1/agent-platform/agents", params={"project_id": PROJECT})
    assert a1.status_code == 200, a1.text
    agent = next(a for a in a1.json()["agents"] if a["key"] == key)
    assert agent["kind"] == "assistant" and agent["name"] == "Triage bot"
    assert sorted((x["environment"], x["version"]) for x in agent["live"]) == [
        ("prod", 2), ("staging", 3)]
    assert set(a1.json()["summary"]) == {"live_agents", "bots_running", "need_attention",
                                          "spend_7d_usd"}
    card = client.get(f"/v1/agents/{key}/endpoints", params={"project_id": PROJECT}).json()
    assert card["mcp"]["tool"] == f"agent_{key}" and card["a2a"]["enabled"] is False

    # Worker registration and the scaling signal.
    reg = client.post("/v1/workers/register", json={
        "project_id": PROJECT, "worker_id": "w-p3", "capabilities": ["agent.run"],
        "metadata": {"slots": 2, "busy_slots": 0, "host": "boss-1"}, "actor": "ceo"})
    assert reg.status_code == 200 and reg.json()["slots"] == 2, reg.text
    with engine.begin() as c:
        thread = agent_protocol.create_thread(c, project_id=PROJECT, agent_key=key, actor="ceo")
        for word in ("one", "two", "three"):
            other = agent_protocol.create_thread(c, project_id=PROJECT, agent_key=key,
                                                 actor="ceo")
            agent_protocol.start_run(c, project_id=PROJECT, thread_id=other["thread_id"],
                                     actor="ceo", input=word)
    scaling = client.get("/v1/workers/scaling", params={"project_id": PROJECT}).json()
    assert scaling["queued_runs"] >= 3 and scaling["signal"] == "scale_up"
    assert scaling["slots"] >= 2
    _drain(engine, PROJECT)
    assert client.get("/v1/workers/scaling", params={"project_id": PROJECT}).json()[
        "queued_runs"] == 0

    # MCP: one tool per agent deployed to prod; calling it runs the agent.
    registry = ToolRegistry()

    class Ctx:
        engine = None

    Ctx.engine = engine
    names = sync(registry, project_id=PROJECT, engine=engine)
    assert f"agent_{key}" in names
    spec = registry.get(f"agent_{key}")
    assert spec.permission == "agent:invoke" and spec.kind == "write"
    worker = threading.Thread(target=lambda: [(_drain(engine, PROJECT), time.sleep(0.2))
                                              for _ in range(20)], daemon=True)
    worker.start()
    out = spec.handler(Ctx, project_id=PROJECT, actor="ceo", args={"input": "sweep",
                                                                   "timeout": 20})
    worker.join(10)
    assert out["ok"] and out["status"] == "success" and out["output"] == "v2: swept", out

    # FORK: the thread's run, re-run from its first checkpoint as a new run on the same thread.
    with engine.begin() as c:
        run = agent_protocol.start_run(c, project_id=PROJECT, thread_id=thread["thread_id"],
                                       actor="ceo", input="sweep")
    _drain(engine, PROJECT)
    forked = client.post(f"/v1/threads/{thread['thread_id']}/runs/{run['run_id']}/fork",
                         json={"project_id": PROJECT, "actor": "ceo"})
    assert forked.status_code == 201, forked.text
    fork = forked.json()
    assert fork["parent_run_id"] == run["run_id"] and fork["version"] == 2
    assert fork["forked_from"]["run_id"] == run["run_id"]


def test_the_priors_build_is_a_scheduled_bot(engine):
    from boss_engine.services import bots

    bots.tick(engine, project_id=PROJECT)
    with engine.connect() as c:
        trig = c.execute(text("SELECT * FROM agent_triggers WHERE project_id = :p "
                              "AND legacy_ref LIKE 'priors_build:%'"), {"p": PROJECT}
                         ).mappings().one()
    assert trig["kind"] == "cron" and trig["config"]["every_seconds"] == 3600
    _due_now(engine, trig["id"])
    fired = bots.tick(engine, project_id=PROJECT)["fired"]
    assert [f["trigger_id"] for f in fired] == [trig["id"]]
    _drain(engine, PROJECT)
    run = _run(engine, fired[0]["run_id"])
    assert run["status"] == "completed", run["error"]
    assert json.loads(run["output_text"])["org_id"] == trig["legacy_ref"].split(":", 1)[1]
