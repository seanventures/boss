"""A build that passed its gates and then failed to reach GitHub retries ONLY the push and the PR.

GitHub has intermittent errors. A failed clone is retried in place (transient_retry). A push or a
pull request that still fails after its in-place retries, at the very end of a finished, gated
build, used to block the run ("PR not created after 3 attempts") or crash into a full automatic
rebuild: new design, tests, implementation and review for work that was already done.

Now the governed pipeline keeps what to deliver on the run (hold_delivery: the diff, the PR
summary, the gate verdict, the lineage branch) and fails it `recoverable`, naming the operation
("GitHub returned 502 while pushing the work and opening the pull request ... Retry pushes and
opens the pull request only"). Any Retry of that run — the console's, a workflow step's, BOSS's
own — runs redeliver_code_task: the push and the PR, on the same run, and nothing else.
"""
from __future__ import annotations

import inspect
import json
import types

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.identity import lineage
from boss_engine.services import code_task_service as cts
from boss_engine.services import governed_code_task_service as governed

from .test_a_resumed_build_runs_as_the_person import _Jobs, _ports  # noqa: F401
from .test_an_automatic_retry_runs_under_the_persons_grant_or_not_at_all import WORKER_KEY, _workflow_build
from .test_identity_agents_workflow_consent import env  # noqa: F401  (full-schema fixture)
from .test_no_step_is_blocked_by_consent import staffed  # noqa: F401

FILES = [{"path": "helpdesk-ai/src/server.ts", "content": "export {}\n"},
         {"path": "helpdesk-ai/tests/tickets.test.ts", "content": "test('x', () => {})\n"}]


@pytest.fixture()
def github(monkeypatch):
    """The software factory's PR effect, answering what the test says; every call recorded."""
    calls: list[dict] = []
    answers: list[dict] = []

    class Factory:
        def __init__(self, settings, ports=None):
            pass

        def propose_pull_request(self, connection, project_id, task_id, **kw):
            calls.append(kw)
            return answers.pop(0)

    budget = types.SimpleNamespace(reserve=lambda *a, **k: {"status": "reserved", "reservation_id": "res_1"})
    monkeypatch.setattr(cts, "SoftwareFactoryService", Factory)
    monkeypatch.setattr(cts, "_engine_ports", lambda: types.SimpleNamespace(
        budget=budget, persistence=types.SimpleNamespace(transaction=lambda e: e.begin(),
                                                         list_runs=lambda *a, **k: [])))
    return types.SimpleNamespace(calls=calls, answers=answers)


def _held(env, *, running=True):
    """A gated build whose delivery failed, held by the pipeline's own hold_delivery."""
    _, crun, _ = _workflow_build(env, error=None)
    with env.engine.begin() as c:
        c.execute(text("UPDATE code_task_runs SET status='running', phase='opening_pr', "
                       "finalize_ctx = finalize_ctx || CAST(:f AS JSONB) WHERE id=:i"),
                  {"i": crun, "f": json.dumps({"completed_stages": ["design", "write_tests", "implement",
                                                                   "review"], "root_run_id": crun})})
        out = governed.hold_delivery(
            c, crun, env.project, files=FILES, summary="HelpDesk AI: tickets + login",
            quality_gate_id="qg_1", branch=f"boss/code/{crun}",
            detail="Server error '502 Bad Gateway' for url 'https://api.github.com/repos/acme/web/contents/x'")
    return crun, out


def _row(env, crun):
    with env.engine.begin() as c:
        return c.execute(text("SELECT status, phase, error, pull_request_url, finalize_ctx FROM code_task_runs "
                              "WHERE id=:i"), {"i": crun}).mappings().one()


def _attempts(env, crun) -> tuple[int, int]:
    with env.engine.begin() as c:
        children = c.execute(text("SELECT count(*) FROM code_task_runs WHERE finalize_ctx->>'parent_run_id'=:r"),
                             {"r": crun}).scalar()
        jobs = c.execute(text("SELECT count(*) FROM jobs WHERE kind='code.task' AND payload->>'run_id'=:r"),
                         {"r": crun}).scalar()
    return children, jobs


