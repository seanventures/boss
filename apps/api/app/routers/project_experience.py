"""The Project experience API (BOSSAPP-913 / 914): docs/product/PROJECT_EXPERIENCE_CONTRACT.md.

GET  /v1/projects/summary                         §1  the Projects list (cards)
GET  /v1/projects/{id}/overview                   §2  the Overview tab
GET  /v1/projects/{id}/work?state=&q=             §3  the Work tab
GET  /v1/projects/{id}/work/{work}/story          §3  one piece of work, step by step (drawer 05b)
POST /v1/projects/{id}/work                       §3  new work, through THE start door
GET  /v1/projects/{id}/work/plan?kind=&workflow_key=&detail=  §3  Helm's plan: strip, estimates, options
POST /v1/projects/{id}/work/understand            §3  Helm's understanding: the brief, one turn
POST /v1/projects/{id}/plan                       §6  New work › Add to Next (nothing starts)
GET  /v1/projects/{id}/knowledge                  §4  Repo & knowledge
GET  /v1/projects/{id}/deployments                §5  Deployments
GET  /v1/projects/{id}/captain                    §7  the captain and squad
POST /v1/projects/{id}/captain                    §7  change the captain (owner/admin)
GET  /v1/projects/{id}/onboarding                 §8  the onboarding agent's record
POST /v1/projects/{id}/onboarding/start           §8  start (or show) onboarding
POST /v1/projects/{id}/onboarding/backlog/accept §8  accept proposed backlog onto Roadmap › Next
GET  /v1/projects/{id}/onboarding/deploy-target   §8  what "Use for staging" will make
POST /v1/projects/{id}/onboarding/deploy-target   §8  make it in place (step-up), the row turns green
GET  /v1/projects/{id}/hosts/{host}/free-port         the port a new target there would get
GET  /v1/projects/{id}/plan                       §6  the Roadmap's Now · Next · Later lanes
POST /v1/projects/{id}/plan/{item}                §6  move an accepted item (Next ↔ Later, order)
POST /v1/projects/onboarding/execute                  the worker's leased onboarding job
GET  /v1/jump?q=&project_id=                      §9  ⌘K
GET  /v1/projects/{id}/settings                   §11 Project › Settings (sections + actions)
GET  /v1/projects/{id}/demo-logins                §11 demo users per environment (names only)
GET  /v1/projects/{id}/demo-logins/reveal?env=    §11 the passwords: owner/admin + step-up
GET  /v1/projects/{id}/health                     §12 the Health tab

Reads are one round trip per tab and scoped like every project read (project:read). Every write
goes through a path that already exists (the start door, the jobs queue) except the captain
choice, which is new state.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, authenticated, permit

router = APIRouter(tags=["projects"])
_settings = get_settings()
_engine = get_engine(_settings)


def _missing(error: LookupError) -> HTTPException:
    return HTTPException(status_code=404, detail=str(error) or "Not found.")


def _require_project(connection, project_id: str) -> None:
    from boss_engine.services.project_experience.common import require_project

    try:
        require_project(connection, project_id)
    except LookupError as error:
        raise _missing(error) from None


def _actor(request: Request) -> str:
    from apps.api.app import identity_guard
    from apps.api.app.main import current_api_principal

    ctx = identity_guard.request_auth(request)
    if ctx is not None:
        return identity_guard.actor_for(ctx)
    return current_api_principal.get() or "operator"


def _legacy_bar(connection, project_id: str, permission: str) -> None:
    """Pre-identity installs (shadow mode): the legacy membership bar, as the archive and start
    routes keep it. Under enforce, @permit has decided already."""
    from apps.api.app import identity_guard
    from apps.api.app.main import (current_api_principal, current_transport_authenticated,
                                   require_project_actor)

    principal = current_api_principal.get()
    if not identity_guard.identity_enforced() and principal and current_transport_authenticated.get():
        require_project_actor(connection, project_id, principal, permission)


# ── §1 the Projects list ────────────────────────────────────────────────────────────────────

@router.get("/v1/projects/summary")
@authenticated(note="Lists only the projects visible_project_rows says the caller may read "
                    "(project:read per project), the same answer as GET /v1/projects.")
def projects_summary(request: Request, include_archived: bool = False) -> dict[str, Any]:
    from apps.api.app.main import visible_project_rows
    from boss_engine.services.project_experience import overview

    rows = visible_project_rows(request, include_archived=include_archived)
    with begin(_engine) as connection:
        # The install's untouched bootstrap project is not "a project" (00a-02/13/34): the
        # rail, Projects and first run never list it.
        items = [i for i in overview.summary(connection, [str(r["id"]) for r in rows])
                 if not i.get("placeholder")]
        # Screen 02's card facts (ring, status lines, crew, caps): cards.extras, batched.
        from boss_engine.services.project_experience import cards

        more = cards.extras(connection, [i["id"] for i in items])
    for item in items:
        item.update(more.get(item["id"], {}))
    caps = [i["week_cap_usd"] for i in items if i.get("week_cap_usd") and not i.get("archived")]
    first_run = not any(i.get("repo") for i in items)
    return {"items": items, "first_run": first_run,
            "week_cap_usd": round(sum(caps), 2) if caps else None,
            "first_run_href": "?area=projects&first_run=1" if first_run else None}


# ── §2 Overview, §3 Work, §4 Knowledge, §5 Deployments, §12 Health ──────────────────────────

@router.get("/v1/projects/{project_id}/overview")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def project_overview(project_id: str) -> dict[str, Any]:
    from boss_engine.services.project_experience import overview

    with begin(_engine) as connection:
        try:
            return overview.view(connection, project_id)
        except LookupError as error:
            raise _missing(error) from None


@router.get("/v1/projects/{project_id}/work")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def project_work(project_id: str, state: Optional[str] = None, q: Optional[str] = None
                 ) -> dict[str, Any]:
    from boss_engine.services.project_experience import work

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        return work.view(connection, project_id, state=state or None, q=q or None)


@router.get("/v1/projects/{project_id}/work/plan")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def project_work_plan(project_id: str, kind: str = "feature",
                      workflow_key: Optional[str] = None, detail: bool = False) -> dict[str, Any]:
    """Helm's plan for new work (screen 05): the workflow Start would run for this kind (or the
    one picked with "change"), its real steps as a strip, time and cost from this project's own
    history, and the workflows "change" offers. `detail` (the New work modal): the workflow's
    steps, each option's steps and estimate, and the issue / target / tell defaults."""
    from boss_engine.services.project_experience import new_work

    kind = (kind or "feature").strip().lower()
    if kind not in new_work.KINDS:
        raise HTTPException(status_code=422, detail={
            "error": "bad_kind", "message": "kind is feature, bug or ticket."})
    with begin(_engine) as connection:
        _require_project(connection, project_id)
        try:
            return new_work.plan(connection, project_id, kind, workflow_key or None,
                                 detail=detail)
        except LookupError as error:
            raise HTTPException(status_code=422, detail={
                "error": "unknown_workflow", "message": str(error)}) from None


