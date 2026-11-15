"""Shared helpers for the event-intake tests (P9a): the bench DB, a project, a connection, signed
requests for each adapter, and a local deployment with an intake route."""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Optional

import pytest
from sqlalchemy import create_engine, text

# Secrets written by these tests go to Postgres, never to a vault the laptop may or may not run.
os.environ.setdefault("BOSS_COMMUNITY_SECRET_STORE", "database")

PG_URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")


def pg_engine():
    if not PG_URL.startswith("postgres"):
        pytest.skip("needs BOSS_TEST_DATABASE_URL")
    from boss_engine.db import ensure_schema

    eng = create_engine(PG_URL, future=True, pool_pre_ping=True)
    ensure_schema(eng)
    return eng


def make_project(engine) -> str:
    pid = f"p_intake_{uuid.uuid4().hex[:10]}"
    with engine.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES ('org_intake', 'intake') "
                       "ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_intake', 'org_intake', "
                       "'intake') ON CONFLICT DO NOTHING"))
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                       "(:p, 'ws_intake', :p, :p)"), {"p": pid})
        c.execute(text(
            "INSERT INTO project_members (id, project_id, member_id, display_name, member_type, "
            "role_keys, permissions) VALUES (:i, :p, 'ceo', 'ceo', 'human', '[\"ceo\"]'::jsonb, "
            "'[\"decision_resolve\"]'::jsonb) ON CONFLICT DO NOTHING"),
            {"i": f"pm_{pid}_ceo", "p": pid})
    return pid


def purge(engine, pid: str) -> None:
    with engine.begin() as c:
        for sql in (
            "DELETE FROM agent_trigger_fires WHERE project_id = :p",
            "DELETE FROM agent_triggers WHERE project_id = :p",
            "DELETE FROM intake_events WHERE project_id = :p",
            "DELETE FROM intake_connections WHERE project_id = :p",
            "DELETE FROM jobs WHERE project_id = :p",
            "DELETE FROM agent_runs WHERE project_id = :p",
            "DELETE FROM agent_threads WHERE project_id = :p",
            "DELETE FROM agent_deployments WHERE project_id = :p",
            "DELETE FROM events WHERE project_id = :p",
            "DELETE FROM project_members WHERE project_id = :p",
        ):
            try:
                with c.begin_nested():
                    c.execute(text(sql), {"p": pid})
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
    with engine.begin() as c:
        try:
            c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})
        except Exception:  # noqa: BLE001
            pass


def make_connection(engine, pid: str, kind: str = "webhook", *, name: str = "test",
                    config: Optional[dict] = None, secrets: Optional[dict] = None,
                    limits: Optional[dict] = None) -> dict:
    from boss_engine.services import event_intake

    with engine.begin() as c:
        out = event_intake.create_connection(c, project_id=pid, kind=kind, actor="ceo", name=name,
                                             config=config or {}, secrets=secrets or {},
                                             limits=limits or {})
    return out


def token_of(url_path: str) -> str:
    return url_path.rsplit("/", 1)[-1]


# ---- signed requests ---------------------------------------------------------------------------

def standard_webhook_request(secret: str, body: dict | bytes, *, msg_id: Optional[str] = None,
                             timestamp: Optional[int] = None, extra_headers: Optional[dict] = None):
    from boss_engine.connectors.intake.contract import RawRequest
    from boss_engine.connectors.intake.webhook.adapter import standard_webhooks_signature

    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    msg_id = msg_id or f"msg_{uuid.uuid4().hex[:12]}"
    ts = str(timestamp if timestamp is not None else int(time.time()))
    headers = {"content-type": "application/json", "webhook-id": msg_id, "webhook-timestamp": ts,
               "webhook-signature": standard_webhooks_signature(secret, msg_id, ts, raw),
               **{k.lower(): v for k, v in (extra_headers or {}).items()}}
    return RawRequest(method="POST", headers=headers, body=raw)


def boss_v1_request(secret: str, body: dict | bytes, *, delivery: Optional[str] = None,
                    timestamp: Optional[int] = None):
    from boss_engine.connectors.intake.contract import RawRequest
    from boss_engine.connectors.intake.webhook.adapter import boss_v1_signature

    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    ts = str(timestamp if timestamp is not None else int(time.time()))
    headers = {"content-type": "application/json", "x-boss-timestamp": ts,
               "x-boss-signature": boss_v1_signature(secret, ts, raw)}
    if delivery:
        headers["x-boss-delivery"] = delivery
    return RawRequest(method="POST", headers=headers, body=raw)


