"""Event rules list filters (BOSSAPP-1181): source, agent, and enabled, on Postgres."""
from __future__ import annotations

from boss_engine.services import agent_triggers

from ._intake_support import (
    deploy_local_agent,
    make_connection,
    make_project,
    make_route,
    make_slack,
    pg_engine,
    purge_slack,
)


def test_rules_list_filters_by_source_agent_and_enabled():
    engine = pg_engine()
    pid = make_project(engine)
    other = make_project(engine)
    try:
        slack = make_slack(engine, pid)
        hook = make_connection(engine, pid, "webhook", name="hook")
        alpha = deploy_local_agent(engine, pid, key="alpha")
        beta = deploy_local_agent(engine, pid, key="beta")
        hook_on = make_route(engine, pid, alpha["id"], hook["id"], name="hook-on", priority=5)
        slack_on = make_route(engine, pid, alpha["id"], slack["connection"]["id"],
                              name="slack-on", priority=10)
        slack_off = make_route(engine, pid, beta["id"], slack["connection"]["id"],
                               name="slack-off", priority=20)
        with engine.begin() as c:
            agent_triggers.set_paused(c, project_id=pid, trigger_id=slack_off["id"], actor="ceo",
                                     paused=True, reason="off")
        odep = deploy_local_agent(engine, other, key="alpha")
        oconn = make_connection(engine, other, "webhook", name="other")
        outside = make_route(engine, other, odep["id"], oconn["id"], name="outside", priority=1)

        def ids(**query):
            with engine.begin() as c:
                return [row["id"] for row in agent_triggers.list_intake_rules(c, pid, **query)]

        assert ids() == [hook_on["id"], slack_on["id"], slack_off["id"]]
        assert outside["id"] not in ids()
        assert ids(source=slack["connection"]["id"]) == [slack_on["id"], slack_off["id"]]
        assert ids(source="slack") == [slack_on["id"], slack_off["id"]]
        assert ids(source=hook["id"]) == [hook_on["id"]]
        assert ids(source="webhook") == [hook_on["id"]]
        assert ids(agent="alpha") == [hook_on["id"], slack_on["id"]]
        assert ids(agent="beta") == [slack_off["id"]]
        assert ids(enabled=True) == [hook_on["id"], slack_on["id"]]
        assert ids(enabled=False) == [slack_off["id"]]
        assert ids(source=slack["connection"]["id"], agent="alpha", enabled=True) == [slack_on["id"]]
        with engine.begin() as c:
            paused = agent_triggers.list_intake_rules(c, pid, source="slack", enabled=False)
        assert paused[0]["id"] == slack_off["id"]
        assert paused[0]["on"] is False
        assert paused[0]["agent_key"] == "beta"
        assert paused[0]["connection_id"] == slack["connection"]["id"]
        assert paused[0]["source_kind"] == "slack"
    finally:
        purge_slack(engine, pid)
        purge_slack(engine, other)