@router.get("/v1/projects/{project_id}/work/{work_id}/story")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def project_work_story(project_id: str, work_id: str) -> dict[str, Any]:
    """The work drawer (screen 05b): each station in words with its proof link, the run's
    number, duration, preview and cost split."""
    from boss_engine.services.project_experience import work_story

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        try:
            return work_story.story(connection, project_id, work_id)
        except LookupError as error:
            raise _missing(error) from None


class UnderstandBody(BaseModel):
    kind: str = Field("feature", description="feature | bug | ticket")
    text: str = Field("", description="The person's words, as typed (an issue link is read).")
    #: The conversation so far, oldest first: [{from: "you"|"helm", text, ask?}].
    chat: list[dict[str, Any]] = Field(default_factory=list)
    #: The card as the person sees it now (their own edits included).
    brief: Optional[dict[str, Any]] = None
    #: A Rewrite's direction ("smaller first step").
    hint: str = ""
    #: The modal's draft id: the turn's transcript is kept on its own thread.
    draft: str = ""
    workflow_key: Optional[str] = None


@router.post("/v1/projects/{project_id}/work/understand")
@permit(P.ASSISTANT_CHAT, scope=Scope.project(path="project_id"))
def project_work_understand(project_id: str, payload: UnderstandBody,
                            request: Request) -> dict[str, Any]:
    """HELM'S UNDERSTANDING (the New work modal): one turn through the one agent pipeline that
    returns the brief — title, what changes, area from the code graph, done when, risk, Helm's
    additions marked — updated from the whole conversation, plus what Helm says and the one
    question it may ask. No model, or no time: `status: no_model`, and Start uses the words."""
    import re as _re

    from boss_engine.services.project_experience import new_work, understanding

    kind = (payload.kind or "feature").strip().lower()
    if kind not in new_work.KINDS:
        raise HTTPException(status_code=422, detail={
            "error": "bad_kind", "message": "kind is feature, bug or ticket."})
    draft = _re.sub(r"[^A-Za-z0-9_-]", "", payload.draft or "")[:40]
    with begin(_engine) as connection:
        _require_project(connection, project_id)
        workflows = new_work.options(connection, project_id, kind)
        return understanding.understand(
            connection, _settings, project_id, actor=_actor(request), kind=kind,
            words=payload.text or "", chat=[m for m in payload.chat if isinstance(m, dict)][-24:],
            brief=payload.brief, hint=payload.hint or "", draft=draft, workflows=workflows)


