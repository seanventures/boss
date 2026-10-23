"""POST /v1/projects: a project made in an org is as complete as the default one, and usable at once.

BOSS had the `project:create` permission and no route that checked it, so an org had exactly the
projects setup and seed made (the banking case study needs three: northpeak-bank-a/-ap/-b). The
route seeds through `bootstrap.seed_project_defaults`, the function the default project is seeded
with, so the two cannot drift apart. Step-up follows the rule every other step-up permission
follows: a person needs a fresh second factor (a PAT never satisfies it); a service account's own
key is exempt (authz._step_up_exempt), exactly as it is for `org:update`.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app.main import app
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine import bootstrap
from boss_engine.config import get_settings
from boss_engine.identity import authz
from boss_engine.identity.principal import AuthContext, AuthMethod, CredentialRef, PrincipalKind, SessionRef
from boss_engine.identity.scope import ResolvedScope, ScopeKind


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


@pytest.fixture(scope="module")
def owner(world):
    person = world.person("project-owner")
    world.bind(person, "owner")
    return person


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _slug(prefix: str = "northpeak-bank") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


def _stale(world: IdentityWorld, person) -> AuthContext:
    """A session whose second factor is older than any step-up window."""
    now = datetime.now(timezone.utc)
    session = SessionRef(id=f"ses_{uuid.uuid4().hex[:10]}", mfa_at=now - timedelta(hours=3),
                         auth_methods=("password", "totp"), idle_expires_at=now + timedelta(hours=1),
                         absolute_expires_at=now + timedelta(days=1))
    return AuthContext(person, AuthMethod.SESSION, session=session)


def _service_key(world: IdentityWorld, sa, scopes: tuple[str, ...]) -> AuthContext:
    """A service account's own key (kind `sak`), as the case-study harness's would be."""
    key_id = f"key_{uuid.uuid4().hex[:12]}"
    with world.engine.begin() as db:
        db.execute(text(
            "INSERT INTO identity_api_credentials (id, principal_id, kind, name, token_hash, scopes, "
            "created_by) VALUES (:id, :p, 'sak', 'case-study', :h, :s, 'test')"),
            {"id": key_id, "p": sa.id, "h": uuid.uuid4().hex, "s": list(scopes)})
    cred = CredentialRef(id=key_id, kind="sak", principal_id=sa.id, scopes=frozenset(scopes))
    return AuthContext(sa, AuthMethod.API_KEY, credential=cred)


def _rows(engine, sql: str, **params):
    with engine.connect() as db:
        return db.execute(text(sql), params).all()


def test_a_new_project_is_seeded_like_the_default_one_and_works(world, owner, client, engine,
                                                                monkeypatch):
    seeded = []
    real_seed = bootstrap.seed_project_defaults

    def spy(connection, settings, project_id, **kwargs):
        seeded.append(project_id)
        return real_seed(connection, settings, project_id, **kwargs)

    monkeypatch.setattr(bootstrap, "seed_project_defaults", spy)
    slug = _slug()
    with world.acting_as(world.ctx(owner)):
        got = client.post("/v1/projects", json={
            "name": "Northpeak Bank A", "slug": slug, "description": "Arm A of the case study"})
    assert got.status_code == 201, got.text
    body = got.json()
    assert (body["id"], body["slug"], body["name"]) == (slug, slug, "Northpeak Bank A")
    assert body["org_id"] == world.org and body["workspace_id"] == world.workspace
    assert body["description"] == "Arm A of the case study"
    # THE SAME SEED AS THE DEFAULT PROJECT, not a copy of part of it.
    assert seeded == [slug]

    default = get_settings().default_project_id
    policy = "SELECT daily_cap_usd_micros, monthly_cap_usd_micros, max_concurrent_jobs, " \
             "max_workers_total, allowed_model_tiers, soft_cap_pct FROM budget_policies " \
             "WHERE scope_type = 'project' AND scope_id = :p"
    assert _rows(engine, policy, p=slug) == _rows(engine, policy, p=default) != []
    members = "SELECT member_id, role_keys FROM project_members WHERE project_id = :p ORDER BY member_id"
    assert _rows(engine, members, p=slug) == _rows(engine, members, p=default) != []
    roles = {r[0] for r in _rows(engine, "SELECT role_key FROM roles WHERE project_id = :p", p=slug)}
    assert roles >= {r[0] for r in _rows(engine, "SELECT role_key FROM roles WHERE project_id = :p",
                                         p=default)} and roles
    assert _rows(engine, "SELECT count(*) FROM role_skills WHERE project_id = :p", p=slug)[0][0] > 0
    servers = {r[0] for r in _rows(engine, "SELECT name FROM mcp_servers WHERE project_id = :p", p=slug)}
    assert servers >= {s["name"] for s in bootstrap.BUILTIN_MCP_SERVERS}
    assert _rows(engine, "SELECT count(*) FROM workflow_definitions WHERE project_id = :p",
                 p=slug)[0][0] > 0

    with world.acting_as(world.ctx(owner)):
        listed = client.get("/v1/projects")
        assert slug in {p["id"] for p in listed.json()["items"]}, listed.text
        repos = client.get("/v1/github/repos", params={"project_id": slug})
        assert repos.status_code == 200, repos.text
        setting = client.put("/v1/settings/knowledge_graph",
                             json={"project_id": slug, "value": "keyword"})
        assert setting.status_code == 200, setting.text
    from boss_engine.services import project_settings

    with engine.connect() as db:
        assert project_settings.effective(db, slug, get_settings(), "knowledge_graph") == "keyword"

    # A BUILD STARTS IN IT, on the scripted engine (no model): the run row and its code.task job
    # land in the new project, under its budget policy.
    task = client.post("/v1/code-tasks", json={
        "project_id": slug, "title": "probe the new project", "instructions": "slugify",
        "owner": "northpeak", "repo": "bank-a", "model_config": {"engine": "scripted"},
        "open_pr": False, "actor": "ceo"})
    assert task.status_code == 200, task.text
    run_id = task.json()["run_id"]
    run = _rows(engine, "SELECT project_id, spec->'model_config'->>'engine' FROM code_task_runs "
                        "WHERE id = :r", r=run_id)
    assert run == [(slug, "scripted")]
    jobs = _rows(engine, "SELECT project_id FROM jobs WHERE kind = 'code.task' AND payload->>'run_id' = :r",
                 r=run_id)
    assert jobs == [(slug,)]


def test_the_same_slug_again_is_409_with_the_existing_project(world, owner, client):
    slug = _slug("northpeak-bank-ap")
    with world.acting_as(world.ctx(owner)):
        first = client.post("/v1/projects", json={"name": "Northpeak Bank AP", "slug": slug})
        again = client.post("/v1/projects", json={"name": "Another name", "slug": slug})
    assert first.status_code == 201, first.text
    assert again.status_code == 409, again.text
    detail = again.json()["detail"]
    assert detail["error"] == "project_exists" and detail["project_id"] == slug


def test_a_slug_is_kebab_case_or_made_from_the_name(world, owner, client):
    with world.acting_as(world.ctx(owner)):
        for bad in ("Northpeak", "north_peak", "-north", "north--peak", "n"):
            got = client.post("/v1/projects", json={"name": "Northpeak", "slug": bad})
            assert got.status_code == 422, (bad, got.text)
        assert client.post("/v1/projects", json={"name": "  "}).status_code == 422
        tag = uuid.uuid4().hex[:6]
        made = client.post("/v1/projects", json={"name": f"Northpeak Bank B {tag}"})
    assert made.status_code == 201, made.text
    assert made.json()["slug"] == f"northpeak-bank-b-{tag}"


@pytest.mark.parametrize("role", ["operator", "viewer"])
def test_a_member_without_project_create_is_refused(world, client, engine, role):
    member = world.person(f"project-{role}-{uuid.uuid4().hex[:4]}")
    world.bind(member, role)
    slug = _slug("refused")
    with world.acting_as(world.ctx(member)):
        got = client.post("/v1/projects", json={"name": "Refused", "slug": slug})
    assert got.status_code == 403, got.text
    assert _rows(engine, "SELECT 1 FROM projects WHERE id = :p", p=slug) == []


def test_a_person_steps_up_and_a_pat_never_satisfies_it(world, owner, client, engine):
    for ctx in (_stale(world, owner), world.ctx(owner, mfa=False),
                world.key_ctx(owner, ("project:create",))):
        slug = _slug("stale")
        with world.acting_as(ctx):
            got = client.post("/v1/projects", json={"name": "Stale", "slug": slug})
        assert got.status_code == 401 and got.json().get("error") == "step_up_required", got.text
        assert _rows(engine, "SELECT 1 FROM projects WHERE id = :p", p=slug) == []


def test_a_service_account_key_creates_without_step_up_as_for_every_step_up_permission(
        world, client, engine):
    sa = world.principal(f"case-study-{uuid.uuid4().hex[:4]}", kind=PrincipalKind.SERVICE_ACCOUNT)
    world.bind(sa, "admin")
    key = _service_key(world, sa, ("project:create", "org:update"))
    # The rule that decides it is the one that already decides org:update (POST /v1/orgs) for the
    # same key: a service account's own key is exempt from step-up outside identity administration.
    org = ResolvedScope(kind=ScopeKind.ORG, org_id=world.org)
    assert "project:create" not in authz.KEY_NEVER_STEPS_UP
    assert authz.check(key, "project:create", org).allowed
    assert authz.check(key, "org:update", org).allowed
    pat = world.key_ctx(world.person(f"pat-{uuid.uuid4().hex[:4]}"), ("project:create", "org:update"))
    world.bind(pat.principal, "admin")
    assert authz.check(pat, "project:create", org).reason == authz.check(pat, "org:update", org).reason \
        == authz.DenyReason.STEP_UP_REQUIRED

    slug = _slug("northpeak-bank-b")
    with world.acting_as(key):
        got = client.post("/v1/projects", json={"name": "Northpeak Bank B", "slug": slug})
    assert got.status_code == 201, got.text
    # ...and a key without the scope cannot, step-up or not.
    narrow = _service_key(world, sa, ("project:read",))
    with world.acting_as(narrow):
        refused = client.post("/v1/projects", json={"name": "Narrow", "slug": _slug("narrow")})
    assert refused.status_code == 403, refused.text


def test_creating_a_project_is_audited_who_when_what(world, owner, client, engine):
    slug = _slug("audited")
    before = datetime.now(timezone.utc) - timedelta(seconds=5)
    with world.acting_as(world.ctx(owner)):
        assert client.post("/v1/projects", json={"name": "Audited", "slug": slug}).status_code == 201
    security = _rows(engine, "SELECT actor_principal_id, actor_kind, ts, permission, details->>'name', "
                             "details->>'slug' FROM identity_security_events WHERE org_id = :o AND "
                             "event_type = 'project.created' AND target_id = :p", o=world.org, p=slug)
    assert len(security) == 1, security
    who, kind, when, permission, name, made = security[0]
    assert (who, kind, permission, name, made) == (owner.id, "user", "project:create", "Audited", slug)
    assert when >= before
    feed = _rows(engine, "SELECT actor, data->>'principal_id' FROM events WHERE project_id = :p AND "
                         "event_type = 'project.created'", p=slug)
    assert len(feed) == 1 and feed[0][1] == owner.id, feed


class _ScriptedModel:
    """The project's model, scripted: as the DECISION step it chooses `create_project` from the
    candidates it is offered; as the answering agent it calls the `create_project` tool once, then
    reports. No paid model is called."""

    def __init__(self, name: str):
        self.name = name
        self.decision_prompts: list[str] = []
        self.tools_offered: list[set[str]] = []
        self.completions = 0

    def create_text_response(self, prompt, system_prompt=None, model=None):
        import json

        self.decision_prompts.append(str(prompt))
        return "resp", json.dumps({"action": "create_project", "confidence": 0.97,
                                   "reason": "they asked for a new project", "question": ""}), \
            {"model": "scripted"}

    def create_chat_completion(self, messages, tools=None, tool_choice="auto"):
        import json

        self.completions += 1
        names = {t["function"]["name"] for t in (tools or [])}
        self.tools_offered.append(names)
        if self.completions == 1 and "create_project" in names:
            args = json.dumps({"name": self.name, "slug": self.name})
            return {"content": None,
                    "tool_calls": [{"id": "call_1", "name": "create_project", "arguments": args}],
                    "raw": {"role": "assistant", "content": None, "tool_calls": [
                        {"id": "call_1", "type": "function",
                         "function": {"name": "create_project", "arguments": args}}]}}, {}
        said = f"Created project {self.name}."
        return {"content": said, "tool_calls": None, "raw": {"role": "assistant", "content": said}}, {}


def test_asked_in_plain_words_the_agent_creates_the_project(client, engine, monkeypatch):
    """"create a project called northpeak-bank-a" in Ask BOSS: the decision step chooses the
    create_project action from the shared catalogue, the agent calls the create_project tool, and
    the project exists, seeded like any other."""
    from boss_engine.services.intent_router import IntentRouter

    slug = _slug("northpeak-bank-a")
    model = _ScriptedModel(slug)

    def use_scripted(self, connection, project_id):
        if self._answer_adapter is not model:
            self._answer_adapter, self._answer_agent_obj = model, None

    monkeypatch.setattr(IntentRouter, "_use_project_chat_model", use_scripted)
    monkeypatch.setattr(IntentRouter, "_a_model_can_answer", lambda *a, **k: True)
    got = client.post("/v1/agent/chat", json={
        "project_id": get_settings().default_project_id, "actor": "ceo",
        "message": f"create a project called {slug}"})
    assert got.status_code == 200, got.text
    assert model.decision_prompts and "create_project" in model.decision_prompts[0]
    assert model.tools_offered and "create_project" in model.tools_offered[0]
    body = got.json()
    assert _rows(engine, "SELECT name FROM projects WHERE id = :p", p=slug) == [(slug,)], body
    assert _rows(engine, "SELECT count(*) FROM budget_policies WHERE scope_id = :p", p=slug)[0][0] == 1


def test_the_agents_create_project_is_governed_like_the_route(world, owner, engine):
    """The tool is decided with the caller's own context: a person needs a fresh second factor,
    and a member without project:create is refused (an agent's run token never holds it: the
    permission is not delegable)."""
    from boss_engine.context import EngineContext
    from boss_engine.tools import build_discuss_read_registry

    assert not authz.PERMISSION_META["project:create"].delegable
    registry = build_discuss_read_registry()
    ctx = EngineContext(settings=get_settings(), engine=engine)
    operator = world.person(f"tool-operator-{uuid.uuid4().hex[:4]}")
    world.bind(operator, "operator")
    settings = get_settings()
    saved = (settings.authz_mode, settings.authz_router_modes)
    settings.authz_mode, settings.authz_router_modes = "enforce", ""
    try:
        for auth, why in ((_stale(world, owner), "step_up_required"),
                          (world.ctx(operator), "forbidden")):
            slug = _slug("tool-refused")
            out = registry.invoke("create_project", ctx, project_id=world.project_a, actor="test",
                                  args={"name": "x", "slug": slug}, auth=auth)
            assert out.get("ok") is False and why in str(out), out
            assert _rows(engine, "SELECT 1 FROM projects WHERE id = :p", p=slug) == []
        slug = _slug("tool-made")
        out = registry.invoke("create_project", ctx, project_id=world.project_a, actor="test",
                              args={"name": "Tool made", "slug": slug}, auth=world.ctx(owner))
        assert out.get("ok") is True and out["project_id"] == slug, out
    finally:
        settings.authz_mode, settings.authz_router_modes = saved
    assert _rows(engine, "SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                         "WHERE p.id = :p", p=slug) == [(world.org,)]
    assert _rows(engine, "SELECT actor_principal_id FROM identity_security_events WHERE "
                         "event_type = 'project.created' AND target_id = :p", p=slug) == [(owner.id,)]
