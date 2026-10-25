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
    BudgetBody, ChecksBody, JobBody, ModelConnectBody, ReleaseHoldBody, RepositoryAppBody, RepositorySelectBody, RepositorySigninBody,
    RepositoryTokenBody, RoutingBody, SetupView, SignInBody, SignInCodeBody, SignInSession,
    SourceModelBody, TryBody, dump,
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

    def _signed_in_person() -> Optional[str]:
        """The PERSON behind this request (the identity context), whatever legacy member name the
        shadow-mode checks see. A repository picked after "Sign in with GitHub" uses their token."""
        try:
            from boss_engine.identity import lineage

            ctx = lineage.current_context()
        except Exception:  # noqa: BLE001
            return None
        pid = getattr(getattr(ctx, "principal", None), "id", None)
        return str(pid) if pid else None

    def view(connection, project_id: str) -> dict[str, Any]:
        return dump(setup_view.build(connection, get_settings(), project_id))

    def fail(error: setup_actions.SetupActionError):
        raise HTTPException(status_code=error.status, detail=error.detail) from error

    def models_target(connection, project_id: str, *, scope: Optional[str] = None,
                      source_id: str = "", actor: str = "setup") -> str:
        """Where a model connection write lands: the org's model home (every project of the
        org then holds a mirror, org_models.sync_sources), or this project with scope='project'
        or for a connection this project made itself."""
        from boss_engine.services import org_models

        try:
            return org_models.write_target(connection, project_id, scope=scope,
                                           source_id=source_id, actor=actor)
        except org_models.OrgModelsForbidden as error:
            raise HTTPException(status_code=403, detail={
                "state": "failed", "headline": "Org admin", "fix": str(error)}) from error

    def mirror(project_id: str) -> None:
        from boss_engine.services import org_models

        with begin(engine) as connection:
            org_models.sync_for(connection, project_id)

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

    @router.post("/v1/setup/repository/signin")
    @permit(P.GITHUB_CONNECT, scope=Scope.project(body="project_id"))
    def post_repository_signin(body: RepositorySigninBody) -> dict[str, Any]:
        """After "Sign in with GitHub": list what THIS person can push to where BOSS is installed,
        and keep one project connection that uses their own sign-in (never the org's token)."""
        actor = actor_for(body.actor)
        person = _signed_in_person() or str(principal() or "")
        with begin(engine) as connection:
            require_actor(connection, body.project_id, actor, "github_manage")
            try:
                return setup_actions.repository_from_signin(connection, get_settings(), body.project_id,
                                                            person_id=person, actor=actor)
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
            target = models_target(connection, body.project_id, scope=body.scope,
                                   source_id=body.source_id or "", actor=actor)
        with begin(engine) as connection:
            try:
                setup_actions.connect_model(
                    connection, get_settings(), target, provider=body.provider,
                    kind=body.kind, api_key=body.api_key or "",
                    credential=body.credential or "",
                    credential_file=body.credential_file or "",
                    base_url=body.base_url or "", model=body.model or "",
                    accept_database_fallback=body.accept_database_fallback, actor=actor,
                    replace_source_id=body.source_id or "",
                    intent=body.idempotency_key or "")
            except setup_actions.SetupActionError as error:
                fail(error)
        mirror(target)
        return fresh_view(body.project_id)

    # ── Subscription sign-in inside the BOSS server (subscription_sign_in) ────────────────────
    # The ONE subscription route: the provider's own login runs here as the API process user;
    # the person opens the printed link and enters the printed code. Connect (POST
    # /v1/setup/models, kind subscription, no credential) then proves it. Same permission as
    # that connect: signing a subscription in changes what this install's models can run on.

    def sign_in_failed(error) -> None:
        raise HTTPException(status_code=error.status, detail=error.detail) from error

    @router.post("/v1/setup/sign-in", response_model=SignInSession)
    @permit(P.MODEL_PROVIDER_MANAGE, scope=Scope.project(body="project_id"))
    def post_sign_in(body: SignInBody) -> dict[str, Any]:
        """Start the provider's own login (codex | claude_code | grok) inside BOSS, or return
        the live one. Already signed in answers `signed_in` at once."""
        from boss_engine.services import subscription_sign_in as sign_in

        actor = actor_for(body.actor)
        try:
            provider = sign_in.normalize_provider(body.provider)
        except sign_in.SignInError as error:
            sign_in_failed(error)
        with begin(engine) as connection:
            require_actor(connection, body.project_id, actor, "entitlement_manage")
            # The org's model home holds the connection (read only: never sets a home).
            try:
                from boss_engine.services import org_models

                home = org_models.home_project(connection,
                                               org_models.org_of(connection, body.project_id))
            except Exception:  # noqa: BLE001
                home = None
            rejected = sign_in.last_login_rejected(connection, [body.project_id, home or ""],
                                                   provider, get_settings())
        try:
            return sign_in.start(provider, project_id=body.project_id, actor=actor,
                                 settings=get_settings(), login_rejected=rejected,
                                 fresh=bool(body.fresh))
        except sign_in.SignInError as error:
            sign_in_failed(error)
        raise AssertionError("unreachable")

    @router.get("/v1/setup/sign-in/{sign_in_id}", response_model=SignInSession)
    @permit(P.MODEL_PROVIDER_MANAGE, scope=Scope.project(query="project_id"))
    def get_sign_in(sign_in_id: str, project_id: str) -> dict[str, Any]:
        from boss_engine.services import subscription_sign_in as sign_in

        with begin(engine) as connection:
            require_actor(connection, project_id, actor_for(None), "entitlement_manage")
        try:
            return sign_in.status(sign_in_id)
        except sign_in.SignInError as error:
            sign_in_failed(error)
        raise AssertionError("unreachable")

    @router.post("/v1/setup/sign-in/{sign_in_id}/code", response_model=SignInSession)
    @permit(P.MODEL_PROVIDER_MANAGE, scope=Scope.project(body="project_id"))
    def post_sign_in_code(sign_in_id: str, body: SignInCodeBody) -> dict[str, Any]:
        """The code the sign-in page showed (Claude), handed to the provider's own login."""
        from boss_engine.services import subscription_sign_in as sign_in

        with begin(engine) as connection:
            require_actor(connection, body.project_id, actor_for(body.actor),
                          "entitlement_manage")
        try:
            return sign_in.submit_code(sign_in_id, body.code)
        except sign_in.SignInError as error:
            sign_in_failed(error)
        raise AssertionError("unreachable")

    @router.delete("/v1/setup/sign-in/{sign_in_id}", response_model=SignInSession)
    @permit(P.MODEL_PROVIDER_MANAGE, scope=Scope.project(query="project_id"))
    def delete_sign_in(sign_in_id: str, project_id: str,
                       actor: Optional[str] = None) -> dict[str, Any]:
        from boss_engine.services import subscription_sign_in as sign_in

        with begin(engine) as connection:
            require_actor(connection, project_id, actor_for(actor), "entitlement_manage")
        try:
            return sign_in.cancel(sign_in_id)
        except sign_in.SignInError as error:
            sign_in_failed(error)
        raise AssertionError("unreachable")

    @router.post("/v1/setup/holds/{receipt_id}/release", response_model=SetupView,
                 response_model_by_alias=True)
    @permit(P.ENTITLEMENT_MANAGE, scope=Scope.project(body="project_id"))
    def post_release_hold(receipt_id: str, body: ReleaseHoldBody) -> dict[str, Any]:
        """A person releases a held connection proof: settled (0 if the provider refused, else
        the shown estimate), its card closed, recorded with who and why."""
        from boss_engine.services.connection_spend import HoldRefused, release_hold

        actor = actor_for(body.actor)
        with begin(engine) as connection:
            require_actor(connection, body.project_id, actor, "entitlement_manage")
            try:
                release_hold(connection, get_settings(), body.project_id, receipt_id,
                             actor=actor, reason=body.reason)
            except HoldRefused as error:
                raise HTTPException(status_code=error.status, detail={
                    "state": "failed", "headline": "Hold not released", "fix": str(error),
                }) from error
        return fresh_view(body.project_id)

    @router.get("/v1/setup/holds/{receipt_id}")
    @permit(P.SETUP_READ, scope=Scope.project(query="project_id"))
    def get_hold(receipt_id: str, project_id: str) -> dict[str, Any]:
        """Review: the facts of one connection proof's receipt and what releasing it would do."""
        from boss_engine.services.connection_spend import HoldRefused, review_hold

        with begin(engine) as connection:
            require_actor(connection, project_id, actor_for(None), "entitlement_manage")
            try:
                return review_hold(connection, project_id, receipt_id)
            except HoldRefused as error:
                raise HTTPException(status_code=error.status, detail={
                    "state": "failed", "headline": "No such proof", "fix": str(error),
                }) from error

    @router.post("/v1/setup/models/{source_id}/model", response_model=SetupView,
                 response_model_by_alias=True)
    @permit(P.MODEL_PROVIDER_MANAGE, scope=Scope.project(body="project_id"))
    def post_source_model(source_id: str, body: SourceModelBody) -> dict[str, Any]:
        actor = actor_for(body.actor)
        with begin(engine) as connection:
            require_actor(connection, body.project_id, actor, "entitlement_manage")
            target = models_target(connection, body.project_id, source_id=source_id, actor=actor)
            try:
                setup_actions.set_source_model(connection, get_settings(), target,
                                               source_id, body.model, actor=actor)
            except setup_actions.SetupActionError as error:
                fail(error)
        mirror(target)
        return fresh_view(body.project_id)

    @router.delete("/v1/setup/models/{source_id}", response_model=SetupView,
                   response_model_by_alias=True)
    @permit(P.MODEL_PROVIDER_MANAGE, scope=Scope.project(query="project_id"))
    def delete_model(source_id: str, project_id: str, confirm: bool = False,
                     actor: Optional[str] = None) -> dict[str, Any]:
        who = actor_for(actor)
        with begin(engine) as connection:
            require_actor(connection, project_id, who, "entitlement_manage")
            target = models_target(connection, project_id, source_id=source_id, actor=who)
            try:
                setup_actions.remove_model(connection, get_settings(), target, source_id,
                                           confirm=confirm, actor=who)
            except setup_actions.SetupActionError as error:
                fail(error)
        mirror(target)
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
                                      actor=actor, scope=body.scope)
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