class NewWorkBody(BaseModel):
    kind: str = Field("feature", description="feature | bug | ticket")
    text: str = Field(..., description="What to do, in the person's words.")
    title: Optional[str] = None
    #: Where the suggestion came from (an onboarding candidate's ref, a roadmap item).
    source_ref: Optional[str] = None
    #: The issue it came from (GitHub / Plane), kept on the run for the row's #12 chip.
    source_url: Optional[str] = None
    #: "change" on Helm's plan: another active product workflow of this project.
    workflow_key: Optional[str] = None
    #: The console's readiness sheet: start although these are missing (the start door's own).
    start_anyway: Optional[list[str]] = None
    accept: Optional[list[str]] = None
    dedupe_key: Optional[str] = None
    #: Helm's understanding as confirmed (the New work modal): it becomes the run's directive
    #: (the work's spec), and its `done_when` the build's acceptance criteria (the PR checklist).
    brief: Optional[dict[str, Any]] = None
    #: The criteria on their own (a planned item started later carries them this way).
    acceptance_criteria: Optional[list[str]] = None
    priority: Optional[str] = None
    #: Who else to tell (an email or a channel), for the workflow's notify step.
    notify_channel: Optional[str] = None
    #: The Issue chip: {"mode": "create"} files the work in the project's tracker first.
    issue: Optional[dict[str, Any]] = None


def _brief_fields(payload: "NewWorkBody", said: str) -> dict[str, Any]:
    """The directive, title and criteria a confirmed brief gives the run (else the words)."""
    from boss_engine.services.project_experience import understanding

    brief = payload.brief if isinstance(payload.brief, dict) else None
    criteria = [" ".join(str(c).split())[:300] for c in
                ((brief or {}).get("done_when") or payload.acceptance_criteria or [])
                if str(c or "").strip()][:12]
    out: dict[str, Any] = {"directive": understanding.directive(said, brief)[:8000]}
    if criteria:
        out["acceptance_criteria"] = criteria
    if brief and str(brief.get("title") or "").strip():
        out["title"] = " ".join(str(brief["title"]).split())[:80]
    prio = (payload.priority or "").strip().lower()
    if prio in understanding.PRIORITIES:
        out["priority"] = prio
    if (payload.notify_channel or "").strip():
        out["notify_channel"] = " ".join(payload.notify_channel.split())[:200]
    return out


def _title_of(text_in: str, fallback: str) -> str:
    """THE SHORT TITLE stored when work is created (titles.shorten): a name like "Add SLA
    timers to tickets", never the brief's first 80 characters cut mid-sentence. The run, its
    build and its pull request all carry it; the brief stays whole in `directive`."""
    from boss_engine.services.project_experience.titles import shorten

    return shorten(text_in or "", 6) or fallback


