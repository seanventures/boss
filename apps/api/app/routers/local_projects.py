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
GET  /v1/projects/{id}/local-changes/{n}/tree        the Code tab's tree at head|base (BOSSAPP-948)
GET  /v1/projects/{id}/local-changes/{n}/file        one file at head|base|a commit of the change
GET  /v1/projects/{id}/local-changes/{n}/comments    the line-comment threads (local_review)
POST /v1/projects/{id}/local-changes/{n}/comments    a comment on a line (or the change)
POST …/local-changes/{n}/comments/{id}/reply         a reply in a thread
POST …/local-changes/{n}/comments/{id}/resolve       a person resolves (or reopens) a thread
POST /v1/projects/{id}/local-changes/{n}/request-changes   a person's Request changes → fix round
POST /v1/projects/{id}/local-changes/{n}/review      run a pushed change's review now
POST /v1/projects/{id}/local-repo/working-copies     `boss clone` records where it cloned
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
        # THE RECONCILER (BOSSAPP-948): a branch that moved without a push through BOSS's
        # remote is caught here — its change set follows the head, a person's new branch
        # becomes a change. Never fatal for the list.
        from boss_engine.services import local_git_hooks

        try:
            with connection.begin_nested():
                local_git_hooks.reconcile(connection, _settings, project_id)
        except Exception:  # noqa: BLE001
            pass
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
            view["commits"] = local_repo.commits_between(git_dir, base, row["branch"]
                                                         if local_repo.branch_exists(git_dir, row["branch"])
                                                         else row["head_sha"])
        except Exception as error:  # noqa: BLE001 — the record still shows
            view.update({"files": [], "diff": "", "commits": [],
                         "diff_error": f"{type(error).__name__}: {str(error)[:200]}"})
        # WHERE IT LIVES and the comment threads (BOSSAPP-948, local_review).
        from boss_engine.services import local_review

        try:
            view["where"] = local_review.where(connection, _settings, row)
        except Exception as error:  # noqa: BLE001 — the record still shows
            view["where"] = {"error": f"{type(error).__name__}: {str(error)[:200]}"}
        view["comments"] = local_review.threads(connection, row["id"])
        view["comment_summary"] = local_review.summary(connection, row["id"])
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


# ── local review: the Code tab, line comments, Request changes (BOSSAPP-948) ─────────────────

def _review_refused(error: Exception) -> JSONResponse:
    return JSONResponse(error.body() if hasattr(error, "body") else {"error": "refused",
                                                                     "message": str(error)},
                        status_code=int(getattr(error, "status", 422) or 422))


@router.get("/v1/projects/{project_id}/local-changes/{number}/tree")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def local_change_tree(project_id: str, number: int, ref: Optional[str] = "head") -> Any:
    from boss_engine.services import local_review

    with begin(_engine) as connection:
        _require(connection, project_id)
        try:
            return local_review.tree(connection, _settings, project_id, number, ref)
        except local_review.ReviewError as error:
            return _review_refused(error)


@router.get("/v1/projects/{project_id}/local-changes/{number}/file")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def local_change_file(project_id: str, number: int, path: str, ref: Optional[str] = "head") -> Any:
    from boss_engine.services import local_review

    with begin(_engine) as connection:
        _require(connection, project_id)
        try:
            return local_review.read_file(connection, _settings, project_id, number, path, ref)
        except local_review.ReviewError as error:
            return _review_refused(error)


@router.get("/v1/projects/{project_id}/local-changes/{number}/comments")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def local_change_comments(project_id: str, number: int) -> Any:
    from boss_engine.services import local_review

    with begin(_engine) as connection:
        _require(connection, project_id)
        try:
            row = local_review.change_row(connection, project_id, number)
        except local_review.ReviewError as error:
            return _review_refused(error)
        return {"number": int(number), "items": local_review.threads(connection, row["id"]),
                "summary": local_review.summary(connection, row["id"])}


class CommentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(..., min_length=1, max_length=8000)
    path: Optional[str] = Field(default=None, max_length=500)
    line: Optional[int] = Field(default=None, ge=1)
    end_line: Optional[int] = Field(default=None, ge=1)
    side: str = Field(default="new", pattern="^(new|old|right|left)$")
    commit_sha: Optional[str] = Field(default=None, max_length=64)
    severity: Optional[str] = Field(default=None, max_length=20,
                                    description="must fix | should fix | nit")
    origin: str = Field(default="console", pattern="^(console|cli)$")


def _author_kind(request: Request) -> str:
    """A person's comment is a required input of the fix round; an agent's is not."""
    from apps.api.app import identity_guard

    ctx = identity_guard.request_auth(request)
    if ctx is None:
        return "human"
    kind = getattr(getattr(getattr(ctx, "principal", None), "kind", None), "value", "")
    return "human" if kind in ("user", "") else "agent"


