"""A CHANGE NEVER SILENTLY DROPS AN APP'S MONITORING (P0-A, PR paramg/boss-sandbox#234).

The incident fix "KeyError on POST /orders" ended with a commit replacing order-api's 259-line
boss.deploy.yaml (Beacon's pack: `alerts:` with its rules, `dashboard:`) by the 15-line one from
before the pack. The app's tests/test_deploy_manifest.py pinned the manifest's exact keys, the
pack broke it, and the implementer (who may not edit tests) wrote the manifest that test pinned.
The fixtures are the real files from that PR's history.

1. The PR gate puts undeclared removals back; a monitoring change that names them may remove.
2. Every prompt that writes code or tests says the pack is not theirs to remove.
3. Set up monitoring shows Beacon the app's manifest tests and writes the ones it would break,
   rewritten, before the pack — so the pack and the app's own test agree at the source.
4. A deploy whose manifest lost rules keeps them running, marks them and raises an alert.
"""
from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from sqlalchemy import text

from boss_engine.schemas import FileChange
from boss_engine.services import monitoring_guard as mg

FIX = Path(__file__).parent / "fixtures"
WITH_PACK = (FIX / "pr234_manifest_with_pack.yaml").read_text()
TRIMMED = (FIX / "pr234_manifest_trimmed.yaml").read_text()
PINNING_TEST = (FIX / "pr234_test_deploy_manifest.py.txt").read_text()
PATH = "order-api/boss.deploy.yaml"


# 1. The gate ------------------------------------------------------------------------------
def test_pr234s_trim_is_seen_as_removing_the_whole_pack():
    gone = mg.removed(WITH_PACK, TRIMMED)
    assert "alerts.latency_p95" in gone and "alerts.error_rate" in gone
    assert any(g.startswith("rule ") for g in gone)
    assert any(g.startswith("panel ") for g in gone)
    assert mg.removed(TRIMMED, WITH_PACK) == []            # adding a pack removes nothing


def test_the_gate_puts_the_pack_back_and_leaves_the_rest_of_the_change():
    code = FileChange(path="order-api/app/routers/orders.py", content="fixed = True\n")
    edited = TRIMMED.replace("interval: 15", "interval: 30")   # an in-scope manifest edit
    files, kept = mg.guard_files([code, FileChange(path=PATH, content=edited)],
                                 lambda p: WITH_PACK if p == PATH else None)
    assert files[0] == code
    out = files[1].content
    assert mg.removed(WITH_PACK, out) == []                     # every rule and panel is back
    assert "interval: 30" in out                                # the change's own edit stays
    assert yaml.safe_load(out)["alerts"] == yaml.safe_load(WITH_PACK)["alerts"]
    assert kept and kept[0]["path"] == PATH and "alerts.latency_p95" in kept[0]["removed"]
    assert "Monitoring kept" in mg.pr_section(kept) and PATH in mg.pr_section(kept)


def test_an_unchanged_manifest_after_restore_is_not_written_at_all():
    files, kept = mg.guard_files([FileChange(path=PATH, content=TRIMMED)], lambda p: WITH_PACK)
    assert files == [] and kept                         # PR #234's commit would not exist


def test_a_monitoring_change_may_remove_what_it_names_and_nothing_else():
    doc = yaml.safe_load(WITH_PACK)
    rules = doc["alerts"]["rules"]
    victim = rules[0]["alert"]
    # the pack minus one rule
    doc2 = json.loads(json.dumps(doc))
    doc2["alerts"]["rules"] = rules[1:]
    after = yaml.safe_dump(doc2, sort_keys=False)
    assert mg.removed(WITH_PACK, after) == [f"rule {victim}"]
    files, kept = mg.guard_files([FileChange(path=PATH, content=after)], lambda p: WITH_PACK,
                                 allowed=[f"rule {victim}"])
    assert kept == [] and files[0].content == after
    files, kept = mg.guard_files([FileChange(path=PATH, content=after)], lambda p: WITH_PACK)
    assert kept and kept[0]["removed"] == [f"rule {victim}"]
    assert mg.removed(WITH_PACK, files[0].content) == []


def test_every_pr_boss_opens_goes_through_the_gate_before_anything_is_written():
    from boss_engine.services.software_factory import SoftwareFactoryService

    src = inspect.getsource(SoftwareFactoryService.propose_pull_request)
    assert "monitoring_removals" in inspect.signature(
        SoftwareFactoryService.propose_pull_request).parameters
    assert src.index("monitoring_guard.guard_files(") < src.index("self.github.upsert_file(")
    assert "monitoring_guard.pr_section(" in src


# 2. The prompts ---------------------------------------------------------------------------
def test_every_prompt_that_writes_code_or_tests_says_the_pack_is_not_theirs():
    from boss_engine.services import governed_code_task_service as g

    prompts = [
        g._qa_prompt("Fix", "fix it", ["works"], "pytest", app_root="order-api"),
        g._qa_prompt("Fix", "fix it", ["works"], "npm run verify", web=True, app_root="web"),
        g._qa_prompt("Fix", "fix it", ["works"], "x", stack_note="two services"),
        g._impl_prompt("Fix", "fix it", ["t.py"], "pytest", app_root="order-api"),
        g._solo_prompt("Fix", "fix it", ["works"], "pytest"),
        g._solo_prompt("Fix", "fix it", ["works"], "npm run verify", web=True),
    ]
    for p in prompts:
        assert mg.KEEP_MONITORING_RULE in p


# 3. Set up monitoring and the app's own manifest test agree -------------------------------
class TreeRepo:
    def __init__(self, files):
        self.files = dict(files)
        self.commits = []

    def list_paths(self, branch):
        return sorted(self.files)

    def read_text_file(self, branch, path):
        return self.files.get(path)

    def upsert_file(self, branch, path, content, message):
        self.files[path] = content
        self.commits.append(path)
        return f"{len(self.commits):040x}"


