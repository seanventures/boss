"""LOCAL-FIRST PROJECTS over HTTP (BOSSAPP-915: 918-924). Engine: services/local_repo.py (git),
local_repo_host.py (the adapter + change sets), local_projects.py (binding, import, checks),
local_publish.py (publish), github_outage.py (push pending).

GET  /v1/projects/{id}/local-repo                    the project's local repository, if any
POST /v1/projects/{id}/local-repo                    code location "local" for an existing project
POST /v1/projects/{id}/local-repo/checks             the onboarding agent's machine checks (+ fixes)
POST /v1/projects/{id}/local-repo/import             import code: a folder here, or on a host
GET  /v1/projects/{id}/local-changes                 local change sets (a local repo's PRs)
GET  /v1/projects/{id}/local-changes/{n}             one: branch, diff, files, commits, review
                                                     comments, sandbox checks, its decision
POST /v1/projects/{id}/local-changes/{n}/merge       Merge into the default branch (a person's
                                                     approval of the change's review decision)
POST /v1/projects/{id}/publish                       Publish to GitHub (a person's decision)
POST /v1/projects/{id}/github-repository             make an empty GitHub repository (BOSSAPP-917)
GET  /v1/projects/{id}/github-repository/check       can BOSS make it (or publish into it)? reads only
GET  /v1/projects/{id}/local-folders                 the import directory's folders, with their facts
GET  /v1/projects/{id}/push-pending                  work waiting for GitHub, and why
POST /v1/repos/push-pending/execute                  the worker's leased push-pending job
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit

router = APIRouter(tags=["projects"])
_settings = get_settings()
_engine = get_engine(_settings)


def _require(connection, project_id: str) -> None:
    from boss_engine.services.project_experience.common import require_project

    try:
        require_project(connection, project_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from None


def _actor(request: Request) -> str:
    from apps.api.app import identity_guard

    ctx = identity_guard.request_auth(request)
    return identity_guard.actor_for(ctx) if ctx is not None else "operator"


def _people_only(request: Request, what: str) -> None:
    """A person's decision: an agent's or a service's credential is refused, in words."""
    from apps.api.app import identity_guard

    ctx = identity_guard.request_auth(request)
    if ctx is None:
        return
    from boss_engine.identity.legacy import is_bridge_principal

    if not getattr(ctx, "is_human_session", False) or is_bridge_principal(ctx.principal.id):
        raise HTTPException(status_code=403, detail={
            "error": "people_only", "message": f"{what} is a person's decision: sign in and do it "
                                               "from the console."})


def _refused(error: Exception) -> JSONResponse:
    body = error.body() if hasattr(error, "body") else {"error": "refused", "message": str(error)}
    return JSONResponse(body, status_code=int(getattr(error, "status", 422) or 422))


def _local_binding(connection, project_id: str, *, include_mirror: bool = True) -> dict[str, Any]:
    from boss_engine.services.local_projects import local_binding

    binding = local_binding(connection, project_id, include_mirror=include_mirror)
    if binding is None:
        raise HTTPException(status_code=404, detail={
            "error": "not_local", "message": "This project's code is not kept locally."})
    return binding


# ── the local repository ────────────────────────────────────────────────────────────────────

class LocalRepoBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(default=None, max_length=80,
                                description="The repository's name (default: the project's).")


@router.get("/v1/projects/{project_id}/local-repo")
@permit(P.REPOSITORY_READ, scope=Scope.project(path="project_id"))
def local_repo_status(project_id: str) -> dict[str, Any]:
    from boss_engine.services import local_projects

    with begin(_engine) as connection:
        _require(connection, project_id)
        return local_projects.status(connection, _settings, project_id)


@router.post("/v1/projects/{project_id}/local-repo")
@permit(P.REPOSITORY_MANAGE, scope=Scope.project(path="project_id"))
def local_repo_create(project_id: str, request: Request, payload: LocalRepoBody) -> Any:
    """Keep this project's code locally: a bare repository with a first commit, bound as the
    project's primary repository (when it has none) — no GitHub needed."""
    from boss_engine.services import local_projects

    with begin(_engine) as connection:
        _require(connection, project_id)
        project = connection.execute(text("SELECT name, description FROM projects WHERE id = :p"),
                                     {"p": project_id}).mappings().first()
        try:
            return local_projects.create_for_project(
                connection, _settings, project_id, name=payload.name or str(project["name"]),
                actor=_actor(request), title=str(project["name"]),
                description=str(project.get("description") or ""))
        except local_projects.LocalProjectError as error:
            return _refused(error)


class ChecksBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fix: bool = True


@router.post("/v1/projects/{project_id}/local-repo/checks")
@permit(P.WORKFLOW_RUN_START, scope=Scope.project(path="project_id"))
def local_repo_checks(project_id: str, request: Request, payload: ChecksBody) -> dict[str, Any]:
    """The onboarding agent's first stage for a local project: git, the repository, disk, the
    network and the model, fixing what it can (the repository, its first commit)."""
    from boss_engine.services import local_projects

    with begin(_engine) as connection:
        _require(connection, project_id)
        return local_projects.machine_checks(connection, _settings, project_id, fix=payload.fix,
                                             actor=_actor(request))


class ImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(..., max_length=500, description="A folder in the import directory (this "
                                                       "machine) or an absolute path on host_id.")
    host_id: Optional[str] = Field(default=None, max_length=120,
                                   description="A verified server: the folder is read over ssh.")
    name: Optional[str] = Field(default=None, max_length=80)
    keep_history: bool = True


@router.post("/v1/projects/{project_id}/local-repo/import")
@permit(P.REPOSITORY_MANAGE, scope=Scope.project(path="project_id"))
def local_repo_import(project_id: str, request: Request, payload: ImportBody) -> Any:
    from boss_engine.services import local_projects

    with begin(_engine) as connection:
        _require(connection, project_id)
        name = payload.name or payload.path.rstrip("/").rsplit("/", 1)[-1] or project_id
        try:
            return local_projects.import_code(
                connection, _settings, project_id, name=name, actor=_actor(request),
                path=payload.path, host_id=payload.host_id, keep_history=payload.keep_history)
        except local_projects.LocalProjectError as error:
            return _refused(error)


# ── local change sets ───────────────────────────────────────────────────────────────────────

def _adapter(binding: dict[str, Any], project_id: str):
    from boss_engine.services.local_repo_host import LocalGitAdapter

    return LocalGitAdapter(_settings, owner=binding["owner"], repo=binding["name"],
                           project_id=project_id, engine=_engine)


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _row_view(row: dict[str, Any]) -> dict[str, Any]:
    from boss_engine.services.local_repo_host import change_set_url

    return {"number": int(row["number"]), "url": change_set_url(row["repository"], row["number"]),
            "repository": row["repository"], "branch": row["branch"],
            "base_branch": row["base_branch"], "title": row["title"], "state": row["state"],
            "draft": bool(row["draft"]), "labels": row["labels"] or [], "run_id": row["run_id"],
            "head_sha": row["head_sha"], "merged_sha": row["merged_sha"],
            "merged_by": row["merged_by"], "merged_at": _iso(row["merged_at"]),
            "pending_push": bool(row["pending_push"]), "github_pr_url": row["github_pr_url"],
            "created_at": _iso(row["created_at"]), "updated_at": _iso(row["updated_at"])}


@router.get("/v1/projects/{project_id}/local-changes")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def local_changes(project_id: str, state: Optional[str] = None) -> dict[str, Any]:
    with begin(_engine) as connection:
        _require(connection, project_id)
        rows = connection.execute(text(
            "SELECT * FROM local_change_sets WHERE project_id = :p AND "
            "(CAST(:s AS TEXT) IS NULL OR state = :s) ORDER BY created_at DESC LIMIT 200"),
            {"p": project_id, "s": state}).mappings().all()
        return {"project_id": project_id, "items": [_row_view(dict(r)) for r in rows]}


def _decision_for(connection, url: str) -> Optional[dict[str, Any]]:
    row = connection.execute(text(
        "SELECT id, status, question FROM decisions WHERE recommendation->>'pull_request_url' = :u "
        "AND (recommendation->>'action') = 'review_pull_request' ORDER BY created_at DESC LIMIT 1"),
        {"u": url}).mappings().first()
    return dict(row) if row else None


def _change_row(connection, project_id: str, number: int) -> dict[str, Any]:
    row = connection.execute(text(
        "SELECT * FROM local_change_sets WHERE project_id = :p AND number = :n "
        "ORDER BY (state = 'open') DESC, created_at DESC LIMIT 1"),
        {"p": project_id, "n": int(number)}).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "no_change",
                                                     "message": f"There is no change #{number}."})
    return dict(row)