def github_request(secret: str, event: str, body: dict | bytes, *, delivery: Optional[str] = None,
                   extra_headers: Optional[dict] = None):
    from boss_engine.connectors.intake.contract import RawRequest
    from boss_engine.connectors.intake.github.adapter import github_signature

    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    headers = {"content-type": "application/json", "x-github-event": event,
               "x-github-delivery": delivery or f"d-{uuid.uuid4()}",
               "x-hub-signature-256": github_signature(secret, raw),
               **{k.lower(): v for k, v in (extra_headers or {}).items()}}
    return RawRequest(method="POST", headers=headers, body=raw)


def github_pr_opened(number: int = 7, *, sender: str = "priya", sender_type: str = "User",
                     repo: str = "northpeak/payments-api") -> dict:
    return {"action": "opened", "number": number,
            "pull_request": {"number": number, "title": "Fix the payments job",
                             "body": "Retries the nightly job.", "state": "open", "draft": False,
                             "html_url": f"https://github.com/{repo}/pull/{number}",
                             "labels": [{"name": "bug"}], "updated_at": "2026-11-14T09:14:02Z",
                             "base": {"ref": "main", "sha": "a" * 40},
                             "head": {"ref": "fix/payments", "sha": "b" * 40}},
            "repository": {"full_name": repo, "private": True},
            "sender": {"login": sender, "id": 42, "type": sender_type},
            "installation": {"id": 9001}}


def github_push(*, commits: int = 2, repo: str = "northpeak/payments-api",
                with_files: bool = True) -> dict:
    items = []
    for n in range(commits):
        commit: dict[str, Any] = {"id": f"{n:040x}", "message": f"commit {n}",
                                  "timestamp": "2026-11-14T09:00:00Z",
                                  "author": {"name": "Priya", "username": "priya"}}
        if with_files:
            commit.update(added=[f"src/new_{n}.py"], removed=[], modified=["README.md"])
        items.append(commit)
    return {"ref": "refs/heads/main", "before": "c" * 40, "after": "d" * 40, "created": False,
            "deleted": False, "forced": False, "compare": f"https://github.com/{repo}/compare/x",
            "commits": items, "head_commit": items[-1] if items else None,
            "pusher": {"name": "priya"}, "repository": {"full_name": repo, "private": False},
            "sender": {"login": "priya", "id": 42, "type": "User"}}


# ---- agents ------------------------------------------------------------------------------------

def deploy_local_agent(engine, project_id: str, *, key: Optional[str] = None) -> dict:
    from boss_engine.agents import versions
    from boss_engine.services import agent_deployments

    key = key or f"support_{uuid.uuid4().hex[:6]}"
    with engine.begin() as c:
        versions.save(c, project_id, {
            "apiVersion": "boss.agent/v1", "kind": "Agent",
            "metadata": {"key": key, "name": "Support agent"},
            "spec": {"mission": "Answer support questions.", "prompts": {"system": "I am v1."},
                     "limits": {"max_steps": 6}}}, actor="ceo", source="operator")
        return agent_deployments.deploy(c, project_id, key, "dev", actor="ceo", version=1)


def make_route(engine, project_id: str, deployment_id: str, connection_id: str, *,
               name: str = "route", concurrency: str = "always_enqueue") -> dict:
    from boss_engine.services import agent_triggers

    with engine.begin() as c:
        return agent_triggers.create(c, project_id=project_id, deployment_id=deployment_id,
                                     kind="intake", actor="ceo", name=name,
                                     concurrency=concurrency, connection_id=connection_id,
                                     config={"goal": "Handle the event."})


def runs_of(engine, project_id: str) -> list[dict]:
    with engine.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT id, status, trigger_id, thread_id FROM agent_runs WHERE project_id = :p "
            "ORDER BY created_at"), {"p": project_id}).mappings().all()]


def event_row(engine, event_id: str) -> dict:
    with engine.begin() as c:
        return dict(c.execute(text("SELECT * FROM intake_events WHERE id = :i"),
                              {"i": event_id}).mappings().one())
