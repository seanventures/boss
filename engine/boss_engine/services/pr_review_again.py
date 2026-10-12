"""Review this PR again: the adversarial review, run on an open pull request's REAL diff.

For a pull request whose review was hollow (PR #223: "no changes relative to HEAD, nothing to
review" on 77 files), or one a person wants looked at again. It reads the pull request's
changed files from GitHub at its head commit — the change as a human reviewer sees it, never a
working tree against HEAD — builds the reviewer's input with the one builder every seat uses
(review_input.build), runs the adversarial reviewer with the pipeline's own prompt, asks again
once if the review did not see the change, records the findings on the build as a new review
round, and posts that round to the pull request (pr_review_post) with the description
regenerated from the records.

Two halves, like every long job in BOSS: `request` (API, MCP tool, console button) records the
ask and queues a `pull_request.review_again` job; `execute` (the API, under the worker's lease)
does the work. A request while one is already queued or running returns that one.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Optional

from sqlalchemy import text

from . import review_comments, review_input

log = logging.getLogger(__name__)

JOB_KIND = "pull_request.review_again"
#: Contents read from GitHub for at most this many files; every changed path is still listed.
MAX_FILES_READ = 150
_PR_URL = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")


class ReviewAgainRefused(Exception):
    """Not possible for this build, in words (no pull request, not in this project)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_row(connection, project_id: str, run_id: Optional[str],
             number: Optional[int]) -> dict:
    if run_id:
        row = connection.execute(text(
            "SELECT id, project_id, title, spec, state, finalize_ctx, pull_request_url, "
            "pull_request_number FROM code_task_runs WHERE id=:id AND project_id=:p"),
            {"id": run_id, "p": project_id}).mappings().first()
    else:
        row = connection.execute(text(
            "SELECT id, project_id, title, spec, state, finalize_ctx, pull_request_url, "
            "pull_request_number FROM code_task_runs WHERE project_id=:p "
            "AND pull_request_number=:n ORDER BY created_at DESC LIMIT 1"),
            {"p": project_id, "n": int(number or 0)}).mappings().first()
    if row is None:
        raise ReviewAgainRefused(
            f"no build {run_id} in this project" if run_id
            else f"no build in this project opened pull request #{number}")
    if not row["pull_request_url"] or not row["pull_request_number"]:
        raise ReviewAgainRefused("this build has no pull request to review")
    return dict(row)


def _j(value: Any) -> dict:
    if isinstance(value, str):
        try:
            return json.loads(value) or {}
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def request(connection, *, project_id: str, actor: str, run_id: Optional[str] = None,
            pull_request_number: Optional[int] = None) -> dict:
    """Queue a review of the build's pull request. Idempotent while one is in flight."""
    from .core import emit_event
    from .jobs import enqueue_job

    row = _run_row(connection, project_id, run_id, pull_request_number)
    fctx = _j(row["finalize_ctx"])
    current = fctx.get("pr_review_again") if isinstance(fctx.get("pr_review_again"), dict) else {}
    if current.get("status") in ("queued", "running"):
        return {**current, "run_id": row["id"], "already": True}
    job = enqueue_job(connection, project_id, JOB_KIND,
                      {"run_id": row["id"], "pull_request_number": int(row["pull_request_number"]),
                       "requested_by": actor},
                      dedupe_key=f"pr-review-again:{row['id']}:{_now()[:16]}", max_attempts=1)
    record = {"status": "queued", "job_id": job.get("id"), "requested_by": actor,
              "requested_at": _now(), "pull_request_number": int(row["pull_request_number"])}
    connection.execute(text(
        "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx,'{}'::jsonb) "
        "|| jsonb_build_object('pr_review_again', CAST(:r AS JSONB)), updated_at=now() "
        "WHERE id=:id"), {"r": json.dumps(record), "id": row["id"]})
    emit_event(connection, project_id, "code_task.pr_review_again_requested", actor,
               "code_task_run", row["id"],
               {"pull_request_number": int(row["pull_request_number"]), "job_id": job.get("id")})
    return {**record, "run_id": row["id"]}


def _set(engine, run_id: str, record: dict) -> None:
    from .code_task_service import _transaction

    with _transaction(engine) as c:
        c.execute(text(
            "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx,'{}'::jsonb) "
            "|| jsonb_build_object('pr_review_again', CAST(:r AS JSONB)), updated_at=now() "
            "WHERE id=:id"), {"r": json.dumps(record), "id": run_id})


