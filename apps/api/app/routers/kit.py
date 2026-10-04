"""The Kit's API: abilities, their connections, tests, holders and tool policy (skills-mcp §6.3).

Every write binds through `require_write_actor` with `pack_manage` (the signed-in principal
wins over a claimed actor). A test talks to someone else's server, so it runs OUTSIDE any
database transaction: the services take `begin` and open short transactions around the network
call, never across it.

Mounted from main.py with the auth helpers injected, so this module does not import main.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from boss_engine.db import begin


class KitBody(BaseModel):
    project_id: str
    actor: Optional[str] = None


class ConnectBody(KitBody):
    provider: str
    fields: dict[str, Any] = Field(default_factory=dict)


class TryBody(KitBody):
    input: str = ""


class HoldersBody(KitBody):
    roles: list[str] = Field(default_factory=list)


class PolicyBody(KitBody):
    tool_ref: str
    policy: str


class EnabledBody(KitBody):
    enabled: bool


class RecommendedBody(KitBody):
    roles: Optional[list[str]] = None


class CrewBody(KitBody):
    live: bool = False


class McpTestBody(BaseModel):
    project_id: str
    actor: Optional[str] = None
    url: str
    auth: Optional[dict[str, Any]] = None


class CatalogueAddBody(KitBody):
    token: Optional[str] = None


#: Tests run side by side, at most this many at once (spec §6.4 `Test all`).
TEST_CONCURRENCY = 4


def build_kit_router(
    *,
    engine,
    settings: Callable[[], Any],
    require_write_actor: Callable[..., str],
    require_reader: Callable[..., None],
) -> APIRouter:
    from boss_engine.services import kit, kit_connections
    from boss_engine.services.mcp_registry import McpServerError, McpServerInUse

    router = APIRouter(tags=["kit"])

    def opener():
        return begin(engine)

    def _fail(error: Exception):
        if isinstance(error, kit_connections.KitFieldError):
            raise HTTPException(status_code=422, detail={"field": error.field,
                                                         "message": str(error)}) from error
        if isinstance(error, kit.KitError):
            raise HTTPException(status_code=error.status, detail=str(error)) from error
        if isinstance(error, McpServerInUse):
            raise HTTPException(status_code=409, detail=str(error)) from error
        if isinstance(error, McpServerError):
            raise HTTPException(status_code=422, detail=str(error)) from error
        raise error

    def _writer(body: KitBody) -> str:
        with begin(engine) as connection:
            return require_write_actor(connection, body.project_id, body.actor, "pack_manage")

    def _ability(project_id: str, key: str) -> dict[str, Any]:
        try:
            return kit.ability_def(settings(), key)
        except kit.KitError as error:
            _fail(error)
            raise

    def _probe_stale(project_id: str) -> None:
        """Ask the servers not checked in the last minute, as the MCP list does with `stale`."""
        from boss_engine.services.mcp_registry import servers_with_tools

        try:
            servers_with_tools(opener, settings(), project_id, enabled_only=True)
        except Exception:  # noqa: BLE001 — the Kit still renders what was recorded
            pass

    @router.get("/v1/kit")
    def get_kit(project_id: str, check: str = "stale") -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
        if check != "none":
            _probe_stale(project_id)
        with begin(engine) as connection:
            return kit.kit_view(connection, settings(), project_id)

    @router.get("/v1/kit/{ability}")
    def get_ability(ability: str, project_id: str) -> dict[str, Any]:
        with begin(engine) as connection:
            require_reader(connection, project_id)
            try:
                return kit.ability_view(connection, settings(), project_id, ability)
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise

    @router.post("/v1/kit/{ability}/connect")
    def connect(ability: str, body: ConnectBody) -> dict[str, Any]:
        """Save & test: the ENTERED values are tested first and stored only if the test passes;
        a failure answers 422 {field, message} and writes nothing."""
        actor = _writer(body)
        spec = _ability(body.project_id, ability)
        try:
            outcome = kit_connections.connect(opener, settings(), body.project_id, spec,
                                              provider_key=body.provider, fields=body.fields,
                                              actor=actor)
        except Exception as error:  # noqa: BLE001
            _fail(error)
            raise
        with begin(engine) as connection:
            kit.record_test(connection, body.project_id, ability, outcome, actor=actor)
            return kit.ability_view(connection, settings(), body.project_id, ability)

    @router.delete("/v1/kit/{ability}/connection")
    def disconnect(ability: str, project_id: str, actor: Optional[str] = None
                   ) -> dict[str, Any]:
        spec = _ability(project_id, ability)
        with begin(engine) as connection:
            who = require_write_actor(connection, project_id, actor, "pack_manage")
            try:
                kit_connections.disconnect(connection, settings(), project_id, spec, who)
                return kit.ability_view(connection, settings(), project_id, ability)
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise

    def _run_test(project_id: str, spec: dict[str, Any], actor: str) -> dict[str, Any]:
        outcome = kit_connections.run_test(opener, settings(), project_id, spec)
        with begin(engine) as connection:
            kit.record_test(connection, project_id, spec["key"], outcome, actor=actor)
        return outcome

    @router.post("/v1/kit/{ability}/test")
    def test(ability: str, body: KitBody) -> dict[str, Any]:
        actor = _writer(body)
        outcome = _run_test(body.project_id, _ability(body.project_id, ability), actor)
        with begin(engine) as connection:
            return {**outcome,
                    "ability": kit.ability_view(connection, settings(), body.project_id,
                                                ability)}

    @router.post("/v1/kit/test-all")
    def test_all(body: KitBody) -> dict[str, Any]:
        """Re-run every connection test now, four at a time."""
        actor = _writer(body)
        with begin(engine) as connection:
            view = kit.kit_view(connection, settings(), body.project_id)
        keys = [a["key"] for a in view["abilities"]
                if a["status"] not in ("setup", "needs_repo", "off")]
        defs = {k: _ability(body.project_id, k) for k in keys}
        with ThreadPoolExecutor(max_workers=TEST_CONCURRENCY) as pool:
            outcomes = dict(zip(keys, pool.map(
                lambda k: _run_test(body.project_id, defs[k], actor), keys)))
        with begin(engine) as connection:
            return {"tests": outcomes,
                    "kit": kit.kit_view(connection, settings(), body.project_id)}

    @router.post("/v1/kit/{ability}/try")
    def try_it(ability: str, body: TryBody) -> dict[str, Any]:
        """One read-only call through BOSS's receipted path, as BOSS. Tier 0 only."""
        actor = _writer(body)
        spec = _ability(body.project_id, ability)
        with begin(engine) as connection:
            view = kit.ability_view(connection, settings(), body.project_id, ability)
        if not view.get("try"):
            raise HTTPException(status_code=409,
                                detail=f"{spec['label']} has no read-only tool to try.")
        value = str(body.input or "").strip()
        if not value:
            raise HTTPException(status_code=422, detail={
                "field": view["try"]["field"]["name"], "message": "Type something to try"})
        try:
            with begin(engine) as connection:
                return _try(connection, settings(), body.project_id, spec, value, actor)
        except HTTPException:
            raise
        except Exception as error:  # noqa: BLE001 — a failed try IS the answer
            return {"ok": False, "preview": _said(error), "receipt_id": None}

    @router.put("/v1/kit/{ability}/holders")
    def put_holders(ability: str, body: HoldersBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, "pack_manage")
            try:
                return kit.set_holders(connection, settings(), body.project_id, ability,
                                       body.roles, actor=actor)
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise

    @router.post("/v1/kit/{ability}/policy")
    def set_policy(ability: str, body: PolicyBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, "pack_manage")
            try:
                return kit.set_policy(connection, settings(), body.project_id, ability,
                                      body.tool_ref, body.policy, actor=actor)
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise

    @router.post("/v1/kit/{ability}/enabled")
    def set_enabled(ability: str, body: EnabledBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, "pack_manage")
            try:
                return kit.set_enabled(connection, settings(), body.project_id, ability,
                                       body.enabled, actor=actor)
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise

    @router.post("/v1/kit/recommended")
    def recommended(body: RecommendedBody) -> dict[str, Any]:
        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, "pack_manage")
            return kit.apply_recommended(connection, settings(), body.project_id, actor=actor,
                                         roles=body.roles)

    @router.post("/v1/kit/test-crew")
    def test_crew(body: CrewBody) -> dict[str, Any]:
        """Every Ready ability's test, then for each Essential: can an agent use it now?"""
        actor = _writer(body)
        with begin(engine) as connection:
            view = kit.kit_view(connection, settings(), body.project_id)
        keys = [a["key"] for a in view["abilities"] if a["status"] == "ready"]
        defs = {k: _ability(body.project_id, k) for k in keys}
        with ThreadPoolExecutor(max_workers=TEST_CONCURRENCY) as pool:
            list(pool.map(lambda k: _run_test(body.project_id, defs[k], actor), keys))
        with begin(engine) as connection:
            rows = kit.crew_rows(connection, settings(), body.project_id)
        out: dict[str, Any] = {"rows": rows}
        if body.live:
            out["live"] = _live_checks(body.project_id, actor)
        return out

    def _live_checks(project_id: str, actor: str) -> list[dict[str, Any]]:
        """Each check runs AS the agent, through the grant and the receipted runtime path an
        agent's own call takes — so it proves the agent can, not only that the server answers."""
        from boss_engine.services.agent_loop import _invoke_agent_skill, _invoke_mcp_skill
        from boss_engine.services.mcp_registry import find_mcp_server

        checks: list[dict[str, Any]] = []
        with begin(engine) as connection:
            server = find_mcp_server(connection, project_id, "web-intel")
            tool = next((t for t in (server or {}).get("tools") or []
                         if t.get("name") == "fetch_page"), None)
            if server is None or tool is None:
                checks.append({"role_key": "pm", "ok": False,
                               "detail": "web-intel is not reachable"})
            else:
                got = _invoke_mcp_skill(connection, settings(), project_id, server, tool,
                                        {"url": "https://example.com", "render": False},
                                        run_id=f"kit-crew-{int(time.time())}", step=0,
                                        actor=actor, caller="pm", mcp_reach=None)
                ok = got.get("status") == "succeeded"
                title = str(((got.get("result") or {}).get("output") or {}).get("title") or "")
                checks.append({"role_key": "pm", "ok": ok,
                               "detail": title or got.get("reason") or got.get("status"),
                               "receipt_id": (got.get("result") or {}).get("receipt_id")})
        with begin(engine) as connection:
            got = _invoke_agent_skill(connection, settings(), project_id, "repo.read",
                                      "backend_engineer", {"tool": "git_history", "limit": 1},
                                      actor=actor)
            ok = got.get("status") == "succeeded"
            checks.append({"role_key": "backend_engineer", "ok": ok,
                           "detail": "Read the repo" if ok else (got.get("reason")
                                                                 or got.get("status")),
                           "receipt_id": got.get("id")})
        return checks

    # ----------------------------------------------------------------- MCP additions
    @router.post("/v1/mcp/servers/test")
    def test_mcp(body: McpTestBody) -> dict[str, Any]:
        """Test a URL and sign-in as TYPED. Stores nothing (Save & test, then register)."""
        from boss_engine.services.mcp_registry import test_entered

        # A WRITER'S CHECK, not a reader's: it makes the API call a URL somebody typed, which is
        # the first half of registering a server.
        with begin(engine) as connection:
            require_write_actor(connection, body.project_id, body.actor, "pack_manage")
        try:
            return test_entered(settings(), None, body.project_id, body.url, body.auth)
        except McpServerError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.post("/v1/mcp/servers/{server_id}/review")
    def review(server_id: str, body: KitBody) -> dict[str, Any]:
        from boss_engine.services.mcp_registry import review_mcp_server

        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, "pack_manage")
            row = review_mcp_server(connection, body.project_id, server_id, actor=actor)
        if row is None:
            raise HTTPException(status_code=404, detail="MCP server not found.")
        return row

    class ToolPolicyBody(KitBody):
        tool: str
        policy: str

    @router.post("/v1/mcp/servers/{server_id}/policy")
    def server_tool_policy(server_id: str, body: ToolPolicyBody) -> dict[str, Any]:
        """Allow / Ask / Off for one tool of one server (the Library's MCP drawer). 409 below
        the floor: a tool that changes the world always asks a person."""
        from boss_engine.services.mcp_registry import PolicyBelowFloor, set_tool_policy

        with begin(engine) as connection:
            actor = require_write_actor(connection, body.project_id, body.actor, "pack_manage")
            try:
                return set_tool_policy(connection, body.project_id, server_id, body.tool,
                                       body.policy, actor=actor)
            except PolicyBelowFloor as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            except McpServerError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error

    @router.get("/v1/mcp/catalogue")
    def catalogue(project_id: Optional[str] = None) -> dict[str, Any]:
        items = kit_connections.catalogue(settings())
        if project_id:
            from boss_engine.services.mcp_registry import list_mcp_servers

            with begin(engine) as connection:
                require_reader(connection, project_id)
                have = {str(s["name"]).lower() for s in list_mcp_servers(connection,
                                                                          project_id)}
            for item in items:
                item["added"] = item["name"].lower() in have
        return {"items": items}

    @router.post("/v1/mcp/catalogue/{key}")
    def add_from_catalogue(key: str, body: CatalogueAddBody) -> dict[str, Any]:
        """Add a curated server: test it with the token typed, register it only on a pass."""
        from boss_engine.services.mcp_registry import record_reachability, test_entered

        actor = _writer(body)
        entry = kit_connections.catalogue_entry(key)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"no catalogue server {key!r}.")
        auth: dict[str, Any] = {"type": entry.get("auth") or "none"}
        if entry.get("header_name"):
            auth["header_name"] = entry["header_name"]
        if body.token:
            auth["secret"] = body.token
        probe = test_entered(settings(), None, body.project_id, entry["url"], auth,
                             timeout=kit_connections.TEST_TIMEOUT_SECONDS)
        if not probe["ok"]:
            raise HTTPException(status_code=422, detail={
                "field": "token" if entry.get("auth") != "none" else None,
                "message": str(probe.get("error") or "Can't reach server")})
        with begin(engine) as connection:
            try:
                server = kit_connections.add_catalogue_server(
                    connection, settings(), body.project_id, key, token=body.token, actor=actor)
            except Exception as error:  # noqa: BLE001
                _fail(error)
                raise
            record_reachability(connection, body.project_id, server["id"], reachable=True,
                                tools=probe["tools"])
            from boss_engine.services.mcp_registry import get_mcp_server

            return {"server": get_mcp_server(connection, body.project_id, server["id"]),
                    "test": probe}

    return router


