"""THE LOCAL GIT REMOTE'S HOOKS (BOSSAPP-948). git's receive-pack runs them during a push through
the API's `git http-backend` (local_git_http), from the image's hooks directory
(services/git_hooks, set by core.hooksPath in the environment — no clone can change them).

    pre-receive    the fence: refuses, in words, a push to the default branch ("main moves only
                   through BOSS's merge"), a tag, a force push, a delete, a ref outside
                   refs/heads (git notes refs/notes/boss-review excepted), and a boss/* branch
                   while a run is building it
    post-receive   each pushed branch becomes a change set (opened, or updated and its comments
                   re-anchored), its review is queued, and git notes on refs/notes/boss-review
                   become comments; it prints `remote: BOSS opened change #N <url>`

`reconcile` does the post-receive work for refs that moved any other way (the change list reads
it), so a change set never lags its branch.

    python -m boss_engine.services.local_git_hooks pre-receive|post-receive
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Iterable, Mapping, Optional

ZERO = "0" * 40
NOTES_REF = "refs/notes/boss-review"


def _say(words: str) -> None:
    sys.stderr.write(words.rstrip() + "\n")
    sys.stderr.flush()


def _is_ancestor(old: str, new: str) -> bool:
    done = subprocess.run(["git", "merge-base", "--is-ancestor", old, new], capture_output=True)
    return done.returncode == 0


def check_update(old: str, new: str, ref: str, *, default_branch: str, busy: Iterable[str],
                 is_ancestor=_is_ancestor) -> Optional[str]:
    """Why this ref update is refused, in words — or None when it may land."""
    if ref == NOTES_REF:
        return None
    if ref.startswith("refs/tags/"):
        return (f"{ref[len('refs/tags/'):]}: tags are not pushed here. BOSS tags a release when "
                "it ships one.")
    if not ref.startswith("refs/heads/"):
        return f"{ref}: only branches (refs/heads/*) are pushed to BOSS."
    branch = ref[len("refs/heads/"):]
    if branch == default_branch:
        return (f"{default_branch} moves only through BOSS's merge. Push a branch: it becomes a "
                "change you can review and merge.")
    if new == ZERO:
        return (f"{branch}: deleting a branch is not done by push. Close its change in BOSS "
                "instead.")
    if branch in set(busy):
        return (f"{branch}: a BOSS run is building this branch right now. Push again when it "
                "has finished (its change page says so).")
    if old != ZERO and not is_ancestor(old, new):
        return (f"{branch}: a force push would rewrite history BOSS has reviewed. Pull, merge or "
                "rebase onto it locally, and push a fast-forward (or a new branch).")
    return None


def pre_receive(lines: Iterable[str], env: Mapping[str, str]) -> int:
    default = env.get("BOSS_GIT_DEFAULT_BRANCH") or "main"
    try:
        busy = json.loads(env.get("BOSS_GIT_BUSY_BRANCHES") or "[]")
    except ValueError:
        busy = []
    refused = 0
    for line in lines:
        parts = line.split()
        if len(parts) != 3:
            continue
        why = check_update(parts[0], parts[1], parts[2], default_branch=default, busy=busy)
        if why:
            _say(why)
            refused += 1
    return 1 if refused else 0


# ── post-receive: a pushed branch becomes a change ────────────────────────────────────────────

def _subject(git_dir: Any, sha: str) -> str:
    from . import local_repo

    return local_repo.git(["log", "-1", "--format=%s", sha], git_dir=git_dir, check=False).strip()


def _change_url(console: str, project_id: str, number: int) -> str:
    base = (console or "http://localhost:3000").rstrip("/")
    return f"{base}/changes/{project_id}/{int(number)}"


def open_or_update(c: Any, settings: Any, *, project_id: str, repository: str, branch: str,
                   head: str, pusher: str, jobs: Any = None, queue_review: bool = True
                   ) -> dict[str, Any]:
    """The change set for a pushed branch: opened (author = the pusher, label `pushed`) or its
    head moved (comments re-anchored). Its review is queued (the review panel, in BOSS's sandbox,
    on this branch) unless one is already running for it."""
    from sqlalchemy import text

    from . import local_repo, local_review
    from .core import emit_event, make_id
    from .local_repo_host import change_set_url

    owner, _, name = repository.partition("/")
    git_dir = local_repo.bare_path(owner, name, settings)
    default = local_repo.default_branch(git_dir)
    c.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
              {"k": f"local_change_sets:{repository}"})
    existing = c.execute(text(
        "SELECT * FROM local_change_sets WHERE repository = :r AND branch = :b AND state = 'open' "
        "ORDER BY number DESC LIMIT 1"), {"r": repository, "b": branch}).mappings().first()
    opened = existing is None
    if existing is None:
        number = int(c.execute(text(
            "SELECT COALESCE(MAX(number), 0) + 1 FROM local_change_sets WHERE repository = :r"),
            {"r": repository}).scalar() or 1)
        cs_id = make_id("lcs")
        c.execute(text(
            "INSERT INTO local_change_sets (id, project_id, repository, number, branch, base_branch, "
            "title, body, head_sha, base_sha, labels, author) VALUES (:id, :p, :r, :n, :b, :base, "
            ":t, '', :h, :bs, CAST(:l AS JSONB), :a)"),
            {"id": cs_id, "p": project_id, "r": repository, "n": number, "b": branch,
             "base": default, "t": (_subject(git_dir, head) or branch)[:500], "h": head,
             "bs": local_repo.branch_sha(git_dir, default), "l": json.dumps(["pushed"]),
             "a": pusher})
        row = local_review.change_by_id(c, cs_id)
    else:
        row = dict(existing)
        c.execute(text("UPDATE local_change_sets SET head_sha = :h, updated_at = now(), "
                       "labels = CASE WHEN labels @> '[\"pushed\"]'::jsonb THEN labels "
                       "ELSE labels || '[\"pushed\"]'::jsonb END WHERE id = :i"),
                  {"h": head, "i": row["id"]})
        local_review.remap(c, settings, row, head)
        row = local_review.change_by_id(c, row["id"])
    emit_event(c, project_id, "local_repo.branch_pushed", pusher, "local_change_set", row["id"],
               {"number": int(row["number"]), "branch": branch, "head_sha": head,
                "opened": opened})
    review_run = None
    if queue_review and jobs is not None:
        review_run = queue_review_of(c, settings, change=row, pusher=pusher, jobs=jobs)
    return {"number": int(row["number"]), "opened": opened, "change_set_id": row["id"],
            "url": change_set_url(repository, row["number"]), "review_run_id": review_run}


def queue_review_of(c: Any, settings: Any, *, change: Mapping[str, Any], pusher: str,
                    jobs: Any, raise_refusals: bool = False) -> Optional[str]:
    """The review panel for a pushed change: a build pinned to the pushed branch whose job is to
    check and review it (BOSS's sandbox checks, the review panel, the review decision)."""
    from sqlalchemy import text

    from . import code_task_service
    from .core import emit_event

    owner, _, repo = str(change["repository"]).partition("/")
    try:
        with c.begin_nested():
            run_id = code_task_service.start_code_task(
                c, project_id=change["project_id"], task_id=None,
                title=f"Review change #{change['number']}: {change.get('title') or change['branch']}"[:160],
                instructions=(
                    f"REVIEW A CHANGE A PERSON PUSHED. {pusher} pushed branch {change['branch']} "
                    "to BOSS's local repository; it is change "
                    f"#{change['number']} (“{change.get('title') or change['branch']}”). Its code is "
                    "already on this branch. Do not rewrite it or add features: install, build and "
                    "run its tests, fix only what fails, and let the review panel review it."),
                acceptance_criteria=["The change builds and its tests pass in BOSS's sandbox.",
                                     "The review panel reviewed the change."],
                test_commands=[], owner=owner, repo=repo, base_branch=change["branch"],
                finalize_ctx={"local_change_branch": change["branch"],
                              "local_change_set_id": change["id"], "local_review_of_push": True,
                              "requested_by": pusher},
                jobs=jobs)
        c.execute(text(
            "UPDATE local_change_sets SET run_id = :r, updated_at = now() WHERE id = :i"),
            {"r": run_id, "i": change["id"]})
        emit_event(c, change["project_id"], "local_repo.review_queued", pusher,
                   "local_change_set", change["id"], {"number": int(change["number"]),
                                                     "run_id": run_id})
        return run_id
    except Exception as error:  # noqa: BLE001 — the change set stands; the review waits for a press
        if raise_refusals:
            raise
        emit_event(c, change["project_id"], "local_repo.review_not_queued", pusher,
                   "local_change_set", change["id"],
                   {"number": int(change["number"]),
                    "reason": f"{type(error).__name__}: {str(error)[:300]}"})
        return None


def post_receive(lines: Iterable[str], env: Mapping[str, str], *, settings: Any = None,
                 engine: Any = None, jobs: Any = None) -> int:
    from ..config import get_settings
    from ..db import get_engine
    from . import local_review
    from .jobs import JobLeaseService

    settings = settings or get_settings()
    engine = engine or get_engine(settings)
    jobs = jobs or JobLeaseService(settings)
    project_id = env.get("BOSS_GIT_PROJECT") or ""
    repository = env.get("BOSS_GIT_REPOSITORY") or ""
    default = env.get("BOSS_GIT_DEFAULT_BRANCH") or "main"
    pusher = env.get("BOSS_GIT_PUSHER") or "someone"
    console = env.get("BOSS_GIT_CONSOLE") or ""
    if not project_id or not repository:
        _say("BOSS could not tell which project this push is for; nothing was opened.")
        return 0
    # The API queues each change's review itself, as the pusher (local_git._serve): this process
    # holds nobody's identity. It is told which changes were touched through this file.
    queue = env.get("BOSS_GIT_QUEUE_REVIEW", "1") != "0"
    updates = [line.split() for line in lines if len(line.split()) == 3]
    notes_pushed = any(ref == NOTES_REF for _o, _n, ref in updates)
    touched: list[dict[str, Any]] = []
    for old, new, ref in updates:
        if not ref.startswith("refs/heads/") or new == ZERO:
            continue
        branch = ref[len("refs/heads/"):]
        if branch == default:
            continue
        try:
            with engine.begin() as c:
                out = open_or_update(c, settings, project_id=project_id, repository=repository,
                                     branch=branch, head=new, pusher=pusher, jobs=jobs,
                                     queue_review=queue)
            touched.append(out)
            verb = "opened" if out["opened"] else "updated"
            _say(f"BOSS {verb} change #{out['number']}  {branch} → {default}")
            _say(f"  {_change_url(console, project_id, out['number'])}")
            _say("  the review panel checks it next")
        except Exception as error:  # noqa: BLE001 — the push landed; say what did not
            _say(f"BOSS could not open a change for {branch}: {type(error).__name__}: "
                 f"{str(error)[:200]}. The branch is pushed; the change list will pick it up.")
    touched_file = env.get("BOSS_GIT_TOUCHED_FILE")
    if touched_file:
        with open(touched_file, "w", encoding="utf-8") as fh:
            json.dump([t["change_set_id"] for t in touched], fh)
    if notes_pushed:
        made = 0
        with engine.begin() as c:
            from sqlalchemy import text as _t

            for row in c.execute(_t(
                    "SELECT * FROM local_change_sets WHERE repository = :r AND state = 'open'"),
                    {"r": repository}).mappings().all():
                made += local_review.import_notes(c, settings, dict(row), author=pusher)
        _say(f"BOSS read {made} comment{'s' if made != 1 else ''} from git notes "
             f"({NOTES_REF}).")
    return 0


def reconcile(c: Any, settings: Any, project_id: str, *, jobs: Any = None) -> list[dict[str, Any]]:
    """Branches that moved without a push through BOSS (or before its hooks ran): an open change
    set follows its branch's head; a person's branch with commits ahead and no change becomes
    one. boss/* branches a run made get their change set from the run, never from here."""
    from sqlalchemy import text

    from . import local_repo, local_review
    from .local_projects import local_binding

    binding = local_binding(c, project_id, include_mirror=False)
    if binding is None:
        return []
    git_dir = local_repo.bare_path(binding["owner"], binding["name"], settings)
    if not (git_dir / "HEAD").is_file():
        return []
    default = local_repo.default_branch(git_dir)
    tip = local_repo.branch_sha(git_dir, default)
    out = []
    open_sets = {r["branch"]: dict(r) for r in c.execute(text(
        "SELECT * FROM local_change_sets WHERE repository = :r AND state = 'open'"),
        {"r": binding["full_name"]}).mappings()}
    for branch in local_repo.branches(git_dir):
        if branch == default:
            continue
        head = local_repo.branch_sha(git_dir, branch) or ""
        row = open_sets.get(branch)
        if row is not None:
            if head and row.get("head_sha") != head:
                c.execute(text("UPDATE local_change_sets SET head_sha = :h, updated_at = now() "
                               "WHERE id = :i"), {"h": head, "i": row["id"]})
                local_review.remap(c, settings, row, head)
                out.append({"branch": branch, "number": row["number"], "moved": True})
            continue
        if branch.startswith("boss/") or not head or not tip:
            continue
        if local_repo.merge_base(git_dir, head, tip) == head:
            continue                                     # nothing ahead of the default branch
        seen = c.execute(text(
            "SELECT 1 FROM local_change_sets WHERE repository = :r AND branch = :b AND "
            "(head_sha = :h OR merged_sha = :h)"),
            {"r": binding["full_name"], "b": branch, "h": head}).first()
        if seen:
            continue
        opened = open_or_update(c, settings, project_id=project_id,
                                repository=binding["full_name"], branch=branch, head=head,
                                pusher="git", jobs=jobs, queue_review=jobs is not None)
        out.append({"branch": branch, **opened})
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("pre-receive", "post-receive"):
        _say("usage: python -m boss_engine.services.local_git_hooks pre-receive|post-receive")
        return 2
    lines = sys.stdin.read().splitlines()
    if argv[1] == "pre-receive":
        return pre_receive(lines, os.environ)
    try:
        return post_receive(lines, os.environ)
    except Exception as error:  # noqa: BLE001 — post-receive cannot undo the push; it says so
        _say(f"BOSS could not record this push: {type(error).__name__}: {str(error)[:200]}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
