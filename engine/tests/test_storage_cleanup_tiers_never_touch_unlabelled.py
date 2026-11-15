"""The tiered executor, pure parts (LLD §4.3, §9): tier = reversibility × label × approved rule;
an unlabelled item is tier 3 whatever else is true; the plan is tier-ordered with ONE tier-3 ask;
a real run is bound to the dry run by plan_hash; the incident's flow labels stay short.
"""
from __future__ import annotations

from boss_engine.services import storage_cleanup as sc
from boss_engine.services import storage_incidents as si


def _tool(name: str) -> sc.ToolDef:
    return sc.TOOL_BY_NAME[name]


def test_tiering_never_deletes_an_unlabelled_item():
    for tool in sc.TOOLS:
        assert sc.tier_of(tool, labelled=False, rule_approved=True) == 3
        assert sc.tier_of(tool, labelled=False, rule_approved=False) == 3


def test_tiering_is_reversibility_times_label_times_rule():
    assert sc.tier_of(_tool("storage.prune_build_cache")) == 1
    assert sc.tier_of(_tool("storage.docker_trim")) == 1
    assert sc.tier_of(_tool("storage.remove_backups")) == 3
    assert sc.tier_of(_tool("storage.remove_backups"), rule_approved=True) == 2
    assert sc.tier_of(_tool("storage.drop_partitions"), rule_approved=True) == 2
    # a safe tool never becomes tier 3 because a rule is missing: rules gate the irreversible
    assert sc.tier_of(_tool("storage.remove_images"), rule_approved=False) == 1


def test_every_tool_is_typed_and_shell_free():
    for tool in sc.TOOLS:
        assert tool.where in ("host", "api")
        assert tool.reversibility in ("safe", "irreversible")
        assert len(tool.label.split()) <= si.MAX_LABEL_WORDS
        if tool.where == "host":
            assert tool.verb is not None
        if tool.reversibility == "irreversible":
            assert tool.needs_rule and tool.rule_kind, tool.name


def test_plan_hash_binds_tool_host_args_and_items_but_not_the_run_flags():
    a = sc.plan_hash("storage.remove_backups", "h1", {"keep_newest": 10, "dry_run": True}, ["b1", "b2"])
    b = sc.plan_hash("storage.remove_backups", "h1", {"keep_newest": 10, "dry_run": False, "plan_hash": a}, ["b2", "b1"])
    assert a == b, "dry_run and plan_hash are not part of the plan"
    assert a != sc.plan_hash("storage.remove_backups", "h1", {"keep_newest": 20}, ["b1", "b2"])
    assert a != sc.plan_hash("storage.remove_backups", "h1", {"keep_newest": 10}, ["b1", "b2", "b3"])
    assert a != sc.plan_hash("storage.remove_backups", "h2", {"keep_newest": 10}, ["b1", "b2"])


def test_a_real_run_requires_the_dry_runs_plan_hash():
    step = {"tool": "storage.remove_backups", "args": {"keep_newest": 10, "install_id": "i1"}, "plan_hash": None}
    assert not sc.dry_run_matches(step, ["b1"], "h1"), "no dry run yet: refused"
    step["plan_hash"] = sc.plan_hash("storage.remove_backups", "h1", step["args"], ["b1"])
    assert sc.dry_run_matches(step, ["b1"], "h1")
    assert not sc.dry_run_matches(step, ["b1", "b_new"], "h1"), "an item that appeared since: refused"