def test_a_held_delivery_names_the_failed_operation_and_keeps_what_to_deliver(env, staffed):  # noqa: F811
    crun, out = _held(env)
    row = _row(env, crun)
    assert out["delivery_held"] and row["status"] == "failed"
    assert row["error"].startswith("GitHub returned 502 while pushing the work and opening the pull request")
    assert "Retry pushes and opens the pull request only" in row["error"]
    held = row["finalize_ctx"]["pending_delivery"]
    assert held["files"] == FILES and held["quality_gate_id"] == "qg_1" and held["branch"] == f"boss/code/{crun}"
    assert row["finalize_ctx"]["failure_class"] == "recoverable"


def test_retry_after_a_push_failure_re_runs_only_the_push(env, staffed, github):  # noqa: F811
    crun, _ = _held(env)
    before = _attempts(env, crun)
    github.answers.append({"status": "created", "pull_request_url": "https://github.com/acme/web/pull/7",
                           "pull_request_number": 7, "decision_id": "dec_1"})
    with lineage.acting(WORKER_KEY):
        out = cts.retry_code_task(env.engine, get_settings(), _Jobs(), crun, fix_note="")
    # ONE delivery, of exactly the kept files to the lineage branch, with the gate verdict.
    assert len(github.calls) == 1
    call = github.calls[0]
    assert [(f.path, f.content) for f in call["files"]] == [(f["path"], f["content"]) for f in FILES]
    assert call["branch_name"] == f"boss/code/{crun}" and call["quality_gate_id"] == "qg_1"
    # The same run completes; no new attempt, no new job: no design, tests, build or review.
    assert out["delivery_only"] and out["run_id"] == crun and out["status"] == "completed"
    assert _attempts(env, crun) == before
    row = _row(env, crun)
    assert row["status"] == "completed" and row["pull_request_url"].endswith("/pull/7")
    assert "pending_delivery" not in row["finalize_ctx"]


def test_a_delivery_that_fails_again_stays_held_and_says_so(env, staffed, github):  # noqa: F811
    crun, _ = _held(env)
    github.answers.append({"status": "failed", "message": "Server error '503 Service Unavailable'"})
    out = cts.retry_code_task(env.engine, get_settings(), _Jobs(), crun)
    row = _row(env, crun)
    assert out["status"] == "failed" and out["delivery_only"]
    assert row["status"] == "failed" and row["error"].startswith("GitHub returned 503 while pushing")
    assert row["finalize_ctx"]["pending_delivery"]["files"] == FILES
    assert _attempts(env, crun)[0] == 0


def test_boss_retries_a_held_delivery_by_itself_a_bounded_number_of_times(env, staffed, github):  # noqa: F811
    crun, _ = _held(env)
    for _ in range(cts.AUTO_DELIVERY_RETRIES):
        github.answers.append({"status": "failed", "message": "502 Bad Gateway"})
        with lineage.acting(WORKER_KEY):
            assert cts.retry_code_task(env.engine, get_settings(), _Jobs(), crun,
                                       automatic=True)["delivery_only"]
    with pytest.raises(cts.RetryNeedsPerson, match="already retried this delivery"):
        with lineage.acting(WORKER_KEY):
            cts.retry_code_task(env.engine, get_settings(), _Jobs(), crun, automatic=True)
    assert len(github.calls) == cts.AUTO_DELIVERY_RETRIES and _attempts(env, crun)[0] == 0


def test_the_pipeline_holds_a_failed_delivery_instead_of_blocking_or_rebuilding():
    src = inspect.getsource(governed)
    held = src.index("return hold_delivery(")
    assert held < src.index('rescued = _rescue_pr(f"the pull-request step failed 3 times')
    assert 'if status not in ("created", "succeeded", "blocked", "reused"):' in src
    # The infra retry hands a delivery-only result straight back (same run, no new attempt).
    assert 'if retried.get("delivery_only"):' in inspect.getsource(governed._retry_after_infra_failure)


@pytest.mark.parametrize("detail,said", [
    ("Server error '502 Bad Gateway' for url", "GitHub returned 502 while pushing"),
    ("ReadTimeout: timed out", "GitHub did not answer in time while pushing"),
    ("ConnectError: [Errno 54] Connection reset by peer", "BOSS could not reach GitHub while pushing"),
])
def test_the_words_name_the_operation_and_what_retry_does(detail, said):
    words = governed.delivery_failure_words(detail)
    assert words.startswith(said) and words.endswith("Retry pushes and opens the pull request only.")
