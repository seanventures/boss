"""Shared support for the squad tests (ambient/squads.py, squad_turns.py; BOSSAPP-908).

Nothing here is collected. No test calls a model: members answer through
`squad_demo.ScriptedSquadModel`, which speaks the one pipeline's JSON contract, so every answer
still passes agent_decision's argument schema, the runner's check and the board's validation.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Iterable, Optional

from sqlalchemy import text

from boss_engine.services.ambient import squads
from boss_engine.services.ambient.squad_demo import DemoLaunchers, ScriptedSquadModel

from ._ambient_support import open_item

__all__ = ["add_roles", "squad_item", "ScriptedSquadModel", "Launchers", "move", "settings"]

CVE_ROLES = ("security_reviewer", "software_engineer", "qa", "adversarial_reviewer")
LEAD, ENGINEER, QA, CRITIC = (f"agent:{r}" for r in CVE_ROLES)


def settings():
    from boss_engine.config import get_settings

    return get_settings()


def add_roles(c, pid: str, keys: Iterable[str], *, lifecycle: Optional[dict] = None) -> None:
    for key in keys:
        c.execute(text(
            "INSERT INTO roles (id, project_id, role_key, display_name, spec, lifecycle) VALUES "
            "(:id, :p, :k, :n, '{}'::jsonb, :l) ON CONFLICT (project_id, role_key) DO NOTHING"),
            {"id": f"role_{uuid.uuid4().hex[:10]}", "p": pid, "k": key,
             "n": key.replace("_", " ").title(), "l": (lifecycle or {}).get(key, "active")})


def squad_item(c, pid: str, *, task_class: str = "security_patch", fingerprint: str = "fp:cve",
               roles: Iterable[str] = CVE_ROLES, template: Optional[str] = None) -> dict[str, Any]:
    """An open item with a squad seated (the lead holds it). Returns assemble's result + item."""
    add_roles(c, pid, roles)
    item = open_item(c, pid, task_class=task_class, fingerprint=fingerprint,
                     title="lodash CVE-2021-23337 in web", subject_ref="package:lodash")["item"]
    got = squads.assemble(c, item["id"], template=template)
    return {**got, "item": item}


def move(_move: str, /, **arguments: Any) -> dict[str, Any]:
    return {"action": _move, "arguments": arguments}


class Launchers(DemoLaunchers):
    """DemoLaunchers whose run ends with `run_status` (completed by default) or that refuse."""

    def __init__(self, *, run_status: str = "completed", refuse: Optional[str] = None,
                 pr: str = "acme/web#41"):
        super().__init__(panel_verdict="Bump and escape in one PR", pr=pr)
        self.run_status, self.refuse = run_status, refuse
        self.calls: list[dict[str, Any]] = []

    def start(self, connection, **kw):
        self.calls.append({"start": kw})
        if self.refuse:
            return {"error": self.refuse}
        got = super().start(connection, **kw)
        if self.run_status != "completed":
            connection.execute(text(
                "UPDATE workflow_runs SET status = :s, error = 'tests failed: 2 of 40', "
                "output = '{}'::jsonb WHERE id = :r"), {"s": self.run_status, "r": got["run_id"]})
        return got

    def convene(self, connection, **kw):
        self.calls.append({"convene": kw})
        if self.refuse:
            return {"error": self.refuse}
        return super().convene(connection, **kw)


def board_kinds(c, item_id: str) -> list[str]:
    return list(c.execute(text(
        "SELECT kind FROM work_item_messages WHERE work_item_id = :w ORDER BY created_at, id"),
        {"w": item_id}).scalars())


def dumps(value: Any) -> str:
    return json.dumps(value, default=str)