def test_the_steward_hashes_exactly_like_the_planner():
    """The helper recomputes the hash on the host (scripts/install/steward.py.plan_hash); the two
    implementations must agree byte for byte or every real run would be refused."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "install" / "steward.py"
    spec = importlib.util.spec_from_file_location("_steward", path)
    steward = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(steward)
    args = {"keep_newest": 10, "keep_days": 7, "cap_gb": 20, "install_id": "i1", "dry_run": True}
    items = ["boss-backup-2", "boss-backup-1"]
    assert steward.plan_hash("storage.remove_backups", "h1", args, items) == \
        sc.plan_hash("storage.remove_backups", "h1", args, items)
    from boss_engine.services import host_jobs

    assert steward.canonical("j", "h", "du", {"a": 1}, "2026-11-14T00:00:00+00:00") == \
        host_jobs.canonical("j", "h", "du", {"a": 1}, "2026-11-14T00:00:00+00:00")


def test_incident_flow_nodes_are_short_and_in_order():
    inc = {"id": "si_1", "host_id": "h1", "status": "critical", "severity": "critical",
           "opened_at": None, "hold_since": "2026-11-14T18:02:00+00:00", "hold_released_at": None,
           "paused_runs": 14, "freed_gb": 42.0, "free_pct_now": 17.0, "target_pct": 25.0,
           "free_pct_start": 9.0, "cause_text": "Backups grew.",
           "plan": [{"tool": "storage.prune_build_cache", "tier": 1, "state": "done", "freed_gb": 8.0},
                    {"tool": "storage.remove_images", "tier": 1, "state": "running"},
                    {"tool": "storage.remove_backups", "tier": 3, "state": "ask", "label": "Remove old backups",
                     "items": 348, "gb": 161.0}]}
    flow = si.flow_of(inc)
    keys = [n["key"] for n in flow]
    assert keys == ["detected", "paused", "cleaning", "asking", "freed", "resumed", "reported"]
    for n in flow:
        assert len(n["label"].split()) <= si.MAX_LABEL_WORDS, n
        assert len(n["badges"]) <= 2
        assert n["state"] in ("done", "active", "waiting", "ask")
        assert n["type"] in ("signal", "agent", "action", "decision", "needs_you", "outcome")
    by = {n["key"]: n for n in flow}
    assert by["paused"]["state"] == "done" and by["paused"]["label"] == "Paused 14 runs"
    assert by["cleaning"]["state"] == "active"
    assert by["asking"]["state"] == "ask" and by["asking"]["type"] == "needs_you"
    assert by["freed"]["state"] == "waiting" and by["resumed"]["state"] == "waiting"
    assert by["reported"]["state"] == "waiting"


def test_a_warn_incident_has_no_pause_or_resume_nodes_and_resolution_closes_the_flow():
    inc = {"id": "si_2", "host_id": "h1", "status": "resolved", "severity": "warn", "freed_gb": 12.0,
           "free_pct_now": 30.0, "target_pct": 25.0, "resolved_at": "2026-11-14T18:21:00+00:00", "plan": []}
    flow = si.flow_of(inc)
    assert [n["key"] for n in flow] == ["detected", "cleaning", "freed", "reported"]
    assert all(n["state"] == "done" for n in flow)


def test_explain_cause_picks_the_biggest_avoidable_tenant():
    th = {"pg_temp_critical_gb": 5.0}
    key, text = si.explain_cause({"total_gb": 500, "free_gb": 40, "backups_gb": 170, "backups_n": 358,
                                  "images_gb": 30, "cache_gb": 8}, None, th)
    assert key == "backups.no_retention" and "358" in text
    key, _ = si.explain_cause({"total_gb": 500, "free_gb": 40, "pg_temp_gb": 11.0, "backups_gb": 2}, None, th)
    assert key == "pg.temp_spill"
    key, _ = si.explain_cause({"total_gb": 500, "free_gb": 40, "docker_alloc_gb": 128, "docker_used_gb": 60}, None, th)
    assert key == "docker.vm_unreclaimed"

    class FC:
        growth_gb_per_day, days_until_full = 12.0, 3.3

    key, text = si.explain_cause({"total_gb": 500, "free_gb": 40}, FC(), th)
    assert key == "growth.fast" and "12.0 GB a day" in text
    assert si.explain_cause({"total_gb": 500, "free_gb": 40}, None, th)[0] == "disk.low"
