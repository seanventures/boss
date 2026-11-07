"""ADD A PROJECT v2: the one screen's API (docs/product/wireframes/project-experience/
new-project-v2; services/project_experience/add_project.py).

POST /v1/projects/{id}/add-project/understand   Helm's understanding of the words (one agent turn)
POST /v1/projects/{id}/add-project/connectors   the connectors a confirmed brief calls for
GET  /v1/projects/{id}/add-project/preview      what BOSS will do: workflows, captain, staging, dots
GET  /v1/projects/{id}/add-project/repo/{repo}  what Helm found in a registry repository
GET  /v1/projects/{id}/add-project/started      the new project's first view (seed, stations, Now)
POST /v1/add-project                            Create and start, in ONE transaction

`{id}` is the project open now: the org's registry, hosts, installed workflows and the model
route are read through it, exactly as the old sheet and New build read them.

CREATE AND START IS CHECKED WHOLE, THEN COMMITTED. Phase 1 is ONE transaction: the project,
its repositories (or folder), captain, crew, components, the staging target, the connectors' test
keys and the seed — and, before it commits, every check the start would make: the start door's
own readiness gate (workflow_readiness.refuse_unless_ready on the new project's workflow
version, with the person's Start anyway) and, for connectors, the template plan's dry run
(stack, secrets, egress, conflicts). A refusal anywhere rolls ALL of it back and names its step,
so "Start anyway" or a fix retries from nothing. Phase 2 starts it (the start door, or the
template apply/compose path) in its own transaction: the start mints the run's agent passports
on their own connection, which cannot see an uncommitted project. A refusal only the start can
make (a consent grant, a pre-check) keeps the project and answers `retry.project_id`; the same
call with `project_id` starts it, never a second project. Each step's permission is checked at
the new project, in the transaction that does it.
"""
from __future__ import annotations

import concurrent.futures
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit

router = APIRouter(tags=["projects"])
_settings = get_settings()
_engine = get_engine(_settings)
_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="add-project")
#: The wait past the turn's own deadline before the person is told to continue.
_SLACK_S = 10.0


def _require_project(connection, project_id: str) -> None:
    from boss_engine.services.project_experience.common import require_project

    try:
        require_project(connection, project_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error) or "Not found.") from None


def _actor(request: Request) -> str:
    from .project_experience import _actor as actor_of

    return actor_of(request)


def _org(connection, project_id: str) -> str:
    from boss_engine.multirepo.common import org_of_project

    return str(org_of_project(connection, project_id) or _settings.default_org_id)


def _catalogue(connection, org_id: str) -> list[dict[str, Any]]:
    from boss_engine.templates import service

    try:
        with connection.begin_nested():
            cards = service.list_templates(connection, org_id, kind="connector")
    except Exception:  # noqa: BLE001 — no catalogue: no connector suggestions
        return []
    # The catalogue as Helm is offered it: key, name, what it adds, which stacks.
    return [{"key": c.get("template_key") or c.get("key"), "name": c.get("name"),
             "summary": c.get("summary"), "stacks": c.get("stacks") or []}
            for c in cards if c.get("status") == "active" and (c.get("template_key") or c.get("key"))]


def _stacks() -> list[dict[str, Any]]:
    from boss_engine.services import stacks

    caps = stacks.capabilities()
    return [{"id": a.get("id"), "label": a.get("label"), "ready": a.get("ready", True)}
            for a in caps.get("adapters") or [] if a.get("id")]


# ── Helm's understanding ────────────────────────────────────────────────────────────────────

class UnderstandBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    words: str = Field(..., max_length=8000)
    hint: Optional[str] = Field(default=None, max_length=300)
    previous: Optional[dict[str, Any]] = None
    #: Lines the person edited by hand: kept as written through a rewrite.
    pinned: Optional[dict[str, Any]] = None


def _understand(project_id: str, payload: UnderstandBody, actor: str) -> dict[str, Any]:
    from boss_engine.services.project_experience import add_project

    with begin(_engine) as connection:
        catalogue = _catalogue(connection, _org(connection, project_id))
        flows = add_project.workflows(connection, project_id)
        return add_project.understand(
            connection, _settings, project_id, words=payload.words, hint=payload.hint or "",
            previous=payload.previous, pinned=payload.pinned, catalogue=catalogue,
            stacks=_stacks(), workflows=flows, actor=actor)


