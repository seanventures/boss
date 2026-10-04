"""Every agent holds its role's default loadout (skills-mcp §3.2, §6.2, test 6).

Role specs name ABILITIES (`permissions.abilities`); the seed expands them through the kit file
into the same role_skills and role_mcp_servers rows a person would write. The ● cells of the
spec's table are exactly what each role holds on a fresh project — except Library docs, whose
server (Context7) is added from the catalogue, after which the next sync grants it. A removal
survives the next sync.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

PROJECT = "boss-dogfood"

GRANTED = {
    "engineering_captain": {"read_repo", "tests_ci", "file_tickets", "watch_health"},
    "backend_engineer": {"read_repo", "open_prs", "tests_ci", "library_docs", "watch_health"},
    "frontend_engineer": {"read_repo", "open_prs", "tests_ci", "library_docs", "web_research"},
    "software_engineer": {"read_repo", "open_prs", "tests_ci", "library_docs", "watch_health"},
    "operations_engineer": {"read_repo", "tests_ci", "file_tickets", "deploy", "watch_health"},
    "sre": {"read_repo", "file_tickets", "watch_health"},
    "pm": {"read_repo", "web_research", "web_search", "render_docs", "file_tickets"},
    "qa": {"read_repo", "tests_ci", "web_research"},
    "security_reviewer": {"read_repo", "tests_ci", "web_research"},
    "chief_architect": {"read_repo", "library_docs", "web_research", "web_search",
                        "watch_health"},
    "adversarial_reviewer": {"read_repo", "tests_ci"},
    "docs_writer": {"read_repo", "open_prs", "library_docs", "render_docs"},
    "release_manager": {"read_repo", "tests_ci", "render_docs", "deploy", "watch_health"},
    "program_manager": {"read_repo", "file_tickets", "email"},
    "design_critic": {"read_repo", "web_research"},
}
RECOMMENDED_ONLY = {
    "engineering_captain": {"team_chat"}, "backend_engineer": {"web_search"},
    "frontend_engineer": {"web_search"}, "software_engineer": {"web_search"},
    "operations_engineer": {"team_chat"}, "sre": {"team_chat", "deploy"},
    "pm": {"email", "team_chat"}, "qa": {"file_tickets"}, "security_reviewer": {"web_search"},
    "release_manager": {"email", "team_chat"}, "program_manager": {"team_chat"},
}


def _sync(engine):
    from boss_engine.services.agents import sync_role_mcp

    with engine.begin() as c:
        return sync_role_mcp(c, project_id=PROJECT, actor="test-seed")


def _held(engine) -> dict[str, set[str]]:
    from boss_engine.config import get_settings
    from boss_engine.services import kit

    with engine.begin() as c:
        abilities = kit.definitions(get_settings())["abilities"]
        held = kit.holders(c, get_settings(), PROJECT, abilities)
    out: dict[str, set[str]] = {}
    for key, roles in held.items():
        for role in roles:
            out.setdefault(role, set()).add(key)
    return out


@pytest.fixture()
def roles(engine):
    from sqlalchemy import text

    with engine.begin() as c:
        present = set(c.execute(text("SELECT role_key FROM roles WHERE project_id=:p"),
                                {"p": PROJECT}).scalars())
    missing = set(GRANTED) - present
    assert not missing, f"the cast is not installed: {missing}"
    return present


def test_a_fresh_project_holds_exactly_the_granted_cells(engine, roles):
    _sync(engine)
    held = _held(engine)
    for role, wanted in GRANTED.items():
        # Context7 is not registered until someone adds it: nobody can hold Library docs yet.
        assert held.get(role, set()) == wanted - {"library_docs"}, role


def test_library_docs_is_granted_once_its_server_is_added(engine, roles):
    from boss_engine.config import get_settings
    from boss_engine.services.kit_connections import add_catalogue_server
    from boss_engine.services.mcp_registry import delete_mcp_server

    with engine.begin() as c:
        server = add_catalogue_server(c, get_settings(), PROJECT, "context7", token=None,
                                      actor="ceo")
    try:
        _sync(engine)
        held = _held(engine)
        for role, wanted in GRANTED.items():
            assert held.get(role, set()) == wanted, role
    finally:
        # mcp_servers outlives a test here (the suite's reset does not cover it).
        with engine.begin() as c:
            delete_mcp_server(c, PROJECT, server["id"], actor="ceo", release_grants=True)
            c.execute(text("DELETE FROM seed_exclusions WHERE project_id=:p "
                           "AND kind='mcp_server' AND subject='context7'"), {"p": PROJECT})


def test_the_role_recommends_its_granted_and_offered_abilities(client, engine, roles):
    _sync(engine)
    kit = client.get("/v1/kit", params={"project_id": PROJECT, "check": "none"}).json()
    by_key = {a["key"]: set(a["recommended"]) for a in kit["abilities"]}
    for role in GRANTED:
        recommended = {k for k, rs in by_key.items() if role in rs}
        assert recommended == GRANTED[role] | RECOMMENDED_ONLY.get(role, set()), role


def test_a_removal_survives_the_next_sync(client, engine, roles):
    _sync(engine)
    holders = sorted(r for r, keys in _held(engine).items() if "web_research" in keys)
    assert "pm" in holders
    put = client.put("/v1/kit/web_research/holders", json={
        "project_id": PROJECT, "actor": "ceo", "roles": [r for r in holders if r != "pm"]})
    assert put.status_code == 200, put.text
    assert "pm" not in put.json()["holders"]
    _sync(engine)
    assert "web_research" not in _held(engine).get("pm", set())
    # Juno keeps Web search, which shares web-intel: only research's tools went.
    assert "web_search" in _held(engine)["pm"]
