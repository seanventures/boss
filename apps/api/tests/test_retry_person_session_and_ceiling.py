"""BOSSAPP-1300(a): POST /v1/code-tasks/{run_id}/retry checks direct person session + ceiling."""
from __future__ import annotations

import json

from sqlalchemy import text

from apps.api.app.services.core import make_id
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity.principal import RunMode


def _seed_local_infra_failed_run(engine, project_id: str, *, attempt: int = 2) -> str:
    run_id = make_id("crun")
    fctx = {
        "failure_class": "infra",
        "execution_substrate": "local_subprocess",
        "attempt": attempt,
        "reason_code": "pipeline_exception",
        "root_run_id": run_id,
    }
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO code_task_runs (id, project_id, task_id, status, phase, title, "
                "spec, finalize_ctx, error) VALUES (:id, :p, NULL, 'failed', 'done', 'Retry app', "
                "CAST(:spec AS JSONB), CAST(:fc AS JSONB), 'local infra failure')"
            ),
            {
                "id": run_id,
                "p": project_id,
                "spec": json.dumps(
                    {
                        "instructions": "build it",
                        "acceptance_criteria": ["works"],
                        "test_commands": [],
                        "owner": "o",
                        "repo": "r",
                        "base_branch": "main",
                    }
                ),
                "fc": json.dumps(fctx),
            },
        )
    return run_id


def test_retry_by_person_requires_direct_human_session_and_bounds_attempt_ceiling(
    client, engine, monkeypatch
):
    from boss_engine.identity import consent

    monkeypatch.setattr(consent, "require_authority", lambda *a, **k: None)
    world = IdentityWorld.create(engine)
    alice = world.person("alice")
    world.bind(alice, "owner")
    agent = world.agent("forge", ceiling=("code_task:retry",), sponsor=alice)

    # 1. Direct human session counts as by_person=True and bypasses the breaker once.
    r1 = _seed_local_infra_failed_run(engine, world.project_a, attempt=2)
    with world.acting_as(world.ctx(alice)):
        res = client.post(
            f"/v1/code-tasks/{r1}/retry",
            json={"actor": alice.id},
        )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "queued"

    # 2. OBO run_token with principal=alice MUST NOT count as by_person=True.
    r2 = _seed_local_infra_failed_run(engine, world.project_a, attempt=2)
    obo_ctx = world.run_ctx(
        alice, (agent,),
        grant_permissions=("code_task:retry",),
        token_scope=("code_task:retry",),
        project_id=world.project_a,
        mode=RunMode.OBO,
    )
    with world.acting_as(obo_ctx):
        refused_obo = client.post(
            f"/v1/code-tasks/{r2}/retry",
            json={"actor": alice.id},
        )
    assert refused_obo.status_code == 404, refused_obo.text
    assert "circuit breaker" in refused_obo.text

    # 3. API key for alice MUST NOT count as by_person=True.
    r3 = _seed_local_infra_failed_run(engine, world.project_a, attempt=2)
    with world.acting_as(world.key_ctx(alice, ("code_task:retry",))):
        refused_key = client.post(
            f"/v1/code-tasks/{r3}/retry",
            json={"actor": alice.id},
        )
    assert refused_key.status_code == 404, refused_key.text
    assert "circuit breaker" in refused_key.text

    # 4. Non-person caller cannot inflate attempt_ceiling past SHEPHERD_MAX_LINEAGE_ATTEMPTS (3).
    r4 = _seed_local_infra_failed_run(engine, world.project_a, attempt=3)
    with world.acting_as(world.key_ctx(alice, ("code_task:retry",))):
        capped = client.post(
            f"/v1/code-tasks/{r4}/retry",
            json={"actor": alice.id, "fix_note": "retry anyway", "attempt_ceiling": 99},
        )
    assert capped.status_code == 404, capped.text
    assert "retry ceiling reached" in capped.text

    # 5. Direct human session CAN retry attempt 3 with explicit attempt_ceiling <= 20.
    with world.acting_as(world.ctx(alice)):
        ok = client.post(
            f"/v1/code-tasks/{r4}/retry",
            json={"actor": alice.id, "fix_note": "human retry", "attempt_ceiling": 4},
        )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "queued"
