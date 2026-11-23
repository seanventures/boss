"""The Workflow Marketplace: packs install into a project's OWN workflows, update, uninstall and
publish, and DQ / Incident Response are no longer shipped by default.

    "I want DQ, Helpdesk workflow (SRE workflow) into the marketplace, we dont have to ship by
     default we should have Install button once installed it will be showing in their custom /
     local workflows and not in Standard ones. When people build the workflow, they can publish
     to the Marketplace and they can be installed into the app using Install button."

Pinned here: a new project gets neither pack; Install copies a pack into Yours with its pack and
version; an unmodified copy takes a newer version by itself and a modified one is offered it
(never overwritten); Uninstall is refused while a run is going and keeps the history of one that
ran; an org's own workflow publishes as a versioned pack only that org sees, republishing bumps
the version and brings installed copies along; projects that had DQ / Incident Response keep
them as installed copies (the schema migration), with their versions and runs.
"""
from __future__ import annotations

import copy
import os
import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import workflow_catalogue as catalogue
from boss_engine.services import workflow_marketplace as market
from boss_engine.services import workflows

TEST_DB = os.environ.get(
    "BOSS_TEST_DATABASE_URL", "postgresql+psycopg://boss:boss@localhost:5432/boss_test"
)
SCHEMA_SQL = Path(__file__).resolve().parents[2] / "db" / "schema.sql"
SRE = "sre-helpdesk"
IR = "incident_response"


def _schema_block() -> tuple[list[str], str]:
    """The marketplace section of schema.sql: its DDL, and the migration UPDATE (as shipped)."""
    sql = SCHEMA_SQL.read_text(encoding="utf-8")
    start = sql.index("-- THE WORKFLOW MARKETPLACE")
    split = sql.index("-- DQ AND INCIDENT RESPONSE MOVED")
    end = sql.index(";", sql.index("UPDATE workflow_definitions", split)) + 1

    def statements(chunk: str) -> list[str]:
        body = "\n".join(line for line in chunk.splitlines() if not line.strip().startswith("--"))
        return [s.strip() for s in body.split(";") if s.strip()]

    return statements(sql[start:split]), statements(sql[split:end])[0]


@pytest.fixture(scope="module")
def eng():
    engine = create_engine(TEST_DB, future=True)
    try:
        with engine.begin() as c:
            c.execute(text("SELECT 1 FROM workflow_definitions LIMIT 1"))
    except Exception as error:                      # pragma: no cover
        pytest.skip(f"boss_test unavailable: {error}")
    ddl, _ = _schema_block()
    with engine.begin() as c:
        for column in ("standard_version_id", "standard_content_hash",
                       "standard_available_version_id", "standard_dismissed_version_id"):
            c.exec_driver_sql(
                f"ALTER TABLE workflow_definitions ADD COLUMN IF NOT EXISTS {column} TEXT")
        for statement in ddl:
            c.exec_driver_sql(statement)
    return engine


def _new_project(c, workspace=None, name="marketplace test") -> str:
    pid = f"p_{uuid.uuid4().hex[:10]}"
    wid = workspace or c.execute(text("SELECT workspace_id FROM projects LIMIT 1")).scalar()
    if wid is None:
        pytest.skip("boss_test has no workspace")
    c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) "
                   "VALUES (:i, :w, :n, :s)"), {"i": pid, "w": wid, "n": name, "s": pid})
    return pid


def _cleanup(eng, pids):
    with eng.begin() as c:
        for pid in pids:
            for sql in ("DELETE FROM workflow_pack_versions WHERE pack_id IN "
                        "(SELECT id FROM workflow_packs WHERE source_project_id=:p)",
                        "DELETE FROM workflow_packs WHERE source_project_id=:p",
                        "DELETE FROM workflow_schedules WHERE project_id=:p",
                        "DELETE FROM workflow_step_runs WHERE project_id=:p",
                        "DELETE FROM workflow_runs WHERE project_id=:p",
                        "DELETE FROM workflow_mappings WHERE project_id=:p",
                        "DELETE FROM workflow_versions WHERE project_id=:p",
                        "DELETE FROM workflow_definitions WHERE project_id=:p",
                        "DELETE FROM events WHERE project_id=:p",
                        "DELETE FROM receipts WHERE project_id=:p",
                        "DELETE FROM projects WHERE id=:p"):
                try:
                    with c.begin_nested():
                        c.execute(text(sql), {"p": pid})
                except Exception:
                    pass


