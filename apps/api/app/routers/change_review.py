"""CHANGE REVIEW over HTTP (BOSSAPP-954): one API shape over two sources (services/change_review.py).

A local change set keeps its address, /v1/projects/{id}/local-changes/{n} (its summary, tree,
file, comments, merge and request-changes routes stay in routers/local_projects.py); a GitHub pull
request of the project is /v1/projects/{id}/github-changes/{owner}/{repo}/{n}. Under either:

GET  …                          the change (GitHub only here; local_projects answers the local one)
GET  …/files?base=&head=        the changed files and the unified diff (the change, or base…head)
GET  …/compare?base=&head=      the same, named as git names it
GET  …/commits                  its commits, each with the comments it fixed
GET  …/commits/{sha}            one commit's diff
GET  …/checks                   BOSS's build, and GitHub's check runs
GET  …/viewed  POST …/viewed    the person's Viewed ticks and the head they last reviewed
GET  …/agent                    the PR agent: its task, the comments, the chat
POST …/agent/messages           a message to it (steered while it runs, else kept for the fix run)
GitHub only (local_projects has the local ones):
GET  …/tree  GET …/file  GET/POST …/comments  POST …/comments/{id}/reply  …/resolve
POST …/merge  POST …/request-changes
GET  /v1/github-changes/resolve?owner=&repo=   the projects a GitHub repository belongs to
GET  /v1/projects/{id}/history[?repo=&branch=]   the default branch's commits, merges linked
GET  /v1/projects/{id}/history/{sha}            one commit's own changes
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit

from .local_projects import (
    CommentBody, MergeBody, ReplyBody, RequestChangesBody, ResolveBody, _actor, _author,
    _people_only, _require, _signer,
)

router = APIRouter(tags=["projects"])
_settings = get_settings()
_engine = get_engine(_settings)

LOCAL = "/v1/projects/{project_id}/local-changes/{number}"
GITHUB = "/v1/projects/{project_id}/github-changes/{owner}/{repo}/{number}"


def _refused(error: Exception) -> JSONResponse:
    return JSONResponse(error.body() if hasattr(error, "body") else {"error": "refused",
                                                                     "message": str(error)},
                        status_code=int(getattr(error, "status", 422) or 422))


def _person(request: Request) -> str:
    from apps.api.app import identity_guard

    ctx = identity_guard.request_auth(request)
    pid = str(getattr(getattr(ctx, "principal", None), "id", "") or "")
    return pid or _actor(request)


def _person_token(request: Request) -> Optional[Callable[[], Optional[str]]]:
    """The signed-in person's linked GitHub account, asked for only when a comment is posted."""
    from apps.api.app import identity_guard

    ctx = identity_guard.request_auth(request)
    if ctx is None:
        return None

    def token() -> Optional[str]:
        from boss_engine.identity import vault

        try:
            with begin(_engine) as db:
                return vault.get_credential(vault.ConnectionRef("github", None),
                                            vault.CredentialPurpose.GITHUB_AS_USER,
                                            ctx=ctx, db=db).token
        except Exception:  # noqa: BLE001 — not linked: BOSS posts it for them
            return None
    return token


def _run(project_id: str, number: int, owner: Optional[str], repo: Optional[str],
         fn: Callable[[Any, Any], Any]) -> Any:
    from boss_engine.services import change_review

    with begin(_engine) as connection:
        _require(connection, project_id)
        try:
            src = change_review.source_for(connection, _settings, project_id, number, owner=owner,
                                           repo=repo, engine=_engine)
            return fn(connection, src)
        except change_review.ChangeReviewError as error:
            return _refused(error)


# ── shared bodies ─────────────────────────────────────────────────────────────────────────────

class ViewedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(default="", max_length=500, description="A file, or \"\" for the whole "
                                                               "change (Since my review).")
    sha: Optional[str] = Field(default=None, max_length=64)
    viewed: bool = True


class MessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(..., min_length=1, max_length=4000)


def _files(base: Optional[str], head: Optional[str]) -> Callable[[Any, Any], Any]:
    return lambda c, s: s.files(base or None, head or None)


def _viewed_get(request: Request) -> Callable[[Any, Any], Any]:
    from boss_engine.services import change_review

    return lambda c, s: change_review.viewed(c, s.project_id, s.key, _person(request))