@router.post("/v1/projects/{project_id}/work")
@permit(P.WORKFLOW_RUN_START, scope=Scope.project(path="project_id"))
def project_new_work(project_id: str, payload: NewWorkBody) -> dict[str, Any]:
    """New work through THE start door (start_delivery_workflow), exactly as the project page's
    "What should we add?" box starts it: the project's mapped small-feature workflow (or Minor
    Feature) on its repository, or its new-app default when it has none. Consent, readiness and
    step-up refusals pass through unchanged so the console's existing sheets handle them."""
    from apps.api.app import identity_guard
    from apps.api.app.main import start_delivery_workflow
    from apps.api.app.request_bodies import WorkflowStartBody
    from boss_engine.services import start_project
    from boss_engine.services.project_experience import new_work, work
    from boss_engine.services.project_experience.common import project_href

    kind = (payload.kind or "feature").strip().lower()
    if kind not in ("feature", "bug", "ticket"):
        raise HTTPException(status_code=422, detail={
            "error": "bad_kind", "message": "kind is feature, bug or ticket."})
    said = (payload.text or "").strip()
    if not said:
        raise HTTPException(status_code=422, detail={
            "error": "text_required", "message": "Say what to do."})
    with begin(_engine) as connection:
        page = None
        try:
            page = start_project.view(connection, project_id)
        except LookupError as error:
            raise _missing(error) from None
        try:
            chosen = new_work.choose(connection, project_id, kind, payload.workflow_key or None)
        except LookupError as error:
            raise HTTPException(status_code=422, detail={
                "error": "unknown_workflow", "message": str(error)}) from None
    repo = (page.get("repo") or {}).get("full_name")
    # A TICKET pasted as a link keeps the link (the row's #12 chip); so does work started from an
    # issue the onboarding agent found (`issue:12`) or a roadmap item that carries one.
    link = (payload.source_url or "").strip() or (new_work.ticket_link(said) if kind == "ticket"
                                                  else None)
    issue = new_work.issue_of(payload.source_ref, link, repo)
    from boss_engine.services.project_experience.titles import strip_label

    briefed = _brief_fields(payload, said)
    title = (strip_label(" ".join((payload.title or "").split()))[:80]
             or briefed.pop("title", None)
             or (new_work.ticket_title(said, link) if kind == "ticket" else None)
             or _title_of(said, page["name"]))
    briefed.pop("title", None)
    # THE ISSUE CHIP's "create": the work is filed in the project's tracker first, so the run,
    # its row and its pull request carry the issue. A tracker that refuses never stops Start.
    issue_error = None
    if not issue and isinstance(payload.issue, dict) and payload.issue.get("mode") == "create":
        with begin(_engine) as connection:
            made = new_work.create_issue(connection, project_id, title=title,
                                         body=briefed["directive"])
        issue = made.get("issue")
        issue_error = made.get("error")
        if issue:
            link = issue["external_url"]
    run_input: dict[str, Any] = {"title": title, "work_kind": kind, **briefed}
    if repo:
        run_input["repository"] = repo
    if payload.source_ref or link:
        run_input["source_ref"] = str(payload.source_ref or link)[:200]
    if issue:
        run_input["source_url"] = str(issue["external_url"])[:500]
    body: dict[str, Any] = {"project_id": project_id, "input": run_input,
                            "actor": identity_guard.request_actor() or None}
    if chosen.get("workflow_key"):
        body["workflow_key"] = chosen["workflow_key"]
    else:
        body["work_class"] = chosen.get("work_class") or start_project.NEW_APP_CLASS
    if payload.start_anyway:
        body["start_anyway"] = list(payload.start_anyway)
    if payload.accept:
        body["accept"] = list(payload.accept)
    if payload.dedupe_key:
        body["dedupe_key"] = payload.dedupe_key
    started = start_delivery_workflow(WorkflowStartBody(**body))
    run_id = str(((started or {}).get("run") or {}).get("id") or "")
    with begin(_engine) as connection:
        if run_id and payload.source_ref:
            # Started from a roadmap item (plan.link_started): it leaves Next / Later.
            from boss_engine.services.project_experience import plan

            plan.link_started(connection, project_id, payload.source_ref, run_id)
        found = next((w for w in work.everything(connection, project_id) if w["id"] == run_id), None)
    out = {"work": found, "run_id": run_id or None,
           "href": project_href(project_id, tab="work", drawer=f"run:{run_id}") if run_id else
           project_href(project_id, tab="work"),
           "duplicate": bool(((started or {}).get("run") or {}).get("duplicate"))}
    for key in ("readiness", "precheck", "ignored_input"):
        if (started or {}).get(key):
            out[key] = started[key]
    if issue:
        out["issue"] = issue
    if issue_error:
        out["issue_error"] = issue_error
    return out


