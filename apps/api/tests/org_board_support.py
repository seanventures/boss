"""Fixtures for the Organization board tests (org-view spec §7.1): agents in their own teams and
acts written straight into the ledger, so every expected number is a count the test can make
itself. The real-run journeys (gates, failed steps) use workflow_management_support."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import text

from boss_engine.services import agents_org_board

PROJECT = "boss-dogfood"
NOW = datetime.now(timezone.utc)


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:8]}"


def team(prefix: str = "t") -> str:
    """A team of the test's own (a free-text team is its own key), so seeded agents in the
    shipped teams never land in the numbers."""
    return f"{prefix}{uuid4().hex[:6]}"


def role(engine, key: str, team_key: str, **spec: Any) -> str:
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO roles (id, project_id, role_key, display_name, spec) "
            "VALUES (:id, :p, :k, :n, CAST(:s AS jsonb))"),
            {"id": uid("role"), "p": PROJECT, "k": key, "n": key.replace("_", " ").title(),
             "s": json.dumps({"team": team_key, **spec})})
    return key


def act(engine, role_key: Optional[str], *, parent: Optional[str] = None,
        root: Optional[str] = None, kind: str = "step", status: str = "complete",
        started: timedelta = timedelta(hours=2), ended: Optional[timedelta] = timedelta(hours=1),
        how: str = "named", handed_by: str = "agent", label: str = "Work",
        run_id: Optional[str] = None, decision_id: Optional[str] = None) -> str:
    """One act, `started` and `ended` ago. `ended=None` leaves it open."""
    act_id = uid("act")
    with engine.begin() as c:
        c.execute(text(
            """INSERT INTO agent_acts (act_id, project_id, role_key, kind, status, parent_act_id,
                   root_act_id, handed_by_kind, how, accountable, label, started_at, ended_at,
                   workflow_run_id, decision_id)
               VALUES (:a, :p, :r, :k, :s, :parent, :root, :hb, :how, 'ceo', :l, :st, :en,
                       :run, :dec)"""),
            {"a": act_id, "p": PROJECT, "r": role_key, "k": kind, "s": status, "parent": parent,
             "root": root or parent or act_id, "hb": handed_by, "how": how, "l": label,
             "st": NOW - started, "en": (NOW - ended) if ended is not None else None,
             "run": run_id, "dec": decision_id})
    return act_id


def root(engine, *, started: timedelta = timedelta(hours=3), run_id: Optional[str] = None) -> str:
    """A run's root: the person starting it (no agent)."""
    return act(engine, None, kind="run", status="complete", started=started,
               ended=timedelta(minutes=1), how="started", handed_by="person", run_id=run_id)


def call(engine, *, act_id: Optional[str] = None, role_key: Optional[str] = None,
         stage: Optional[str] = None, micros: int = 1_000_000, included: bool = False,
         model: str = "claude-sonnet-4-6", ago: timedelta = timedelta(hours=1)) -> str:
    call_id = uid("llm")
    with engine.begin() as c:
        c.execute(text(
            """INSERT INTO llm_calls (id, project_id, stage, engine, model, cost_usd_micros,
                   created_at, included_in_subscription, role, act_id)
               VALUES (:id, :p, :st, 'native', :m, :c, :at, :inc, :r, :a)"""),
            {"id": call_id, "p": PROJECT, "st": stage, "m": model, "c": micros,
             "at": NOW - ago, "inc": included, "r": role_key, "a": act_id})
    return call_id


def board(client, days: int = 7, **headers) -> Any:
    agents_org_board.clear_cache()
    return client.get("/v1/agents/org-board", params={"project_id": PROJECT, "days": days},
                      headers=headers)


def team_row(body: dict, key: str) -> dict:
    found = [t for t in body["teams"] if t["key"] == key]
    assert found, [t["key"] for t in body["teams"]]
    return found[0]


def agent_row(body: dict, key: str) -> dict:
    found = [a for a in body["agents"] if a["role_key"] == key]
    assert found, key
    return found[0]


def workflow(engine, steps: list[dict], *, name: Optional[str] = None) -> str:
    """A product workflow whose active version is `steps`, written as publish writes it. These
    tests are about what the board READS from a reachable version, not about publish's checks
    (which refuse a step naming an agent the project lacks — exactly the stale state a deleted
    agent leaves behind)."""
    def_id, ver_id, key = uid("wfd"), uid("wfv"), uid("wf")
    spec = {"entry_step": steps[0]["id"], "steps": steps}
    with engine.begin() as c:
        c.execute(text(
            """INSERT INTO workflow_definitions (id, project_id, workflow_key, name, status,
                   namespace, created_by) VALUES (:d, :p, :k, :n, 'active', 'product', 'ceo')"""),
            {"d": def_id, "p": PROJECT, "k": key, "n": name or key})
        c.execute(text(
            """INSERT INTO workflow_versions (id, project_id, workflow_definition_id, version,
                   status, spec, content_hash, created_by)
               VALUES (:v, :p, :d, 1, 'published', CAST(:s AS jsonb), :h, 'ceo')"""),
            {"v": ver_id, "p": PROJECT, "d": def_id, "s": json.dumps(spec), "h": uid("h")})
        c.execute(text("UPDATE workflow_definitions SET active_version_id = :v WHERE id = :d"),
                  {"v": ver_id, "d": def_id})
    return key


def step(step_id: str, *, captain: Optional[str] = None, **binding: Any) -> dict:
    out: dict[str, Any] = {"id": step_id, "kind": "capability", "title": step_id.title(),
                           "next": [], "binding": {"agent_work": True, **binding}}
    if captain:
        out["captain"] = captain
    return out
