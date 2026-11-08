"""GITHUB IS DOWN, THE BUILD GOES ON (BOSSAPP-922).

transient_retry already gives a GitHub blip ~40 s of patience in place. When GitHub is still not
answering after that, a GitHub project's build used to fail. Now it keeps going on this machine:

  * MIRROR. Every GitHub build refreshes a bare mirror of its repository at build start
    (`local_repo.ensure_mirror`: all branches and tags). When GitHub cannot be reached even for
    that, the mirror keeps what it had.
  * CLONE. A clone that still fails after the retries clones the mirror instead.
  * COMMITS. `ResilientGitHub` is the build's repository adapter: GitHub's, until a write fails
    on an outage; from then on (sticky, for the rest of the build) the same calls go to the
    mirror through `LocalGitAdapter`, so the stages' commits land locally, and "opening the pull
    request" records a change set marked `pending_push`.
  * PUSH PENDING. The build ends "waiting for GitHub", in words, never failed: a
    `repo_push_pending` row and a `repo.push_pending` worker job, retried with backoff, push the
    branch from the mirror and open the real pull request the moment GitHub answers, then point
    the run, its change set and its review decision at it.
  * SAID. The Shepherd's stuck-work view (`waiting`) lists what is waiting on GitHub and why.

GitHub's 4xx answers (auth, not found) are never an outage: they fail as before.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from sqlalchemy import text

from . import local_repo, transient_retry

log = logging.getLogger(__name__)

JOB_KIND = "repo.push_pending"
#: Waits between push attempts, then the last one repeated (minutes): ~1 day before giving up.
BACKOFF_MIN = (1, 2, 5, 10, 20, 30)
MAX_ATTEMPTS = 60
WAITING_STATUS = "waiting_for_github"

#: The adapter calls a build writes or reads the branch with; these move to the mirror.
_FALLBACK_OPS = frozenset({
    "ensure_branch", "upsert_file", "upsert_binary_file", "commit_files", "create_pull_request",
    "branch_exists", "read_text_file", "list_paths", "default_branch_sha", "add_labels",
    "create_issue_comment", "create_review", "review_comments_of", "update_review_body",
    "reply_to_review_comment", "pull_request_snapshot", "pull_request_meta",
    "pull_request_files", "get_pull_request_diff", "update_pull_request_body",
    "mark_ready_for_review", "pull_request_reviews",
})


def is_outage(error: BaseException) -> bool:
    """GitHub did not answer (5xx, a timeout, a reset, git's RPC failures) — never a 4xx."""
    if transient_retry.is_transient_http_error(error):
        return True
    return transient_retry.is_transient_git_error(str(error))


def mirror_ready(owner: str, repo: str, settings: Any = None) -> bool:
    return (local_repo.mirror_path(owner, repo, settings) / "HEAD").is_file()


def access_for(egress: Any, owner: str, repo: str, token: Optional[str]) -> tuple[str, dict[str, str]]:
    """(url, env) for BOSS's own git to fetch the repository, the way the build's clone reaches
    it: through egress (its config and BOSS's own credential helper) or with the token in git's
    environment only — never in the URL or on disk."""
    from .repo_host import git_access, git_config_env

    if egress is None:
        access = git_access("github", owner, repo, token or "")
        return access.url, dict(access.env)
    pairs = [(k, v) for k, v in (getattr(egress, "git_config", None) or []) if k != "credential.helper"]
    own = getattr(egress, "own_git", None)
    env: dict[str, str] = {}
    if own is not None:
        pairs += list(getattr(own, "config", None) or [])
        env.update(dict(getattr(own, "env", None) or {}))
    count = int(env.pop("GIT_CONFIG_COUNT", "0") or 0)
    existing = [(env.pop(f"GIT_CONFIG_KEY_{i}"), env.pop(f"GIT_CONFIG_VALUE_{i}")) for i in range(count)]
    env.update(git_config_env(existing + pairs))
    return str(egress.clone_url), env


def refresh_mirror(settings: Any, owner: str, repo: str, *, url: str,
                   env: Optional[Mapping[str, str]] = None, args: Optional[list[str]] = None,
                   timeout: int = 300) -> dict[str, Any]:
    """Bring the repository's mirror up to date at build start. Never raises: a GitHub that
    cannot be reached leaves the mirror as it was, and says so."""
    try:
        local_repo.ensure_mirror(owner, repo, url, settings=settings, env=env, timeout=timeout)
        return {"ok": True, "path": str(local_repo.mirror_path(owner, repo, settings))}
    except Exception as error:  # noqa: BLE001 — the build goes on with whatever the mirror has
        said = str(error)[-300:]
        return {"ok": False, "error": said, "have_mirror": mirror_ready(owner, repo, settings)}


class ResilientGitHub:
    """GitHub's adapter, with the repository's local mirror behind it for an outage."""

    def __init__(self, primary: Any, *, settings: Any, project_id: str,
                 run_id: Optional[str] = None, engine: Any = None,
                 on_switch: Optional[Callable[[str], None]] = None):
        self._primary = primary
        self._settings = settings
        self._project_id = project_id
        self._run_id = run_id
        self._engine = engine
        self._on_switch = on_switch
        self._mirror: Any = None
        self.offline_reason: Optional[str] = None

    # The adapter's identity is the GitHub repository's, offline or not.
    @property
    def repository_full_name(self) -> str:
        return self._primary.repository_full_name

    @property
    def offline(self) -> bool:
        return self.offline_reason is not None

    @property
    def primary(self) -> Any:
        return self._primary

    def mirror(self) -> Any:
        if self._mirror is None:
            from .local_repo_host import LocalGitAdapter

            owner, _, repo = self._primary.repository_full_name.partition("/")
            self._mirror = LocalGitAdapter(
                self._settings, owner=owner, repo=repo, project_id=self._project_id,
                git_dir=local_repo.mirror_path(owner, repo, self._settings), engine=self._engine,
                pending_push=True)
        return self._mirror

    def go_offline(self, reason: str) -> None:
        if self.offline:
            return
        self.offline_reason = reason[-300:]
        log.warning("github outage: %s continues on its local mirror: %s",
                    self.repository_full_name, self.offline_reason)
        if self._on_switch is not None:
            try:
                self._on_switch(self.offline_reason)
            except Exception:  # noqa: BLE001 — recording it never stops the build
                log.exception("could not record the switch to the local mirror")

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        target = getattr(self._primary, name)
        if name not in _FALLBACK_OPS or not callable(target):
            return target

        def call(*args: Any, **kwargs: Any) -> Any:
            if self.offline:
                return getattr(self.mirror(), name)(*args, **kwargs)
            try:
                return target(*args, **kwargs)
            except Exception as error:  # noqa: BLE001 — re-raised unless GitHub is down
                owner, _, repo = self.repository_full_name.partition("/")
                if not is_outage(error) or not mirror_ready(owner, repo, self._settings):
                    raise
                self.go_offline(f"{name}: {type(error).__name__}: {error}")
                return getattr(self.mirror(), name)(*args, **kwargs)
        return call


# ── push pending ─────────────────────────────────────────────────────────────────────────────

def record_pending(c: Any, *, project_id: str, owner: str, repo: str, branch: str,
                   base_branch: str, run_id: Optional[str], change_set_id: Optional[str],
                   reason: str, enqueue: bool = True) -> str:
    """The branch waits for GitHub: one open row per branch (idempotent), and its first job."""
    from .core import emit_event, make_id

    existing = c.execute(text(
        "SELECT id FROM repo_push_pending WHERE owner = :o AND repo = :r AND branch = :b "
        "AND status IN ('waiting', 'pushing')"), {"o": owner, "r": repo, "b": branch}).scalar()
    if existing:
        return str(existing)
    pid = make_id("rpp")
    c.execute(text(
        "INSERT INTO repo_push_pending (id, project_id, owner, repo, branch, base_branch, run_id, "
        "change_set_id, reason) VALUES (:id, :p, :o, :r, :b, :base, :run, :cs, :why)"),
        {"id": pid, "p": project_id, "o": owner, "r": repo, "b": branch, "base": base_branch,
         "run": run_id, "cs": change_set_id, "why": reason[:500]})
    emit_event(c, project_id, "repo.push_pending", "boss", "repo_push_pending", pid,
               {"repository": f"{owner}/{repo}", "branch": branch, "run_id": run_id,
                "reason": reason[:300]})
    if enqueue:
        _enqueue(c, project_id, pid, attempt=1, delay_s=60)
    return pid


def _enqueue(c: Any, project_id: str, pending_id: str, *, attempt: int, delay_s: int) -> None:
    from .jobs import enqueue_job

    enqueue_job(c, project_id, JOB_KIND, {"pending_id": pending_id, "attempt": attempt},
                max_attempts=1, run_after_seconds=max(0, int(delay_s)),
                dedupe_key=f"push-pending:{pending_id}:{attempt}", data_boundary="internal")


def backoff_minutes(attempt: int) -> int:
    return BACKOFF_MIN[min(max(attempt, 1) - 1, len(BACKOFF_MIN) - 1)]


GitHubFactory = Callable[[Any, str, str], tuple[Any, Any]]


def _default_github(c: Any, settings: Any, project_id: str, owner: str, repo: str) -> tuple[Any, Any]:
    """GitHub's adapter for the repository and how BOSS's own git reaches it (url, env)."""
    from .github_adapter import GitHubAdapter
    from .github_connections import github_token_for
    from .repo_host import git_access

    token = github_token_for(c, settings, project_id, owner, repo)
    adapter = GitHubAdapter(settings, owner=owner, repo=repo, token=token or None)
    return adapter, git_access("github", owner, repo, token or "")


def attempt(engine: Any, settings: Any, pending_id: str, *,
            github: Optional[Callable[..., tuple[Any, Any]]] = None) -> dict[str, Any]:
    """One try: push the branch from the mirror and open the pull request. GitHub still down →
    the next try is queued with backoff; done → the run, its change set and its decision point
    at the real pull request."""
    from .core import emit_event

    with engine.begin() as c:
        row = c.execute(text("SELECT * FROM repo_push_pending WHERE id = :i FOR UPDATE"),
                        {"i": pending_id}).mappings().first()
        if row is None:
            return {"status": "failed", "retry": False, "error": "no such push"}
        row = dict(row)
        if row["status"] not in ("waiting", "pushing"):
            return {"status": "succeeded", "state": row["status"], "again": False}
        c.execute(text("UPDATE repo_push_pending SET status = 'pushing', attempts = attempts + 1, "
                       "updated_at = now() WHERE id = :i"), {"i": pending_id})
        adapter, access = (github or _default_github)(c, settings, row["project_id"], row["owner"],
                                                     row["repo"])
    owner, repo, branch = row["owner"], row["repo"], row["branch"]
    attempts = int(row["attempts"] or 0) + 1
    git_dir = local_repo.mirror_path(owner, repo, settings)
    try:
        _push_branch(git_dir, branch, access)
        with engine.begin() as c:
            cs = (c.execute(text("SELECT * FROM local_change_sets WHERE id = :i"),
                            {"i": row["change_set_id"]}).mappings().first()
                  if row.get("change_set_id") else None)
        title = str((cs or {}).get("title") or f"BOSS: {branch}")
        body = str((cs or {}).get("body") or "")
        note = ("\n\n> Built while GitHub was unreachable; BOSS kept the commits on this machine "
                "and pushed them when GitHub answered again.")
        pr = adapter.create_pull_request(branch, title, body + note, base=row["base_branch"])
    except Exception as error:  # noqa: BLE001 — an outage waits; anything else is said
        outage = is_outage(error)
        said = f"{type(error).__name__}: {error}"[-400:]
        with engine.begin() as c:
            if outage and attempts < MAX_ATTEMPTS:
                wait = backoff_minutes(attempts)
                c.execute(text(
                    "UPDATE repo_push_pending SET status = 'waiting', last_error = :e, "
                    "next_attempt_at = now() + make_interval(mins => :m), updated_at = now() "
                    "WHERE id = :i"), {"e": said, "m": wait, "i": pending_id})
                _enqueue(c, row["project_id"], pending_id, attempt=attempts + 1, delay_s=wait * 60)
                return {"status": "succeeded", "state": "waiting", "again": True,
                        "next_in_minutes": wait, "error": said}
            c.execute(text("UPDATE repo_push_pending SET status = 'failed', last_error = :e, "
                           "updated_at = now() WHERE id = :i"), {"e": said, "i": pending_id})
            words = (f"GitHub did not answer for {attempts} tries; the work is safe on this machine "
                     f"in {owner}/{repo}'s mirror, branch {branch}." if outage else
                     f"Pushing {branch} to {owner}/{repo} was refused: {said}")
            emit_event(c, row["project_id"], "repo.push_pending_failed", "boss",
                       "repo_push_pending", pending_id, {"words": words, "run_id": row["run_id"]})
            if row.get("run_id"):
                c.execute(text(
                    "UPDATE code_task_runs SET phase = 'push_failed', finalize_ctx = "
                    "COALESCE(finalize_ctx, '{}'::jsonb) || jsonb_build_object('github_pending_words', "
                    "CAST(:w AS text)), updated_at = now() WHERE id = :r"), {"w": words, "r": row["run_id"]})
        return {"status": "failed", "retry": False, "error": words}
    with engine.begin() as c:
        _point_at_github(c, row, pr.url, int(pr.number))
        c.execute(text("UPDATE repo_push_pending SET status = 'done', pull_request_url = :u, "
                       "last_error = NULL, updated_at = now() WHERE id = :i"),
                  {"u": pr.url, "i": pending_id})
        emit_event(c, row["project_id"], "repo.push_pending_done", "boss", "repo_push_pending",
                   pending_id, {"pull_request_url": pr.url, "run_id": row["run_id"],
                                "attempts": attempts})
    return {"status": "succeeded", "state": "done", "again": False, "pull_request_url": pr.url}


def _push_branch(git_dir: Any, branch: str, access: Any) -> None:
    """Push the mirror's branch. When GitHub's copy moved on during the outage (an earlier
    checkpoint that reached it), GitHub's commits are merged in first: nothing of either side is
    lost, and nothing is forced."""
    env = access.boss_environ() if hasattr(access, "boss_environ") else dict(access.env or {})
    remote = local_repo.git(["ls-remote", access.url, f"refs/heads/{branch}"], git_dir=git_dir,
                            env=env, timeout=120).split()
    theirs = remote[0] if remote else None
    if theirs and local_repo.merge_base(git_dir, theirs, branch) != theirs:
        tmp = f"boss-github-sync/{branch}"
        local_repo.git(["fetch", "--quiet", access.url, f"+refs/heads/{branch}:refs/heads/{tmp}"],
                       git_dir=git_dir, env=env, timeout=300)
        try:
            local_repo.merge(git_dir, tmp, branch, f"Merge GitHub's {branch} (pushed during an outage)")
        finally:
            local_repo.git(["update-ref", "-d", f"refs/heads/{tmp}"], git_dir=git_dir, check=False)
    local_repo.push(git_dir, access.url, [f"refs/heads/{branch}:refs/heads/{branch}"], env=env)


def _point_at_github(c: Any, row: Mapping[str, Any], url: str, number: int) -> None:
    """The run, its change set and its review decision now name the real pull request."""
    from .local_repo_host import change_set_url

    old_url = None
    if row.get("change_set_id"):
        cs = c.execute(text("SELECT repository, number FROM local_change_sets WHERE id = :i"),
                       {"i": row["change_set_id"]}).mappings().first()
        if cs:
            old_url = change_set_url(cs["repository"], cs["number"])
        c.execute(text("UPDATE local_change_sets SET state = 'published', pending_push = false, "
                       "github_pr_url = :u, github_pr_number = :n, updated_at = now() WHERE id = :i"),
                  {"u": url, "n": number, "i": row["change_set_id"]})
    repoint_pull_request(c, old_url=old_url, new_url=url, number=number, run_id=row.get("run_id"))


def repoint_pull_request(c: Any, *, old_url: Optional[str], new_url: str, number: int,
                         run_id: Optional[str]) -> None:
    """Every record that named the local change set names the GitHub pull request instead."""
    if run_id:
        c.execute(text(
            "UPDATE code_task_runs SET pull_request_url = :u, "
            "status = CASE WHEN status = :w THEN COALESCE(finalize_ctx->>'status_before_github_wait', "
            "'completed') ELSE status END, "
            "phase = CASE WHEN phase = :w THEN 'done' ELSE phase END, "
            "finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) - 'github_pending' "
            "|| jsonb_build_object('github_pushed_at', CAST(:t AS text)), updated_at = now() "
            "WHERE id = :r"),
            {"u": new_url, "w": WAITING_STATUS, "t": datetime.now(timezone.utc).isoformat(),
             "r": run_id})
    if old_url:
        c.execute(text(
            "UPDATE decisions SET recommendation = recommendation || jsonb_build_object("
            "'pull_request_url', CAST(:u AS text), 'pull_request_number', CAST(:n AS int)), "
            "updated_at = now() WHERE recommendation->>'pull_request_url' = :old"),
            {"u": new_url, "n": number, "old": old_url})


def execute_job(engine: Any, settings: Any, *, job_id: str, worker_id: str, lease_token: str
                ) -> dict[str, Any]:
    """The worker's leased `repo.push_pending` job."""
    from .jobs import JobLeaseService, LeaseNotFoundError

    with engine.begin() as c:
        try:
            job = JobLeaseService(settings).require_active_lease(c, worker_id, job_id, lease_token)
        except LeaseNotFoundError as exc:
            return {"status": "failed", "retry": False, "error": f"lease: {exc}"}
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else \
        json.loads(job.get("payload") or "{}")
    return attempt(engine, settings, str(payload.get("pending_id") or ""))


def waiting(c: Any, project_id: Optional[str] = None) -> list[dict[str, Any]]:
    """What is waiting on GitHub, and why — the Shepherd's stuck-work diagnosis reads this."""
    rows = c.execute(text(
        "SELECT id, project_id, owner, repo, branch, run_id, status, attempts, next_attempt_at, "
        "last_error, reason, created_at FROM repo_push_pending WHERE status IN ('waiting', "
        "'pushing', 'failed') AND (CAST(:p AS TEXT) IS NULL OR project_id = :p) "
        "ORDER BY created_at"), {"p": project_id}).mappings().all()
    out = []
    for r in rows:
        when = r["next_attempt_at"]
        out.append({
            **{k: r[k] for k in ("id", "project_id", "owner", "repo", "branch", "run_id", "status",
                                 "attempts")},
            "next_attempt_at": when.isoformat() if hasattr(when, "isoformat") else when,
            "words": (f"{r['branch']} of {r['owner']}/{r['repo']} is built and committed on this "
                      f"machine; it waits for GitHub to answer ({r['attempts']} tries so far"
                      + (f", last: {str(r['last_error'])[:160]}" if r["last_error"] else "")
                      + "). BOSS pushes it and opens the pull request on its own."
                      if r["status"] != "failed" else
                      f"{r['branch']} of {r['owner']}/{r['repo']} could not be pushed: "
                      f"{str(r['last_error'] or '')[:200]}"),
        })
    return out


def mark_run_waiting(c: Any, run_id: str, *, reason: str, pending_id: str) -> None:
    """The build ends "waiting for GitHub": its work is done and safe, the push is queued."""
    c.execute(text(
        "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) || "
        "jsonb_build_object('status_before_github_wait', status, 'github_pending', "
        "jsonb_build_object('pending_id', CAST(:pid AS text), 'reason', CAST(:why AS text), "
        "'words', CAST(:words AS text))), status = :w, phase = :w, updated_at = now() "
        "WHERE id = :r"),
        {"pid": pending_id, "why": reason[:300], "w": WAITING_STATUS, "r": run_id,
         "words": "Waiting for GitHub: the build finished and its commits are safe on this "
                  "machine; BOSS pushes them and opens the pull request as soon as GitHub "
                  "answers."})


__all__ = ["JOB_KIND", "ResilientGitHub", "WAITING_STATUS", "attempt", "backoff_minutes",
           "execute_job", "is_outage", "mark_run_waiting", "mirror_ready", "record_pending",
           "refresh_mirror", "repoint_pull_request", "waiting"]