@pytest.fixture()
def project(eng):
    with eng.begin() as c:
        pid = _new_project(c)
    yield pid
    _cleanup(eng, [pid])


def _row(c, project, key=IR):
    row = c.execute(text("SELECT * FROM workflow_definitions WHERE project_id=:p "
                         "AND workflow_key=:k"), {"p": project, "k": key}).mappings().first()
    return dict(row) if row else None


def _listed(c, project, key=IR):
    return next((w for w in workflows.list_definitions(c, project, namespace="product")
                 if w["workflow_key"] == key), None)


def _active_spec(c, row):
    return c.execute(text("SELECT spec FROM workflow_versions WHERE id=:v"),
                     {"v": row["active_version_id"]}).scalar()


def _ship_newer(monkeypatch, version="1.1"):
    """BOSS ships SRE / Helpdesk v1.1: the investigation step has a new name."""
    entries = []
    for entry in catalogue.CATALOGUE:
        if entry["key"] == IR:
            entry = copy.deepcopy(entry)
            entry["steps"][0]["title"] = "Investigate the alert"
        entries.append(entry)
    monkeypatch.setattr(catalogue, "CATALOGUE", entries)
    pack = market.SHIPPED_PACKS[SRE]
    monkeypatch.setitem(market.SHIPPED_PACKS, SRE, market.ShippedPack(
        pack.pack_id, pack.name, pack.summary, version, pack.workflow_keys, pack.ontology_pack))


def _edit(c, project, title="Our own build step"):
    row = _row(c, project)
    spec = copy.deepcopy({k: v for k, v in _active_spec(c, row).items()
                          if k != workflows.AGENT_RULES_KEY})
    spec["steps"][1]["title"] = title
    workflows.publish_version(c, project_id=project, definition_id=row["id"], spec=spec,
                              actor="test")


def _running_run(c, project, row):
    rid = f"wfr_{uuid.uuid4().hex[:10]}"
    c.execute(text(
        "INSERT INTO workflow_runs (id, project_id, workflow_definition_id, workflow_version_id, "
        "status, actor) VALUES (:id, :p, :d, :v, 'running', 'test')"),
        {"id": rid, "p": project, "d": row["id"], "v": row["active_version_id"]})
    return rid


# ── not shipped by default ───────────────────────────────────────────────────────────────────

def test_a_new_project_gets_neither_pack_by_default(eng, project):
    with eng.begin() as c:
        result = catalogue.seed(c, project)
        skipped = {i["key"]: i.get("skipped") for i in result["installed"]}
        for key in market.PACK_OF_KEY:
            assert _row(c, project, key) is None, key
            assert "Marketplace pack" in (skipped.get(key) or ""), skipped.get(key)
        # A standard still is one.
        assert _row(c, project, "standard_product_delivery")["is_catalogue"] is True
        assert not (catalogue.missing_keys(c, project) & set(market.PACK_OF_KEY))
        # The catalogue still describes them, tagged with their pack (examples, start forms).
        tagged = {i["key"]: i["pack"] for i in catalogue.catalogue_summary(prd_auto_approve=True)}
        assert tagged[IR] == SRE and tagged["dq_incident_rca"] == "banking-dq"
        assert tagged["standard_product_delivery"] is None


def test_the_packs_are_versioned():
    assert market.SHIPPED_PACKS["banking-dq"].workflow_keys == (
        "dq_incident_rca", "dq_incident_escalation", "dq_monitor_intake")
    assert market.SHIPPED_PACKS[SRE].workflow_keys == (IR,)
    for pack in market.SHIPPED_PACKS.values():
        assert re.match(r"^\d+\.\d+$", pack.version), pack
    # Monitoring, PLRR and verify recovery stay Standard: steps, not packs.
    for key in ("monitor", "monitoring_pack", "plrr", "verify_recovery"):
        assert key not in market.PACK_OF_KEY
    assert market.next_version(None) == "1.0"
    assert market.next_version("0.3") == "0.4" and market.next_version("1.9") == "1.10"
    assert market.version_key("1.10") > market.version_key("1.9")
    assert market.version_key("1.0.0") == market.version_key("1.0")


