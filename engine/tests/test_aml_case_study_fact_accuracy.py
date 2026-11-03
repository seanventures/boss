"""AML video-polish fact-accuracy fixes (BOSS task "AML video polish"): the analyst's cited
payment count must match the alert's real flagged count (not the rule's threshold), evidence
notes must read as a product/owner label rather than a raw account id, the deterministic wire
totals on ALERT-3's network must name the three companies that actually received the wires (never
a non-recipient relative), and a closed/escalated precedent's name must never be cut mid-word.
Run against a real disposable postgres (DATABASE_URL), never the live DB.
"""
from __future__ import annotations

import json
import os
import re
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.db import ensure_schema
from boss_engine.services import mcp_registry
from boss_engine.services.knowledge import aml_alert_pipeline as pipeline
from boss_engine.services.knowledge import aml_sample, ontology_packs

_RAW_ID = re.compile(r"\b(?:ACC|CP|CUST)-\d{3,}\b")


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
        from tools import mcp_aml_mock

        return mcp_aml_mock.TOOL_FNS[server["name"]][tool](args)

    monkeypatch.setattr(mcp_registry, "call_tool_unattended", call)
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        suffix = uuid.uuid4().hex[:8]
        org, ws, project = f"org_amlfa_{suffix}", f"ws_amlfa_{suffix}", f"p_amlfa_{suffix}"
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
    aml_sample.load_sample(connection, project_id=connection.project_id, actor="ceo")
    return connection, connection.project_id


class _Scripted:
    """Answers whatever JSON the test gave it, ignoring the prompt — a role agent whose only
    job here is to hand `_ask_role` a fixed answer to parse."""

    def __init__(self, answer):
        self._answer = answer

    def run_role(self, prompt, cwd, *, read_only=False, max_turns=None, **_kw):
        return {"summary": f"{pipeline._shared._MARK_OPEN}\n{json.dumps(self._answer)}\n"
                           f"{pipeline._shared._MARK_CLOSE}"}


def test_p0_analyst_payment_count_must_match_flagged_count_not_the_rule_threshold(
        loaded, monkeypatch):
    """[1] The AML Analyst must cite the alert's real flagged count (evidence.flagged_count), not
    the rule's "10 or more" threshold. A `payment_count` that disagrees is a hard guard failure:
    no recommendation, not a free pass."""
    c, project = loaded
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    intake = pipeline.run_alert_intake(c, project_id=project, run_id=run_id,
                                       step_id="alert_intake", alert_id="ALERT-1")
    evidence = pipeline.run_evidence_collection(
        c, project_id=project, run_id=run_id, step_id="evidence_collection",
        alert_id="ALERT-1", case_id=intake["case_id"])
    flagged_count = evidence.get("flagged_count")
    assert flagged_count, "evidence_collection must report flagged_count"
    assert flagged_count != 10, (
        "ALERT-1's real flagged count must not collapse onto the rule's '10 or more' threshold")

    context = {"steps": {"alert_intake": intake, "evidence_collection": evidence,
                          "network_mapping": {}, "historical_pattern": {}}}
    step = {"id": "aml_analysis"}

    # Mismatched payment_count (the threshold, not the real count) -> guard rejects.
    monkeypatch.setattr(pipeline, "_role_agent", lambda *a, **k: (
        _Scripted({"recommendation": "close_false_positive", "confidence": 0.8,
                   "rationale": "scripted", "cited": list(evidence.get("kyc") or {}) or ["x"],
                   "payment_count": 10}), "scripted"))
    rejected = pipeline.run_aml_analysis(c, None, project, step, context, {})
    assert rejected["recommendation"] is None, (
        "a payment_count that disagrees with evidence.flagged_count must be rejected, not accepted")

    # Correct payment_count -> the analyst's answer is allowed through.
    cited = list(pipeline._evidence_list(c, project, intake, {}))[:1] or ["x"]
    monkeypatch.setattr(pipeline, "_role_agent", lambda *a, **k: (
        _Scripted({"recommendation": "close_false_positive", "confidence": 0.8,
                   "rationale": "scripted", "cited": cited,
                   "payment_count": flagged_count}), "scripted"))
    accepted = pipeline.run_aml_analysis(c, None, project, step, context, {})
    assert accepted["recommendation"] == "close_false_positive"


def test_p1_evidence_note_names_the_product_and_owner_never_the_raw_account_id(loaded):
    """[2] evidence_collection's note must read as "{customer}'s {product}" (e.g. "Harbor Point
    Cafe LLC's Business Payroll"), never the raw account id — the id stays in structured output
    only."""
    c, project = loaded
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    intake = pipeline.run_alert_intake(c, project_id=project, run_id=run_id,
                                       step_id="alert_intake", alert_id="ALERT-1")
    evidence = pipeline.run_evidence_collection(
        c, project_id=project, run_id=run_id, step_id="evidence_collection",
        alert_id="ALERT-1", case_id=intake["case_id"])
    note = evidence["note"]
    assert "Harbor Point Cafe LLC" in note, note
    assert not _RAW_ID.search(note), f"raw id leaked into the evidence note: {note!r}"
    assert intake.get("account_id"), "the raw account id must still be on structured output"


