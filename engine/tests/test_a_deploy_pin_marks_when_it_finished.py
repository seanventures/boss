"""A deploy pin marks when a deploy FINISHED (spec §5.4): `deploy_pins(target_ids, since)` pins
every stack deployment and deployment run that finished at or after `since`, at its finished_at,
with the deploy's word (Live for the newest success of the target, Done for an older one,
Failed). A deploy still running has no pin yet."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import infra_view as iv

PG_URL = os.environ.get("BOSS_TEST_DATABASE_URL", "")
HOME = "boss-dogfood"


@pytest.fixture(scope="module")
def eng():
    if not PG_URL.startswith("postgres"):
        pytest.skip("needs BOSS_TEST_DATABASE_URL")
    from apps.api.tests.infra_seed import clear, seed

    e = create_engine(PG_URL, future=True)
    seed(e, HOME, samples=False)
    with e.begin() as c:
        c.execute(text(
            "INSERT INTO deployment_runs (id, project_id, deployment_target_id, app_name, "
            "environment, repo, target_kind, target_ref, commit_ref, status, trigger, created_by, "
            "started_at) VALUES ('dr_running', :p, 't_live', 'invoice', 'prod', 'acme/invoice', "
            "'ssh', 'x', 'cccccccccccccccccccccccccccccccccccccccc', 'running', 'manual', 'ceo', "
            "now())"), {"p": HOME})
    yield e
    with e.begin() as c:
        clear(c, HOME)


def test_pins_sit_at_finished_at_and_say_the_deploy_word(eng):
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    with eng.begin() as c:
        pins = iv.deploy_pins(c, ["t_live", "t_stack", "t_down", "t_never"], since)
        finished = {r[0]: r[1] for r in c.execute(text(
            "SELECT id, finished_at FROM deployment_runs WHERE project_id = :p "
            "UNION ALL SELECT id, finished_at FROM stack_deployments WHERE project_id = :p"),
            {"p": HOME})}
    assert [p["ref"] for p in pins["t_live"]] == ["delivery:dr_live"], \
        "the 50-hour-old deploy is outside the window and the running one has not finished"
    assert pins["t_live"][0]["at"] == finished["dr_live"].isoformat()
    assert pins["t_live"][0]["word"] == "Live" and pins["t_live"][0]["sha7"] == "a1b2c3d"
    assert [(p["ref"], p["word"]) for p in pins["t_stack"]] == [("stack:sd_failed", "Failed")]
    assert [(p["ref"], p["word"]) for p in pins["t_down"]] == [("delivery:dr_down_fail",
                                                               "Failed")]
    assert pins["t_never"] == []


def test_an_older_success_is_done_and_the_newest_live(eng):
    since = datetime.now(timezone.utc) - timedelta(days=7)
    with eng.begin() as c:
        pins = iv.deploy_pins(c, ["t_live", "t_stack"], since)
    assert [(p["ref"], p["word"]) for p in pins["t_live"]] == [
        ("delivery:dr_live_old", "Done"), ("delivery:dr_live", "Live")]
    assert [(p["ref"], p["word"]) for p in pins["t_stack"]] == [
        ("stack:sd_ok", "Live"), ("stack:sd_failed", "Failed")]
    times = [p["at"] for p in pins["t_live"]]
    assert times == sorted(times), "oldest first, as a time axis reads"
