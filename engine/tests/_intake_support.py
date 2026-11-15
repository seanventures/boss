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
            "DELETE FROM chat_outbox WHERE project_id = :p",
            "DELETE FROM chat_identity_links WHERE endpoint_id IN "
            "(SELECT id FROM chat_endpoints WHERE project_id = :p)",
            "DELETE FROM scoped_budget_policies WHERE scope_id IN "
            "(SELECT id FROM agent_triggers WHERE project_id = :p)",
            "DELETE FROM llm_calls WHERE project_id = :p",
            "DELETE FROM agent_triggers WHERE project_id = :p",
            "DELETE FROM intake_events WHERE project_id = :p",
            "DELETE FROM intake_connections WHERE project_id = :p",
            "DELETE FROM jobs WHERE project_id = :p",
            "DELETE FROM agent_runs WHERE project_id = :p",
            "DELETE FROM agent_threads WHERE project_id = :p",
            "DELETE FROM agent_deployments WHERE project_id = :p",
            "DELETE FROM chat_endpoints WHERE project_id = :p",
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
               name: str = "route", concurrency: str = "always_enqueue",
               config: Optional[dict] = None, priority: int = 100,
               identity_mode: Optional[str] = None, thread_key: Optional[str] = None,
               limits: Optional[dict] = None, quiet_hours: Optional[dict] = None) -> dict:
    from boss_engine.services import agent_triggers

    with engine.begin() as c:
        return agent_triggers.create(c, project_id=project_id, deployment_id=deployment_id,
                                     kind="intake", actor="ceo", name=name,
                                     concurrency=concurrency, connection_id=connection_id,
                                     config={"goal": "Handle the event.", **(config or {})},
                                     priority=priority, identity_mode=identity_mode,
                                     thread_key=thread_key, limits=limits,
                                     quiet_hours=quiet_hours)


def attach_chat_endpoint(engine, project_id: str, connection_id: str, *,
                         team_id: str = "T0TEST") -> str:
    """A Slack chat endpoint owned by the connection (what a Slack intake connection has, P9a-3):
    identity links, link prompts and one-line replies go through it."""
    endpoint_id = f"che_{uuid.uuid4().hex[:10]}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO chat_endpoints (id, project_id, kind, name, config, created_by) "
            "VALUES (:i, :p, 'slack', 'test slack', CAST(:cfg AS JSONB), 'ceo')"),
            {"i": endpoint_id, "p": project_id, "cfg": json.dumps({"team_id": team_id})})
        c.execute(text("UPDATE intake_connections SET chat_endpoint_id = :e, "
                       "config = config || CAST(:cfg AS JSONB) WHERE id = :c"),
                  {"e": endpoint_id, "c": connection_id, "cfg": json.dumps({"team_id": team_id})})
    return endpoint_id


def make_person(engine, email: str, *, name: str = "Priya", org_id: str = "org_intake",
                verified: bool = True) -> str:
    from boss_engine.identity.users import create_user

    with engine.begin() as c:
        return create_user(c, org_id=org_id, email=email, display_name=name,
                           email_verified=verified)


def link_chat_user(engine, endpoint_id: str, external_user_id: str, principal_id: str) -> str:
    link_id = f"chl_{uuid.uuid4().hex[:10]}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO chat_identity_links (id, endpoint_id, external_user_id, external_name, "
            " principal_id, person_actor, verified_at, method) VALUES (:i, :e, :u, 'Priya', :p, "
            " :p, now(), 'code')"),
            {"i": link_id, "e": endpoint_id, "u": external_user_id, "p": principal_id})
    return link_id


def runs_of(engine, project_id: str) -> list[dict]:
    with engine.begin() as c:
        return [dict(r) for r in c.execute(text(
            "SELECT id, status, trigger_id, thread_id FROM agent_runs WHERE project_id = :p "
            "ORDER BY created_at"), {"p": project_id}).mappings().all()]


def event_row(engine, event_id: str) -> dict:
    with engine.begin() as c:
        return dict(c.execute(text("SELECT * FROM intake_events WHERE id = :i"),
                              {"i": event_id}).mappings().one())


# ---- Slack (P9a-3/6) ----------------------------------------------------------------------------

SLACK_SIGNING = "slack-signing-secret-for-tests"
SLACK_BOT_TOKEN = "xoxb-test-bot-token-never-leaves-the-outbox"
SLACK_TEAM = "T0TEST"
SLACK_BOT_USER = "U0BOSS"
SLACK_BOT_ID = "B0BOSS"