def test_p1_network_mapping_wire_totals_name_the_real_recipients_only(loaded):
    """[4]/[5] ALERT-3's deterministic wire_recipients must be the three ring companies that
    actually received the 30 wires (never Rhea Marlow, who receives funds FROM one of them, not
    from the subject), and the aggregated total/count must match the ledger."""
    c, project = loaded
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    intake = pipeline.run_alert_intake(c, project_id=project, run_id=run_id,
                                       step_id="alert_intake", alert_id="ALERT-3")

    agent = _Scripted({"related_parties": [], "summary": "scripted"})
    orig = pipeline._role_agent
    pipeline._role_agent = lambda *a, **k: (agent, "scripted")
    try:
        mapped = pipeline.run_aml_network_mapping(
            c, project_id=project, run_id=run_id, step_id="network_mapping",
            alert_id="ALERT-3", case_id=intake["case_id"])
    finally:
        pipeline._role_agent = orig

    recipients = {r["name"] for r in (mapped.get("wire_recipients") or [])}
    assert recipients == {"Lumen Trade Ltd", "Coastal Import Partners Ltd",
                          "Brightline Holdings Ltd"}, recipients
    assert "Rhea Marlow" not in recipients
    assert mapped.get("wire_count") == 30, mapped.get("wire_count")
    assert abs(float(mapped.get("wire_total") or 0) - 272918.64) < 0.01, mapped.get("wire_total")


def test_p0_sar_narrative_rejects_a_wire_total_recipient_outside_wire_recipients(
        loaded, monkeypatch):
    """[5] The SAR Narrator's wire_total_recipients must be a subset of the deterministic
    wire_recipients; naming Rhea Marlow (who is not a wire recipient) as one must reject the
    draft (drafted: False), while naming only the real recipients drafts normally."""
    c, project = loaded
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    intake = pipeline.run_alert_intake(c, project_id=project, run_id=run_id,
                                       step_id="alert_intake", alert_id="ALERT-3")
    wire_recipients = [{"name": "Lumen Trade Ltd"}, {"name": "Coastal Import Partners Ltd"},
                       {"name": "Brightline Holdings Ltd"}]
    network = {"related_parties": [], "wire_recipients": wire_recipients,
              "wire_total": 272918.64, "wire_count": 30, "note": "scripted network"}
    context = {"steps": {"alert_intake": intake, "evidence_collection": {}, "network_mapping": network,
                          "aml_analysis": {}}}
    step = {"id": "narrative"}
    monkeypatch.setattr(pipeline, "person_determination", lambda *a, **k: {
        "determination": "escalate", "decided_by": "bsa.officer", "reason": "scripted escalate"})

    monkeypatch.setattr(pipeline, "_role_agent", lambda *a, **k: (
        _Scripted({"narrative": "WHO: Rhea Marlow. WHY: scripted.", "cited": [],
                   "wire_total_recipients": ["Lumen Trade Ltd", "Rhea Marlow"]}), "scripted"))
    bad = pipeline.run_sar_narrative(c, None, project, step, context, {})
    assert bad["drafted"] is False, "a wire_total_recipients entry outside wire_recipients must reject the draft"

    monkeypatch.setattr(pipeline, "_role_agent", lambda *a, **k: (
        _Scripted({"narrative": "WHO: Rhea Marlow, family member. WHY: scripted.", "cited": [],
                   "wire_total_recipients": ["Lumen Trade Ltd", "Coastal Import Partners Ltd",
                                             "Brightline Holdings Ltd"]}), "scripted"))
    good = pipeline.run_sar_narrative(c, None, project, step, context, {})
    assert good["drafted"] is True, good.get("note")


def test_p1_closure_precedent_name_never_cuts_the_reason_mid_word(loaded, monkeypatch):
    """[6] aml_closure's precedent node name must be "{alert_id} · {customer_name} · closed as a
    false positive" (never a fixed-length slice of the person's reason), with the full reason kept
    in attrs."""
    c, project = loaded
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    long_reason = ("documented biweekly payroll for 14 employees matches historical pattern and "
                   "the beneficial owner confirmed the payroll register on file")
    intake = pipeline.run_alert_intake(c, project_id=project, run_id=run_id,
                                       step_id="alert_intake", alert_id="ALERT-1")
    monkeypatch.setattr(pipeline, "person_determination", lambda *a, **k: {
        "determination": "close_false_positive", "decided_by": "dana.analyst",
        "reason": long_reason, "agreed_with_agent": None, "recommendation": None})

    result = pipeline.run_aml_closure(c, project_id=project, run_id=run_id,
                                      step_id="aml_closure", alert_id="ALERT-1",
                                      case_id=intake["case_id"])

    row = c.execute(text("SELECT name, attrs FROM kg_nodes WHERE id = :i"),
                    {"i": result["precedent_id"]}).mappings().first()
    assert row["name"] == "ALERT-1 · Harbor Point Cafe LLC · closed as a false positive", row["name"]
    attrs = row["attrs"] if isinstance(row["attrs"], dict) else json.loads(row["attrs"])
    assert attrs.get("reason") == long_reason, "the full reason must survive in attrs, uncut"