def _viewed_set(request: Request, payload: ViewedBody) -> Callable[[Any, Any], Any]:
    from boss_engine.services import change_review

    def go(c: Any, s: Any) -> Any:
        sha = payload.sha or s.head()
        return change_review.set_viewed(c, s.project_id, s.key, _person(request), path=payload.path,
                                        sha=sha, viewed_=payload.viewed)
    return go


def _agent(request: Request) -> Callable[[Any, Any], Any]:
    from boss_engine.services import change_review

    return lambda c, s: change_review.agent_pane(c, s, _person(request))


def _message(request: Request, payload: MessageBody) -> Callable[[Any, Any], Any]:
    from boss_engine.services import change_review

    return lambda c, s: change_review.send_message(c, s, body=payload.body,
                                                   author=_author(request), actor=_actor(request))


# ── the local change set: what local_projects does not already answer ────────────────────────

@router.get(LOCAL + "/files")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def local_files(project_id: str, number: int, base: Optional[str] = None,
                head: Optional[str] = None) -> Any:
    return _run(project_id, number, None, None, _files(base, head))


@router.get(LOCAL + "/compare")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def local_compare(project_id: str, number: int, base: str = "base", head: str = "head") -> Any:
    return _run(project_id, number, None, None, _files(base, head))


@router.get(LOCAL + "/commits")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def local_commits(project_id: str, number: int) -> Any:
    return _run(project_id, number, None, None, lambda c, s: {"items": s.commits()})


@router.get(LOCAL + "/commits/{sha}")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def local_commit(project_id: str, number: int, sha: str) -> Any:
    return _run(project_id, number, None, None, lambda c, s: s.commit(sha))


@router.get(LOCAL + "/checks")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def local_checks(project_id: str, number: int) -> Any:
    return _run(project_id, number, None, None, lambda c, s: {"checks": s.checks()})


@router.get(LOCAL + "/viewed")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def local_viewed(project_id: str, number: int, request: Request) -> Any:
    return _run(project_id, number, None, None, _viewed_get(request))


@router.post(LOCAL + "/viewed")
@permit(P.CODE_TASK_STEER, scope=Scope.project(path="project_id"))
def local_viewed_set(project_id: str, number: int, request: Request, payload: ViewedBody) -> Any:
    return _run(project_id, number, None, None, _viewed_set(request, payload))


@router.get(LOCAL + "/agent")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def local_agent(project_id: str, number: int, request: Request) -> Any:
    return _run(project_id, number, None, None, _agent(request))


@router.post(LOCAL + "/agent/messages")
@permit(P.CODE_TASK_STEER, scope=Scope.project(path="project_id"))
def local_agent_message(project_id: str, number: int, request: Request,
                        payload: MessageBody) -> Any:
    return _run(project_id, number, None, None, _message(request, payload))


# ── a GitHub pull request of the project ──────────────────────────────────────────────────────

@router.get("/v1/github-changes/resolve")
@permit(P.PROJECT_READ, scope=Scope.org())
def github_change_resolve(owner: str, repo: str, request: Request) -> dict[str, Any]:
    """Which of the caller's projects a GitHub repository belongs to: a PR link opens BOSS's
    change page for it, or GitHub when it is none of theirs."""
    from boss_engine.services import change_review

    from .identity_access import readable_projects

    with begin(_engine) as connection:
        found = change_review.projects_for_repository(connection, owner.strip(), repo.strip())
    allowed = readable_projects(request, found)
    return {"owner": owner, "repo": repo,
            "projects": [p for p in found if allowed is None or p in allowed]}


@router.get(GITHUB)
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def github_change(project_id: str, owner: str, repo: str, number: int) -> Any:
    return _run(project_id, number, owner, repo, lambda c, s: s.summary())


@router.get(GITHUB + "/files")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def github_files(project_id: str, owner: str, repo: str, number: int, base: Optional[str] = None,
                 head: Optional[str] = None) -> Any:
    return _run(project_id, number, owner, repo, _files(base, head))


@router.get(GITHUB + "/compare")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def github_compare(project_id: str, owner: str, repo: str, number: int, base: str = "base",
                   head: str = "head") -> Any:
    return _run(project_id, number, owner, repo, _files(base, head))