def _within(fn, wait_s: float, words: str) -> dict[str, Any]:
    """Helm's turn, bounded twice: the turn's own deadline (understanding.turn, call_deadline)
    and, should a backend ignore it, this wait — past it the person keeps their words and can
    continue."""
    from boss_engine.services.project_experience import add_project

    job = _POOL.submit(fn)
    try:
        return job.result(timeout=wait_s + _SLACK_S)
    except concurrent.futures.TimeoutError:
        return {"status": "timeout", "brief": add_project.raw_brief(words),
                "picks": {"stack": None, "connectors": [], "workflow": None},
                "message": f"Helm took longer than {int(wait_s)} s. Your words are kept as they "
                           "are; you can continue, or Rewrite to ask again."}
    except Exception as error:  # noqa: BLE001 — Helm's reading is a help, never a failed screen
        import logging

        logging.getLogger(__name__).warning("add-project understanding failed", exc_info=True)
        return {"status": "no_model", "brief": add_project.raw_brief(words),
                "picks": {"stack": None, "connectors": [], "workflow": None},
                "message": f"Helm could not read it now ({type(error).__name__}). Your words are "
                           "kept as they are; you can continue."}


@router.post("/v1/projects/{project_id}/add-project/understand")
@permit(P.PROJECT_CREATE, scope=Scope.org())
def add_project_understand(project_id: str, payload: UnderstandBody,
                           request: Request) -> dict[str, Any]:
    """Helm rewrites the words into a brief and picks the stack, connectors and workflow, with
    the words that led to each. A model turn costs: it is asked by whoever may create a project
    (project:create), never by a viewer. status ok | no_model | timeout | empty; never a 5xx for a model
    that is missing or slow."""
    from boss_engine.services.project_experience import add_project

    with begin(_engine) as connection:
        _require_project(connection, project_id)
    actor = _actor(request)
    return _within(lambda: _understand(project_id, payload, actor), add_project.WAIT_S,
                   payload.words)


class ConnectorsBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    brief: dict[str, Any]
    words: str = Field(default="", max_length=8000)


@router.post("/v1/projects/{project_id}/add-project/connectors")
@permit(P.PROJECT_CREATE, scope=Scope.org())
def add_project_connectors(project_id: str, payload: ConnectorsBody,
                           request: Request) -> dict[str, Any]:
    """The picks for a brief the person edited by hand (Fix it › Save): the same turn, with
    every line of the brief kept as written, answering only the stack, connectors and workflow."""
    from boss_engine.services.project_experience import add_project

    pinned = {k: v for k, v in payload.brief.items() if k in add_project.BRIEF_FIELDS}
    body = UnderstandBody(words=payload.words or add_project.render_brief(payload.brief),
                          pinned=pinned, previous=payload.brief)
    with begin(_engine) as connection:
        _require_project(connection, project_id)
    actor = _actor(request)
    out = _within(lambda: _understand(project_id, body, actor), add_project.WAIT_S, body.words)
    return {"status": out.get("status"), "picks": out.get("picks"), "message": out.get("message")}


# ── what BOSS will do, and what Helm found ─────────────────────────────────────────────────

@router.get("/v1/projects/{project_id}/add-project/preview")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def add_project_preview(project_id: str, mode: str = "idea", app_name: str = "") -> dict[str, Any]:
    """The six boxes' choices in one read: installed workflows (real steps, approvals,
    estimates), the default, captain and crew, staging hosts with a free port, readiness dots."""
    from boss_engine.services.project_experience import add_project

    mode = mode if mode in ("idea", "repo", "folder") else "idea"
    with begin(_engine) as connection:
        _require_project(connection, project_id)
        out = add_project.preview(connection, _settings, project_id, mode=mode,
                                  app_name=app_name[:120])
    owner = getattr(_settings, "target_github_owner", "") or ""
    repo = getattr(_settings, "target_github_repo", "") or ""
    out["default_repository"] = f"{owner}/{repo}" if owner and repo else None
    out["stacks"] = _stacks()
    return out


