"""THE LOCAL GIT REMOTE over HTTP (BOSSAPP-948). Engine: services/local_git_http.py (git's own
`git http-backend`), services/local_git_hooks.py (the push fence and a pushed branch becoming a
change). The console's web server proxies /git/* here, so the clone URL is
http://localhost:3000/git/<project>.git.

GET  /git/{project_id}.git/info/refs                 refs (repository:read; with
                                                     ?service=git-receive-pack also repository:push)
POST /git/{project_id}.git/git-upload-pack           clone, fetch, pull (repository:read)
POST /git/{project_id}.git/git-receive-pack          push (repository:push)
     /git/{project_id}/{repo}.git/...                the same for another local repository

Sign-in: the console's session cookie, or HTTP Basic with a BOSS token (boss_pat_, or the host
tools' boss_sak_) as the password — Basic is accepted on /git/* only (identity/authn.py). A 401
here carries `WWW-Authenticate: Basic`, so git asks for (or its credential helper supplies) it.
Reach: this machine only unless BOSS_GIT_REMOTE_REACH=network (local_git_http.is_local_request).
"""
from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from boss_engine.config import get_settings
from boss_engine.db import begin, get_engine
from boss_engine.identity import P, Scope, permit

router = APIRouter(tags=["git"])
_settings = get_settings()
_engine = get_engine(_settings)


