"""Real Postgres cancellation barriers; only the model callback is deterministic."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from apps.api.app import main as api_main
from apps.api.tests import test_discovery_community_gate as gate_tests
from apps.api.tests import test_starting_a_workflow_twice_makes_one_run as workflow_tests
from apps.api.tests.test_discovery_community_gate import (
    PROJECT,
    _Agent,
    _events,
    _execute,
    _lease,
    _receipts,
    _start,
)
from boss_engine.services import discovery_service as ds, discovery_spend as spend, jobs
from boss_engine.services.shepherd_service import sweep_abandoned_runs


governed = gate_tests.governed
workflow_key = workflow_tests.workflow_key


def _cancel(engine, run_id, *, force_job=True):
    with engine.begin() as c:
        if force_job:
            job_id = c.execute(
                text("SELECT state->>'job_id' FROM discovery_runs WHERE id=:id"), {"id": run_id}
            ).scalar_one()
            return jobs.JobLeaseService(api_main.settings).cancel_queued_job(
                c, job_id, reason="test operator cancellation", force=True, project_id=PROJECT
            )
        c.execute(text("UPDATE discovery_runs SET status='cancelled' WHERE id=:id"), {"id": run_id})


def _status(engine, run_id):
    with engine.connect() as c:
        return c.execute(
            text("SELECT status FROM discovery_runs WHERE id=:id"), {"id": run_id}
        ).scalar_one()


@pytest.mark.parametrize("field", ["discovery_run_id", "run_id"])
def test_cancel_before_start_and_repeat_preserve_terminal_lineage(engine, governed, field):
    run_id = _start()
    lease = _lease(engine, run_id)
    with engine.begin() as c:
        c.execute(
            text(
                "UPDATE jobs SET payload=(payload - 'discovery_run_id') || "
                "jsonb_build_object(CAST(:field AS text),CAST(:r AS text)) WHERE id=:j"
            ),
            {"field": field, "r": run_id, "j": lease["job_id"]},
        )
    _cancel(engine, run_id)
    _cancel(engine, run_id)
    assert _execute(run_id, lease) == {"status": "cancelled", "retry": False}
    with engine.begin() as c:
        assert jobs.enqueue_job(c, PROJECT, "discovery.run", {field: run_id})["status"] == "skipped"
    assert governed.calls == [] and _receipts(engine, run_id) == []


@pytest.mark.parametrize("outcome", ["success", "uncertain"])
def test_cancel_during_accepted_call_retains_receipt_and_never_advances(
    engine, governed, monkeypatch, outcome
):
    run_id = _start()
    lease = _lease(engine, run_id)
    entered, finish = threading.Event(), threading.Event()
    original = governed.run_research

    def research(prompt, **kwargs):
        entered.set()
        assert finish.wait(10), "test did not release callback"
        if outcome == "uncertain":
            raise TimeoutError("provider acceptance uncertain")
        return original(prompt)

    monkeypatch.setattr(governed, "run_research", research)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_execute, run_id, lease)
        try:
            assert entered.wait(10), "pipeline never reached callback"
            _cancel(engine, run_id)
        finally:
            finish.set()
        result = future.result(timeout=10)
    assert result["status"] == "cancelled" and result["retry"] is False
    assert _status(engine, run_id) == "cancelled"
    assert _receipts(engine, run_id) == ["in_progress"]
    assert _events(engine, run_id, "discovery.reconciliation_required") == 1
    assert _events(engine, run_id, "discovery.completed") == 0
    assert _events(engine, run_id, "discovery.failed") == 0
    with engine.connect() as c:
        assert (
            c.execute(
                text("SELECT status FROM budget_reservations WHERE reason LIKE :r"),
                {"r": f"Discovery {run_id} stage %"},
            ).scalar_one()
            == "active"
        )
    calls = list(governed.calls)
    assert _execute(run_id, lease)["status"] == "cancelled"
    assert governed.calls == calls
    if outcome == "success":
        with engine.connect() as c:
            result = c.execute(
                text("SELECT result FROM receipts WHERE dedupe_key LIKE :d"),
                {"d": f"model-gateway:{PROJECT}:discovery:{run_id}:%"},
            ).scalar_one()
        assert result["unsettled_output"]["cost_usd_micros"] == 1000
        assert result["submitted"] is True


@pytest.mark.parametrize("force_job", [False, True])
def test_cancel_after_authority_before_submit_does_not_call(
    engine, governed, monkeypatch, force_job
):
    run_id = _start()
    lease = _lease(engine, run_id)
    original_open = spend.DiscoveryStageSpend._open
    original_check = spend.DiscoveryStageSpend.check_active
    opened = False

    def open_stage(self, *args):
        nonlocal opened
        result = original_open(self, *args)
        opened = True
        return result

    def check(self, connection):
        nonlocal opened
        if opened:
            opened = False
            _cancel(engine, run_id, force_job=force_job)
        return original_check(self, connection)

    monkeypatch.setattr(spend.DiscoveryStageSpend, "_open", open_stage)
    monkeypatch.setattr(spend.DiscoveryStageSpend, "check_active", check)
    result = _execute(run_id, lease)
    assert result["status"] == "cancelled" and not result["retry"]
    assert governed.calls == []
    assert _receipts(engine, run_id) == (["in_progress"] if force_job else ["failed"])
    with engine.connect() as c:
        status = c.execute(
            text("SELECT status FROM budget_reservations WHERE reason LIKE :r"),
            {"r": f"Discovery {run_id} stage %"},
        ).scalar_one()
    assert status == ("active" if force_job else "released")
    if force_job:
        with engine.connect() as c:
            evidence = c.execute(
                text("SELECT result FROM receipts WHERE dedupe_key LIKE :d"),
                {"d": f"model-gateway:{PROJECT}:discovery:{run_id}:%"},
            ).scalar_one()
        assert evidence["submitted"] is False and evidence["unsettled_output"] is None
        assert _events(engine, run_id, "discovery.reconciliation_required") == 1


@pytest.mark.parametrize("field", ["discovery_run_id", "run_id"])
@pytest.mark.parametrize("job_status", ["queued", "leased", "running"])
def test_deferred_live_job_is_not_an_orphan(engine, governed, field, job_status):
    run_id = _start()
    with engine.begin() as c:
        c.execute(
            text("UPDATE discovery_runs SET updated_at=now()-interval '40 minutes' WHERE id=:r"),
            {"r": run_id},
        )
        c.execute(
            text(
                "UPDATE jobs SET payload=jsonb_build_object(CAST(:f AS text),CAST(:r AS text)), "
                "status=:s, run_after=now()+interval '1 hour' WHERE id=(SELECT state->>'job_id' "
                "FROM discovery_runs WHERE id=:r)"
            ),
            {"r": run_id, "f": field, "s": job_status},
        )
    sweep_abandoned_runs(engine, api_main.settings, PROJECT)
    assert _status(engine, run_id) == "running"


def test_cancelled_run_rejects_phase_save_fail_and_deferred_successor(engine, governed):
    run_id = _start()
    _cancel(engine, run_id)
    with pytest.raises(spend.DiscoveryStopped):
        ds._phase(engine, run_id, "shape")
    with pytest.raises(spend.DiscoveryStopped):
        ds._save(engine, run_id, {}, 9, "fixture")
    assert ds._fail_run(engine, PROJECT, run_id, RuntimeError("late")) == "cancelled"
    with pytest.raises(spend.DiscoveryStopped):
        ds._defer_run(engine, PROJECT, run_id, {"discovery_run_id": run_id}, 1, "busy")
    assert _events(engine, run_id, "discovery.failed") == 0


def test_late_legacy_completion_cannot_overwrite_cancel(engine, governed, monkeypatch):
    run_id = _start()
    original = ds._evaluate_gate

    def gate(dossier):
        _cancel(engine, run_id)
        return original(dossier)

    monkeypatch.setattr(ds, "_evaluate_gate", gate)
    agent = _Agent()
    ds._run_discovery(
        run_id,
        PROJECT,
        "fixture",
        "",
        api_main.settings,
        research=lambda stage, prompt: agent.run_research(prompt),
    )
    assert _status(engine, run_id) == "cancelled"
    assert _events(engine, run_id, "discovery.completed") == 0


def test_conflicting_lineage_never_submits(engine, governed):
    run_id = _start()
    lease = _lease(engine, run_id)
    with engine.begin() as c:
        c.execute(
            text('UPDATE jobs SET payload=payload || \'{"run_id":"other"}\'::jsonb WHERE id=:j'),
            {"j": lease["job_id"]},
        )
    with pytest.raises(ValueError, match="Conflicting"):
        _execute(run_id, lease)
    assert governed.calls == []


@pytest.mark.parametrize("when", ["before_submit", "after_acceptance"])
def test_reclaimed_job_cannot_submit_or_finish_a_run(engine, governed, monkeypatch, when):
    run_id = _start()
    lease = _lease(engine, run_id)

    def reclaim():
        with engine.begin() as c:
            c.execute(
                text(
                    "UPDATE jobs SET lease_owner='new-worker', lease_token_hash='new-token' WHERE id=:j"
                ),
                {"j": lease["job_id"]},
            )

    if when == "before_submit":
        original = spend.DiscoveryStageSpend._ready_to_submit

        def before(self, c, authority):
            reclaim()
            return original(self, c, authority)

        monkeypatch.setattr(spend.DiscoveryStageSpend, "_ready_to_submit", before)
    else:
        original = governed.run_research

        def after(prompt, **kwargs):
            result = original(prompt)
            reclaim()
            return result

        monkeypatch.setattr(governed, "run_research", after)
    result = _execute(run_id, lease)
    assert result["retry"] is False
    assert len(governed.calls) == (0 if when == "before_submit" else 1)
    assert _status(engine, run_id) == "running"
    assert _receipts(engine, run_id) == ["in_progress"]
    assert _events(engine, run_id, "discovery.reconciliation_required") == 1
    assert _events(engine, run_id, "discovery.completed") == 0


def test_workflow_cancel_reaches_discovery_and_repeated_cancel_is_idempotent(
    client, engine, governed, workflow_key
):
    from boss_engine.services import workflows

    run_id = _start()
    response = workflow_tests._start(client, workflow_key, "discovery-parent")
    assert response.status_code == 200, response.text
    parent = response.json()["run"]["id"]
    with engine.begin() as c:
        c.execute(
            text(
                "UPDATE workflow_step_runs SET status='waiting', output="
                "jsonb_build_object('await',jsonb_build_object('kind','discovery','id',CAST(:r AS text))) "
                "WHERE workflow_run_id=:w"
            ),
            {"r": run_id, "w": parent},
        )
        workflows.cancel_run(c, PROJECT, parent, "ceo", reason="stop", compensate=False)
    with engine.begin() as c:
        workflows.cancel_run(c, PROJECT, parent, "ceo", reason="stop twice", compensate=False)
        job_status = c.execute(
            text(
                "SELECT status FROM jobs WHERE id=(SELECT state->>'job_id' "
                "FROM discovery_runs WHERE id=:r)"
            ),
            {"r": run_id},
        ).scalar_one()
    assert _status(engine, run_id) == job_status == "cancelled"
    assert _events(engine, parent, "workflow.work_cancelled") == 1


def test_wrong_project_or_kind_cannot_keep_an_orphan_alive(engine, governed):
    run_id = _start()
    with engine.begin() as c:
        assert jobs.stopped_run_reason(
            c, "discovery.run", {"discovery_run_id": run_id}, "foreign-project"
        )
        c.execute(
            text("UPDATE discovery_runs SET updated_at=now()-interval '40 minutes' WHERE id=:r"),
            {"r": run_id},
        )
        c.execute(
            text(
                "UPDATE jobs SET kind='canary.noop' WHERE id=(SELECT state->>'job_id' FROM discovery_runs WHERE id=:r)"
            ),
            {"r": run_id},
        )
    sweep_abandoned_runs(engine, api_main.settings, PROJECT)
    assert _status(engine, run_id) == "cancelled"


def test_cancel_between_deep_scouts_stops_the_next_reservation(engine, governed, monkeypatch):
    run_id = ds.start_discovery(
        project_id=PROJECT,
        title="deep",
        seed="fixture",
        context="",
        settings=api_main.settings,
        deep=True,
    )
    original = governed.run_research

    def research(prompt, **kwargs):
        result = original(prompt)
        _cancel(engine, run_id, force_job=False)
        return result

    monkeypatch.setattr(governed, "run_research", research)
    assert _execute(run_id, _lease(engine, run_id))["status"] == "cancelled"
    assert len(governed.calls) == 1
    assert _receipts(engine, run_id) == ["succeeded"]
    assert _events(engine, run_id, "discovery.completed") == 0


def test_ordinary_code_and_meeting_cancellation_still_closes_only_its_run(engine):
    from uuid import uuid4

    for kind, table_name, title_column in (
        ("code.task", "code_task_runs", "title"),
        ("meeting.step", "meeting_runs", "directive"),
    ):
        run_id, job_id = "run_" + uuid4().hex, "job_" + uuid4().hex
        with engine.begin() as c:
            c.execute(
                text(
                    f"INSERT INTO {table_name}(id,project_id,{title_column}) VALUES(:r,:p,'fixture')"
                ),
                {"r": run_id, "p": PROJECT},
            )
            c.execute(
                text(
                    "INSERT INTO jobs(id,project_id,kind,payload,status) "
                    "VALUES(:j,:p,:kind,jsonb_build_object('run_id',CAST(:r AS text)),'queued')"
                ),
                {"j": job_id, "p": PROJECT, "kind": kind, "r": run_id},
            )
            service = jobs.JobLeaseService(api_main.settings)
            refused = service.cancel_queued_job(
                c, job_id, reason="wrong project", project_id="foreign"
            )
            assert refused["ok"] is False
            assert (
                c.execute(
                    text(f"SELECT status FROM {table_name} WHERE id=:r"), {"r": run_id}
                ).scalar_one()
                == "running"
            )
            stopped = service.cancel_queued_job(c, job_id, reason="stop", project_id=PROJECT)
            assert stopped["status"] == "cancelled"
            assert (
                c.execute(
                    text(f"SELECT status FROM {table_name} WHERE id=:r"), {"r": run_id}
                ).scalar_one()
                == "cancelled"
            )


@pytest.mark.parametrize("changed", ["failure_owner", "run_owner_after_read"])
def test_deadletter_resume_requires_the_current_failure_owner(
    engine, governed, monkeypatch, changed
):
    run_id = _start()
    lease = _lease(engine, run_id)
    with engine.begin() as c:
        c.execute(
            text(
                "UPDATE discovery_runs SET status='failed', "
                "state=state || jsonb_build_object('failed_by_job', CAST(:owner AS text)) WHERE id=:r"
            ),
            {
                "r": run_id,
                "owner": "another-job" if changed == "failure_owner" else lease["job_id"],
            },
        )
    if changed == "run_owner_after_read":
        original = jobs.JobLeaseService.require_active_lease

        def acquire(self, *args, **kwargs):
            job = original(self, *args, **kwargs)
            with engine.begin() as c:
                c.execute(
                    text(
                        'UPDATE discovery_runs SET state=state || \'{"job_id":"new-owner"}\'::jsonb WHERE id=:r'
                    ),
                    {"r": run_id},
                )
            return job

        monkeypatch.setattr(jobs.JobLeaseService, "require_active_lease", acquire)
    assert _execute(run_id, lease) == {"status": "failed", "retry": False}
    assert _events(engine, run_id, "discovery.resumed") == 0
    assert governed.calls == []


def _deadletter_discovery(engine, lease):
    with engine.begin() as c:
        return jobs.JobLeaseService(api_main.settings).fail(
            c,
            lease["worker_id"],
            lease["job_id"],
            lease["lease_token"],
            {"message": "worker stopped"},
            False,
            0,
        )["deadletter_id"]


@pytest.mark.parametrize(
    "change", ["cancelled", "completed", "owner", "failed_by", "conflict", "foreign"]
)
def test_deadletter_cannot_reopen_stopped_or_unrelated_discovery(engine, governed, change):
    from uuid import uuid4

    run_id = _start()
    lease = _lease(engine, run_id)
    deadletter = _deadletter_discovery(engine, lease)
    with engine.begin() as c:
        if change in {"cancelled", "completed"}:
            c.execute(
                text("UPDATE discovery_runs SET status=:s WHERE id=:r"), {"s": change, "r": run_id}
            )
        elif change in {"owner", "failed_by"}:
            key = "job_id" if change == "owner" else "failed_by_job"
            c.execute(
                text(
                    "UPDATE discovery_runs SET state=state || jsonb_build_object(CAST(:k AS text), 'other') WHERE id=:r"
                ),
                {"k": key, "r": run_id},
            )
        elif change == "conflict":
            c.execute(
                text(
                    "UPDATE jobs SET payload=payload || jsonb_build_object('run_id','other') WHERE id=:j"
                ),
                {"j": lease["job_id"]},
            )
        else:
            foreign = "foreign_" + uuid4().hex
            c.execute(
                text(
                    "INSERT INTO projects(id,workspace_id,name,slug) SELECT :p,workspace_id,'Foreign',:p FROM projects WHERE id=:home"
                ),
                {"p": foreign, "home": PROJECT},
            )
            c.execute(
                text("UPDATE discovery_runs SET project_id=:p WHERE id=:r"),
                {"p": foreign, "r": run_id},
            )
    before = _status(engine, run_id)
    with pytest.raises(ValueError):
        with engine.begin() as c:
            api_main.deadletter_remediation.requeue_job_deadletter(
                c, deadletter_id=deadletter, actor="ceo"
            )
    assert _status(engine, run_id) == before
    assert _events(engine, run_id, "discovery.resumed") == 0
    assert governed.calls == []
    with engine.connect() as c:
        assert (
            c.execute(
                text("SELECT requeued_job_id FROM deadletters WHERE id=:d"), {"d": deadletter}
            ).scalar_one()
            is None
        )
        assert (
            c.execute(
                text(
                    "SELECT count(*) FROM jobs WHERE payload->'requeue'->>'source_deadletter_id'=:d"
                ),
                {"d": deadletter},
            ).scalar_one()
            == 0
        )


def test_deadletter_requeue_replays_the_stage_already_paid_for(engine, governed):
    run_id = _start()
    governed.fail_at, governed.fail_with = "synthesize", RuntimeError("definite fixture failure")
    lease = _lease(engine, run_id)
    assert _execute(run_id, lease)["retry"] is True
    deadletter = _deadletter_discovery(engine, lease)
    with engine.begin() as c:
        api_main.deadletter_remediation.requeue_job_deadletter(
            c, deadletter_id=deadletter, actor="ceo"
        )
    assert _execute(run_id, _lease(engine, run_id)) == {"status": "completed", "retry": False}
    assert governed.calls == ["signal_sweep", "synthesize", "synthesize", "vet", "shape"]
    assert _events(engine, run_id, "discovery.stage_replayed") == 1


@pytest.mark.parametrize("kind", ["code.task", "meeting.step"])
def test_ordinary_deadletter_requeue_preserves_job_contract(engine, kind):
    from uuid import uuid4

    job_id, deadletter = "job_" + uuid4().hex, "dlq_" + uuid4().hex
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO jobs(id,project_id,kind,payload,status) VALUES(:j,:p,:k,'{}'::jsonb,'deadlettered')"
            ),
            {"j": job_id, "p": PROJECT, "k": kind},
        )
        c.execute(
            text(
                "INSERT INTO deadletters(id,project_id,source_kind,source_id,reason,payload) VALUES(:d,:p,'job',:j,'fixture','{}'::jsonb)"
            ),
            {"d": deadletter, "j": job_id, "p": PROJECT},
        )
        requeued = api_main.deadletter_remediation.requeue_job_deadletter(
            c, deadletter_id=deadletter, actor="ceo"
        )
        row = (
            c.execute(
                text("SELECT kind,project_id,status,payload FROM jobs WHERE id=:j"),
                {"j": requeued["requeued_job_id"]},
            )
            .mappings()
            .one()
        )
    assert row["kind"] == kind and row["project_id"] == PROJECT and row["status"] == "queued"
    assert row["payload"]["requeue"]["source_job_id"] == job_id
