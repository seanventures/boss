"""What the workflow-management end-to-end tests share: the calls the console makes, and a model.

Everything here goes through the HTTP API the console and the worker use — `/check`, `/publish`,
`/start`, `/run-capabilities`, the decisions inbox, the runs view — on the real database. The only
thing replaced is the model on the other end of an agent step, because a scripted run must never
become a paid one, and because a test has to be able to make a step fail on purpose.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

PROJECT = "boss-dogfood"


# ── The model an agent step talks to ──────────────────────────────────────────────────────────


class ScriptedModel:
    """Stands in for `agent_loop.make_chat_adapter`. Answers "Done." and records what it was
    offered; while `failing` is set it raises, the way a provider outage reaches a step."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.failing: Optional[str] = None

    def create_chat_completion(self, messages, tools=None, **_kwargs):
        prompt = "\n".join(str(m.get("content") or "") for m in messages or [])
        self.calls.append({
            "prompt": prompt,
            "tools": sorted(str((t.get("function") or {}).get("name") or "")
                            for t in (tools or [])),
        })
        if self.failing and self.failing in prompt:
            raise RuntimeError("the model provider is down")
        return ({"role": "assistant", "content": "Done.", "tool_calls": [],
                 "raw": {"role": "assistant", "content": "Done."}},
                {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})


class UnreachableMcp:
    """An MCP client that lists one tool and is never called: a step scoped to a server must
    reach that server's tools and nothing else, and no test may open a socket to find out."""

    def __init__(self, _settings) -> None:
        pass

    def list_tools(self, _url):
        return [{"name": "post", "description": "Post a message."}]


def install_model(monkeypatch) -> ScriptedModel:
    from boss_engine.services import agent_loop

    model = ScriptedModel()
    monkeypatch.setattr(agent_loop, "make_chat_adapter", lambda _settings: model)
    monkeypatch.setattr(agent_loop, "MCPClient", UnreachableMcp)
    return model


# ── Agents, skills and MCP servers ────────────────────────────────────────────────────────────


def key(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:8]}"


def skills(client, n: int = 2) -> list[str]:
    items = client.get("/v1/skills", params={"project_id": PROJECT}).json()["items"]
    assert len(items) >= n, "this project has too few built-in skills; the fixture changed"
    return [i["id"] for i in items[:n]]


def new_agent(client, role_key: str, *, holds: tuple[str, ...] = ()) -> str:
    made = client.post("/v1/roles", json={
        "project_id": PROJECT, "role_key": role_key,
        "display_name": role_key.replace("_", " ").title()})
    assert made.status_code == 201, made.text
    for skill in holds:
        grant_skill(client, role_key, skill)
    return role_key


def grant_skill(client, role_key: str, skill: str) -> None:
    granted = client.post("/v1/role-skills", json={
        "project_id": PROJECT, "role_key": role_key, "skill_id": skill, "actor": "ceo"})
    assert granted.status_code == 201, granted.text


def mcp_server(engine) -> dict:
    from boss_engine.services.mcp_registry import register_mcp_server

    with engine.begin() as c:
        return register_mcp_server(c, PROJECT, key("srv"), "http://mcp.invalid/mcp",
                                   actor="ceo")


def grant_mcp(client, role_key: str, server: dict) -> None:
    given = client.post("/v1/role-mcp-servers", json={
        "project_id": PROJECT, "actor": "ceo", "role_key": role_key,
        "mcp_server_id": server["id"], "tools": ["post"]})
    assert given.status_code in (200, 201), given.text


# ── Building a workflow, the way the editor saves one ─────────────────────────────────────────


def name() -> str:
    return f"Built here {uuid4().hex[:8]}"


def post(client, path: str, **body):
    return client.post(f"/v1/delivery-workflows/{path}",
                       json={"project_id": PROJECT, "actor": "ceo", **body})


def check(client, spec: dict, definition_id: Optional[str] = None) -> dict:
    checked = post(client, "check", spec=spec, definition_id=definition_id)
    assert checked.status_code == 200, checked.text
    return checked.json()


def publish(client, spec: dict, *, definition_id: Optional[str] = None,
            title: Optional[str] = None, **extra):
    return post(client, "publish", definition_id=definition_id, name=title or name(),
                spec=spec, **extra)


def published(client, spec: dict, **kwargs) -> dict:
    saved = publish(client, spec, **kwargs)
    assert saved.status_code == 200, saved.text
    return saved.json()