@router.post("/v1/projects/{project_id}/local-changes/{number}/comments")
@permit(P.CODE_TASK_STEER, scope=Scope.project(path="project_id"))
def local_change_comment(project_id: str, number: int, request: Request, payload: CommentBody) -> Any:
    from boss_engine.services import local_review

    with begin(_engine) as connection:
        _require(connection, project_id)
        try:
            return local_review.add_comment(
                connection, _settings, project_id=project_id, number=number, body=payload.body,
                author=_actor(request), author_kind=_author_kind(request),
                path=payload.path, line=payload.line, end_line=payload.end_line,
                side=payload.side, commit_sha=payload.commit_sha, severity=payload.severity,
                origin=payload.origin)
        except local_review.ReviewError as error:
            return _review_refused(error)


class ReplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(..., min_length=1, max_length=8000)
    origin: str = Field(default="console", pattern="^(console|cli)$")


@router.post("/v1/projects/{project_id}/local-changes/{number}/comments/{comment_id}/reply")
@permit(P.CODE_TASK_STEER, scope=Scope.project(path="project_id"))
def local_change_comment_reply(project_id: str, number: int, comment_id: int, request: Request,
                               payload: ReplyBody) -> Any:
    from boss_engine.services import local_review

    with begin(_engine) as connection:
        _require(connection, project_id)
        try:
            return local_review.add_comment(
                connection, _settings, project_id=project_id, number=number, body=payload.body,
                author=_actor(request), author_kind=_author_kind(request),
                origin=payload.origin, reply_to=comment_id)
        except local_review.ReviewError as error:
            return _review_refused(error)


class ResolveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reopen: bool = False


@router.post("/v1/projects/{project_id}/local-changes/{number}/comments/{comment_id}/resolve")
@permit(P.CODE_TASK_STEER, scope=Scope.project(path="project_id"))
def local_change_comment_resolve(project_id: str, number: int, comment_id: int, request: Request,
                                 payload: ResolveBody) -> Any:
    """A PERSON resolves a thread (the agent replies "Fixed …"; the person confirms)."""
    from boss_engine.services import local_review

    _people_only(request, "Resolving a review thread")
    with begin(_engine) as connection:
        _require(connection, project_id)
        try:
            return local_review.resolve(connection, project_id=project_id, number=number,
                                        comment_id=comment_id, actor=_actor(request),
                                        reopen=payload.reopen)
        except local_review.ReviewError as error:
            return _review_refused(error)


class RequestChangesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: Optional[str] = Field(default=None, max_length=4000)


@router.post("/v1/projects/{project_id}/local-changes/{number}/request-changes")
@permit(P.DECISION_APPROVE, scope=Scope.project(path="project_id"))
def local_change_request_changes(project_id: str, number: int, request: Request,
                                 payload: RequestChangesBody) -> Any:
    """REQUEST CHANGES: a person sends the review decision back; that starts the fix round on
    the change's branch with their open line comments as required inputs."""
    from apps.api.app.main import decisions, jobs
    from boss_engine.services import local_review
    from boss_engine.services.decisions import InvalidDecisionTransitionError

    _people_only(request, "Requesting changes")
    with begin(_engine) as connection:
        _require(connection, project_id)
        try:
            return local_review.request_changes(
                connection, _settings, project_id=project_id, number=number, note=payload.note,
                actor=_actor(request), jobs=jobs, decisions=decisions)
        except local_review.ReviewError as error:
            return _review_refused(error)
        except InvalidDecisionTransitionError as error:
            return JSONResponse({"error": "not_pending", "message": str(error)}, status_code=409)


@router.post("/v1/projects/{project_id}/local-changes/{number}/review")
@permit(P.CODE_TASK_CREATE, scope=Scope.project(path="project_id"))
def local_change_review(project_id: str, number: int, request: Request) -> Any:
    """Run the review of a pushed change now (a push queues it; when that could not start —
    consent, the budget — the change page offers this)."""
    from apps.api.app.main import jobs
    from boss_engine.services import local_git_hooks, local_review

    with begin(_engine) as connection:
        _require(connection, project_id)
        try:
            row = local_review.change_row(connection, project_id, number)
        except local_review.ReviewError as error:
            return _review_refused(error)
        if row["state"] != "open":
            return JSONResponse({"error": "not_open", "message": f"Change #{number} is "
                                 f"{row['state']}."}, status_code=409)
        run_id = local_git_hooks.queue_review_of(connection, _settings, change=row,
                                                 pusher=_actor(request), jobs=jobs,
                                                 raise_refusals=True)
        return {"number": int(number), "run_id": run_id}


class WorkingCopyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(..., min_length=1, max_length=500)
    branch: Optional[str] = Field(default=None, max_length=250)
    machine: str = Field(default="", max_length=120)
    repository: Optional[str] = Field(default=None, max_length=200)


@router.post("/v1/projects/{project_id}/local-repo/working-copies")
@permit(P.REPOSITORY_READ, scope=Scope.project(path="project_id"))
def local_repo_working_copy(project_id: str, request: Request, payload: WorkingCopyBody) -> Any:
    """`boss clone` says where it cloned, so the change page lists the working copy."""
    from boss_engine.services import local_review

    with begin(_engine) as connection:
        _require(connection, project_id)
        binding = _local_binding(connection, project_id)
        try:
            return local_review.record_working_copy(
                connection, project_id, repository=payload.repository or binding["full_name"],
                path=payload.path, branch=payload.branch, machine=payload.machine,
                actor=_actor(request))
        except local_review.ReviewError as error:
            return _review_refused(error)


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