# ── install into Yours ───────────────────────────────────────────────────────────────────────

def test_install_puts_the_pack_in_yours_with_its_provenance(eng, project):
    with eng.begin() as c:
        catalogue.seed(c, project)
        listed = {p["id"]: p for p in market.list_packs(c, project)}
        assert listed[SRE]["installed"] is False and listed[SRE]["publisher"] == "BOSS"
        assert [w["key"] for w in listed[SRE]["workflows"]] == [IR]
        out = market.install(c, project, SRE, "test")
        row = _row(c, project)
        assert out["version"] == "1.0" and out["installed"][0]["key"] == IR
        assert row["is_catalogue"] is False and row["namespace"] == "product"
        assert row["marketplace_pack_id"] == SRE and row["marketplace_version"] == "1.0"
        assert row["active_version_id"] and row["standard_version_id"] == row["active_version_id"]
        item = _listed(c, project)
        assert item["is_catalogue"] is False
        assert item["marketplace"] == {"pack_id": SRE, "pack_name": "SRE / Helpdesk",
                                       "source": "boss", "version": "1.0", "modified": False,
                                       "update": None, "waiting": None}
        again = {p["id"]: p for p in market.list_packs(c, project)}[SRE]
        assert again["installed"] is True and again["installed_version"] == "1.0"
        assert again["update_available"] is None
        # Installing again is a no-op: the same definition and version.
        market.install(c, project, SRE, "test")
        assert _row(c, project)["active_version_id"] == row["active_version_id"]
        # A boot seed after the install leaves it alone (never made a standard again).
        catalogue.seed(c, project)
        assert _row(c, project)["is_catalogue"] is False


def test_install_refuses_beside_a_workflow_of_the_same_key(eng, project):
    with eng.begin() as c:
        workflows.create_definition(c, project_id=project, workflow_key=IR, name="Our IR",
                                    description="", actor="test",
                                    namespace=workflows.PRODUCT_NAMESPACE)
        with pytest.raises(market.MarketplaceError, match="already has a workflow"):
            market.install(c, project, SRE, "test")


def test_the_dq_pack_installs_waiting_for_its_connectors(eng, project):
    """Without the DQ Monitor connector the DQ workflows cannot publish: they are installed and
    say what they wait for; the ontology kinds the contracts read are added with the pack."""
    from boss_engine.services.knowledge.ontology_packs import has_pack

    with eng.begin() as c:
        listed = {p["id"]: p for p in market.list_packs(c, project)}["banking-dq"]
        needs = {n["key"]: n for n in listed["needs"]}
        assert needs["mcp:dq-monitor"]["ok"] is False
        assert needs["mcp:dq-monitor"]["note"] == "not set up"
        assert listed["agents"], "the pack names the agents it adds"
        market.install(c, project, "banking-dq", "test")
        assert has_pack(c, project_id=project, pack_id="banking-dq")
        rows = [_row(c, project, k) for k in market.SHIPPED_PACKS["banking-dq"].workflow_keys]
        assert all(r and r["is_catalogue"] is False for r in rows)
        assert all(r["marketplace_pack_id"] == "banking-dq" for r in rows)
        waiting = [r for r in rows if r["marketplace_waiting"]]
        assert waiting and all("dq-monitor" in r["marketplace_waiting"] for r in waiting)
        item = _listed(c, project, waiting[0]["workflow_key"])
        assert item["marketplace"]["waiting"]


# ── update: unmodified takes it, modified is offered it ─────────────────────────────────────

def test_an_unmodified_install_takes_a_newer_pack_by_itself(eng, project, monkeypatch):
    with eng.begin() as c:
        market.install(c, project, SRE, "test")
        before = _row(c, project)
        _ship_newer(monkeypatch)
        synced = market.sync_shipped(c)
        after = _row(c, project)
        assert project in synced
        assert after["active_version_id"] != before["active_version_id"]
        assert after["marketplace_version"] == "1.1"
        assert _active_spec(c, after)["steps"][0]["title"] == "Investigate the alert"
        assert _listed(c, project)["marketplace"]["update"] is None
        # Unchanged since: nothing more.
        assert market.sync_shipped(c).get(project) is None