def change_from_pull_request(github: Any, number: int, head_sha: str) -> review_input.ChangeUnderReview:
    """The pull request's change as the reviewer judges it: every file it touches, and the text
    at its head commit of those BOSS reads (removed and binary files are listed, not read)."""
    from .governed_code_task_service import workspace_keepable

    files = github.pull_request_files(number)
    paths = [f["path"] for f in files if f.get("path")]
    readable = [f["path"] for f in files if f.get("path") and f.get("status") != "removed"
                and workspace_keepable(f["path"])]
    budget = MAX_FILES_READ

    def read(path: str) -> Optional[str]:
        nonlocal budget
        if path not in readable or budget <= 0:
            return None
        budget -= 1
        try:
            return github.read_text_file(head_sha, path)
        except Exception:  # noqa: BLE001 — binary or unreadable: listed, not read
            return None
    return review_input.build({}, paths, read=read, keep=workspace_keepable,
                              measured=f"the pull request's base, at its head commit "
                                       f"{head_sha[:7]}")


def execute(engine, settings, payload: dict, *, github: Any = None, agent: Any = None) -> dict:
    """Run one queued review. `github` / `agent` are injected by tests; production resolves
    the project's GitHub connection and its review lane exactly as a build does."""
    from .code_task_service import _engine_ports, _transaction
    from .core import emit_event
    from . import governed_code_task_service as gov
    from .llm_telemetry import set_llm_context

    run_id = str(payload.get("run_id") or "")
    with _transaction(engine) as c:
        row = c.execute(text(
            "SELECT id, project_id FROM code_task_runs WHERE id=:id"), {"id": run_id}).first()
    if row is None:
        return {"status": "failed", "error": f"no build {run_id}"}
    project_id = str(row[1])
    with _transaction(engine) as c:
        run = _run_row(c, project_id, run_id, None)
    number = int(run["pull_request_number"])
    record = {**(_j(run["finalize_ctx"]).get("pr_review_again") or {}), "status": "running",
              "started_at": _now()}
    _set(engine, run_id, record)
    spec = _j(run["spec"])
    evidence = _j(run["state"])
    m = _PR_URL.search(str(run["pull_request_url"] or ""))
    owner, repo = (m.group(1), m.group(2)) if m else (spec.get("owner"), spec.get("repo"))
    reservation_id = None
    workdir: Optional[Path] = None
    ports = _engine_ports()
    try:
        if github is None:
            from .software_factory import SoftwareFactoryService

            sf = SoftwareFactoryService(settings, owner=owner, repo=repo, ports=ports)
            with _transaction(engine) as c:
                sf.bind_project_github(c, project_id, owner=owner, repo=repo,
                                       selector=f"{owner}/{repo}")
            github = sf.github
        meta = github.pull_request_meta(number)
        change = change_from_pull_request(github, number, meta["head_sha"])
        if change.empty:
            raise ReviewAgainRefused(f"pull request #{number} changes no files")
        # A READ-ONLY TREE of the changed files, so the reviewer can open any of them.
        workdir = Path(tempfile.mkdtemp(prefix=f"review-again-{run_id}-"))
        for path, content in change.files.items():
            target = (workdir / path).resolve()
            if workdir.resolve() not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        if agent is None:
            from .code_agent import make_code_agent
            from .project_settings import applied

            with _transaction(engine) as c:
                eff = applied(c, project_id, settings)
                reserved = ports.budget.reserve(
                    c, project_id, category="code_task",
                    amount_usd_micros=int(getattr(eff, "code_task_budget_usd_micros", 0)
                                          or 5_000_000),
                    reason=f"review pull request #{number} again", expires_in_minutes=60,
                    risk_tier=spec.get("risk_tier"))
                if reserved.get("status") != "reserved":
                    raise ReviewAgainRefused(
                        "the budget refused the review: "
                        + str(reserved.get("reason") or reserved.get("refusal") or "denied"))
                reservation_id = reserved["reservation_id"]
                lane = (eff.code_review_engine or eff.code_agent_engine or "claude_code").lower()
                agent = make_code_agent(eff, engine=lane, connection=c, project_id=project_id)
        set_llm_context(db_engine=engine, project_id=project_id, run_id=run_id,
                        stage="review_code")
        prompt = gov._review_prompt(meta.get("title") or run["title"],
                                    spec.get("acceptance_criteria", []),
                                    instructions=spec.get("instructions", ""),
                                    change=change.block())
        rr = agent.run_role(prompt, workdir, read_only=True) or {}
        hollow = review_input.hollow_reason(rr.get("summary", ""), change.paths)
        if hollow:
            first = int(rr.get("cost_usd_micros") or 0)
            rr = agent.run_role(review_input.retry_preface(hollow, change) + prompt, workdir,
                                read_only=True) or {}
            rr["cost_usd_micros"] = int(rr.get("cost_usd_micros") or 0) + first
            hollow = review_input.hollow_reason(rr.get("summary", ""), change.paths)
        summary = rr.get("summary", "")
        verdict = "not_run" if hollow else gov._parse_verdict(summary)
        findings = review_comments.parse_findings(summary, stage="review_code")
        # A NEW ROUND on the build: after every round it already has.
        rounds, _, _ = gov._review_record(engine, run_id, project_id)
        next_round = max([int(r.get("round") or 0) for r in rounds if int(r.get("round") or 0)
                          < 100] + [-1]) + 1
        if not rounds and evidence.get("review_summary"):
            from .pr_review_post import legacy_round

            # The build's own first review, judged against the change it was meant to see: PR
            # #223's "nothing to review" is recorded as hollow, never replayed as a pass.
            gov._record_review_round(engine, run_id, project_id, round_i=0,
                                     seats=legacy_round(evidence, change.paths)["seats"])
            next_round = max(next_round, 1)
        gov._record_review_comments(engine, run_id, project_id,
                                    {"review_summary": summary, "security_summary": "",
                                     "consistency_summary": ""}, round_i=next_round)
        gov._record_review_round(engine, run_id, project_id, round_i=next_round,
                                 kind="review_again", seats={
                                     "review_code": {"verdict": verdict, "summary": summary,
                                                     "hollow": hollow}})

        class _SF:  # the poster reads `.github`, as on the pipeline's own factory
            pass
        sf_like = _SF()
        sf_like.github = github
        posted = gov._post_review_comments_to_pr(
            sf_like, {"pull_request_number": number}, evidence, engine, project_id, run_id)
        # THE DESCRIPTION, regenerated from the records (never agent chat).
        with _transaction(engine) as c:
            gate = c.execute(text(
                "SELECT id, gates_passed, gates_failed FROM quality_gates "
                "WHERE code_task_run_id=:r ORDER BY created_at DESC LIMIT 1"),
                {"r": run_id}).mappings().first()
        gates = {}
        if gate:
            gates.update({str(g): True for g in _jl(gate["gates_passed"])})
            gates.update({str(g): False for g in _jl(gate["gates_failed"])})
        described = False
        try:
            body = gov._generated_pr_summary(
                engine, settings, run_id, project_id, title=meta.get("title") or run["title"],
                evidence=evidence, gates=gates, paths=change.paths,
                qg_id=str((gate or {}).get("id") or ""))
            github.update_pull_request_body(number, body)
            described = True
        except Exception as exc:  # noqa: BLE001 — the review stands without a new description
            log.warning("pr_review_again: description not updated for #%s: %s", number, exc)
        record = {**record, "status": "done", "finished_at": _now(), "verdict": verdict,
                  "hollow": hollow, "round": next_round, "findings": len(findings),
                  "changed_files": len(change.paths), "posted": bool(posted.get("posted")),
                  "description_updated": described,
                  **({"review_url": posted["review_url"]} if posted.get("review_url") else {}),
                  **({"reason": posted["reason"]} if posted.get("reason") else {}),
                  "cost_usd_micros": int(rr.get("cost_usd_micros") or 0)}
        _set(engine, run_id, record)
        with _transaction(engine) as c:
            emit_event(c, project_id, "code_task.pr_review_again_done", "boss",
                       "code_task_run", run_id,
                       {k: v for k, v in record.items() if k not in ("requested_by",)})
        return record
    except Exception as exc:  # noqa: BLE001 — the result is a sentence on the build page
        log.warning("pr_review_again failed for %s", run_id, exc_info=True)
        words = str(exc).strip() or type(exc).__name__
        from .pr_review_post import permission_words

        words = permission_words(exc, f"{owner}/{repo}") or words
        record = {**record, "status": "failed", "finished_at": _now(), "reason": words[:400]}
        _set(engine, run_id, record)
        with _transaction(engine) as c:
            emit_event(c, project_id, "code_task.pr_review_again_failed", "boss",
                       "code_task_run", run_id, {"reason": words[:400]})
        return record
    finally:
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)
        if reservation_id:
            try:
                with _transaction(engine) as c:
                    ports.budget.release(c, project_id, reservation_id,
                                         "review again finished; its model calls are metered "
                                         "on the build")
            except Exception:  # noqa: BLE001
                log.warning("pr_review_again: reservation %s not released", reservation_id)


def _jl(value: Any) -> list:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return list(value or [])
