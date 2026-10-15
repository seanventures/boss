"""A card must not say "never deployed" about an app that is serving traffic.

The delivery card read `stage.deploy_status`, and a stage exists only for a deployment that
came from a code task with a PR:

    FROM code_task_runs r LEFT JOIN deployment_runs d ON …code_task_run_id = r.id
    WHERE r.pull_request_url IS NOT NULL

An operator pressing Deploy creates a deployment_run with trigger='manual' and NO stage. So the
card reported "NEVER DEPLOYED" for a target whose app answered 200 on the host:

    deprun_112e61add9…  succeeded  hetzner-ci-01  http://192.0.2.202:3011  → 200

The badge was answering "has a merge-driven stage?" while appearing to answer "has this ever
deployed?". Two further gaps made it impossible for the UI to recover: the stage carried no
target_id, so it could not be matched to the card at all, and the target carried no history of
its own.
"""

import inspect

from boss_engine.services import delivery_pipeline


def test_a_stage_says_which_target_it_belongs_to():
    src = inspect.getsource(delivery_pipeline.pipeline_view)
    assert "d.deployment_target_id AS target_id" in src, (
        "without it the console cannot match a stage to the card it is drawing")


def test_every_target_carries_its_last_deployment():
    src = inspect.getsource(delivery_pipeline.pipeline_view)
    assert '"last_deployment": latest.get(t["id"])' in src


def test_the_history_is_read_from_deployment_runs_not_from_code_tasks():
    """The whole defect: deployments were only visible through code tasks."""
    src = inspect.getsource(delivery_pipeline.pipeline_view)
    latest_block = src[src.index("DISTINCT ON (deployment_target_id)"):]
    assert "FROM deployment_runs" in latest_block
    assert "code_task_runs" not in latest_block.split("ORDER BY")[0]


def test_it_picks_the_most_recent_deployment_per_target():
    src = inspect.getsource(delivery_pipeline.pipeline_view)
    assert "DISTINCT ON (deployment_target_id)" in src
    assert "COALESCE(finished_at, started_at, created_at) DESC" in src, (
        "a run that started and never finished must still count as the latest")


def test_the_card_falls_back_to_the_targets_own_history():
    """Every target carries its own newest deployment (`last_deployment`), so a card with no
    pipeline stage still shows how its last deploy went. The console's old delivery card that
    read it (apps/web/app/delivery-ui.tsx, `stage?.deploy_status ?? last?.status`) was removed
    with the v1 console; the server half is what is left to hold."""
    src = inspect.getsource(delivery_pipeline.pipeline_view)
    assert '"last_deployment": latest.get(t["id"])' in src
    assert "d.status AS deploy_status" in src