def test_a_modified_install_is_offered_the_update_never_overwritten(eng, project, monkeypatch):
    with eng.begin() as c:
        market.install(c, project, SRE, "test")
        _edit(c, project)
        edited = _row(c, project)
        run = _running_run(c, project, edited)
        _ship_newer(monkeypatch)
        market.sync_shipped(c)
        offered = _row(c, project)
        assert offered["active_version_id"] == edited["active_version_id"]
        assert offered["marketplace_version"] == "1.0"
        item = _listed(c, project)["marketplace"]
        assert item["modified"] is True and item["update"] == {"version": "1.1"}
        card = {p["id"]: p for p in market.list_packs(c, project)}[SRE]
        assert card["update_available"] == "1.1"
        state = market.install_state(c, offered, detail=True)
        assert state["update"]["applies_cleanly"] is True and state["update"]["changes"]
        # Take it, keeping my change: both are in the result.
        market.answer_update(c, project, offered["id"], "test", "take_keep")
        taken = _row(c, project)
        spec = _active_spec(c, taken)
        assert spec["steps"][0]["title"] == "Investigate the alert"
        assert spec["steps"][1]["title"] == "Our own build step"
        assert taken["marketplace_version"] == "1.1"
        assert market.install_state(c, taken)["update"] is None
        # The run in flight kept the version it started on.
        pinned = c.execute(text("SELECT workflow_version_id FROM workflow_runs WHERE id=:r"),
                           {"r": run}).scalar()
        assert pinned == edited["active_version_id"]


def test_not_now_hides_the_offer(eng, project, monkeypatch):
    with eng.begin() as c:
        market.install(c, project, SRE, "test")
        _edit(c, project)
        _ship_newer(monkeypatch)
        market.sync_shipped(c)
        row = _row(c, project)
        market.answer_update(c, project, row["id"], "test", "dismiss")
        assert _listed(c, project)["marketplace"]["update"] is None
        with pytest.raises(workflows.WorkflowValidationError):
            market.answer_update(c, project, row["id"], "test", "overwrite")


# ── uninstall ────────────────────────────────────────────────────────────────────────────────

def test_uninstall_is_refused_while_a_run_is_going_and_keeps_history(eng, project):
    with eng.begin() as c:
        market.install(c, project, SRE, "test")
        row = _row(c, project)
        run = _running_run(c, project, row)
        with pytest.raises(market.MarketplaceError, match="Runs are still going"):
            market.uninstall(c, project, SRE, "test")
        assert _row(c, project)["status"] == "active"
        c.execute(text("UPDATE workflow_runs SET status='completed' WHERE id=:r"), {"r": run})
        out = market.uninstall(c, project, SRE, "test")
        assert out["removed"] == [{"key": IR, "outcome": "retired"}]
        assert _listed(c, project) is None
        assert {p["id"]: p for p in market.list_packs(c, project)}[SRE]["installed"] is False
        # Installed again: the same workflow comes back, its run still readable.
        market.install(c, project, SRE, "test")
        back = _row(c, project)
        assert back["id"] == row["id"] and back["status"] == "active"


@pytest.mark.parametrize("status", ["paused", "blocked", "waiting", "compensating"])
def test_uninstall_is_refused_for_any_run_that_has_not_ended(eng, project, status):
    with eng.begin() as c:
        market.install(c, project, SRE, "test")
        run = _running_run(c, project, _row(c, project))
        c.execute(text("UPDATE workflow_runs SET status=:s WHERE id=:r"), {"s": status, "r": run})
        with pytest.raises(market.MarketplaceError, match="Runs are still going"):
            market.uninstall(c, project, SRE, "test")


def test_a_runnable_install_is_never_marked_waiting(eng, project, monkeypatch):
    """A newer pack spec that cannot publish here leaves the running version alone."""
    with eng.begin() as c:
        market.install(c, project, SRE, "test")
        before = _row(c, project)
        def blocked(*a, **k):
            raise workflows.WorkflowBindingBlockedError("needs the dq-monitor connector")
        monkeypatch.setattr("boss_engine.services.workflow_standards.record_shipped", blocked)
        market.sync_shipped(c)
        after = _row(c, project)
        assert after["marketplace_waiting"] is None
        assert after["active_version_id"] == before["active_version_id"]


