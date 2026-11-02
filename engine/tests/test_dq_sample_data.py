"""The banking DQ case study's synthetic world (tools/dq_sample) is deterministic, internally
consistent, and the committed rulebook PDF carries every rule and threshold as real text.

No database, no network, no model. Run:
    PYTHONPATH=.:engine python3 -m pytest engine/tests/test_dq_sample_data.py -q
"""
from __future__ import annotations

import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

import pytest

from tools.dq_sample import data as D

PDF = Path(__file__).resolve().parents[2] / "tools" / "dq_sample" / \
    "NorthPeak_Loan_DQ_Rulebook_v3.2.pdf"


@pytest.fixture(scope="module")
def ds() -> D.Dataset:
    return D.generate()


@lru_cache(maxsize=1)
def _pdf_text() -> str:
    from pypdf import PdfReader
    text = " ".join(p.extract_text() or "" for p in PdfReader(str(PDF)).pages)
    return re.sub(r"\s+", " ", text)


def test_generation_is_deterministic():
    D.generate.cache_clear()
    a = D.generate()
    D.generate.cache_clear()
    b = D.generate()
    assert a.nodes() == b.nodes()
    assert a.edges() == b.edges()
    assert D.answer_key() == D.answer_key()
    # a different seed moves only sample keys and baselines, never the rules or the answer key
    c = D.generate(seed=7)
    assert [r.rule_id for r in c.rules] == [r.rule_id for r in a.rules]
    assert D.answer_key(7)["exception_groups"] == D.answer_key()["exception_groups"]


def test_counts(ds):
    assert len(ds.rules) == 96
    assert len({r.rule_id for r in ds.rules}) == 96
    assert len({r.monitor_id for r in ds.rules}) == 96
    assert len(ds.cdes) == 24
    assert len(ds.exception_groups) >= 12
    assert len([i for i in ds.incidents if i.status == "closed"]) >= 10
    assert len(ds.findings) >= 5 and len(ds.issues) >= 5
    assert Counter(r.family for r in ds.rules) == {
        "LN": 18, "TX": 18, "SC": 10, "AR": 10, "CH": 8, "CL": 10, "GL": 8, "RP": 10, "FD": 4}
    assert {r.dimension for r in ds.rules} == set(D.DIMENSIONS)
    retired = {rid for rid, _, _ in ds.retired_rules}
    assert len(retired) == 4 and not retired & {r.rule_id for r in ds.rules}


def test_every_reference_resolves(ds):
    columns = {c.key for t in ds.tables for c in t.columns}
    tables = {t.key for t in ds.tables}
    lines = {ln.line_id for ln in ds.report_lines}
    for r in ds.rules:
        assert r.cde_ids, r.rule_id
        for c in r.cde_ids:
            ds.cde(c)
        assert set(r.columns) <= columns, r.rule_id
        assert set(r.tables) <= tables, r.rule_id
        assert set(r.report_lines) <= lines, r.rule_id
        assert {x for x in r.related} <= {x.rule_id for x in ds.rules}, r.rule_id
        assert r.warning.value <= r.error.value, r.rule_id
        if r.feed_id:
            ds.feed(r.feed_id)
    for c in ds.cdes:
        assert set(c.columns) <= columns and set(c.reported_in) <= lines
        assert set(c.upstream) <= {x.cde_id for x in ds.cdes}
    for g in ds.exception_groups:
        r = ds.rule(g.rule_id)
        assert g.monitor_rule_id == r.monitor_id and g.rule_version == r.version
        assert g.column in columns and g.table in tables
        assert ds.calendar_day(g.cob).business_day, g.group_id
    # every edge joins two nodes that exist, through a declared relationship
    nodes = {(n["kind"], n["key"]) for n in ds.nodes()}
    declared = {(a, rel, b) for a, rel, b, _ in D.RELATIONSHIPS}
    for sk, skey, rel, tk, tkey in ds.edges():
        assert (sk, skey) in nodes and (tk, tkey) in nodes, (sk, skey, rel, tk, tkey)
        assert (sk, rel, tk) in declared
    assert {n["kind"] for n in ds.nodes()} == set(D.KG_KINDS) | set(D.CONTEXT_KINDS)


def test_cdes_map_to_real_fineract_tables(ds):
    for c in ds.cdes:
        for col in c.columns:
            t = ds.table(col.rsplit(".", 1)[0])
            assert t.fineract, col
    on_graph = {c.columns[0].split(".")[0] for c in ds.cdes} & D.CODE_GRAPH_TABLES
    assert {"m_loan", "m_loan_transaction", "m_loan_repayment_schedule",
            "m_loan_arrears_aging", "m_loan_charge", "m_client", "acc_gl_journal_entry"} <= on_graph


