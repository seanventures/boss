"""Repo and models: one view, one checks endpoint, one connect per object.

`GET /v1/setup` renders `setup_view.build` — no network, no decryption. Every mutation returns
the same view, so the console replaces its state instead of judging anything itself. Probes and
credential writes live in `setup_actions`; this module is only the HTTP edge.

Mounted from main.py with the auth helpers injected, so this module does not import main.
"""
from __future__ import annotations

from boss_engine.identity import P, PublicReason, Scope, authenticated, permit, public  # noqa: E402,F401  (identity S2)
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Request

from boss_engine.db import begin

from ..schemas_setup import (
    BudgetBody, ChecksBody, JobBody, ModelConnectBody, RepositoryAppBody, RepositorySelectBody,
    RepositoryTokenBody, RoutingBody, SetupView, SourceModelBody, TryBody, dump,
)


def build_setup_router(
    *,
    engine,
    get_settings: Callable[[], Any],
    require_actor: Callable[..., None],
    principal: Callable[[], Optional[str]],
    require_reader: Optional[Callable[..., None]] = None,
) -> APIRouter:
    from boss_engine.services import setup_actions, setup_view

    router = APIRouter(tags=["setup"])

    def actor_for(claimed: Optional[str]) -> str:
        return str(principal() or claimed or "ceo").strip() or "ceo"

    def view(connection, project_id: str) -> dict[str, Any]:
        return dump(setup_view.build(connection, get_settings(), project_id))

    def fail(error: setup_actions.SetupActionError):
        raise HTTPException(status_code=error.status, detail=error.detail) from error

    def fresh_view(project_id: str) -> dict[str, Any]:
        with begin(engine) as connection:
            return view(connection, project_id)

    @router.get("/v1/setup", response_model=SetupView, response_model_by_alias=True)
    @permit(P.SETUP_READ, scope=Scope.project(query="project_id"))
    def get_setup(project_id: str) -> dict[str, Any]:
        """Repo and models, decided once on the server. Never touches the network."""
        with begin(engine) as connection:
            require_actor(connection, project_id, actor_for(None), "entitlement_manage")
            return view(connection, project_id)

    @router.post("/v1/setup/checks", response_model=SetupView, response_model_by_alias=True)
    @permit(P.SETUP_MANAGE, scope=Scope.project(body="project_id"))
    def post_checks(body: ChecksBody) -> dict[str, Any]:
        """Prove things now and record when: GitHub's three probes, one question per model."""
        actor = actor_for(body.actor)
        with begin(engine) as connection:
            require_actor(connection, body.project_id, actor, "entitlement_manage")
            if setup_actions.first_press(body.project_id, body.idempotency_key):
                try:
                    setup_actions.run_checks(connection, get_settings(), body.project_id,
                                             body.target, actor=actor)
                except setup_actions.SetupActionError as error:
                    fail(error)
        return fresh_view(body.project_id)

    @router.post("/v1/setup/repository/token")
    @permit(P.GITHUB_CONNECT, scope=Scope.project(body="project_id"))
    def post_repository_token(body: RepositoryTokenBody) -> dict[str, Any]:
        """Check a pasted GitHub token, keep it, and list the repositories it can see."""
        actor = actor_for(body.actor)
        with begin(engine) as connection:
            require_actor(connection, body.project_id, actor, "github_manage")
            try:
                return setup_actions.repository_token(
                    connection, get_settings(), body.project_id, body.token,
                    connection_id=body.connection_id,
                    accept_database_fallback=body.accept_database_fallback, actor=actor)
            except setup_actions.SetupActionError as error:
                fail(error)
        raise AssertionError("unreachable")

    @router.post("/v1/setup/repository/app")
    @permit(P.GITHUB_CONNECT, scope=Scope.project(body="project_id"))
    def post_repository_app(body: RepositoryAppBody) -> dict[str, Any]:
        """Use the GitHub App connected in GitHub access: list what it can reach (no token)."""
        actor = actor_for(body.actor)
        with begin(engine) as connection:
            require_actor(connection, body.project_id, actor, "github_manage")
            try:
                return setup_actions.repository_from_app(connection, get_settings(), body.project_id,
                                                         actor=actor)
            except setup_actions.SetupActionError as error:
                fail(error)
        raise AssertionError("unreachable")

    @router.get("/v1/setup/repository/repos")
    @permit(P.GITHUB_READ, scope=Scope.project(query="project_id"))
    def get_repository_repos(project_id: str, connection_id: str) -> dict[str, Any]:
        """`+ Add repo`: what the stored token can see. Asks GitHub; the view never does."""
        with begin(engine) as connection:
            require_actor(connection, project_id, actor_for(None), "github_manage")
            try:
                return setup_actions.connection_repos(connection, get_settings(), project_id,
                                                      connection_id)
            except setup_actions.SetupActionError as error:
                fail(error)
        raise AssertionError("unreachable")

    @router.post("/v1/setup/repository/select", response_model=SetupView,
                 response_model_by_alias=True)
    @permit(P.REPOSITORY_REGISTER, scope=Scope.project(body="project_id"))
    def post_repository_select(body: RepositorySelectBody) -> dict[str, Any]:
        """Bind a repository from the picker, then run its three checks."""
        actor = actor_for(body.actor)
        with begin(engine) as connection:
            require_actor(connection, body.project_id, actor, "github_manage")
            try:
                setup_actions.select_repository(
                    connection, get_settings(), body.project_id, body.connection_id,
                    body.full_name, make_default=body.make_default, actor=actor)
            except setup_actions.SetupActionError as error:
                fail(error)
            except ValueError as error:
                raise HTTPException(status_code=422, detail={
                    "state": "no_access", "headline": "No access", "fix": str(error)[:200],
                }) from error
        return fresh_view(body.project_id)

    @router.post("/v1/setup/repository/{binding_id}/default", response_model=SetupView,
                 response_model_by_alias=True)
    @permit(P.REPOSITORY_MANAGE, scope=Scope.project(query="project_id"))
    def post_repository_default(binding_id: str, project_id: str,
                                actor: Optional[str] = None) -> dict[str, Any]:
        who = actor_for(actor)
        with begin(engine) as connection:
            require_actor(connection, project_id, who, "github_manage")
            setup_actions.make_default(connection, project_id, binding_id, actor=who)
        return fresh_view(project_id)

    @router.delete("/v1/setup/repository/{binding_id}", response_model=SetupView,
                   response_model_by_alias=True)
    @permit(P.REPOSITORY_MANAGE, scope=Scope.project(query="project_id"))
    def delete_repository(binding_id: str, project_id: str,
                          actor: Optional[str] = None) -> dict[str, Any]:
        who = actor_for(actor)
        with begin(engine) as connection:
            require_actor(connection, project_id, who, "github_manage")
            try:
                setup_actions.remove_repository(connection, project_id, binding_id, actor=who)
            except setup_actions.SetupActionError as error:
                fail(error)
        return fresh_view(project_id)

    @router.post("/v1/setup/models", response_model=SetupView, response_model_by_alias=True)
    @permit(P.MODEL_PROVIDER_MANAGE, scope=Scope.project(body="project_id"))
    def post_models(body: ModelConnectBody) -> dict[str, Any]:
        """Store a model credential and prove it: one budgeted, receipted question."""
        actor = actor_for(body.actor)
        # The committed proof receipt decides replay across processes/restarts.
        # A short in-process cache must not hide a refused or changed request.
        with begin(engine) as connection:
            require_actor(connection, body.project_id, actor, "entitlement_manage")
            try:
                setup_actions.connect_model(
                    connection, get_settings(), body.project_id, provider=body.provider,
                    kind=body.kind, api_key=body.api_key or "",
                    credential=body.credential or "",
                    credential_file=body.credential_file or "",
                    base_url=body.base_url or "", model=body.model or "",
                    accept_database_fallback=body.accept_database_fallback, actor=actor,
                    replace_source_id=body.source_id or "",
                    intent=body.idempotency_key or "")
            except setup_actions.SetupActionError as error:
                fail(error)
        return fresh_view(body.project_id)

    @router.post("/v1/setup/models/{source_id}/model", response_model=SetupView,
                 response_model_by_alias=True)
    @permit(P.MODEL_PROVIDER_MANAGE, scope=Scope.project(body="project_id"))
    def post_source_model(source_id: str, body: SourceModelBody) -> dict[str, Any]:
        actor = actor_for(body.actor)
        with begin(engine) as connection:
            require_actor(connection, body.project_id, actor, "entitlement_manage")
            try:
                setup_actions.set_source_model(connection, get_settings(), body.project_id,
                                               source_id, body.model, actor=actor)
            except setup_actions.SetupActionError as error:
                fail(error)
        return fresh_view(body.project_id)

    @router.delete("/v1/setup/models/{source_id}", response_model=SetupView,
                   response_model_by_alias=True)
    @permit(P.MODEL_PROVIDER_MANAGE, scope=Scope.project(query="project_id"))
    def delete_model(source_id: str, project_id: str, confirm: bool = False,
                     actor: Optional[str] = None) -> dict[str, Any]:
        who = actor_for(actor)
        with begin(engine) as connection:
            require_actor(connection, project_id, who, "entitlement_manage")
            try:
                setup_actions.remove_model(connection, get_settings(), project_id, source_id,
                                           confirm=confirm, actor=who)
            except setup_actions.SetupActionError as error:
                fail(error)
        return fresh_view(project_id)

    @router.get("/v1/setup/models/{provider}/catalogue")
    @permit(P.SETUP_READ, scope=Scope.project(query="project_id"))
    def get_model_catalogue(provider: str, project_id: str, source_id: str = "",
                            base_url: str = "") -> dict[str, Any]:
        """The provider's own model list, with the key this project stored for it."""
        with begin(engine) as connection:
            require_actor(connection, project_id, actor_for(None), "entitlement_manage")
            names = setup_actions.model_catalogue(connection, get_settings(), project_id,
                                                  provider, source_id=source_id,
                                                  base_url=base_url)
        return {"models": names}

    @router.post("/v1/setup/models/{provider}/catalogue")
    @permit(P.MODEL_PROVIDER_MANAGE, scope=Scope.project(body="project_id"))
    def post_model_catalogue(provider: str, body: ModelConnectBody) -> dict[str, Any]:
        """The same list for a key that was just pasted and is not stored yet. A POST, so the
        key never travels in a URL."""
        with begin(engine) as connection:
            require_actor(connection, body.project_id, actor_for(body.actor),
                          "entitlement_manage")
            names = setup_actions.model_catalogue(
                connection, get_settings(), body.project_id, provider,
                api_key=body.api_key or "", base_url=body.base_url or "")
        return {"models": names}

    @router.put("/v1/setup/jobs/{job}", response_model=SetupView, response_model_by_alias=True)
    @permit(P.SETUP_MANAGE, scope=Scope.project(body="project_id"))
    def put_job(job: str, body: JobBody) -> dict[str, Any]:
        """Engine and model for one job, written together — or back to Auto."""
        actor = actor_for(body.actor)
        with begin(engine) as connection:
            require_actor(connection, body.project_id, actor, "providerops_manage")
            try:
                setup_actions.set_job(connection, get_settings(), body.project_id, job,
                                      body.choice.model_dump() if body.choice else None,
                                      actor=actor)
            except setup_actions.SetupActionError as error:
                fail(error)
        return fresh_view(body.project_id)

    @router.put("/v1/setup/routing", response_model=SetupView, response_model_by_alias=True)
    @permit(P.SETUP_MANAGE, scope=Scope.project(body="project_id"))
    def put_routing(body: RoutingBody) -> dict[str, Any]:
        """Model routing (docs/product/MODEL_ROUTER.md): merge a change into the project's
        policy. A stale edit (another save since `version`) is refused with 409; a policy the
        router cannot route on is refused with 422 naming the field."""
        from boss_engine.services.model_router import store as router_store
        from boss_engine.services.model_router.policy import PolicyError

        actor = actor_for(body.actor)
        with begin(engine) as connection:
            require_actor(connection, body.project_id, actor, "providerops_manage")
            try:
                router_store.save_policy(connection, body.project_id, body.policy, actor=actor,
                                         settings=get_settings(),
                                         expected_version=body.version)
            except router_store.PolicyConflict as error:
                raise HTTPException(status_code=409, detail={
                    "headline": "Changed elsewhere", "message": str(error),
                    "version": error.current}) from error
            except (PolicyError, ValueError) as error:
                raise HTTPException(status_code=422, detail={
                    "headline": "Not saved", "message": str(error)}) from error
        return fresh_view(body.project_id)

    @router.post("/v1/setup/routing/catalogue", response_model=SetupView,
                 response_model_by_alias=True)
    @permit(P.MODEL_PROVIDER_MANAGE, scope=Scope.project(body="project_id"))
    def post_routing_catalogue(body: ChecksBody) -> dict[str, Any]:
        """Refresh OpenRouter's model list and prices with the project's stored key."""
        from boss_engine.services import openrouter_catalog

        actor = actor_for(body.actor)
        with begin(engine) as connection:
            require_actor(connection, body.project_id, actor, "entitlement_manage")
            openrouter_catalog.refresh(connection, get_settings(), body.project_id)
        from boss_engine.services.model_router import store as router_store

        router_store.invalidate(body.project_id)
        return fresh_view(body.project_id)

    @router.get("/v1/model-routes")
    @permit(P.MODEL_PROVIDER_READ, scope=Scope.project(query="project_id"))
    def get_model_routes(project_id: str, unit_id: str) -> dict[str, Any]:
        """Why this model: every route the router chose for one run, thread or meeting."""
        from boss_engine.services.model_router.store import routes_for

        with begin(engine) as connection:
            if require_reader is not None:
                require_reader(connection, project_id)
            else:
                require_actor(connection, project_id, actor_for(None), "entitlement_manage")
            rows = [r for r in routes_for(connection, unit_id) if r.get("project_id") in
                    (None, project_id)]
        return {"unit_id": unit_id, "routes": rows}

    @router.put("/v1/setup/budget", response_model=SetupView, response_model_by_alias=True)
    @permit(P.BUDGET_MANAGE, scope=Scope.project(body="project_id"))
    def put_budget(body: BudgetBody, request: Request) -> dict[str, Any]:
        """The monthly cap — the same setting Govern edits, through this same write. Govern
        sends `via: govern` and an `If-Match`; a stale edit is refused with 412."""
        from boss_engine.services.budget import policy_version

        from ..govern_guard import govern_actor, require_fresh

        # The monthly cap is a Govern write wherever it is made: the signed-in actor wins.
        actor = govern_actor(body.actor, principal())
        with begin(engine) as connection:
            require_actor(connection, body.project_id, actor, "budget_manage")
            require_fresh(request, *policy_version(connection, body.project_id))
            try:
                setup_actions.set_budget(connection, get_settings(), body.project_id,
                                         body.monthly_cap_usd, actor=actor,
                                         via=body.via or "repo_and_models")
            except setup_actions.SetupActionError as error:
                fail(error)
        return fresh_view(body.project_id)

    @router.post("/v1/setup/try", response_model=SetupView, response_model_by_alias=True)
    @permit(P.WORKFLOW_RUN_START, scope=Scope.project(body="project_id"))
    def post_try(body: TryBody) -> dict[str, Any]:
        """Try it: one sentence becomes a governed build on the default repository and a PR."""
        actor = actor_for(body.actor)
        if setup_actions.first_press(body.project_id, body.idempotency_key):
            with begin(engine) as connection:
                require_actor(connection, body.project_id, actor, "workflow_execute")
                try:
                    setup_actions.start_try(connection, get_settings(), body.project_id,
                                            body.directive, actor=actor,
                                            idempotency_key=body.idempotency_key or "")
                except setup_actions.SetupActionError as error:
                    fail(error)
        return fresh_view(body.project_id)

    return router