def _said(error: BaseException) -> str:
    from boss_engine.services.mcp_registry import _said as said

    return said(error)[:500]


def _preview(value: Any) -> str:
    """The first 20 lines of a result, as text."""
    import json

    if isinstance(value, dict) and set(value) == {"text"}:
        value = value["text"]
    text = value if isinstance(value, str) else json.dumps(value, indent=2, default=str)
    return "\n".join(str(text).splitlines()[:20])


def _try(connection, settings: Any, project_id: str, ability: dict[str, Any], value: str,
         actor: str) -> dict[str, Any]:
    from boss_engine.services.core import make_id
    from boss_engine.services.mcp_registry import call_tool_with_receipt, find_mcp_server
    from boss_engine.services.skill_executors import SkillExecutorContext
    from boss_engine.services.skill_runtime import SkillRuntimeService

    spec = ability.get("try") or {}
    ref = str(spec.get("tool") or "")
    arg = str(spec.get("arg") or (spec.get("field") or {}).get("name") or "input")
    context = SkillExecutorContext(settings=settings, connection=connection,
                                   project_id=project_id, actor=actor)
    if ref.startswith("mcp:"):
        server_name, _, tool = ref[4:].partition(".")
        server = find_mcp_server(connection, project_id, server_name)
        if server is None:
            raise HTTPException(status_code=409, detail=f"{server_name} is not registered.")
        # `args` in the kit file shapes a call that needs more than the one typed value.
        template = spec.get("args") if isinstance(spec.get("args"), dict) else None
        inputs = ({k: (value if v == "{input}" else v) for k, v in template.items()}
                  if template else {arg: value})
        got = call_tool_with_receipt(
            connection, settings, project_id, server, tool, inputs,
            dedupe_key=f"kit-try:{ability['key']}:{make_id('try')}",
            tenant_scope=context.tenant_scope(),
            # NO PERSON'S APPROVAL IS CARRIED, by design: Try runs only a tier-0 tool set to
            # Allow, so an unreviewed server or an Ask tool is refused here, said, and not run.
            approved_by=None, approval_about=None)
        return {"ok": True, "preview": _preview(got.get("output")),
                "receipt_id": got.get("receipt_id")}
    if ref.startswith("skill:"):
        skill_id = ref[6:]
        inputs = {arg: value}
        if skill_id == "repo.read":
            inputs = {"tool": "read_code", arg: value}
        got = SkillRuntimeService(settings).invoke(
            connection, project_id, skill_id, "kit_try", inputs, mock=False,
            executor_context=context, actor=actor)
        ok = got.get("status") == "succeeded"
        return {"ok": ok, "preview": _preview(got.get("result") if ok else got.get("reason")),
                "receipt_id": got.get("id")}
    raise HTTPException(status_code=409, detail="Nothing to try.")