@router.get("/v1/projects/{project_id}/knowledge")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def project_knowledge(project_id: str, scope: Optional[str] = None) -> dict[str, Any]:
    """`scope`: "app" (default — this project's part of a shared repository) or "repo"."""
    from boss_engine.services.project_experience import knowledge

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        return knowledge.view(connection, project_id, scope=scope)


@router.get("/v1/projects/{project_id}/deployments")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def project_deployments(project_id: str) -> dict[str, Any]:
    from boss_engine.services.project_experience import deployments

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        return deployments.view(connection, project_id)


@router.get("/v1/projects/{project_id}/health")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def project_health(project_id: str) -> dict[str, Any]:
    from boss_engine.services.project_experience import health

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        return health.view(connection, project_id)


# ── §7 the captain ──────────────────────────────────────────────────────────────────────────

@router.get("/v1/projects/{project_id}/captain")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def project_captain(project_id: str) -> dict[str, Any]:
    from boss_engine.services.project_experience import captain

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        return captain.view(connection, project_id)


class CaptainBody(BaseModel):
    agent_id: str = Field(..., description="An agent id or role key of this project.")


@router.post("/v1/projects/{project_id}/captain")
@permit(P.PROJECT_UPDATE, scope=Scope.project(path="project_id"))
def project_captain_change(project_id: str, payload: CaptainBody, request: Request) -> Any:
    from boss_engine.services.project_experience import captain

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        _legacy_bar(connection, project_id, "member_manage")
        try:
            return captain.assign(connection, project_id, payload.agent_id, actor=_actor(request))
        except captain.CaptainRefused as refused:
            return JSONResponse({"error": "not_an_agent", "message": str(refused)},
                                status_code=422)


# ── §8 the onboarding agent ─────────────────────────────────────────────────────────────────

@router.get("/v1/projects/{project_id}/onboarding")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def project_onboarding(project_id: str) -> dict[str, Any]:
    from boss_engine.services.project_experience import onboarding

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        return onboarding.view(connection, project_id)


@router.post("/v1/projects/{project_id}/onboarding/start")
@permit(P.WORKFLOW_RUN_START, scope=Scope.project(path="project_id"))
def project_onboarding_start(project_id: str, request: Request) -> dict[str, Any]:
    """Start onboarding on demand (also started on project creation and when a repository is
    added). A run still moving is returned as it is."""
    from boss_engine.services.project_experience import onboarding

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        _legacy_bar(connection, project_id, "workflow_execute")
        # An archived project answers 409 through the app's ProjectArchived handler.
        return onboarding.start(connection, project_id, trigger="manual", actor=_actor(request))


class BacklogAcceptBody(BaseModel):
    ids: list[str] = Field(default_factory=list, max_length=50)
    lane: str = "next"
    titles: dict[str, str] = Field(default_factory=dict)


@router.post("/v1/projects/{project_id}/onboarding/backlog/accept")
@permit(P.WORK_ITEM_MANAGE, scope=Scope.project(path="project_id"))
def project_onboarding_backlog_accept(project_id: str, payload: BacklogAcceptBody,
                                      request: Request) -> dict[str, Any]:
    """Screen 10's "Accept backlog": the chosen candidates, in the order given, go on the
    Roadmap's Next lane as accepted items (planned work). Nothing starts: a build starts only
    when a person starts an item. Idempotent."""
    from boss_engine.services.project_experience import onboarding

    if not payload.ids:
        raise HTTPException(status_code=422, detail={
            "error": "ids_required", "message": "Pick at least one item to accept."})
    with begin(_engine) as connection:
        _require_project(connection, project_id)
        try:
            return onboarding.accept_backlog(connection, project_id, list(payload.ids),
                                             actor=_actor(request), lane=payload.lane,
                                             titles=payload.titles)
        except LookupError as error:
            raise _missing(error) from None
        except ValueError as error:
            raise HTTPException(status_code=422, detail={"error": "bad_lane",
                                                         "message": str(error)}) from None


def _refused(error: Exception) -> HTTPException:
    """A StackDeployError / HostNotUsable as {headline, action}: words, never raw JSON."""
    said = error.to_dict() if hasattr(error, "to_dict") else {"headline": str(error), "action": ""}
    return HTTPException(status_code=getattr(error, "status_code", 422), detail={
        "headline": said.get("headline") or str(error), "action": said.get("action") or "",
        "message": " ".join(x for x in (said.get("headline"), said.get("action")) if x)})