class FakeSlack:
    """A fake Slack Web API (chat_adapters.TRANSPORT): records every call with the time it
    arrived, answers like Slack does, and can answer 429 / 500 to the next calls of a method."""

    def __init__(self):
        import threading as _threading

        self.calls: list[dict] = []
        self.lock = _threading.Lock()
        self.script: dict[str, list] = {}
        self._ts = 1700000000.0

    def fail_next(self, method: str, *answers) -> None:
        """Queue answers for the next calls of `method`: ("429", retry_after) or ("500",)."""
        self.script.setdefault(method, []).extend(answers)

    def __call__(self, method, url, headers, body, purpose):
        name = url.rsplit("/", 1)[-1]
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            payload = {}
        with self.lock:
            self.calls.append({"name": name, "body": payload, "at": time.monotonic(),
                               "auth": headers.get("Authorization", "")})
            queued = self.script.get(name) or []
            answer = queued.pop(0) if queued else None
            self._ts += 0.0001
            ts = f"{self._ts:.6f}"
        if answer is not None:
            if answer[0] == "429":
                return 429, {"ok": False, "error": "ratelimited", "retry_after": answer[1]}
            return 500, None
        if name == "chat.postMessage":
            return 200, {"ok": True, "channel": payload.get("channel"), "ts": ts,
                         "message": {"bot_id": SLACK_BOT_ID, "ts": ts}}
        if name == "chat.update":
            return 200, {"ok": True, "channel": payload.get("channel"), "ts": payload.get("ts")}
        if name == "conversations.open":
            return 200, {"ok": True, "channel": {"id": "D0DM" + str(payload.get("users"))[-4:]}}
        if name == "chat.startStream":
            return 200, {"ok": True, "ts": ts}
        if name == "apps.connections.open":
            return 200, {"ok": True, "url": "wss://wss-primary.slack.com/link/?ticket=t"}
        return 200, {"ok": True}

    def named(self, name: str) -> list[dict]:
        with self.lock:
            return [c["body"] for c in self.calls if c["name"] == name]


def make_slack(engine, pid: str, *, mode: str = "push", channel: str = "C0SUPPORT",
               config: Optional[dict] = None, secrets: Optional[dict] = None) -> dict:
    """The install's Slack app as a chat endpoint, and the Slack intake connection riding on it
    (one Slack app per install, D1). Returns {endpoint, connection, token}."""
    from boss_engine.config import get_settings
    from boss_engine.services import chat_channels, event_intake

    with engine.begin() as c:
        endpoint = chat_channels.create_endpoint(
            c, get_settings(), project_id=pid, kind="slack", name="slack",
            config={"channel": channel, "team_id": SLACK_TEAM},
            secrets={"signing_secret": SLACK_SIGNING, "bot_token": SLACK_BOT_TOKEN}, actor="ceo")
        conn = event_intake.create_connection(
            c, project_id=pid, kind="slack", actor="ceo", name="slack", mode=mode,
            config={"chat_endpoint_id": endpoint["id"], "bot_user_id": SLACK_BOT_USER,
                    **(config or {})}, secrets=secrets or {})
    return {"endpoint": endpoint, "connection": conn, "token": token_of(conn["url_path"])}


def slack_body(event: dict, *, event_id: Optional[str] = None, team: str = SLACK_TEAM) -> dict:
    return {"token": "x", "team_id": team, "api_app_id": "A0BOSS", "type": "event_callback",
            "event_id": event_id or f"Ev{uuid.uuid4().hex[:10].upper()}",
            "event_time": int(time.time()), "event": event,
            "authorizations": [{"team_id": team, "user_id": SLACK_BOT_USER, "is_bot": True}]}


def slack_request(body: dict | bytes, *, secret: str = SLACK_SIGNING, retry: int = 0,
                  timestamp: Optional[int] = None):
    from boss_engine.connectors.intake.contract import RawRequest
    from boss_engine.services.chat_adapters import slack_signature

    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    ts = str(timestamp if timestamp is not None else int(time.time()))
    headers = {"content-type": "application/json", "x-slack-request-timestamp": ts,
               "x-slack-signature": slack_signature(secret, ts, raw)}
    if retry:
        headers["x-slack-retry-num"] = str(retry)
        headers["x-slack-retry-reason"] = "http_timeout"
    return RawRequest(method="POST", headers=headers, body=raw)


def mention(text_: str = "why did the payments job fail?", *, user: str = "U0PRIYA",
            channel: str = "C0SUPPORT", ts: Optional[str] = None,
            thread_ts: Optional[str] = None) -> dict:
    ev = {"type": "app_mention", "user": user, "channel": channel,
          "text": f"<@{SLACK_BOT_USER}> {text_}", "ts": ts or f"{time.time():.6f}",
          "team": SLACK_TEAM}
    if thread_ts:
        ev["thread_ts"] = thread_ts
    return ev


def channel_message(text_: str, *, user: str = "U0PRIYA", channel: str = "C0SUPPORT",
                    ts: Optional[str] = None, thread_ts: Optional[str] = None,
                    **extra: Any) -> dict:
    ev = {"type": "message", "channel_type": "channel", "user": user, "channel": channel,
          "text": text_, "ts": ts or f"{time.time():.6f}", "team": SLACK_TEAM, **extra}
    if thread_ts:
        ev["thread_ts"] = thread_ts
    return ev


def direct_message(text_: str, *, user: str = "U0PRIYA", channel: str = "D0PRIYA",
                   ts: Optional[str] = None) -> dict:
    return {"type": "message", "channel_type": "im", "user": user, "channel": channel,
            "text": text_, "ts": ts or f"{time.time():.6f}", "team": SLACK_TEAM}


def purge_slack(engine, pid: str) -> None:
    with engine.begin() as c:
        for sql in ("DELETE FROM chat_threads WHERE project_id = :p",
                    "DELETE FROM extension_job_leases WHERE extension_id = 'boss.intake' AND "
                    "job IN (SELECT 'slack_socket:' || id FROM intake_connections "
                    "WHERE project_id = :p)",
                    "DELETE FROM secret_refs WHERE project_id = :p"):
            try:
                with c.begin_nested():
                    c.execute(text(sql), {"p": pid})
            except Exception:  # noqa: BLE001
                pass
    purge(engine, pid)