@router.get(GITHUB + "/tree")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def github_tree(project_id: str, owner: str, repo: str, number: int,
                ref: Optional[str] = "head") -> Any:
    return _run(project_id, number, owner, repo, lambda c, s: s.tree(ref))


@router.get(GITHUB + "/file")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def github_file(project_id: str, owner: str, repo: str, number: int, path: str,
                ref: Optional[str] = "head") -> Any:
    from boss_engine.services import local_review

    def go(c: Any, s: Any) -> Any:
        try:
            return s.blob(path, ref)
        except local_review.ReviewError as error:
            return _refused(error)
    return _run(project_id, number, owner, repo, go)


@router.get(GITHUB + "/commits")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def github_commits(project_id: str, owner: str, repo: str, number: int) -> Any:
    return _run(project_id, number, owner, repo, lambda c, s: {"items": s.commits(),
                                                               "stale": s.stale})


@router.get(GITHUB + "/commits/{sha}")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def github_commit(project_id: str, owner: str, repo: str, number: int, sha: str) -> Any:
    return _run(project_id, number, owner, repo, lambda c, s: s.commit(sha))


@router.get(GITHUB + "/checks")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def github_checks(project_id: str, owner: str, repo: str, number: int) -> Any:
    return _run(project_id, number, owner, repo, lambda c, s: {"checks": s.checks(),
                                                               "stale": s.stale})


@router.get(GITHUB + "/comments")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def github_comments(project_id: str, owner: str, repo: str, number: int) -> Any:
    def go(c: Any, s: Any) -> Any:
        items = s.threads()
        return {"number": int(number), "items": items, "summary": s.comment_summary(items),
                "stale": s.stale, "fetched_at": s.fetched_at}
    return _run(project_id, number, owner, repo, go)


@router.post(GITHUB + "/comments")
@permit(P.CODE_TASK_STEER, scope=Scope.project(path="project_id"))
def github_comment(project_id: str, owner: str, repo: str, number: int, request: Request,
                   payload: CommentBody) -> Any:
    token = _person_token(request)
    return _run(project_id, number, owner, repo, lambda c, s: s.add_comment(
        body=payload.body, author=_author(request), author_kind=_signer(request)["author_kind"],
        principal_id=_signer(request)["principal_id"],
        path=payload.path, line=payload.line, end_line=payload.end_line, side=payload.side,
        commit_sha=payload.commit_sha, severity=payload.severity, origin=payload.origin,
        person_token=token, person=_person(request)))


@router.post(GITHUB + "/comments/{comment_id}/reply")
@permit(P.CODE_TASK_STEER, scope=Scope.project(path="project_id"))
def github_comment_reply(project_id: str, owner: str, repo: str, number: int, comment_id: int,
                         request: Request, payload: ReplyBody) -> Any:
    token = _person_token(request)
    return _run(project_id, number, owner, repo, lambda c, s: s.reply(
        comment_id, body=payload.body, author=_author(request),
        author_kind=_signer(request)["author_kind"], principal_id=_signer(request)["principal_id"],
        person_token=token, person=_person(request)))


@router.post(GITHUB + "/comments/{comment_id}/resolve")
@permit(P.CODE_TASK_STEER, scope=Scope.project(path="project_id"))
def github_comment_resolve(project_id: str, owner: str, repo: str, number: int, comment_id: int,
                           request: Request, payload: ResolveBody) -> Any:
    _people_only(request, "Resolving a review thread")
    token = _person_token(request)
    return _run(project_id, number, owner, repo, lambda c, s: s.resolve(
        comment_id, actor=_author(request), reopen=payload.reopen, person_token=token,
        person=_person(request)))


@router.post(GITHUB + "/request-changes")
@permit(P.DECISION_APPROVE, scope=Scope.project(path="project_id"))
def github_request_changes(project_id: str, owner: str, repo: str, number: int, request: Request,
                           payload: RequestChangesBody) -> Any:
    """FIX N OPEN COMMENTS: a fix run on the PR's own branch, the open comments its inputs."""
    from apps.api.app.main import decisions, jobs
    from boss_engine.services.decisions import InvalidDecisionTransitionError

    _people_only(request, "Requesting changes")

    def go(c: Any, s: Any) -> Any:
        try:
            return s.request_changes(note=payload.note or "", actor=_actor(request), jobs=jobs,
                                     decisions=decisions)
        except InvalidDecisionTransitionError as error:
            return JSONResponse({"error": "not_pending", "message": str(error)}, status_code=409)
    return _run(project_id, number, owner, repo, go)


