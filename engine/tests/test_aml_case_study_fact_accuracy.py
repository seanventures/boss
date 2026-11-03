"""Case study 4 fix: the FLAGGED line reports flagged transactions grouped by direction + kind,
instead of summing cash deposits IN into outbound wires OUT.

Against a real disposable postgres (DATABASE_URL), the same fixture other AML tests use.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.db import ensure_schema
from boss_engine.services import mcp_registry
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflow_catalogue as catalogue
from boss_engine.services import workflows
from boss_engine.services.knowledge import aml_alert_pipeline as pipeline
from boss_engine.services.knowledge import aml_sample, ontology_packs
from tools import aml_sample_data as data
from tools import mcp_aml_mock

KEY = "aml_alert_investigation"
WORKER = "worker-aml-flagged"


def _database_url() -> str:
    return os.environ.get("BOSS_KG_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


@pytest.fixture(scope="module")
def pg_engine():
    url = _database_url()
    if not url:
        pytest.skip("DATABASE_URL must be set to a reachable postgres for these tests")
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        pytest.skip(f"postgres is not reachable: {exc.__class__.__name__}")
    ensure_schema(engine)
    return engine


@pytest.fixture()
def connection(pg_engine, monkeypatch):
    def call(settings, server, tool, args):
        return mcp_aml_mock.TOOL_FNS[server["name"]][tool](args)

    monkeypatch.setattr(mcp_registry, "call_tool_unattended", call)
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        suffix = uuid.uuid4().hex[:8]
        org, ws, project = f"org_amlf_{suffix}", f"ws_amlf_{suffix}", f"p_amlf_{suffix}"
        try:
            conn.execute(text("INSERT INTO orgs (id, name) VALUES (:i, 'AML')"), {"i": org})
            conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:i, :o, 'AML')"),
                         {"i": ws, "o": org})
            conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                              "(:i, :w, 'NorthPeak AML', :s)"), {"i": project, "w": ws, "s": project})
            conn.project_id = project  # type: ignore[attr-defined]
            yield conn
        finally:
            transaction.rollback()
            ontology_packs._PROJECT_PACK_CACHE.pop(project, None)


@pytest.fixture()
def loaded(connection):
    result = aml_sample.load_sample(connection, project_id=connection.project_id, actor="ceo")
    return connection, connection.project_id, result


def _real_group_totals():
    """Independent ground truth for ALERT-3's flagged transactions, from the generator itself —
    never the pipeline under test."""
    ds = data.generate()
    alert3 = ds.alerts["ALERT-3"]
    by_txn = {t["transaction_id"]: t for t in ds.transactions}
    flagged = [by_txn[tid] for tid in alert3["flagged_transactions"]]
    groups: dict[tuple[str, str], list[dict]] = {}
    for t in flagged:
        groups.setdefault((t["direction"], t["channel"]), []).append(t)
    return flagged, groups


# P0: evidence_collection groups ALERT-3's 12 flagged transactions by direction+kind, with real
# Decimal totals and date ranges, and no single cross-direction combined total anywhere.
def test_p0_evidence_collection_groups_alert3_by_direction_and_kind(loaded):
    c, project, _ = loaded
    flagged, real_groups = _real_group_totals()
    naive_combined_total = round(sum(t["amount"] for t in flagged), 2)

    evidence = pipeline.run_evidence_collection(
        c, project_id=project, run_id="rf1", step_id="evidence_collection", alert_id="ALERT-3")

    assert evidence["payment_count"] == len(flagged) == 12
    groups = evidence["flagged_groups"]
    found = {(g["direction"], g["channel"]): g for g in groups}
    assert set(found) == set(real_groups)
    assert sum(g["count"] for g in groups) == evidence["payment_count"]

    for key, txns in real_groups.items():
        group = found[key]
        expected_total = sum(Decimal(str(t["amount"])) for t in txns)
        assert isinstance(group["total"], Decimal)
        assert group["total"] == expected_total
        dates = sorted(t["booked_at"] for t in txns)
        assert group["first"] == dates[0] and group["last"] == dates[-1]

    line = evidence["flagged_line"]
    assert "cash deposit" in line and "outbound wire" in line
    assert f"{naive_combined_total:,.2f}" not in line
    assert line in evidence["note"]


# P0: ALERT-1 (single direction, single day) is unchanged in meaning: pinned by a snapshot of
# its own real figures, computed independently of the pipeline under test.
def test_p0_evidence_collection_alert1_unchanged(loaded):
    c, project, _ = loaded
    ds = data.generate()
    alert1 = ds.alerts["ALERT-1"]
    by_txn = {t["transaction_id"]: t for t in ds.transactions}
    flagged = [by_txn[tid] for tid in alert1["flagged_transactions"]]
    expected_total = sum(Decimal(str(t["amount"])) for t in flagged)
    expected_dates = {t["booked_at"] for t in flagged}
    assert len(expected_dates) == 1, "ALERT-1 is a single-day batch"

    evidence = pipeline.run_evidence_collection(
        c, project_id=project, run_id="rf2", step_id="evidence_collection", alert_id="ALERT-1")

    assert evidence["payment_count"] == len(flagged) == 14
    assert len(evidence["flagged_groups"]) == 1
    group = evidence["flagged_groups"][0]
    assert group["count"] == 14 and group["total"] == expected_total
    assert group["first"] == group["last"] == next(iter(expected_dates))
    line = evidence["flagged_line"]
    assert "14" in line and "outbound" in line
    assert f" to " not in line.split(":", 1)[-1] or "wire" not in line  # single-day: no range
    assert line.count(";") == 0, "a single group renders with no semicolon separator"


# P1: the real end-to-end runtime drives ALERT-3 with a scripted analyst that echoes the
# FLAGGED TRANSACTIONS line from its own prompt (proving the evidence note's flagged_line and
# the analyst's prompt never drift apart); the final recommendation states both groups and
# never a combined cross-direction total.
def test_p1_e2e_recommendation_cites_groups_not_combined_sum(loaded, monkeypatch):
    c, project, _ = loaded
    flagged, _real_groups = _real_group_totals()
    naive_combined_total = round(sum(t["amount"] for t in flagged), 2)

    class ScriptedEchoAgents:
        def run_role(self, prompt, cwd, *, read_only=False, max_turns=None, **_kw):
            if prompt.startswith("# AML Analyst"):
                marker = "FLAGGED TRANSACTIONS:"
                assert marker in prompt, "the analyst prompt must carry the evidence's own line"
                flagged_line = prompt.split(marker, 1)[1].splitlines()[0].strip()
                answer = {"recommendation": "escalate", "confidence": 0.9,
                          "rationale": flagged_line, "cited": [], "precedents_applied": []}
            elif prompt.startswith("# Network Mapper"):
                answer = {"related_parties": [], "summary": "scripted"}
            else:
                answer = {"narrative": "WHO: Q. Marlow.\n\nWHY: scripted.", "cited": []}
            return {"summary": f"{pipeline._shared._MARK_OPEN}\n{json.dumps(answer)}\n"
                               f"{pipeline._shared._MARK_CLOSE}"}

    monkeypatch.setattr(pipeline, "_role_agent", lambda *a, **k: (ScriptedEchoAgents(), "s"))

    entry = next(e for e in catalogue.CATALOGUE if e["key"] == KEY)
    definition_id = c.execute(text("SELECT id FROM workflow_definitions WHERE project_id = :p "
                                   "AND workflow_key = :k"), {"p": project, "k": KEY}).scalar()
    version = c.execute(text("SELECT id FROM workflow_versions WHERE workflow_definition_id = :d "
                             "ORDER BY version DESC LIMIT 1"), {"d": definition_id}).scalar()
    run_id = workflows.start_run(c, project_id=project, actor="ceo",
                                 input_data={"alert_id": "ALERT-3"},
                                 dedupe_key=f"amlf_{uuid.uuid4().hex[:10]}",
                                 workflow_version_id=version)["id"]
    spec_steps = {s["id"]: s for s in catalogue.entry_spec(entry)["steps"]}

    from types import SimpleNamespace
    ctx = SimpleNamespace(settings=None, engine=None)
    for _ in range(40):
        claimed = workflows.claim_step(c, project_id=project, run_id=run_id, worker_id=WORKER,
                                       lease_seconds=600)
        if claimed is not None:
            step = spec_steps[claimed["step_id"]]
            output = runner.execute_step(
                c, ctx, project_id=project, actor="ceo", step=step, spec_steps=spec_steps,
                context={**runner._run_context(c, run_id), "run_id": run_id})
            workflows.complete_step(c, project_id=project, step_run_id=claimed["id"],
                                    worker_id=WORKER, output=output, evidence=[])
            continue
        workflows.advance_run(c, project, run_id)
        waiting = c.execute(text(
            "SELECT step_id FROM workflow_step_runs WHERE workflow_run_id = :r AND "
            "status = 'waiting' AND decision_id IS NOT NULL"), {"r": run_id}).scalars().all()
        if waiting:
            break
    else:
        raise AssertionError("the run did not reach the analyst decision gate")

    analysis = c.execute(text("SELECT output FROM workflow_step_runs WHERE workflow_run_id = :r "
                              "AND step_id = 'aml_analysis' ORDER BY iteration DESC LIMIT 1"),
                         {"r": run_id}).scalar() or {}
    rationale = json.dumps(analysis.get("produced") or analysis)
    assert "cash deposit" in rationale and "outbound wire" in rationale
    assert f"{naive_combined_total:,.2f}" not in rationale


# P1: the fail-closed reconciliation inside the grouping helper raises rather than returning a
# note when a group's figures do not reconcile to the real transaction data.
def test_p1_flagged_groups_reconciliation_raises_on_corrupted_total(loaded, monkeypatch):
    c, project, _ = loaded
    facts = pipeline._alert_facts(c, project, "ALERT-3")

    real_attrs = pipeline._attrs

    def tampered_attrs(node):
        attrs = dict(real_attrs(node))
        if attrs.get("kind") == "transaction" or "amount" in attrs:
            attrs["amount"] = (attrs.get("amount") or 0) + 999999
        return attrs

    monkeypatch.setattr(pipeline, "_attrs", tampered_attrs)
    with pytest.raises(ValueError):
        pipeline._flagged_groups(c, project, facts["transaction_node_ids"])