@router.get("/v1/projects/{project_id}/local-changes/{number}")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def local_change(project_id: str, number: int) -> dict[str, Any]:
    from boss_engine.services import local_repo
    from boss_engine.services.local_repo_host import LocalGitAdapter

    with begin(_engine) as connection:
        _require(connection, project_id)
        row = _change_row(connection, project_id, number)
        owner, _, name = row["repository"].partition("/")
        git_dir = local_repo.bare_path(owner, name, _settings)
        adapter = LocalGitAdapter(_settings, owner=owner, repo=name, project_id=project_id,
                                  git_dir=git_dir, engine=_engine)
        view = _row_view(row)
        view["body"] = row["body"]
        try:
            view["files"] = adapter.pull_request_files(number)
            view["diff"] = adapter.get_pull_request_diff(number)[:400_000]
            base = row["base_branch"] if row["state"] == "open" else (row["base_sha"] or row["base_branch"])
            # A merged or closed change set is what it was when it closed: its recorded head,
            # never its branch, which a later build on the same branch moves on.
            head = (row["branch"] if row["state"] == "open"
                    and local_repo.branch_exists(git_dir, row["branch"])
                    else row["head_sha"] or row["branch"])
            view["commits"] = local_repo.commits_between(git_dir, base, head)
        except Exception as error:  # noqa: BLE001 — the record still shows
            view.update({"files": [], "diff": "", "commits": [],
                         "diff_error": f"{type(error).__name__}: {str(error)[:200]}"})
        view["review"] = [{"id": n["number"], "kind": n["kind"], "event": n["event"],
                           "body": n["body"], "comments": n["comments"] or [],
                           "in_reply_to": n["in_reply_to"], "author": n["author"],
                           "created_at": _iso(n["created_at"])} for n in adapter.notes(number)]
        checks = None
        if row.get("run_id"):
            run = connection.execute(text(
                "SELECT status, phase, state->'gates' AS gates, state->>'tests_passed' AS tests "
                "FROM code_task_runs WHERE id = :r"), {"r": row["run_id"]}).mappings().first()
            if run:
                checks = {"where": "BOSS's sandbox", "run_status": run["status"],
                          "tests_passed": run["tests"], "gates": run["gates"]}
        view["checks"] = checks
        decision = _decision_for(connection, view["url"])
        view["decision"] = decision
        view["merge"] = ({"how": "approve the review decision", "decision_id": decision["id"]}
                         if decision and decision["status"] == "pending" and row["state"] == "open"
                         else None)
        if view["merge"] is not None:
            # HELD BY CI (BOSSAPP-946): the same answer the gate gives, before the press.
            from boss_engine.services import ci_gate

            view["merge"]["held"] = ci_gate.approval_hold(connection, project_id, {
                "action": "review_pull_request", "pull_request_url": view["url"],
                "head_sha": row.get("head_sha") or ""})
        return view


class MergeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: Optional[str] = Field(default=None, max_length=2000)
    force: bool = False


@router.post("/v1/projects/{project_id}/local-changes/{number}/merge")
@permit(P.DECISION_APPROVE, scope=Scope.project(path="project_id"))
def local_change_merge(project_id: str, number: int, request: Request, payload: MergeBody) -> Any:
    """Merge to the default branch: a PERSON's approval of the change's review decision (the
    "Merge to the default branch" boundary). Approving it runs BOSS's governed merge, which for a
    local repository is a fast-forward or merge commit in the bare repository."""
    from apps.api.app.main import approve_decision_card
    from boss_engine.schemas import DecisionActionRequest

    _people_only(request, "Merging to the default branch")
    with begin(_engine) as connection:
        _require(connection, project_id)
        row = _change_row(connection, project_id, number)
        if row["state"] != "open":
            return JSONResponse({"error": "not_open", "message": f"Change #{number} is {row['state']}."},
                                status_code=409)
        from boss_engine.services.local_repo_host import change_set_url

        decision = _decision_for(connection, change_set_url(row["repository"], row["number"]))
    if decision is None or decision["status"] != "pending":
        return JSONResponse({"error": "no_review_decision", "message":
                             f"Change #{number} has no review waiting for a decision."}, status_code=409)
    return approve_decision_card(decision["id"], DecisionActionRequest(
        actor=_actor(request), note=payload.note or f"Merge change #{number}", force=payload.force))


# ── publish to GitHub, and making the GitHub repository ─────────────────────────────────────

class PublishBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: str = Field(..., max_length=39)
    repo: str = Field(..., max_length=100)
    private: bool = True
    create: bool = Field(True, description="Make the repository (else publish to an existing, "
                                           "empty one).")


