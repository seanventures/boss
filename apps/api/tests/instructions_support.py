"""Shared set-up for the agent-instructions tests (agent-loadout spec §8.1)."""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text

PROJECT = "boss-dogfood"
MARKER = "ZEBRA-CHECK-7: always list untested branches first"


def make_agent(client, *, prefix: str = "ins") -> str:
    key = f"{prefix}_{uuid4().hex[:8]}"
    made = client.post("/v1/agents", json={"project_id": PROJECT, "actor": "ceo",
                                           "name": "Checklist Agent", "role_key": key})
    assert made.status_code == 201, made.text
    return key


def add(client, key: str, body: str, *, name: str = "review-checklist.md", modes=None,
        source: str = "file", dry_run: bool = False, actor: str = "ceo", headers=None):
    payload = {"project_id": PROJECT, "actor": actor, "name": name, "body": body,
               "source": source, "dry_run": dry_run}
    if modes is not None:
        payload["modes"] = modes
    return client.post(f"/v1/agents/{key}/instructions", json=payload, headers=headers or {})


def version_of(engine, key: str) -> tuple[int, str]:
    with engine.connect() as c:
        row = c.execute(text("SELECT identity_version, fingerprint FROM roles WHERE project_id = :p "
                             "AND role_key = :r"), {"p": PROJECT, "r": key}).mappings().one()
    return int(row["identity_version"]), str(row["fingerprint"])


def stored(engine, key: str) -> int:
    with engine.connect() as c:
        return int(c.execute(text("SELECT count(*) FROM agent_instructions WHERE project_id = :p "
                                  "AND role_key = :r"), {"p": PROJECT, "r": key}).scalar())