def test_running_example_egl_0588(ds):
    g = ds.exception_group(D.RUNNING_EXAMPLE)
    assert (g.monitor_rule_id, g.rule_version, g.cob) == ("DQX-0588", 2, "2026-10-05")
    r = ds.rule_by_monitor_id("DQX-0588")
    assert r.rule_id == "NPB-DQ-LN-003" and "CDE-LN-001" in r.cde_ids
    assert g.column == "m_loan.principal_outstanding_derived" and g.monitor_grade == "Error"
    key = D.answer_key()["exception_groups"][D.RUNNING_EXAMPLE]
    assert key["rulebook_grade"] == "Error"
    assert key["incident_action"] == "open"
    assert key["root_cause_family"] == "source_processing"
    assert key["precedent"] == "INC-2187" and key["pattern"] == "RPAT-COB-ORDER"
    assert key["lane"] == "lane_source_processing" and not key["loop_back"]
    # the precedent is real, closed, recurring, and the change that broke it is on the profile
    inc = ds.incident("INC-2187")
    assert inc.status == "closed" and inc.pattern_id == "RPAT-COB-ORDER"
    assert "INC-2041" in inc.correlated_with
    assert "CHG-7731" in {c.change_id for c in ds.profile("S-LSP").recent_changes}


def test_answer_key_covers_every_path(ds):
    key = D.answer_key()["exception_groups"]
    assert set(key) == {g.group_id for g in ds.exception_groups}
    actions = Counter(v["incident_action"] for v in key.values())
    assert actions["open"] and actions["attach"] and actions["ignore"]
    open_ids = {i.incident_id for i in ds.open_incidents()}
    for gid, v in key.items():
        if v["incident_action"] == "ignore":
            assert v["rulebook_grade"] == "Warning" and v["monitor_grade"] == "Warning"
        if v["incident_action"] == "attach" and v["attach_to"].startswith("INC-"):
            assert v["attach_to"] in open_ids
        if v["precedent"]:
            p = ds.incident(v["precedent"])
            assert p.status == "closed" and p.root_cause_family == v["root_cause_family"]
        if v["lane"]:
            assert v["lane"] in D.LANES
    # the false positive: the DQ Monitor says Error, the rulebook threshold says Warning
    fp = [g for g, v in key.items() if v.get("false_positive")]
    assert len(fp) == 1 and key[fp[0]]["lane"] == "rule_drafting"
    assert key[fp[0]]["monitor_grade"] == "Error" and key[fp[0]]["rulebook_grade"] == "Warning"
    # insufficient evidence: the lineage hop it needs is a known gap
    ie = [g for g, v in key.items() if v["loop_back"]]
    assert len(ie) == 1 and key[ie[0]]["lane"] == "insufficient_evidence"
    gap = D.KNOWN_LINEAGE_GAPS[0]
    assert ds.exception_group(ie[0]).table == gap["to"].rsplit(".", 1)[0]
    assert not [h for h in ds.lineage if h.from_column == gap["from"] and h.to == gap["to"]]
    rules = D.answer_key()["rules"]
    assert len(rules) == 96 and rules["NPB-DQ-LN-003"]["mdrm"] == ["RCON2122", "BHCK2122"]


def test_pdf_carries_every_rule_and_threshold():
    assert PDF.exists(), "run: python3 -m tools.dq_sample.rulebook"
    assert PDF.stat().st_size < 2_000_000
    from pypdf import PdfReader
    reader = PdfReader(str(PDF))
    assert 35 <= len(reader.pages) <= 50
    assert not str((reader.metadata or {}).get("/CreationDate", "")).startswith("D:2026110")
    text = _pdf_text()
    ds = D.generate()
    for r in ds.rules:
        assert f"{r.rule_id} v{r.version}" in text, r.rule_id
        assert f"Check: {r.check}" in text, r.rule_id
        assert f"Warning when: {r.warning.text}" in text, r.rule_id
        assert f"Error when: {r.error.text}" in text, r.rule_id
    for c in ds.cdes:
        assert c.cde_id in text
    for rid, _, _ in ds.retired_rules:
        assert rid in text      # listed in Appendix C, so an extractor must skip it knowingly
    assert "verify every code against the FFIEC MDRM dictionary" in text


def test_pdf_is_current_with_the_data():
    """The committed PDF was rendered from today's data (re-render if this fails)."""
    from tools.dq_sample import rulebook
    html = rulebook.render_html()
    for r in D.generate().rules:
        assert r.title in html, r.rule_id
    text = _pdf_text()
    for r in D.generate().rules:
        assert r.handling in text, r.rule_id