@router.post("/v1/projects/{project_id}/publish")
@permit(P.REPOSITORY_MANAGE, scope=Scope.project(path="project_id"))
def publish_project(project_id: str, request: Request, payload: PublishBody) -> Any:
    from boss_engine.services import local_publish

    _people_only(request, "Publishing to GitHub")
    with begin(_engine) as connection:
        _require(connection, project_id)
        try:
            return local_publish.publish(connection, _settings, project_id, owner=payload.owner,
                                         repo=payload.repo, private=payload.private,
                                         create=payload.create, actor=_actor(request))
        except local_publish.PublishError as error:
            # What already happened stays (resumable): the marker records the step reached.
            return _refused(error)


class CreateRepoBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: str = Field(..., max_length=39)
    repo: str = Field(..., max_length=100)
    private: bool = True


@router.post("/v1/projects/{project_id}/github-repository")
@permit(P.REPOSITORY_MANAGE, scope=Scope.project(path="project_id"))
def create_github_repository(project_id: str, request: Request, payload: CreateRepoBody) -> Any:
    """Make an empty repository on GitHub (BOSSAPP-917 builds its screen on this)."""
    from boss_engine.services import local_publish

    _people_only(request, "Creating a GitHub repository")
    with begin(_engine) as connection:
        _require(connection, project_id)
        reach = local_publish.default_reach(connection, _settings, project_id, actor=_actor(request))
    if not reach.create_token:
        return JSONResponse({"ok": False, "reason": "no_github_credential", "words":
                             "BOSS has no GitHub credential that can create repositories: connect "
                             "the GitHub App with Administration: Read and write."}, status_code=409)
    made = local_publish.create_repository(owner=payload.owner, name=payload.repo,
                                           private=payload.private, token=reach.create_token,
                                           api_base_url=reach.api_base_url,
                                           token_kind=reach.create_token_kind)
    return JSONResponse(made.as_dict(), status_code=200 if made.ok else 409)


@router.get("/v1/projects/{project_id}/github-repository/check")
@permit(P.REPOSITORY_READ, scope=Scope.project(path="project_id"))
def check_github_repository(project_id: str, request: Request, owner: str, repo: str,
                            purpose: str = "create") -> dict[str, Any]:
    """Can BOSS make `owner/repo` on GitHub (or publish into it, purpose=existing)? Reads only:
    the sheet says the engine's words while the person is still choosing (BOSSAPP-917/921)."""
    from boss_engine.services import local_publish

    with begin(_engine) as connection:
        _require(connection, project_id)
        reach = local_publish.default_reach(connection, _settings, project_id, actor=_actor(request),
                                            owner=owner, repo=repo)
    return local_publish.check_repository(
        owner=owner.strip(), name=repo.strip(), token=reach.create_token,
        purpose="existing" if purpose == "existing" else "create",
        api_base_url=reach.api_base_url, token_kind=reach.create_token_kind)


@router.get("/v1/projects/{project_id}/local-folders")
@permit(P.REPOSITORY_READ, scope=Scope.project(path="project_id"))
def local_folders(project_id: str, q: Optional[str] = None) -> dict[str, Any]:
    """Bring existing code › This machine: the folders in the install's import directory, each
    with its history, its GitHub remote (if any) and its size (BOSSAPP-923)."""
    from boss_engine.services import local_projects

    with begin(_engine) as connection:
        _require(connection, project_id)
    return local_projects.list_import_folders(_settings, q or "")


# ── waiting for GitHub ──────────────────────────────────────────────────────────────────────

@router.get("/v1/projects/{project_id}/push-pending")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def push_pending(project_id: str) -> dict[str, Any]:
    from boss_engine.services import github_outage

    with begin(_engine) as connection:
        _require(connection, project_id)
        return {"project_id": project_id, "items": github_outage.waiting(connection, project_id)}


class LeasedJobBody(BaseModel):
    job_id: str
    worker_id: str
    lease_token: str


@router.post("/v1/repos/push-pending/execute")
@permit(P.WORKER_LEASE)
def push_pending_execute(body: LeasedJobBody) -> dict[str, Any]:
    """The worker hands its leased `repo.push_pending` job here (credentials live in the API)."""
    from apps.api.app.main import require_worker_principal
    from boss_engine.services import github_outage

    require_worker_principal()
    return github_outage.execute_job(_engine, _settings, job_id=body.job_id,
                                     worker_id=body.worker_id, lease_token=body.lease_token)