@router.get("/v1/projects/{project_id}/add-project/repo/{repo_id}")
@permit(P.REPOSITORY_READ, scope=Scope.project(path="project_id"))
def add_project_repo(project_id: str, repo_id: str) -> Any:
    """What Helm found: what it is, its main parts, its health, what onboarding will do."""
    from boss_engine.multirepo.common import MultiRepoError
    from boss_engine.services.project_experience import add_project

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        try:
            return add_project.repo_read(connection, project_id, repo_id)
        except (LookupError, MultiRepoError) as error:
            raise HTTPException(status_code=404, detail={
                "error": "repo_not_found", "message": str(error) or "No such repository."}) from None


@router.get("/v1/projects/{project_id}/add-project/started")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def add_project_started(project_id: str) -> dict[str, Any]:
    """The new project's first view (c-after-create): what was confirmed, the first run's
    stations and the Now line. {seed: null} for a project made another way."""
    from boss_engine.services.project_experience import add_project

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        return add_project.started(connection, project_id)


# ── Create and start ────────────────────────────────────────────────────────────────────────

class FolderIn(BaseModel):
    host_id: str = Field(..., max_length=120)
    path: str = Field(..., max_length=500)


class StagingIn(BaseModel):
    host_id: str = Field(..., max_length=120)
    port: Optional[int] = Field(default=None, ge=1, le=65535)


class CreateAndStartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: The project open now: its registry, hosts and workflows were read for the picks.
    context_project_id: str = Field(..., max_length=120)
    kind: str = Field("idea", description="idea | repo | folder")
    name: str = Field(..., min_length=1, max_length=120)
    words: str = Field(default="", max_length=8000)
    brief: Optional[dict[str, Any]] = None
    #: A repository's lines under it: a note to Helm (onboarding, Marshal's first pick).
    note: Optional[str] = Field(default=None, max_length=2000)
    repo_ids: list[str] = Field(default_factory=list, max_length=6)
    folder: Optional[FolderIn] = None
    captain: Optional[str] = Field(default=None, max_length=120)
    crew: Optional[list[str]] = Field(default=None, max_length=20)
    #: The stack adapter id (Stack box) and the language connectors are applied for.
    stack: Optional[str] = Field(default=None, max_length=60)
    stack_language: Optional[str] = Field(default=None, max_length=20)
    connectors: list[str] = Field(default_factory=list, max_length=6)
    #: Test keys a connector needs (write-only; never echoed): {SECRET_NAME: value}.
    connector_secrets: dict[str, str] = Field(default_factory=dict, repr=False)
    #: The hosts the connectors' app talks to, as the person allowed them (the templates' own
    #: consent: apply / compose refuse `egress_not_accepted` without it).
    accept_egress: list[str] = Field(default_factory=list, max_length=40)
    workflow_key: Optional[str] = Field(default=None, max_length=120)
    #: The workflow's own start form, Who to tell (notify_channel) and anything it declared.
    inputs: dict[str, Any] = Field(default_factory=dict)
    notify_when: list[str] = Field(default_factory=list, max_length=6)
    audience: Optional[str] = Field(default=None, max_length=400)
    done_when: list[str] = Field(default_factory=list, max_length=12)
    read_first: list[str] = Field(default_factory=list, max_length=12)
    talks_to: list[dict[str, Any]] = Field(default_factory=list, max_length=8)
    staging: Optional[StagingIn] = None
    map_rules: bool = False
    start_anyway: list[str] = Field(default_factory=list, max_length=20)
    dedupe_key: Optional[str] = Field(default=None, max_length=200)
    #: A retry of phase 2 only: the project an earlier call made whose start was refused.
    project_id: Optional[str] = Field(default=None, max_length=120)


class Refused(Exception):
    def __init__(self, step: str, status: int, body: dict[str, Any]):
        super().__init__(body.get("message") or step)
        self.step, self.status, self.body = step, status, body