def test_uninstall_deletes_a_copy_that_never_ran(eng, project):
    with eng.begin() as c:
        market.install(c, project, SRE, "test")
        assert market.uninstall(c, project, SRE, "test")["removed"] == [
            {"key": IR, "outcome": "deleted"}]
        assert _row(c, project) is None
        with pytest.raises(market.MarketplaceNotFound):
            market.uninstall(c, project, SRE, "test")


# ── publish to the org ───────────────────────────────────────────────────────────────────────

def _own_workflow(c, project, name="Lumen Pay partner review"):
    entry = next(e for e in catalogue.CATALOGUE if e["key"] == "fast_fix")
    key = workflows.free_workflow_key(c, project, name)
    d = workflows.create_definition(c, project_id=project, workflow_key=key, name=name,
                                    description="Check a partner's gateway config.",
                                    actor="test", namespace=workflows.PRODUCT_NAMESPACE)
    workflows.publish_version(c, project_id=project, definition_id=d["id"],
                              spec=catalogue.entry_spec(catalogue._staffed_for(entry, frozenset())),
                              actor="test")
    return d


def test_publish_install_republish_and_unpublish_within_the_org(eng, monkeypatch):
    with eng.begin() as c:
        source = _new_project(c, name="Northgate Gateway")
        other = _new_project(c, name="Northgate Payments")
        org = c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'Elsewhere') RETURNING id"),
                        {"o": f"org_{uuid.uuid4().hex[:8]}"}).scalar()
        ws = c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'x') "
                            "RETURNING id"), {"w": f"ws_{uuid.uuid4().hex[:8]}", "o": org}).scalar()
        outsider = _new_project(c, workspace=ws, name="Outside")
    try:
        with eng.begin() as c:
            mine = _own_workflow(c, source)
            out = market.publish(c, source, mine["id"], "test", name="Gateway tenant onboarding",
                                 version="0.3", publisher="Pat")
            pack = out["pack"]
            assert pack["version"] == "0.3" and pack["publisher"] == "Pat"
            assert pack["name"] == "Gateway tenant onboarding"
            here = {p["id"]: p for p in market.list_packs(c, source)}[pack["id"]]
            assert here["published_here"] is True and here["source"] == "org"
            with pytest.raises(market.MarketplaceError, match="published from this project"):
                market.install(c, source, pack["id"], "test")
            assert pack["id"] not in {p["id"] for p in market.list_packs(c, outsider)}
            seen = {p["id"]: p for p in market.list_packs(c, other)}[pack["id"]]
            assert seen["installed"] is False and seen["workflows"][0]["name"] == mine["name"]
            market.install(c, other, pack["id"], "test")
            copy_row = _row(c, other, mine["workflow_key"])
            assert copy_row["is_catalogue"] is False
            assert copy_row["marketplace_pack_id"] == pack["id"]
            assert copy_row["marketplace_version"] == "0.3"
            # Republish: a later version only; the unmodified copy follows.
            with pytest.raises(market.MarketplaceError, match="later version"):
                market.publish(c, source, mine["id"], "test", version="0.3")
            spec = copy.deepcopy(_active_spec(c, _row(c, source, mine["workflow_key"])))
            spec.pop(workflows.AGENT_RULES_KEY, None)
            spec["steps"][0]["title"] = "Read the partner's config"
            workflows.publish_version(c, project_id=source, definition_id=mine["id"], spec=spec,
                                      actor="test")
            before = _row(c, other, mine["workflow_key"])
            again = market.publish(c, source, mine["id"], "test")
            assert again["pack"]["version"] == "0.4"
            # OFFERED, NEVER ACTIVATED from another project: what runs there is its own call.
            offered = _row(c, other, mine["workflow_key"])
            assert offered["active_version_id"] == before["active_version_id"]
            assert offered["marketplace_version"] == "0.3"
            assert _listed(c, other, mine["workflow_key"])["marketplace"]["update"] == {"version": "0.4"}
            market.answer_update(c, other, offered["id"], "test", "take")
            followed = _row(c, other, mine["workflow_key"])
            assert followed["marketplace_version"] == "0.4"
            assert _active_spec(c, followed)["steps"][0]["title"] == "Read the partner's config"
            assert market.published_from(c, source, mine["id"])["version"] == "0.4"
            # Unpublish: gone from the Marketplace; the installed copy stays.
            with pytest.raises(market.MarketplaceNotFound):
                market.unpublish(c, other, pack["id"], "test")
            market.unpublish(c, source, pack["id"], "test")
            assert pack["id"] not in {p["id"] for p in market.list_packs(c, other)}
            assert _row(c, other, mine["workflow_key"])["status"] == "active"
            # Publishing again after unpublishing is the next version.
            assert market.publish(c, source, mine["id"], "test")["pack"]["version"] == "0.5"
    finally:
        _cleanup(eng, [other, source, outsider])
        with eng.begin() as c:
            c.execute(text("DELETE FROM workspaces WHERE id=:w"), {"w": ws})
            c.execute(text("DELETE FROM orgs WHERE id=:o"), {"o": org})