def test_beacon_is_shown_the_apps_manifest_tests_and_its_rewrite_lands_before_the_pack():
    from boss_engine.services import monitoring_pack as mp

    repo = TreeRepo({PATH: TRIMMED, "order-api/tests/test_deploy_manifest.py": PINNING_TEST,
                     "order-api/tests/test_orders.py": "def test_x(): pass\n",
                     "other-app/tests/test_deploy_manifest.py": "boss.deploy.yaml\n"})
    shown = mp.manifest_tests(repo, "b", PATH)
    assert list(shown) == ["order-api/tests/test_deploy_manifest.py"]   # its own, that reads it
    prompt = mp.prompt_for(app="Order API", env="staging", code={}, series={},
                           existing={"alerts": {}, "panels": []}, adding=False, tests=shown)
    assert "test_manifest_top_level_keys" in prompt and '"tests"' in prompt
    fixed = PINNING_TEST.replace('"metrics",\n    }', '"metrics",\n    } - {"alerts", "dashboard"}')
    answer = {"tests": [{"path": "order-api/tests/test_deploy_manifest.py", "content": fixed},
                        {"path": "order-api/app/main.py", "content": "evil = 1\n"}]}
    tests = mp.tests_to_write(answer, shown)
    assert list(tests) == ["order-api/tests/test_deploy_manifest.py"]  # only files it was shown
    wrote = mp.write_pack(repo, "b", PATH, TRIMMED, WITH_PACK, {}, "order-api", tests)
    assert repo.commits == ["order-api/tests/test_deploy_manifest.py", PATH]
    assert wrote["files_written"] == repo.commits


# 4. The live rule set ---------------------------------------------------------------------
T0 = datetime(2026, 10, 19, 7, 0, tzinfo=timezone.utc)


@pytest.fixture()
def pg():
    from ._obs_seed import pg_engine, project

    eng = pg_engine()
    with eng.begin() as c:
        pid = project(c)
    return eng, pid


def _deploy(c, pid, tid, manifest, at):
    c.execute(text(
        "INSERT INTO stack_deployments (id, project_id, target_id, repository, ref, commit_sha, "
        " manifest, status, created_by, created_at, finished_at) VALUES (:i, :p, :t, 'acme/shop', "
        " 'main', :c, CAST(:m AS jsonb), 'healthy', 'usr_op', :at, :at)"),
        {"i": f"sd_{uuid4().hex[:10]}", "p": pid, "t": tid, "c": uuid4().hex + "0" * 8,
         "m": json.dumps(manifest), "at": at})


def test_a_deploy_that_lost_rules_keeps_them_running_says_so_and_alerts(pg):
    from boss_engine.services import app_alerts as aa
    from boss_engine.services import app_alerts_view as view
    from boss_engine.services.app_deploy_manifest import load_manifest

    eng, pid = pg
    full = load_manifest(WITH_PACK).summary()
    trimmed = load_manifest(TRIMMED).summary()
    with eng.begin() as c:
        tid = f"t_drop_{uuid4().hex[:10]}"
        c.execute(text(
            "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
            " target_kind, target_ref, status, created_by, domains, public_url) VALUES (:t, :p, "
            " 'Order API', 'acme/shop', 'staging', 'stack', 'order-api', 'active', 'test', "
            " '[]'::jsonb, 'http://203.0.113.10:8000/')"), {"t": tid, "p": pid})
        _deploy(c, pid, tid, full, T0 - timedelta(hours=2))
        target = aa.subject(c, tid)
        before = {r["alert"]: r for r in aa.ensure_rules(c, target)}
        customs = [a for a, r in before.items() if r["kind"] == "custom"]
        assert customs and all(before[a]["source"] == "yaml" for a in customs)
        p95 = before["AppLatencyP95"]["params"]["threshold"]

        _deploy(c, pid, tid, trimmed, T0)                      # PR #234's manifest ships
        after = {r["alert"]: r for r in aa.ensure_rules(c, target)}
        for a in customs:                                       # still there, still on, marked
            assert after[a]["enabled"] and after[a]["dropped_from_manifest_at"] is not None
        assert after["AppLatencyP95"]["params"]["threshold"] == p95   # no silent revert
        assert after["AppLatencyP95"]["dropped_from_manifest_at"] is not None
        wire = view._rule_wire(c, after[customs[0]], target, {})
        assert wire["dropped_word"] and "still running" in wire["dropped_word"]
        alert = c.execute(text(
            "SELECT * FROM app_alerts WHERE subject_id = :t AND alertname = :a"),
            {"t": tid, "a": aa.DROPPED_ALERT}).mappings().all()
        assert len(alert) == 1 and alert[0]["state"] == "firing"
        assert set(customs) <= set(json.loads(json.dumps(alert[0]["annotations"]))["dropped"])
        aa.ensure_rules(c, target)                              # once, not per evaluation
        assert c.execute(text("SELECT count(*) FROM app_alerts WHERE subject_id = :t AND "
                              "alertname = :a"), {"t": tid, "a": aa.DROPPED_ALERT}).scalar() == 1

        _deploy(c, pid, tid, full, T0 + timedelta(hours=1))    # the pack is put back
        back = {r["alert"]: r for r in aa.ensure_rules(c, target)}
        assert all(back[a]["dropped_from_manifest_at"] is None for a in customs)
        assert c.execute(text("SELECT state FROM app_alerts WHERE subject_id = :t AND "
                              "alertname = :a"), {"t": tid, "a": aa.DROPPED_ALERT}).scalar() \
            == "resolved"