def _need(request: Request, permission: str, step: str, project_id: Optional[str] = None) -> None:
    """A step's own permission, checked BEFORE anything is written and outside any transaction
    (a check audits its denials on its own connection; inside the create's transaction, which
    holds the audit chain's lock after `project.created`, that wait would never end). A project
    that does not exist yet is the caller's org: what they hold org-wide is what they hold on
    it. The same PDP and mode rule as identity_access.require_permission."""
    from boss_engine.identity import authz
    from boss_engine.identity.scope import ResolvedScope

    from .. import identity_guard

    mode = getattr(request.state, "identity_mode", None) or identity_guard.IDENTITY_MODE.get() or ""
    if mode == "off":
        return
    ctx = getattr(request.state, "identity", None) or identity_guard.REQUEST_IDENTITY.get()
    if ctx is None:
        if mode == "enforce":
            raise Refused(step, 401, {"error": "unauthenticated", "message": "Sign in first."})
        return
    org = ctx.principal.org_id
    scope = ResolvedScope.in_project(org, project_id) if project_id else ResolvedScope.org_level(org)
    decision = authz.check(ctx, permission, scope)
    if not decision.allowed and mode == "enforce":
        body = dict(decision.error_body())
        body["message"] = body.get("message") or f"You may not {step.replace('_', ' ')} ({permission})."
        raise Refused(step, decision.status, body)


def _needs(body: "CreateAndStartBody", kind: str) -> list[tuple[str, str]]:
    """Every permission this Create and start will use, in the order its steps run."""
    out = []
    if body.repo_ids:
        out.append((P.REPOSITORY_MANAGE.value, "add_the_repository"))
    if body.folder is not None:
        out.append((P.PROJECT_UPDATE.value, "add_the_folder"))
    if body.captain and body.captain != "engineering_captain":
        out.append((P.PROJECT_UPDATE.value, "set_the_captain"))
    if body.crew:
        out.append((P.WORK_ITEM_MANAGE.value, "set_the_crew"))
    if body.staging is not None and body.repo_ids:
        out.append((P.DEPLOYMENT_TARGET_MANAGE.value, "make_the_staging_target"))
    if kind == "idea" and body.connectors:
        out.append((P.TEMPLATE_READ.value, "apply_connectors"))
        if body.connector_secrets:
            out.append((P.APP_SECRET_WRITE.value, "set_test_keys"))
    out.append((P.WORKFLOW_RUN_START.value, "start_the_plan" if kind == "idea" else "start_onboarding"))
    return out


def _words_of(exc: HTTPException) -> dict[str, Any]:
    d = exc.detail
    if isinstance(d, dict):
        out = dict(d)
        out.setdefault("message", d.get("message") or d.get("summary") or d.get("error") or "")
        return out
    return {"message": str(d)}


def _run_input(body: CreateAndStartBody, brief: dict[str, Any], repo: Optional[str]
               ) -> dict[str, Any]:
    from boss_engine.services.project_experience import add_project

    directive = add_project.render_brief(brief, body.words) if brief else body.words.strip()
    if body.audience:
        directive += f"\n\nWho it is for: {body.audience.strip()}"
    if body.stack:
        directive += f"\n\nStack: {body.stack}."
    criteria = [c.strip() for c in body.done_when if c.strip()]
    if brief.get("done_when"):
        criteria = [str(brief["done_when"]), *criteria]
    links = [l.strip() for l in body.read_first if l.strip().lower().startswith(("http://", "https://"))]
    run_input: dict[str, Any] = {k: v for k, v in body.inputs.items()
                                 if v not in (None, "") and k not in ("directive", "title")}
    run_input.update({"title": (brief.get("title") or body.name)[:80], "directive": directive[:8000]})
    if criteria:
        run_input["acceptance_criteria"] = criteria[:12]
    if links:
        run_input["reference_links"] = links
    if repo:
        run_input["repository"] = repo
    if body.talks_to:
        run_input["app_links"] = body.talks_to
    return run_input


@router.post("/v1/add-project")
@permit(P.PROJECT_CREATE, scope=Scope.org())
def add_project_create_and_start(request: Request, payload: CreateAndStartBody) -> Any:
    """Create and start (⌘↵): one transaction; a refusal names its step and leaves nothing."""
    try:
        return _create_and_start(request, payload)
    except Refused as refused:
        return JSONResponse({**refused.body, "step": refused.step,
                             "created": False}, status_code=refused.status)


def _confirmed_brief(body: CreateAndStartBody) -> dict[str, Any]:
    from boss_engine.services.project_experience import add_project

    if not body.brief:
        return {}
    brief = add_project.shape_brief(body.brief, words=body.words)
    # The confirmed brief, kept whole: answers to open questions and Helm's marks included.
    given = [o for o in body.brief.get("open_questions") or [] if isinstance(o, dict)]
    brief["open_questions"] = [{**q, "answer": next((o.get("answer") for o in given
                                                     if o.get("question") == q["question"]), None)}
                               for q in brief["open_questions"]]
    brief["added"] = [a for a in body.brief.get("added") or [] if isinstance(a, str)][:20]
    return brief