def test_only_yours_can_be_published(eng, project):
    with eng.begin() as c:
        catalogue.seed(c, project)
        standard = _row(c, project, "standard_product_delivery")
        with pytest.raises(market.MarketplaceError, match="standard"):
            market.publish(c, project, standard["id"], "test")
        market.install(c, project, SRE, "test")
        with pytest.raises(market.MarketplaceError, match="came from the Marketplace"):
            market.publish(c, project, _row(c, project)["id"], "test")
        mine = _own_workflow(c, project)
        with pytest.raises(workflows.WorkflowValidationError):
            market.publish(c, project, mine["id"], "test", version="v-two")


# ── existing projects keep their workflows (the migration) ───────────────────────────────────

def test_the_migration_keeps_existing_dq_and_incident_response_as_installed_copies(eng):
    """schema.sql's own UPDATE, run twice, in a transaction that is rolled back (the shared test
    database's other rows are not this test's to change)."""
    _, migration = _schema_block()
    with eng.connect() as c:
        tx = c.begin()
        try:
            project = _new_project(c)
            made = {}
            for key in (IR, "dq_incident_rca"):
                d = workflows.create_definition(
                    c, project_id=project, workflow_key=key, name=key, description="",
                    actor="boss", namespace=workflows.PRODUCT_NAMESPACE, is_catalogue=True)
                entry = next(e for e in catalogue.CATALOGUE if e["key"] == IR)
                v = workflows.publish_version(
                    c, project_id=project, definition_id=d["id"], actor="boss",
                    spec=catalogue.entry_spec(catalogue._staffed_for(entry, frozenset())))
                c.execute(text("UPDATE workflow_definitions SET standard_version_id = :v, "
                               "standard_content_hash = :h WHERE id = :id"),
                          {"v": v["id"], "h": v["content_hash"], "id": d["id"]})
                made[key] = (d["id"], v["id"])
            run = _running_run(c, project, _row(c, project))
            c.exec_driver_sql(migration)
            c.exec_driver_sql(migration)
            for key, (definition_id, version_id) in made.items():
                row = _row(c, project, key)
                assert row["id"] == definition_id and row["active_version_id"] == version_id
                assert row["is_catalogue"] is False and row["marketplace_version"] == "1.0"
                assert row["marketplace_pack_id"] == market.PACK_OF_KEY[key]
                assert row["standard_version_id"] == version_id
            assert _row(c, project, IR)["status"] == "active"
            status = c.execute(text("SELECT status, workflow_version_id FROM workflow_runs "
                                    "WHERE id=:r"), {"r": run}).mappings().one()
            assert status["status"] == "running" and status["workflow_version_id"] == made[IR][1]
            item = _listed(c, project)
            assert item["is_catalogue"] is False and item["marketplace"]["pack_id"] == SRE
            # A boot seed afterwards neither re-adds them as standards nor retires them.
            catalogue.seed(c, project)
            assert _row(c, project, IR)["is_catalogue"] is False
            assert _row(c, project, IR)["status"] == "active"
            listed = {p["id"]: p for p in market.list_packs(c, project)}
            assert listed[SRE]["installed"] is True
        finally:
            tx.rollback()