@router.post(GITHUB + "/merge")
@permit(P.DECISION_APPROVE, scope=Scope.project(path="project_id"))
def github_merge(project_id: str, owner: str, repo: str, number: int, request: Request,
                 payload: MergeBody) -> Any:
    """Merge: a PERSON's approval of the PR's review decision; BOSS's governed merge follows."""
    from apps.api.app.main import approve_decision_card
    from boss_engine.schemas import DecisionActionRequest
    from boss_engine.services import change_review

    _people_only(request, "Merging to the default branch")
    found: dict[str, Any] = {}

    def go(c: Any, s: Any) -> Any:
        meta = s.meta()
        if meta.get("merged") or str(meta.get("state")) != "open":
            return JSONResponse({"error": "not_open", "message": f"PR #{number} is not open."},
                                status_code=409)
        found["decision"] = change_review._decision_for(c, s.url)  # noqa: SLF001
        return None
    refused = _run(project_id, number, owner, repo, go)
    if refused is not None:
        return refused
    decision = found.get("decision")
    if decision is None or decision["status"] != "pending":
        return JSONResponse({"error": "no_review_decision", "message":
                             f"PR #{number} has no review waiting for a decision."}, status_code=409)
    return approve_decision_card(decision["id"], DecisionActionRequest(
        actor=_actor(request), note=payload.note or f"Merge PR #{number}", force=payload.force))


@router.get(GITHUB + "/viewed")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def github_viewed(project_id: str, owner: str, repo: str, number: int, request: Request) -> Any:
    return _run(project_id, number, owner, repo, _viewed_get(request))


@router.post(GITHUB + "/viewed")
@permit(P.CODE_TASK_STEER, scope=Scope.project(path="project_id"))
def github_viewed_set(project_id: str, owner: str, repo: str, number: int, request: Request,
                      payload: ViewedBody) -> Any:
    return _run(project_id, number, owner, repo, _viewed_set(request, payload))


@router.get(GITHUB + "/agent")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def github_agent(project_id: str, owner: str, repo: str, number: int, request: Request) -> Any:
    return _run(project_id, number, owner, repo, _agent(request))


@router.post(GITHUB + "/agent/messages")
@permit(P.CODE_TASK_STEER, scope=Scope.project(path="project_id"))
def github_agent_message(project_id: str, owner: str, repo: str, number: int, request: Request,
                         payload: MessageBody) -> Any:
    return _run(project_id, number, owner, repo, _message(request, payload))


# ── the project's history: its default branch, merged work included ──────────────────────────

@router.get("/v1/projects/{project_id}/history")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def project_history(project_id: str, repo: Optional[str] = None, branch: Optional[str] = None,
                    limit: int = 50) -> Any:
    """The default branch's commits (main/master), newest first; a merge links its change/PR."""
    from boss_engine.services import change_review

    with begin(_engine) as connection:
        _require(connection, project_id)
        try:
            return change_review.history(connection, _settings, project_id, repo=repo,
                                         branch=branch, limit=limit)
        except change_review.ChangeReviewError as error:
            return _refused(error)
        except Exception as error:  # noqa: BLE001 — GitHub did not answer: said, not a 500
            return JSONResponse({"error": "github_unreachable",
                                 "message": f"GitHub did not answer: {type(error).__name__}."},
                                status_code=503)


@router.get("/v1/projects/{project_id}/history/{sha}")
@permit(P.CODE_TASK_READ, scope=Scope.project(path="project_id"))
def project_history_commit(project_id: str, sha: str, repo: Optional[str] = None) -> Any:
    """One commit's own changes; a merge commit, what it brought in (against its first parent)."""
    from boss_engine.services import change_review

    with begin(_engine) as connection:
        _require(connection, project_id)
        try:
            return change_review.history_commit(connection, _settings, project_id, sha, repo=repo)
        except change_review.ChangeReviewError as error:
            return _refused(error)
        except Exception as error:  # noqa: BLE001
            return JSONResponse({"error": "github_unreachable",
                                 "message": f"GitHub did not answer: {type(error).__name__}."},
                                status_code=503)
