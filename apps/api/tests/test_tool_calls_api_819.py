"""GET /v1/tool-calls and /v1/tool-calls/export (BOSSAPP-819, routers/tool_calls.py).

* The list filters (run, server, tool, decision, outcome, screen, time) and keyset-pages.
* The export is the same filter as NDJSON, one row per line.
* A bad filter or cursor is a 422 in words.
* Authz: a reader of the project lists; a viewer cannot export (activity:export); a person from
  another project reads nothing.
"""
from __future__ import annotations

import json
from uuid import uuid4

from boss_engine.services.tool_call_audit import record_tool_call

from apps.api.tests.identity_fixtures import IdentityWorld


def _seed(engine, project: str, tag: str) -> list[str]:
    ids = []
    for i in range(5):
        with engine.begin() as c:
            ids.append(record_tool_call(
                c, project_id=project, run_id=f"run-{tag}", agent="atlas", principal="usr_1",
                server_id="srv-a" if i < 3 else "srv-b", tool="fetch" if i % 2 else "write",
                arguments={"url": "https://example.com", "token": "abc"},
                decision="ask" if i == 0 else "allow", matched_policy_ids=["ask" if i == 0 else "allow"],
                decision_id="dec_x" if i == 0 else None,
                outcome="refused" if i == 0 else "succeeded", latency_ms=i,
                screen={"mode": "patterns", "verdict": "suspicious" if i == 4 else "clean",
                        "rules": []} if i else None,
                explanation="tier 2 asks a person" if i == 0 else None))
    return ids


def test_the_list_filters_pages_and_exports(client, engine):
    tag = uuid4().hex[:8]
    project = "boss-dogfood"
    ids = _seed(engine, project, tag)
    base = {"project_id": project, "run_id": f"run-{tag}"}

    got = client.get("/v1/tool-calls", params=base)
    assert got.status_code == 200, got.text
    items = got.json()["items"]
    assert [i["id"] for i in items] == list(reversed(ids))
    assert items[-1]["args_redacted"]["token"] == "[redacted]"
    assert items[-1]["explanation"] == "tier 2 asks a person"
    assert items[-1]["decision_id"] == "dec_x"

    def ids_for(**f):
        r = client.get("/v1/tool-calls", params={**base, **f})
        assert r.status_code == 200, r.text
        return [i["id"] for i in r.json()["items"]]

    assert ids_for(server_id="srv-b") == [ids[4], ids[3]]
    assert ids_for(tool="fetch") == [ids[3], ids[1]]
    assert ids_for(decision="ask") == [ids[0]] == ids_for(outcome="refused")
    assert ids_for(screen="suspicious") == [ids[4]]
    assert ids_for(screen="none") == [ids[0]]
    assert ids_for(until="2000-01-01T00:00:00Z") == []

    first = client.get("/v1/tool-calls", params={**base, "limit": 2}).json()
    assert len(first["items"]) == 2 and first["next"]
    second = client.get("/v1/tool-calls", params={**base, "limit": 2, "cursor": first["next"]}).json()
    third = client.get("/v1/tool-calls", params={**base, "limit": 2, "cursor": second["next"]}).json()
    paged = [i["id"] for p in (first, second, third) for i in p["items"]]
    assert paged == list(reversed(ids)) and third["next"] is None

    assert client.get("/v1/tool-calls", params={**base, "screen": "evil"}).status_code == 422
    assert client.get("/v1/tool-calls", params={**base, "cursor": "nope"}).status_code == 422
    assert client.get("/v1/tool-calls", params={**base, "since": "yesterday"}).status_code == 422

    out = client.get("/v1/tool-calls/export", params={**base, "server_id": "srv-a"})
    assert out.status_code == 200, out.text
    assert out.headers["content-type"].startswith("application/x-ndjson")
    lines = [json.loads(line) for line in out.text.splitlines() if line.strip()]
    assert [r["id"] for r in lines] == [ids[2], ids[1], ids[0]]
    assert all(r["args_redacted"]["token"] == "[redacted]" for r in lines)


def test_reading_is_per_project_and_export_needs_the_export_permission(client, engine):
    world = IdentityWorld.create(engine)
    tag = uuid4().hex[:8]
    _seed(engine, world.project_a, tag)
    viewer = world.person("audit viewer")
    world.bind(viewer, "viewer", project=world.project_a)
    owner = world.person("audit owner")
    world.bind(owner, "owner", project=world.project_a)
    foreign = world.person("audit foreigner")
    world.bind(foreign, "owner", project=world.project_b)
    params = {"project_id": world.project_a}
    with world.acting_as(world.ctx(viewer)):
        listed = client.get("/v1/tool-calls", params=params)
        assert listed.status_code == 200, listed.text
        assert len(listed.json()["items"]) == 5
        assert client.get("/v1/tool-calls/export", params=params).status_code == 403
    with world.acting_as(world.ctx(owner)):
        exported = client.get("/v1/tool-calls/export", params=params)
        assert exported.status_code == 200, exported.text
        assert len([x for x in exported.text.splitlines() if x.strip()]) == 5
    with world.acting_as(world.ctx(foreign)):
        assert client.get("/v1/tool-calls", params=params).status_code in (403, 404)
        assert client.get("/v1/tool-calls/export", params=params).status_code in (403, 404)
        mine = client.get("/v1/tool-calls", params={"project_id": world.project_b})
        assert mine.status_code == 200 and mine.json()["items"] == []