@router.get("/v1/projects/{project_id}/onboarding/deploy-target")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def project_onboarding_deploy_target(project_id: str,
                                     host_id: Optional[str] = None) -> dict[str, Any]:
    """What onboarding's "Use for staging" will make (its confirm sheet): what, where, port,
    manifest. Reads only; the port is the first free one in the host's range."""
    from boss_engine.services.deployment_fleet import HostNotUsable
    from boss_engine.services.project_experience import onboarding

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        try:
            return onboarding.staging_proposal(connection, project_id, host_id)
        except LookupError as error:
            raise _missing(error) from None
        except HostNotUsable as error:
            raise _refused(error) from None


class StagingTargetBody(BaseModel):
    host_id: Optional[str] = None
    port: Optional[int] = Field(default=None, ge=1, le=65535)


@router.post("/v1/projects/{project_id}/onboarding/deploy-target")
@permit(P.DEPLOYMENT_TARGET_MANAGE, scope=Scope.project(path="project_id"))
def project_onboarding_deploy_target_use(project_id: str, payload: StagingTargetBody,
                                         request: Request) -> dict[str, Any]:
    """Onboarding's "Use for staging", in place: the staging stack target on the offered host
    (this project's own, or a verified host its organisation shares), then the checklist row is
    done and the run moves on. deployment_target:manage asks for step-up, as Infra's create."""
    from boss_engine.services.deployment_fleet import HostNotUsable
    from boss_engine.services.project_experience import onboarding
    from boss_engine.services.stack_deploy import StackDeployError

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        _legacy_bar(connection, project_id, "providerops_manage")
        try:
            return onboarding.use_for_staging(connection, project_id, host_id=payload.host_id,
                                              port=payload.port, actor=_actor(request))
        except LookupError as error:
            raise _missing(error) from None
        except (HostNotUsable, StackDeployError) as error:
            raise _refused(error) from None


@router.get("/v1/projects/{project_id}/hosts/{host_id}/free-port")
@permit(P.HOST_READ, scope=Scope.project(path="project_id"))
def project_host_free_port(project_id: str, host_id: str,
                           app_name: Optional[str] = None) -> dict[str, Any]:
    """The port a new target on this host would get (deployment_fleet.suggest_port): the app's
    earlier port when free, else the first free one in the host's range. Infra's App target
    sheet suggests it."""
    from boss_engine.services.deployment_fleet import HostNotUsable, suggest_port, usable_host

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        try:
            host = usable_host(connection, host_id, project_id)
        except HostNotUsable as error:
            raise _refused(error) from None
        return {"host_id": host["id"], "address": host["address"],
                "port": suggest_port(connection, host, project_id=project_id,
                                     app_name=str(app_name or ""))}


# ── §6 the roadmap's plan (Now · Next · Later) ──────────────────────────────────────────────

@router.get("/v1/projects/{project_id}/plan")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def project_plan(project_id: str) -> dict[str, Any]:
    from boss_engine.services.project_experience import plan

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        return plan.lanes(connection, project_id)