def _words(status: int, error: str, message: str) -> JSONResponse:
    return JSONResponse({"error": error, "message": message}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def _local_only(request: Request) -> Optional[JSONResponse]:
    from apps.api.app import identity_guard
    from boss_engine.services.local_git_http import is_local_request

    creds = identity_guard._credentials(request)  # noqa: SLF001 — the guard's own client address
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if is_local_request(get_settings(), client_ip=creds.client_ip, host=host):
        return None
    return _words(403, "git_remote_local_only",
                  "BOSS's git remote answers this machine only. Clone from the machine BOSS runs "
                  "on, or set BOSS_GIT_REMOTE_REACH=network.")


def _pusher(request: Request) -> tuple[str, str]:
    from apps.api.app import identity_guard

    ctx = identity_guard.request_auth(request)
    if ctx is None:
        return "operator", "human"
    kind = getattr(getattr(ctx.principal, "kind", None), "value", "")
    return identity_guard.actor_for(ctx), ("human" if kind == "user" else "agent")


async def _read_capped(request: Request, cap: int) -> Optional[bytes]:
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > cap:
            return None
    return bytes(buf)


async def _serve(request: Request, project_id: str, repo: Optional[str], tail: str) -> Any:
    from boss_engine.services import local_git_http as lgh

    refused = _local_only(request)
    if refused is not None:
        return refused
    service = request.query_params.get("service") or ""
    push = tail == "/git-receive-pack" or (tail == "/info/refs" and service == "git-receive-pack")
    if tail == "/info/refs" and service not in lgh.SERVICES:
        return _words(403, "smart_http_only", "BOSS's git remote speaks git's smart HTTP "
                                              "protocol only; use git 2.x.")
    if push:
        # repository:push, checked at info/refs too, so git asks before it uploads a pack.
        from apps.api.app.routers.identity_access import require_permission

        require_permission(request, P.REPOSITORY_PUSH, project_id, label="git")
    with begin(_engine) as c:
        try:
            found = lgh.resolve_repo(c, _settings, project_id, repo)
        except lgh.GitRemoteError as error:
            return _words(error.status, error.error, str(error))
        busy = lgh.busy_branches(c, project_id, found["full_name"]) if push else []
    pusher, kind = _pusher(request)
    if request.method == "POST":
        body = await _read_capped(request, lgh.max_push_bytes(_settings) + 1024 * 1024)
        if body is None:
            return _words(413, "push_too_large",
                          f"A push is at most {int(_settings.git_remote_max_push_mb)} MB here.")
    else:
        body = b""
    env = {**lgh.git_config_env(_settings, push=push),
           **lgh.hook_env(project_id=project_id, repository=found["full_name"],
                          default_branch=found.get("default_branch") or "main", pusher=pusher,
                          pusher_kind=kind, busy=busy, settings=_settings)}
    receiving = tail == "/git-receive-pack"
    touched_file = None
    if receiving:
        import tempfile

        fd, touched_file = tempfile.mkstemp(prefix="boss-push-", suffix=".json")
        os.close(fd)
        env.update({"BOSS_GIT_QUEUE_REVIEW": "0", "BOSS_GIT_TOUCHED_FILE": touched_file})
    try:
        answer = await run_in_threadpool(
            lgh.run_backend, found["git_dir"], method=request.method, path_info=tail,
            query=request.url.query, content_type=request.headers.get("content-type", ""),
            body=body, extra_env=env,
            content_encoding=request.headers.get("content-encoding", ""),
            git_protocol=request.headers.get("git-protocol", ""), remote_user=pusher)
    except lgh.GitRemoteError as error:
        return _words(error.status, error.error, str(error))
    headers = {k: v for k, v in answer.headers if k.lower() not in ("content-length",)}
    headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
    media = headers.pop("Content-Type", None)
    if receiving:
        # A push's answer is small: read it whole, so the hooks have finished and the review of
        # each pushed change is queued HERE, as the pusher (the hook process holds no identity).
        out = await run_in_threadpool(lambda: b"".join(answer.body))
        await run_in_threadpool(_queue_reviews, touched_file, pusher)
        return Response(content=out, status_code=answer.status, headers=headers, media_type=media)
    return StreamingResponse(answer.body, status_code=answer.status, headers=headers,
                             media_type=media)


def _queue_reviews(touched_file: Optional[str], pusher: str) -> None:
    """The review panel for each change the push opened or moved (local_git_hooks.queue_review_of),
    under the pusher's own identity and consent — a refusal is recorded on the change, never lost."""
    import json

    from apps.api.app.main import jobs
    from boss_engine.services import local_git_hooks, local_review

    try:
        with open(str(touched_file or ""), encoding="utf-8") as fh:
            ids = json.load(fh) or []
    except (OSError, ValueError):
        ids = []
    finally:
        if touched_file:
            try:
                os.unlink(touched_file)
            except OSError:
                pass
    for cs_id in ids:
        with begin(_engine) as c:
            row = local_review.change_by_id(c, str(cs_id))
            if row is not None and row.get("state") == "open":
                local_git_hooks.queue_review_of(c, _settings, change=row, pusher=pusher, jobs=jobs)


# The primary repository: /git/<project>.git

@router.get("/git/{project_id}.git/info/refs")
@permit(P.REPOSITORY_READ, scope=Scope.project(path="project_id"))
async def git_info_refs(project_id: str, request: Request) -> Any:
    return await _serve(request, project_id, None, "/info/refs")


@router.post("/git/{project_id}.git/git-upload-pack")
@permit(P.REPOSITORY_READ, scope=Scope.project(path="project_id"))
async def git_upload_pack(project_id: str, request: Request) -> Any:
    return await _serve(request, project_id, None, "/git-upload-pack")


@router.post("/git/{project_id}.git/git-receive-pack")
@permit(P.REPOSITORY_PUSH, scope=Scope.project(path="project_id"))
async def git_receive_pack(project_id: str, request: Request) -> Any:
    return await _serve(request, project_id, None, "/git-receive-pack")


# Another local repository of the project: /git/<project>/<repo>.git

@router.get("/git/{project_id}/{repo}.git/info/refs")
@permit(P.REPOSITORY_READ, scope=Scope.project(path="project_id"))
async def git_info_refs_repo(project_id: str, repo: str, request: Request) -> Any:
    return await _serve(request, project_id, repo, "/info/refs")


@router.post("/git/{project_id}/{repo}.git/git-upload-pack")
@permit(P.REPOSITORY_READ, scope=Scope.project(path="project_id"))
async def git_upload_pack_repo(project_id: str, repo: str, request: Request) -> Any:
    return await _serve(request, project_id, repo, "/git-upload-pack")


@router.post("/git/{project_id}/{repo}.git/git-receive-pack")
@permit(P.REPOSITORY_PUSH, scope=Scope.project(path="project_id"))
async def git_receive_pack_repo(project_id: str, repo: str, request: Request) -> Any:
    return await _serve(request, project_id, repo, "/git-receive-pack")
