"""A stack apply commits its governance first and writes the host with no transaction open.

A deploy takes minutes. While the host is mid-install the ChangeSet is already `applying` and
its receipts already exist — committed, visible to anyone — so a crash leaves a record of what
was being written. A second caller (a restarted worker replaying the job, or another deployment
to the same directory) neither hangs on a lock the apply holds nor fails a deploy that is still
running: it hands its job back to the queue, without spending an attempt, and asks again later.
"""
from __future__ import annotations

import threading

from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.tests.test_a_stack_deploys_only_through_an_approved_changeset import (  # noqa: F401
    HOME,
    _plan,
    stack,
)
from boss_engine.services import stack_deploy
from boss_engine.services.jobs import JobLeaseService


def _approve_and_lease(client, engine, deployment, worker="w1"):
    approved = client.post(f"/v1/decisions/{deployment['decision_id']}/approve",
                           json={"actor": "ceo"})
    assert approved.status_code == 200, approved.text
    return _lease(engine, deployment, worker)


def _lease(engine, deployment, worker="w1"):
    with engine.begin() as connection:
        job = connection.execute(
            text("SELECT id FROM jobs WHERE kind = 'deployment.stack_apply' "
                 "AND payload->>'deployment_id' = :d"), {"d": deployment["id"]}).scalar_one()
        lease = JobLeaseService(api_main.settings).lease_owned_job(
            connection, project_id=HOME, job_id=job, worker_id=worker, lease_ttl_seconds=600)
    return {"deployment_id": deployment["id"], "job_id": job, "worker_id": worker,
            "lease_token": lease["lease_token"]}


def _execute(engine, lease):
    return stack_deploy.execute_stack_job(engine, api_main.settings, **lease)


def _hold_install(host):
    """Make the host's install step block until released; return (entered, release)."""
    entered, release = threading.Event(), threading.Event()
    real = host.stack_exec

    def slow(**kwargs):
        if "./install.sh" in kwargs["command"]:
            entered.set()
            assert release.wait(60), "the test never released the install"
        return real(**kwargs)

    host.stack_exec = slow
    return entered, release


def _in_thread(fn):
    box: dict = {}
    thread = threading.Thread(target=lambda: box.update(result=fn()), daemon=True)
    thread.start()
    return thread, box


def _job(engine, job_id):
    with engine.begin() as connection:
        return connection.execute(text("SELECT status, attempt FROM jobs WHERE id = :j"),
                                  {"j": job_id}).mappings().one()


def test_the_changeset_and_receipts_are_committed_before_the_host_is_written(
        client, engine, stack):
    entered, release = _hold_install(stack["host"])
    deployment = _plan(client, stack)
    lease = _approve_and_lease(client, engine, deployment)
    thread, box = _in_thread(lambda: _execute(engine, lease))
    try:
        assert entered.wait(30), "the apply never reached the install step"
        # Mid-install, from ANOTHER connection: governance is already durable, not pending in
        # an open transaction that a crash would roll back.
        with engine.begin() as connection:
            changeset = connection.execute(text("SELECT status FROM changesets WHERE id = :c"),
                                           {"c": deployment["changeset_id"]}).scalar_one()
            receipt = connection.execute(
                text("SELECT status FROM resource_receipts WHERE changeset_id = :c"),
                {"c": deployment["changeset_id"]}).scalar_one()
            running = connection.execute(
                text("SELECT apply_started_at IS NOT NULL FROM stack_deployments "
                     "WHERE id = :d"), {"d": deployment["id"]}).scalar_one()
        assert changeset == "applying"
        assert receipt == "in_progress"
        assert running
        progress = client.get(f"/v1/deployments/stack/{deployment['id']}").json()["deployment"]
        assert any(s["status"] == "running" for s in progress["steps"]), progress["steps"]
    finally:
        release.set()
        thread.join(60)
    assert box["result"]["status"] == "healthy", box
    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT status FROM resource_receipts WHERE changeset_id = :c"),
            {"c": deployment["changeset_id"]}).scalar_one() == "succeeded"
        assert connection.execute(text("SELECT status FROM changesets WHERE id = :c"),
                                  {"c": deployment["changeset_id"]}).scalar_one() == "applied"


def test_a_replay_while_the_apply_is_still_running_waits_instead_of_failing_or_hanging(
        client, engine, stack):
    entered, release = _hold_install(stack["host"])
    deployment = _plan(client, stack)
    lease = _approve_and_lease(client, engine, deployment)
    first, first_box = _in_thread(lambda: _execute(engine, lease))
    try:
        assert entered.wait(30)
        # A restarted worker replays the job while the API is still installing.
        replay, replay_box = _in_thread(lambda: _execute(engine, lease))
        replay.join(30)
        assert not replay.is_alive(), "the replay hung on a lock the running apply holds"
        result = replay_box["result"]
        assert result["job_deferred"] is True and result["status"] == "running", result
        job = _job(engine, lease["job_id"])
        assert job["status"] == "queued" and job["attempt"] == 0, dict(job)
    finally:
        release.set()
        first.join(60)
    assert first_box["result"]["status"] == "healthy", first_box
    # The deferred job, leased again, finds the verdict and installs nothing more.
    writes = len(stack["host"].writes())
    again = _execute(engine, _lease(engine, deployment, "w2"))
    assert again["status"] == "healthy"
    assert len(stack["host"].writes()) == writes


def test_a_second_deployment_to_a_busy_directory_waits_without_spending_attempts(
        client, engine, stack):
    entered, release = _hold_install(stack["host"])
    first = _plan(client, stack, "main")
    second = _plan(client, stack, "next")
    first_lease = _approve_and_lease(client, engine, first, "w1")
    second_lease = _approve_and_lease(client, engine, second, "w2")
    thread, box = _in_thread(lambda: _execute(engine, first_lease))
    try:
        assert entered.wait(30)
        for _ in range(6):  # more waits than a retrying job had attempts
            waited = _execute(engine, second_lease)
            assert waited["job_deferred"] is True and waited["status"] == "waiting", waited
            assert waited["deployment"]["status"] == "running"
            assert waited["deployment"]["waiting"]["deployment_id"] == first["id"]
            job = _job(engine, second_lease["job_id"])
            assert job["status"] == "queued" and job["attempt"] == 0, dict(job)
            second_lease = _lease(engine, second, "w2")
        assert not any(second["commit"] in c["command"] for c in stack["host"].writes())
    finally:
        release.set()
        thread.join(60)
    assert box["result"]["status"] == "healthy"
    done = _execute(engine, second_lease)
    assert done["status"] == "healthy", done
    assert done["deployment"]["waiting"] is None


def test_a_deadlettered_job_does_not_fail_a_deployment_whose_apply_is_still_running(
        client, engine, stack):
    entered, release = _hold_install(stack["host"])
    deployment = _plan(client, stack)
    lease = _approve_and_lease(client, engine, deployment)
    thread, box = _in_thread(lambda: _execute(engine, lease))
    try:
        assert entered.wait(30)
        with engine.begin() as connection:
            job = dict(connection.execute(text("SELECT * FROM jobs WHERE id = :j"),
                                          {"j": lease["job_id"]}).mappings().one())
            stack_deploy.fail_for_deadlettered_job(connection, job, {"message": "lease lost"},
                                                   api_main.settings)
        fetched = client.get(f"/v1/deployments/stack/{deployment['id']}").json()["deployment"]
        assert fetched["status"] == "running"
    finally:
        release.set()
        thread.join(60)
    assert box["result"]["status"] == "healthy"