@router.post("/v1/projects/{project_id}/plan")
@permit(P.WORKFLOW_RUN_START, scope=Scope.project(path="project_id"))
def project_plan_add(project_id: str, payload: NewWorkBody, request: Request) -> dict[str, Any]:
    """NEW WORK › ADD TO NEXT: the work goes on Roadmap › Next (ranked by its priority, then by
    Marshal) and nothing starts. The same body as Start; its later Start sends it again."""
    from boss_engine.services.project_experience import new_work, plan

    kind = (payload.kind or "feature").strip().lower()
    if kind not in new_work.KINDS:
        raise HTTPException(status_code=422, detail={
            "error": "bad_kind", "message": "kind is feature, bug or ticket."})
    said = (payload.text or "").strip()
    if not said:
        raise HTTPException(status_code=422, detail={
            "error": "text_required", "message": "Say what to do."})
    briefed = _brief_fields(payload, said)
    link = (payload.source_url or "").strip() or new_work.ticket_link(said)
    with begin(_engine) as connection:
        _require_project(connection, project_id)
        if payload.workflow_key:
            try:
                new_work.choose(connection, project_id, kind, payload.workflow_key)
            except LookupError as error:
                raise HTTPException(status_code=422, detail={
                    "error": "unknown_workflow", "message": str(error)}) from None
        issue_error = None
        if not link and isinstance(payload.issue, dict) and payload.issue.get("mode") == "create":
            made = new_work.create_issue(connection, project_id,
                                         title=briefed.get("title") or _title_of(said, "New work"),
                                         body=briefed["directive"])
            link = (made.get("issue") or {}).get("external_url")
            issue_error = made.get("error")
        out = plan.add(connection, project_id, kind=kind, actor=_actor(request),
                       title=(payload.title or briefed.get("title") or _title_of(said, "")),
                       text_in=briefed["directive"], priority=briefed.get("priority", "normal"),
                       workflow_key=payload.workflow_key or None,
                       acceptance_criteria=briefed.get("acceptance_criteria"),
                       notify_channel=briefed.get("notify_channel"), source_url=link)
    if issue_error:
        out["issue_error"] = issue_error
    return out


class PlanMoveBody(BaseModel):
    lane: str
    before: Optional[str] = None


@router.post("/v1/projects/{project_id}/plan/{item_id}")
@permit(P.WORK_ITEM_MANAGE, scope=Scope.project(path="project_id"))
def project_plan_move(project_id: str, item_id: str, payload: PlanMoveBody,
                      request: Request) -> dict[str, Any]:
    """Move an accepted roadmap item between Next and Later, or reorder it (drag on the Plan)."""
    from boss_engine.services.project_experience import plan

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        try:
            return plan.move(connection, project_id, item_id, lane=payload.lane,
                             before=payload.before, actor=_actor(request))
        except LookupError as error:
            raise _missing(error) from None
        except ValueError as error:
            raise HTTPException(status_code=422, detail={"error": "bad_lane",
                                                         "message": str(error)}) from None


class LeasedJobBody(BaseModel):
    job_id: str
    worker_id: str
    lease_token: str


@router.post("/v1/projects/onboarding/execute")
@permit(P.WORKER_LEASE)
def project_onboarding_execute(body: LeasedJobBody) -> dict[str, Any]:
    """The worker hands its leased `project.onboarding` job here: GitHub's credentials, the
    code graph and the dossier are in this container. Fenced by the lease; no model call."""
    from apps.api.app.main import require_worker_principal
    from boss_engine.services.project_experience import onboarding

    require_worker_principal()
    return onboarding.execute_job(_engine, _settings, job_id=body.job_id,
                                  worker_id=body.worker_id, lease_token=body.lease_token)


# ── §9 jump ─────────────────────────────────────────────────────────────────────────────────

@router.get("/v1/jump")
@authenticated(note="Searches only the projects visible_project_rows says the caller may read; a "
                    "project_id outside them answers 404.")
def jump(request: Request, q: str = "", project_id: Optional[str] = None) -> dict[str, Any]:
    from apps.api.app.main import visible_project_rows
    from boss_engine.services.project_experience import jump as jump_service

    visible = [str(r["id"]) for r in visible_project_rows(request)]
    if project_id and project_id not in visible:
        raise HTTPException(status_code=404, detail=f"There is no project {project_id}.")
    with begin(_engine) as connection:
        return {"q": q, "project_id": project_id,
                "items": jump_service.search(connection, q=q, project_ids=visible,
                                             project_id=project_id)}


# ── §11 settings and demo logins ────────────────────────────────────────────────────────────

@router.get("/v1/projects/{project_id}/settings")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def project_settings_view(project_id: str) -> dict[str, Any]:
    from boss_engine.services.project_experience import settings_view

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        return settings_view.view(connection, project_id)


@router.get("/v1/projects/{project_id}/demo-logins")
@permit(P.PROJECT_READ, scope=Scope.project(path="project_id"))
def project_demo_logins(project_id: str) -> dict[str, Any]:
    """Who to sign in as, per environment — names only; the password is behind the reveal."""
    from boss_engine.services.project_experience import demo_logins

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        return demo_logins.listing(connection, project_id)