def _version_of(db, pid: str, workflow_key: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """(workflow_key, active version id) Create and start will run on the new project: the
    picked one, else its new-app default (start_project.how_to_start, the start door's own)."""
    from sqlalchemy import text

    from boss_engine.services import start_project

    key = workflow_key or start_project.how_to_start(db, pid, has_repo=False).get("resolved_key")
    if not key:
        return None, None
    version = db.execute(text(
        "SELECT active_version_id FROM workflow_definitions WHERE project_id = :p AND "
        "workflow_key = :k AND status = 'active'"), {"p": pid, "k": key}).scalar()
    if version is None and workflow_key:
        raise Refused("workflow", 422, {"error": "unknown_workflow",
                                        "message": f"No active workflow {workflow_key!r} to run."})
    return str(key), (str(version) if version else None)


def _create_and_start(request: Request, body: CreateAndStartBody) -> dict[str, Any]:  # noqa: C901
    from apps.api.app import identity_guard, main
    from boss_engine.multirepo import components, project_repos, registry
    from boss_engine.multirepo.common import MultiRepoError
    from boss_engine.services import agent_assignment, workflow_readiness
    from boss_engine.services import projects as project_service
    from boss_engine.services.project_experience import add_project, captain, onboarding
    from boss_engine.services.project_experience.common import project_href

    kind = body.kind if body.kind in ("idea", "repo", "folder") else None
    if kind is None:
        raise Refused("input", 422, {"error": "bad_kind", "message": "kind is idea, repo or folder."})
    if kind == "idea" and not (body.words.strip() or (body.brief or {}).get("title")):
        raise Refused("input", 422, {"error": "words_required", "message": "Say what it should do."})
    if kind == "repo" and not body.repo_ids:
        raise Refused("input", 422, {"error": "repo_required", "message": "Pick a repository."})
    if kind == "folder" and body.folder is None:
        raise Refused("input", 422, {"error": "folder_required", "message": "Give a folder on a host."})
    actor = _actor(request)
    ctx = identity_guard.request_auth(request)
    for permission, step in _needs(body, kind):
        _need(request, permission, step, body.project_id or None)
    brief = _confirmed_brief(body)
    steps: list[dict[str, Any]] = []
    run_input: dict[str, Any] = {}
    staged = None

    if body.project_id:
        # PHASE 2 AGAIN: a project this flow made whose start was refused. Never a second one.
        with begin(_engine) as db:
            _require_project(db, body.project_id)
            seed = add_project.seed_of(db, body.project_id)
            if not seed or seed.get("created_by") != actor:
                raise Refused("project", 409, {"error": "not_a_new_project", "message":
                                               "That project was not made here; start work on "
                                               "it from its Work tab."})
            org_id = _org(db, body.project_id)
            repos = [{"full_name": r} for r in (seed.get("picks") or {}).get("repos") or []]
        pid = body.project_id
        if kind == "idea":
            run_input = _run_input(body, brief, repos[0]["full_name"] if len(repos) == 1 else None)
    else:
        # ── PHASE 1: everything, checked, in ONE transaction ─────────────────────────────────
        with begin(_engine) as db:
            _require_project(db, body.context_project_id)
            org_id = _org(db, body.context_project_id)
            project_type = "knowledge_graph" if body.map_rules and kind != "idea" else "app"
            try:
                created = project_service.create(
                    db, _settings, auth=ctx, name=body.name.strip(), slug=None,
                    description=(brief.get("problem") or body.words.strip())[:2000] or None,
                    actor=actor, principal=main.current_api_principal.get(),
                    entitlements=main.entitlements, members=main.members, specpack=main.specpack,
                    project_type=project_type)
            except project_service.ProjectExists as clash:
                raise Refused("project", 409, {
                    "error": "project_exists", "project_id": clash.project_id,
                    "message": f"A project called {body.name.strip()} exists. Choose another name."}) from None
            except ValueError as error:
                raise Refused("project", 422, {"error": "bad_project", "message": str(error)}) from None
            pid = str(created["project_id"])
            steps.append({"key": "project", "ok": True, "label": "Project created"})

            # where the code lives: repositories, or a folder on a host
            repos: list[dict[str, Any]] = []
            for repo_id in body.repo_ids:
                try:
                    row = registry.get_row(db, org_id, repo_id)
                    if row.get("allowed") is False:
                        raise MultiRepoError(409, "repo_not_allowed",
                                             f"{row['full_name']} is not allowed for BOSS yet: "
                                             "allow it in Repo and models.")
                    project_repos.put(db, _settings, pid, repo_id, actor=actor)
                except MultiRepoError as error:
                    raise Refused("repository", error.status or 409, {
                        **error.body(), "fix_href": "?area=setup&focus=repository"}) from None
                except LookupError as error:
                    raise Refused("repository", 404, {"error": "repo_not_found",
                                                      "message": str(error)}) from None
                repos.append({"id": repo_id, "full_name": str(row["full_name"])})
            if repos:
                steps.append({"key": "repository", "ok": True, "label": "Repository added"
                              if len(repos) == 1 else f"{len(repos)} repositories added"})
            if body.folder is not None:
                from boss_engine.services.project_experience import host_folder

                try:
                    facts = host_folder.probe(db, _settings, pid, body.folder.host_id,
                                              body.folder.path)
                    host_folder.register(db, pid, body.folder.host_id, body.folder.path, facts,
                                         actor=actor)
                except host_folder.FolderError as error:
                    raise Refused("folder", 422, {"error": "folder_refused",
                                                  "message": str(error)}) from None
                steps.append({"key": "folder", "ok": True, "label": "Folder recorded"})

            # captain and crew
            if body.captain and body.captain != "engineering_captain":
                try:
                    captain.assign(db, pid, body.captain, actor=actor)
                except captain.CaptainRefused as error:
                    raise Refused("captain", 422, {"error": "not_an_agent",
                                                   "message": str(error)}) from None
                steps.append({"key": "captain", "ok": True, "label": "Captain set"})
            if body.crew:
                done = agent_assignment.set_team(db, project_id=pid, role_keys=body.crew,
                                                 actor=actor)
                if not done.get("ok"):
                    raise Refused("crew", 422, {"error": "crew_refused", "message": str(
                        done.get("error") or "The crew was refused.")})
                steps.append({"key": "crew", "ok": True, "label": "Crew set"})

            # several repositories: the app's components, mapped (suggested, in build order)
            layout = None
            if kind == "idea" and len(repos) > 1:
                try:
                    suggested = components.suggest(db, pid, [project_repos.binding_for(db, pid, r["id"])
                                                             for r in repos])
                    rows = [{"name": c["name"], "binding_id": c["binding_id"],
                             "path": c.get("path") or "/", "kind": c.get("kind") or "code",
                             "depends_on": c.get("depends_on") or []}
                            for c in suggested.get("components") or []]
                    components.put(db, pid, components.app_key_of(body.name), rows,
                                   app_name=body.name.strip(), actor=actor)
                except MultiRepoError as error:
                    raise Refused("components", error.status or 422, error.body()) from None
                layout = {"mode": "multi", "app_name": body.name.strip()}
                steps.append({"key": "components", "ok": True, "label": "Components mapped"})

            # staging, on a verified host with a free port (it deploys the repository)
            if body.staging is not None and repos:
                from boss_engine.services.deployment_fleet import HostNotUsable
                from boss_engine.services.stack_deploy import StackDeployError

                try:
                    staged = onboarding.use_for_staging(db, pid, host_id=body.staging.host_id,
                                                        port=body.staging.port, actor=actor)
                except (HostNotUsable, StackDeployError, LookupError) as error:
                    said = error.to_dict() if hasattr(error, "to_dict") else {"headline": str(error)}
                    raise Refused("staging", getattr(error, "status_code", 422) or 422, {
                        "error": "staging_refused", "message": " ".join(
                            x for x in (said.get("headline"), said.get("action")) if x)
                        or str(error)}) from None
                staged.pop("onboarding", None)
                steps.append({"key": "staging", "ok": True,
                              "label": f"Staging on {staged.get('host')} :{staged.get('port')}"})

            # the seed: what the person confirmed, read by the PRD meeting and onboarding
            picks = {"captain": body.captain or "engineering_captain", "crew": body.crew or [],
                     "stack": body.stack, "connectors": body.connectors,
                     "workflow_key": body.workflow_key, "repos": [r["full_name"] for r in repos],
                     "folder": body.folder.model_dump() if body.folder else None,
                     "staging": ({**body.staging.model_dump(), "made": bool(staged)}
                                 if body.staging else None),
                     "notify": {"channel": body.inputs.get("notify_channel") or None,
                                "when": body.notify_when},
                     "audience": body.audience, "done_when": body.done_when,
                     "read_first": body.read_first, "map_rules": body.map_rules,
                     "note": (body.note or "").strip() or None}
            add_project.save_seed(db, pid, kind=kind, words=body.words, brief=brief, picks=picks,
                                  actor=actor)

            if kind == "idea":
                # EVERY CHECK THE START MAKES, before anything commits: its readiness gate on the
                # new project (with Start anyway), and the connectors' dry run.
                run_input = _run_input(body, brief, repos[0]["full_name"] if len(repos) == 1 else None)
                if layout:
                    run_input["repo_layout"] = layout
                if body.connectors:
                    _connectors_checked(request, db, body, pid, org_id, run_input, actor)
                else:
                    _key, version = _version_of(db, pid, body.workflow_key)
                    if version:
                        try:
                            workflow_readiness.refuse_unless_ready(
                                db, _settings, pid, version_id=version, input_data=run_input,
                                start_anyway=body.start_anyway)
                        except workflow_readiness.WorkflowNotReadyError as error:
                            raise Refused("start", 409, {
                                "error": "not_ready", "message": str(error),
                                "readiness": error.readiness}) from None

    # ── PHASE 2: start it ───────────────────────────────────────────────────────────────────
    started: dict[str, Any] = {}
    if kind != "idea":
        with begin(_engine) as db:
            view = onboarding.start(db, pid, trigger="created", actor=actor)
        started = {"onboarding": {"run_id": view.get("run_id"), "state": view.get("state")}}
        steps.append({"key": "onboarding", "ok": True, "label": "Onboarding started"})
        href = project_href(pid, tab="onboarding")
    else:
        try:
            started = _start(request, body, pid, org_id, run_input, actor)
        except HTTPException as exc:
            # Only the start could refuse this (a consent grant, a pre-check): the project is
            # kept, and the same call with retry.project_id starts it.
            said = _words_of(exc)
            return JSONResponse({**said, "step": "start", "created": True, "project_id": pid,
                                 "retry": {"project_id": pid}, "steps": steps},
                                status_code=exc.status_code)
        except Refused as refused:
            return JSONResponse({**refused.body, "step": refused.step, "created": True,
                                 "project_id": pid, "retry": {"project_id": pid},
                                 "steps": steps}, status_code=refused.status)
        steps.append({"key": "start", "ok": True, "label": "Planning started"})
        href = project_href(pid, tab="overview")
    return {"created": True, "project_id": pid, "name": body.name.strip(), "kind": kind,
            "href": href, "steps": steps, "started": started, "staging": staged}


def _connector_plan_body(body: CreateAndStartBody, pid: str, run_input: dict[str, Any]) -> dict:
    stack = (body.stack_language or "").strip()
    if not stack:
        raise Refused("connectors", 422, {"error": "stack_required",
                                          "message": "Pick a stack for the connectors."})
    return {"project_id": pid, "new_app": {"name": body.name.strip()}, "stack": stack,
            "note": run_input.get("directive") or body.words,
            "workflow_key": body.workflow_key or None,
            **({"accept_egress": list(body.accept_egress)} if body.accept_egress else {}),
            "start_anyway": body.start_anyway or None, "dedupe_key": body.dedupe_key or None}


def _connectors_checked(request: Request, db, body: CreateAndStartBody, pid: str, org_id: str,
                        run_input: dict[str, Any], actor: str) -> None:
    """Phase 1 for connectors: their test keys written (write-only), then the template plan's
    dry run (stack, secrets, egress, conflicts) — exactly what apply / compose check first."""
    from apps.api.app import identity_guard
    from boss_engine.multirepo import components
    from boss_engine.templates import apply as apply_service
    from boss_engine.templates import compose as compose_service
    from boss_engine.templates import secrets as secrets_service
    from boss_engine.templates import service
    from boss_engine.templates.service import TemplateError

    ctx = identity_guard.request_auth(request)
    actor_id = ctx.principal.id if ctx is not None else actor
    data = _connector_plan_body(body, pid, run_input)
    try:
        if body.connector_secrets:
            manifests = [service.manifest_of(service.get_row(db, org_id, ref))
                         for ref in body.connectors]
            app_key = components.app_key_of(body.name.strip())
            for name, value in body.connector_secrets.items():
                spec = secrets_service.declared_secret(manifests, name)
                if spec is None or not str(value or "").strip():
                    continue
                for env in ("build", "preview"):
                    secrets_service.put(db, org_id=org_id, project_id=pid, app_key=app_key,
                                        name=name, env=env, mode="test", value=str(value),
                                        actor_id=actor_id, spec=spec, may_live=False)
        if len(body.connectors) == 1:
            apply_service.plan(db, org_id=org_id, project_id=pid, template_ref=body.connectors[0],
                               body=data, actor_id=actor_id, write=False)
        else:
            compose_service.plan(db, org_id=org_id, project_id=pid,
                                 template_refs=list(body.connectors), body=data,
                                 actor_id=actor_id, write=False)
    except TemplateError as exc:
        raise Refused("connectors", exc.status, {**exc.body}) from None


def _start(request: Request, body: CreateAndStartBody, pid: str, org_id: str,
           run_input: dict[str, Any], actor: str) -> dict[str, Any]:
    """Phase 2 for an idea: the start door, or (connectors) the template apply / compose path
    New build used — one connector alone, two to six as ONE build."""
    from apps.api.app import identity_guard, main
    from apps.api.app.request_bodies import WorkflowStartBody
    from boss_engine.templates import apply as apply_service
    from boss_engine.templates import compose as compose_service
    from boss_engine.templates.service import TemplateError

    who = identity_guard.request_actor() or None
    if not body.connectors:
        with begin(_engine) as db:
            out = main.start_delivery_workflow(WorkflowStartBody(
                project_id=pid, actor=who,
                **({"workflow_key": body.workflow_key} if body.workflow_key
                   else {"work_class": "project.new"}),
                input=run_input, start_anyway=body.start_anyway or None,
                dedupe_key=body.dedupe_key or f"add-project:{pid}"), connection_in=db)
        run = (out or {}).get("run") or {}
        return {"run_id": run.get("id"), **{k: out[k] for k in ("readiness", "precheck",
                                                                "ignored_input") if out.get(k)}}
    ctx = identity_guard.request_auth(request)
    actor_id = ctx.principal.id if ctx is not None else actor
    data = _connector_plan_body(body, pid, run_input)
    try:
        with begin(_engine) as db:
            if len(body.connectors) == 1:
                plan = apply_service.plan(db, org_id=org_id, project_id=pid,
                                          template_ref=body.connectors[0], body=data,
                                          actor_id=actor_id)
                dedupe = body.dedupe_key or f"template:{plan['application_id']}"
            else:
                plan = compose_service.plan(db, org_id=org_id, project_id=pid,
                                            template_refs=list(body.connectors), body=data,
                                            actor_id=actor_id)
                dedupe = body.dedupe_key or ("template-compose:"
                                             f"{plan['run_input']['template']['application_id']}")
            merged = {**plan["run_input"], **{k: v for k, v in run_input.items()
                                              if k not in plan["run_input"]}}
            out = main.start_delivery_workflow(WorkflowStartBody(
                project_id=pid, actor=who, workflow_key=plan["workflow_key"], input=merged,
                dedupe_key=dedupe, start_anyway=body.start_anyway or None,
                allow_unsupported_stack=True), connection_in=db)
            run = (out or {}).get("run") or {}
            if len(body.connectors) == 1:
                apply_service.record(db, plan, project_id=pid, workflow_run_id=run.get("id"),
                                     actor_id=actor_id)
            else:
                compose_service.record(db, plan, org_id=org_id, project_id=pid,
                                       workflow_run_id=run.get("id"), actor_id=actor_id)
    except TemplateError as exc:
        raise Refused("connectors", exc.status, {**exc.body}) from None
    return {"run_id": run.get("id"), "connectors": list(body.connectors)}
