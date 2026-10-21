"""Knowledge Graph Phase 3: knowledge:read / knowledge:manage and the HTTP API (KNOWLEDGE_GRAPH_SPEC.md §5)."""
from __future__ import annotations

import uuid

from sqlalchemy import text

from apps.api.app.db import begin
from boss_engine.identity import P
from boss_engine.identity.declarations import access_of
from boss_engine.identity.permissions_gen import PASSPORT_PRESETS, PERMISSION_META as PERMISSIONS
from boss_engine.services.knowledge import Scope
from boss_engine.services.knowledge.facts import FactStore, Origin


def test_the_two_permissions_exist_and_manage_needs_step_up():
    read, manage = PERMISSIONS[P.KNOWLEDGE_READ], PERMISSIONS[P.KNOWLEDGE_MANAGE]
    assert read.delegable and not read.step_up
    assert manage.step_up and not manage.delegable   # never an agent's
    assert set(manage.roles) == {"owner", "admin"}


def test_every_passport_preset_may_read_knowledge_and_none_may_manage_it():
    for key, preset in PASSPORT_PRESETS.items():
        assert P.KNOWLEDGE_READ in preset.permissions, key
        assert P.KNOWLEDGE_MANAGE not in preset.permissions, key


def test_the_kg_tools_are_gated_by_knowledge_read():
    from boss_engine.tools.builtin import build_default_registry

    reg = build_default_registry()
    for name in ("kg_symbol", "kg_callers", "kg_blast_radius", "kg_why", "kg_facts", "kg_search", "kg_precedent",
                 "kg_propose_fact"):
        assert reg.get(name).permission == "knowledge:read", name
    assert "decision:read" in reg.get("kg_precedent").also_requires


def test_the_routes_are_gated_as_the_spec_says():
    from apps.api.app.main import app

    want = {("GET", "/v1/knowledge/search"): "knowledge:read",
            ("GET", "/v1/knowledge/nodes/{node_id}"): "knowledge:read",
            ("GET", "/v1/knowledge/blast-radius"): "knowledge:read",
            ("GET", "/v1/knowledge/graph-status"): "knowledge:read",
            ("POST", "/v1/knowledge/facts/{fact_id}/{action}"): "knowledge:manage",
            ("PUT", "/v1/knowledge/settings"): "knowledge:manage"}
    seen = {}
    for route in app.routes:
        for method in getattr(route, "methods", ()) or ():
            if (method, getattr(route, "path", "")) in want:
                decl = access_of(route.endpoint)
                seen[(method, route.path)] = decl.describe() if decl is not None else ""
    assert set(seen) == set(want)
    for key, perm in want.items():
        assert perm in seen[key], (key, seen[key])


def test_search_and_manage_over_http(client, engine):
    with begin(engine) as c:
        pid = c.execute(text("SELECT id FROM projects WHERE id = 'boss-dogfood'")).scalar() or \
            c.execute(text("SELECT id FROM projects ORDER BY created_at LIMIT 1")).scalar_one()
        fid = FactStore().write(c, Scope(pid), {"subject": "ledger.post", "predicate": "is",
                                                "statement": f"Ledger posting is idempotent per key {uuid.uuid4().hex[:6]}",
                                                "kind": "invariant"},
                                Origin("agent", "agent", "test"))["id"]
    r = client.get("/v1/knowledge/search", params={"project_id": pid, "q": "ledger posting idempotent"})
    assert r.status_code == 200, r.text
    assert fid in [f["id"] for f in r.json()["facts"]]
    r = client.get("/v1/knowledge/search", params={"project_id": pid, "q": "ledger posting", "budget_tokens": 200,
                                                   "role": "software_engineer", "stage": "implement"})
    assert r.status_code == 200 and r.json()["tokens"] <= 200
    assert client.get("/v1/knowledge/graph-status", params={"project_id": pid}).status_code == 200
    r = client.post(f"/v1/knowledge/facts/{fid}/forget", json={"project_id": pid})
    assert r.status_code in (200, 401, 403), r.text      # a manage call needs a person with step-up
    if r.status_code == 200:
        assert r.json()["forgotten"] is True