@router.get("/v1/projects/{project_id}/demo-logins/reveal")
@permit(P.SECRET_MANAGE, scope=Scope.project(path="project_id"))
def project_demo_logins_reveal(project_id: str, request: Request,
                               env: str = Query(..., description="preview, staging, …")) -> Any:
    """The demo users' passwords for one environment: owner/admin with a fresh step-up (the
    permission's own rule), only values BOSS generated, never cached, and audited."""
    from apps.api.app.routers.identity_access import request_identity
    from boss_engine.services.project_experience import demo_logins

    ctx = request_identity(request)
    with begin(_engine) as connection:
        _require_project(connection, project_id)
        _legacy_bar(connection, project_id, "member_manage")
        org_id = ctx.principal.org_id if ctx is not None else connection.execute(
            _org_sql(), {"p": project_id}).scalar()
        try:
            out = demo_logins.reveal(connection, _settings, org_id=str(org_id),
                                     project_id=project_id, env=env)
        except LookupError as error:
            return JSONResponse({"error": "no_demo_login", "message": str(error)},
                                status_code=404, headers={"Cache-Control": "no-store"})
        demo_logins.record_reveal(connection, project_id, _actor(request), out["env"],
                                  len(out["logins"]))
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


@router.post("/v1/projects/{project_id}/demo-logins/rotate")
@permit(P.SECRET_MANAGE, scope=Scope.project(path="project_id"))
def project_demo_logins_rotate(project_id: str, request: Request,
                               env: str = Query(..., description="preview, staging, …")) -> Any:
    """Settings › Demo logins › Rotate (13-06): a new generated password for one environment.
    The same bar as the reveal (owner/admin, fresh step-up); audited; no value is returned."""
    from apps.api.app.routers.identity_access import request_identity
    from boss_engine.services.project_experience import settings_view

    ctx = request_identity(request)
    with begin(_engine) as connection:
        _require_project(connection, project_id)
        _legacy_bar(connection, project_id, "member_manage")
        org_id = ctx.principal.org_id if ctx is not None else connection.execute(
            _org_sql(), {"p": project_id}).scalar()
        try:
            out = settings_view.rotate_demo_logins(connection, _settings, org_id=str(org_id),
                                                   project_id=project_id, env=env,
                                                   actor=_actor(request))
        except LookupError as error:
            return JSONResponse({"error": "no_demo_login", "message": str(error)}, status_code=404)
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


class ProjectLevelBody(BaseModel):
    level: str = Field(..., description="A0, A1, A2 or A3")


@router.put("/v1/projects/{project_id}/autonomy")
@permit(P.AUTONOMY_MANAGE, scope=Scope.project(path="project_id"))
def project_autonomy_set(project_id: str, payload: ProjectLevelBody, request: Request) -> Any:
    """Settings › Autonomy & budget › Level (13-20): this project's level for every task class
    (each within what its catalogue allows), recorded like any level change."""
    from boss_engine.services.project_experience import settings_view

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        _legacy_bar(connection, project_id, "role_review")
        try:
            return settings_view.set_project_level(connection, project_id,
                                                   payload.level.strip().upper(),
                                                   actor=_actor(request))
        except ValueError as error:
            return JSONResponse({"error": "bad_level", "message": str(error)}, status_code=422)


class RouteBody(BaseModel):
    contact_point_ids: list[str] = Field(default_factory=list)


@router.put("/v1/projects/{project_id}/notification-routes/{event}")
@permit(P.OBSERVABILITY_MANAGE, scope=Scope.project(path="project_id"))
def project_notification_route_set(project_id: str, event: str, payload: RouteBody,
                                   request: Request) -> Any:
    """Settings › Notifications (13-26…13-28): route Needs you, Incidents or PR ready to this
    project's contact points."""
    from boss_engine.services.project_experience import notify_routes

    with begin(_engine) as connection:
        _require_project(connection, project_id)
        _legacy_bar(connection, project_id, "providerops_manage")
        try:
            return notify_routes.set_route(connection, project_id, event,
                                           payload.contact_point_ids, actor=_actor(request))
        except ValueError as error:
            return JSONResponse({"error": "bad_event", "message": str(error)}, status_code=422)
        except LookupError as error:
            return JSONResponse({"error": "unknown_contact_point", "message": str(error)},
                                status_code=422)


def _org_sql():
    from sqlalchemy import text

    return text("SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                "WHERE p.id = :p")