def spec_of(client, workflow_key: str, version: Optional[int] = None) -> dict:
    params: dict[str, Any] = {"project_id": PROJECT}
    if version is not None:
        params["version"] = version
    loaded = client.get(f"/v1/delivery-workflows/{workflow_key}/spec", params=params)
    assert loaded.status_code == 200, loaded.text
    return loaded.json()


def library(client, *, include_retired: bool = False) -> dict[str, dict]:
    listed = client.get("/v1/delivery-workflows", params={
        "project_id": PROJECT, "include_retired": include_retired})
    assert listed.status_code == 200, listed.text
    return {row["id"]: row for row in listed.json()["items"]}


START_FORM = {
    "type": "object",
    "required": ["instructions"],
    "properties": {
        "instructions": {"type": "string", "title": "What to do"},
        "audience": {"type": "string", "title": "Who it is for"},
    },
}


#: The workflow the web editor writes for Agent task → Approval gate → Agent task ("Needs
#: approval"). Written by apps/web/tests/the-editor-writes-what-the-api-runs.test.ts from the
#: editor's own serialiser, so a change to what the editor saves is run end to end here.
EDITOR_BUILT = Path(__file__).resolve().parents[2] / "web/tests/fixtures/editor-built-workflow.json"


def editor_built(*, agent: str, skill: str, mcp: str) -> dict:
    """The editor's workflow with this test's agent, skill and MCP server put in.

    Its Approval gate carries what the palette served for that block when it was dropped, so
    the fixture is held to `CONTROL_BLOCKS` as well: the editor writes what the API offers."""
    from boss_engine.services.workflow_capabilities import CONTROL_BLOCKS

    raw = EDITOR_BUILT.read_text()
    for placeholder, value in (("{agent}", agent), ("{skill}", skill), ("{mcp}", mcp)):
        raw = raw.replace(json.dumps(placeholder), json.dumps(value))
    spec = json.loads(raw)
    served = next(b for b in CONTROL_BLOCKS if b["kind"] == "decision")["defaults"]
    for gate in (s for s in spec["steps"] if s["kind"] == "decision"):
        assert {k: gate.get(k) for k in served} == served, (
            "the editor fixture's gate is not what the palette serves; regenerate it")
    return spec


def agent_step(step_id: str, *, captain: Optional[str] = None, nxt: tuple[str, ...] = (),
               instructions: Any = None, **binding_extra) -> dict:
    """An "Agent task" in the shape `editor_built` pins: the task wired from the start form unless
    a fixed one is given. For tests about one step's settings, not about the editor."""
    wired = instructions if instructions is not None else {"from_input": "instructions"}
    step: dict[str, Any] = {
        "id": step_id, "kind": "capability", "next": list(nxt), "max_attempts": 1,
        "binding": {"agent_work": True, "inputs": {"instructions": wired}, **binding_extra},
    }
    if captain:
        step["captain"] = captain
    return step


def gate_step(step_id: str, *, nxt: tuple[str, ...] = (), question: Optional[str] = None,
              **extra) -> dict:
    step: dict[str, Any] = {"id": step_id, "kind": "decision", "captain": "human",
                            "next": list(nxt), **extra}
    if question is not None:
        step["question"] = question
    return step


# ── Running it, the way the console and the worker do ─────────────────────────────────────────


def start(client, workflow_key: str, run_input: dict, **extra):
    return post(client, "start", workflow_key=workflow_key, input=run_input, **extra)


def started(client, workflow_key: str, run_input: dict) -> str:
    """Started with the one readiness item a scripted model cannot satisfy — a connected model —
    accepted, which is what the operator's "Start anyway" sends."""
    run = start(client, workflow_key, run_input, start_anyway=["model"])
    assert run.status_code == 200, run.text
    return run.json()["run"]["id"]


def tick(client) -> list[dict]:
    """One pass of the worker's loop."""
    ticked = client.post("/v1/delivery-workflows/run-capabilities", json={"project_id": PROJECT})
    assert ticked.status_code == 200, ticked.text
    return ticked.json()["performed"]


def run_view(client, run_id: str) -> dict:
    """The run as the console's Runs tab reads it."""
    runs = client.get("/v1/delivery-workflows/runs",
                      params={"project_id": PROJECT, "limit": 100}).json()["runs"]
    found = [r for r in runs if r["id"] == run_id]
    assert found, f"run {run_id} is not in the runs view"
    return found[0]


def steps_of(view: dict) -> dict[str, dict]:
    return {s["step_id"]: s for s in view["steps"]}


def statuses(view: dict) -> dict[str, str]:
    return {s["step_id"]: s["status"] for s in view["steps"]}
