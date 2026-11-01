"""A bank's Knowledge Graph from its rulebook (rulebook source G1, G3–G7, G9).

The NorthPeak Bank Loan DQ Rulebook v3.2 PDF goes in through Knowledge Graph › Documents' upload:
sections proposed, nothing written until a person confirms; then the Rulebook Analyst (played by
the scripted conformance engine, tools/dq_sample/scripted_analyst.py, which reads only the
sections' text) proposes one data_quality_rule per rule card with its thresholds and the clause as
evidence, measured against ``answer_key()``. Then: embeddings for the new kinds, the honest
"not embedded" words, citations of clauses and DQ rules logged, the kg_neighbors tool, the agent's
search finding the rules by meaning, and the words-vs-meaning comparison.

Breaks caught: a split that is not deterministic, drops a rule card, or keeps the page footer; a
write before a person confirms, or an agent confirming; a re-upload making new nodes; a proposal
from an unreadable answer, a fabricated quote or an id the clause does not print; a retired rule
proposed; thresholds not approved with their rule; a citation of a clause dropped; a meaning hit
the words column also finds (the demo query proves nothing).

The project's ontology is supplied by patching the effective kinds (the banking-dq pack itself is
being rewritten elsewhere); a project WITHOUT the DQ kinds is refused in words.
"""
from __future__ import annotations

import io
import math
import os
import re
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.db import ensure_schema
from boss_engine.services.knowledge import (
    document_upload, dq_rulebook_sample, embeddings, find, neighbors, ontology_packs, rule_scan,
    rulebook_extract, rules, view,
)
from boss_engine.services.knowledge import learning_conversations as lc
from boss_engine.services.knowledge.store import EDGE_KINDS, NODE_KINDS, PostgresKnowledgeStore
from tools.dq_sample import data as D
from tools.dq_sample import measure as M
from tools.dq_sample.scripted_analyst import ScriptedRulebookAnalyst

P = "proj_rulebook_kg"
ROOT = Path(__file__).resolve().parents[2]
PDF = ROOT / "tools" / "dq_sample" / "NorthPeak_Loan_DQ_Rulebook_v3.2.pdf"
STORE = PostgresKnowledgeStore()

DQ_KINDS = frozenset({"document", "clause", "regulation", "data_quality_rule", "dq_threshold", "cde",
                      "database_table", "database_column", "report_line", "system", "owner"})
DQ_EDGES = frozenset({"contains", "implemented_by", "gap", "CHECKS", "APPLIES_TO", "REACHES", "OF"})

#: The demo query for the words-vs-meaning comparison (G9): its words never all appear in TX-009
#: ("A short payment is held in suspense…": no "happens"), so the words column misses it, while
#: by meaning ("short payment" ~ "a payment less than a periodic payment") it is a top hit.
DEMO_QUERY = "What happens when a payment is short?"


def _database_url() -> str:
    return os.environ.get("BOSS_KG_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


class FakeGovernor:
    estimate_usd_micros = 150_000

    def __init__(self) -> None:
        self.reserved = 0

    def reserve(self):
        self.reserved += 1
        return ("held", self.reserved)

    def settle(self, held, usage):
        return 0

    def fail(self, held, error):  # pragma: no cover
        pass


class Ctx:
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def begin(self):
        yield self.connection


#: A scripted embedder: what each word MEANS, as a few concept axes — "short", "partial", "less"
#: and "smaller" are one idea; "balance", "outstanding" and "principal" another. Deterministic,
#: no model.
_CONCEPTS = {
    "payment": ("payment", "payments", "repayment", "repayments", "paid", "pay", "installment"),
    "short": ("short", "partial", "less", "smaller", "shortfall"),
    "balance": ("balance", "outstanding", "principal"),
    "loan": ("loan", "loans"),
    "date": ("date", "dates", "day", "days", "overdue"),
    "customer": ("client", "customer", "birth"),
    "ledger": ("ledger", "journal", "gl"),
    "report": ("report", "rcon2122", "call", "reporting"),
}


class ConceptEmbedder:
    model_id = "scripted:concepts:9"

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        out = []
        for value in texts:
            words = re.findall(r"[a-z0-9]+", value.lower())
            vector = [0.05]
            for terms in _CONCEPTS.values():
                vector.append(float(sum(1 for w in words if w in terms)))
            norm = math.sqrt(sum(x * x for x in vector)) or 1.0
            out.append([x / norm for x in vector])
        return out


@pytest.fixture(scope="module")
def pg_engine():
    url = _database_url()
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if not url or "test" not in name:
        pytest.skip("DATABASE_URL must name a disposable test database")
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        pytest.skip(f"postgres is not reachable: {exc.__class__.__name__}")
    ensure_schema(engine)
    return engine


@pytest.fixture(scope="module")
def kinds():
    """The project's ontology: core ∪ the regulatory and DQ kinds (patched, see the docstring)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ontology_packs, "effective_node_kinds",
                   lambda connection, *, project_id: NODE_KINDS | DQ_KINDS)
        mp.setattr(ontology_packs, "effective_edge_kinds",
                   lambda connection, *, project_id: EDGE_KINDS | DQ_EDGES)
        yield


def _project(conn) -> None:
    conn.execute(text("INSERT INTO orgs (id, name) VALUES ('org_rbkg', 'Rulebook KG')"))
    conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_rbkg', 'org_rbkg', 'RB')"))
    conn.execute(text(f"INSERT INTO projects (id, workspace_id, name, slug) VALUES ('{P}', 'ws_rbkg', 'RB', 'rb-kg')"))


def _catalogue(conn) -> None:
    """A slice of the bank's platform catalogue, as another loader writes it (README kinds)."""
    for kind, key, name, attrs in (
        ("cde", "CDE-LN-001", "Loan principal outstanding", {}),
        ("cde", "CDE-TX-001", "Transaction amount", {}),
        ("database_table", "m_loan", "m_loan", {}),
        ("database_column", "m_loan.principal_outstanding_derived", "m_loan.principal_outstanding_derived", {}),
        ("report_line", "CR-RCC-12", "RC-C Part I item 12: Total loans", {"mdrm": "RCON2122"}),
    ):
        STORE.upsert_node(conn, project_id=P, kind=kind, node_key=key, name=name, body="",
                          trust_tier="observed", actor_kind="system", attrs=attrs)


@pytest.fixture(scope="module")
def world(pg_engine, kinds):
    """The rulebook uploaded and confirmed, the catalogue slice loaded, and the scripted
    extraction run — once, in one transaction rolled back at the end."""
    with pg_engine.connect() as conn:
        outer = conn.begin()
        try:
            _project(conn)
            _catalogue(conn)
            preview = dq_rulebook_sample.stage(conn, project_id=P, actor_kind="person", actor_ref="ceo")
            written = document_upload.confirm_upload(conn, project_id=P, upload_id=preview["upload_id"],
                                                     actor_kind="person", actor_ref="ceo")
            agent = ScriptedRulebookAnalyst()
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(rulebook_extract, "_role_agent", lambda *a, **k: (agent, "scripted"))
                mp.setattr(rule_scan, "Governor", lambda *a, **k: FakeGovernor())
                result = rulebook_extract.run_step(
                    conn, None, P, {"id": "extract", "run_id": ""}, {}, {"document_id": written["document_id"]})
            # Embedded once (the background pass, with the scripted embedder): every test reads it.
            embedder = ConceptEmbedder()
            while embeddings.embed_pending(conn, STORE, project_id=P, embedder=embedder, limit=500):
                pass
            yield {"conn": conn, "preview": preview, "written": written, "result": result, "agent": agent,
                   "embedder": embedder}
        finally:
            outer.rollback()


@pytest.fixture()
def c(world):
    conn = world["conn"]
    nested = conn.begin_nested()
    try:
        yield conn
    finally:
        nested.rollback()


def _dq(conn, rule_id: str) -> dict:
    return dict(conn.execute(text(
        "SELECT id, kind, name, trust_tier, attrs FROM kg_nodes WHERE project_id = :p AND kind = 'data_quality_rule' "
        "AND node_key = :k"), {"p": P, "k": rule_id}).mappings().one())


def _clause(conn, section: str) -> dict:
    return dict(conn.execute(text(
        "SELECT id, name, body, attrs FROM kg_nodes WHERE project_id = :p AND kind = 'clause' AND name = :s "
        "AND valid_to IS NULL"), {"p": P, "s": section}).mappings().one())


# ── G3: the split (no database) ────────────────────────────────────────────────────────────────

def _pages():
    return document_upload.read_pages(PDF.read_bytes(), PDF.name)


def test_the_shipped_rulebook_splits_into_one_section_per_rule_card_plus_its_front_matter():
    pages = _pages()
    first = document_upload.propose_sections(pages)
    again = document_upload.propose_sections(_pages())
    assert [(s.title, s.page, s.page_end, s.lines) for s in first] == \
        [(s.title, s.page, s.page_end, s.lines) for s in again], "the split is deterministic"
    cards = [s for s in first if s.kind == "rule_card"]
    assert sorted(s.rule_id for s in cards) == sorted(D.answer_key()["rules"]), "one section per rule card"
    assert not set(D.answer_key()["retired_rules"]) & {s.rule_id for s in cards}
    titles = [s.title for s in first if s.kind != "rule_card"]
    for heading in ("0.1 Purpose", "1.1 Warning and Error", "5. Critical data element register",
                    "Appendix C. Change log and retired rules", "Appendix D. Glossary"):
        assert heading in titles
    assert first[0].kind == "title_page" and first[1].kind == "contents"
    assert len(first) == 136
    # Every line is the extracted text, verbatim, on the page it cites; no running footer.
    for section in first:
        for line, page in zip(section.lines, section.line_pages):
            assert line in pages[page - 1]
            assert "Page " not in line or " of 45" not in line
        if section.page == section.page_end:
            assert section.text in pages[section.page - 1]


def test_the_measuring_script_reports_the_scripted_analyst_at_the_bar(capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("measure_rulebook_extraction",
                                                  ROOT / "scripts" / "kg" / "measure_rulebook_extraction.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--scripted"]) == 0
    out = capsys.readouterr().out
    assert "Rules: 96 of 96 found; 96 proposed (precision 100.0%, recall 100.0%)." in out
    assert "Retired rules proposed: 0." in out


def test_an_image_only_pdf_is_refused_in_words():
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    with pytest.raises(document_upload.UploadRefused, match="no text in any of them"):
        document_upload.read_pages(buffer.getvalue(), "scan.pdf")
    with pytest.raises(document_upload.UploadRefused, match="neither a PDF nor a text file"):
        document_upload.read_pages(b"\x00\x01binary", "x.bin")


def test_a_person_may_drop_merge_and_split_sections_and_nothing_is_retyped():
    sections = document_upload._as_dicts(document_upload.propose_sections(_pages()))
    total = sum(len(s["lines"]) for s in sections)
    edited = document_upload.apply_edits(sections, [
        {"op": "drop", "key": "s2"},                        # the contents page
        {"op": "merge", "key": "s2"},                       # "0. About…" + "0.1 Purpose"
        {"op": "split", "key": "s2", "line": 1},            # …and apart again at "0.1 Purpose"
    ])
    assert edited[1]["title"] == "0. About this rulebook" and edited[2]["title"] == "0.1 Purpose"
    assert sum(len(s["lines"]) for s in edited) == total - len(sections[1]["lines"])
    assert [s["key"] for s in edited] == [f"s{i}" for i in range(1, len(edited) + 1)]
    with pytest.raises(document_upload.UploadRefused, match="nothing after it"):
        document_upload.apply_edits(sections, [{"op": "merge", "key": sections[-1]["key"]}])
    with pytest.raises(document_upload.UploadRefused, match="Unknown edit"):
        document_upload.apply_edits(sections, [{"op": "rewrite", "key": "s1"}])


# ── G3: confirm before writing ──────────────────────────────────────────────────────────────────

def test_nothing_is_written_until_a_person_confirms_and_an_agent_cannot(c, world):
    before = c.execute(text("SELECT count(*) FROM kg_nodes WHERE project_id = :p AND kind IN ('document','clause')"),
                       {"p": P}).scalar()
    preview = document_upload.stage_upload(c, project_id=P, payload=b"1. Scope\nAll loans.\n2. Rule\nNo nulls.",
                                           filename="tiny.txt", actor_kind="agent", actor_ref="rulebook_analyst")
    assert preview["status"] == "pending" and preview["counts"]["sections"] == 2
    assert [s["title"] for s in preview["sections"]] == ["1. Scope", "2. Rule"]
    after = c.execute(text("SELECT count(*) FROM kg_nodes WHERE project_id = :p AND kind IN ('document','clause')"),
                      {"p": P}).scalar()
    assert after == before, "a preview writes nothing to the graph"
    with pytest.raises(PermissionError, match="Only a person confirms"):
        document_upload.confirm_upload(c, project_id=P, upload_id=preview["upload_id"], actor_kind="agent",
                                       actor_ref="rulebook_analyst")
    done = document_upload.confirm_upload(c, project_id=P, upload_id=preview["upload_id"], actor_kind="person",
                                          actor_ref="ceo")
    assert len(done["clause_ids"]) == 2
    with pytest.raises(document_upload.UploadRefused, match="already confirmed"):
        document_upload.confirm_upload(c, project_id=P, upload_id=preview["upload_id"], actor_kind="person",
                                       actor_ref="ceo")


def test_the_confirmed_rulebook_is_one_document_and_a_clause_per_section_with_page_citations(c, world):
    written = world["written"]
    assert written["sections"] == 136 and written["rule_cards"] == 96
    card = _clause(c, "NPB-DQ-LN-003")
    assert card["attrs"]["page"] == 15
    assert card["attrs"]["citation"] == "NorthPeak Bank: Loan Data Quality Rulebook v3.2, NPB-DQ-LN-003, p. 15"
    assert card["body"].startswith("NPB-DQ-LN-003 v2 — Principal outstanding reconciles to its components")
    assert card["body"] in _pages()[14], "the clause is the page's text, verbatim"
    documents = [d for d in __import__("boss_engine.services.knowledge.documents", fromlist=["x"]).list_documents(
        c, project_id=P) if d["id"] == written["document_id"]]
    assert documents[0]["pages"] == 45 and len(documents[0]["clauses"]) == 136
    assert documents[0]["clauses"][0]["kind"] == "title_page", "clauses keep the document's order"


def test_uploading_the_same_file_again_lands_on_the_same_nodes(c, world):
    again = dq_rulebook_sample.stage(c, project_id=P, actor_kind="person", actor_ref="ceo")
    done = document_upload.confirm_upload(c, project_id=P, upload_id=again["upload_id"], actor_kind="person",
                                          actor_ref="ceo")
    assert done["document_id"] == world["written"]["document_id"]
    assert sorted(done["clause_ids"]) == sorted(world["written"]["clause_ids"])


# ── G5: the extraction ──────────────────────────────────────────────────────────────────────────

def test_the_scripted_analyst_meets_the_answer_key(c, world):
    result = world["result"]
    assert result["status"] == "succeeded" and result["rules_proposed"] == 96
    assert result["thresholds_proposed"] == 192 and not result["unread_sections"] and not result["refused"]
    measured = M.measure(rulebook_extract.proposals_of(c, P, scan_id=result["scan_id"]))
    assert measured["recall"] >= 0.95 and measured["precision"] >= 0.95, M.report(measured)
    assert measured["retired_proposed"] == []
    assert measured["threshold_accuracy"]["value"] >= 0.95, M.report(measured)
    assert measured["threshold_accuracy"]["metric"] >= 0.95, M.report(measured)
    for field in ("title", "statement", "dimension", "severity", "cdes", "columns", "mdrm", "owner"):
        assert measured["field_accuracy"][field] >= 0.95, (field, M.report(measured))
    # The agent read the PDF's sections, never the sample data: every prompt is sections text.
    assert all("[S1]" in prompt for prompt in world["agent"].prompts)


def test_each_rule_cites_its_clause_and_carries_its_thresholds(c, world):
    rule = _dq(c, "NPB-DQ-LN-003")
    clause = _clause(c, "NPB-DQ-LN-003")
    assert rule["trust_tier"] == "inferred" and rule["attrs"]["status"] == "proposed"
    assert rule["name"] == "NPB-DQ-LN-003 — Principal outstanding reconciles to its components"
    evidence = c.execute(text("SELECT attrs FROM kg_edges WHERE src_id = :r AND dst_id = :c AND kind = 'evidenced_by'"),
                         {"r": rule["id"], "c": clause["id"]}).scalar_one()
    assert evidence["quote"].startswith("Check: For every active loan") and evidence["page"] == 15
    payload = rules.rule_payload(c, P, rule["id"])
    assert payload["kind"] == "data_quality_rule" and payload["evidence"] == []
    (chip,) = payload["clause_evidence"]
    assert chip["section"] == "NPB-DQ-LN-003" and chip["page"] == 15 and chip["document"].startswith("NorthPeak")
    levels = {t["level"]: t for t in payload["dq"]["thresholds"]}
    assert levels["warning"]["value"] == pytest.approx(0.0005) and levels["error"]["value"] == pytest.approx(0.0025)
    assert levels["warning"]["metric"] == "rate" and levels["warning"]["node_id"]
    of = c.execute(text("SELECT count(*) FROM kg_edges WHERE dst_id = :r AND kind = 'OF'"), {"r": rule["id"]}).scalar()
    assert of == 2


def test_rules_link_to_the_catalogue_by_name_and_never_invent_a_node(c, world):
    rule = _dq(c, "NPB-DQ-LN-003")
    linked = {(r["kind"], r["node_key"]) for r in c.execute(text(
        "SELECT e.kind, n.node_key FROM kg_edges e JOIN kg_nodes n ON n.id = e.dst_id WHERE e.src_id = :r "
        "AND e.kind IN ('CHECKS', 'APPLIES_TO', 'REACHES')"), {"r": rule["id"]}).mappings()}
    assert linked == {("CHECKS", "CDE-LN-001"), ("APPLIES_TO", "m_loan.principal_outstanding_derived"),
                      ("APPLIES_TO", "m_loan"), ("REACHES", "CR-RCC-12")}
    links = rule["attrs"]["links"]
    assert "m_loan.principal_disbursed_derived" in links["unmatched"]["column"]
    assert "BHCK2122" in links["unmatched"]["report_line"]
    # Unmatched names became no node.
    assert c.execute(text("SELECT count(*) FROM kg_nodes WHERE project_id = :p AND node_key = 'BHCK2122'"),
                     {"p": P}).scalar() == 0


def test_an_unreadable_answer_proposes_nothing(c, world, monkeypatch):
    class Mumbles:
        def run_role(self, prompt, cwd, **_kw):
            return {"summary": "I could not read these sections, sorry."}

    monkeypatch.setattr(rulebook_extract, "_role_agent", lambda *a, **k: (Mumbles(), "scripted"))
    monkeypatch.setattr(rule_scan, "Governor", lambda *a, **k: FakeGovernor())
    before = c.execute(text("SELECT count(*) FROM kg_nodes WHERE project_id = :p AND kind = 'data_quality_rule'"),
                       {"p": P}).scalar()
    out = rulebook_extract.run_step(c, None, P, {"id": "extract", "run_id": "wfr_mumble000001"}, {},
                                    {"document_id": world["written"]["document_id"]})
    assert out["rules_proposed"] == 0 and len(out["unread_sections"]) == 136
    assert "went unread" in out["summary"]
    assert c.execute(text("SELECT count(*) FROM kg_nodes WHERE project_id = :p AND kind = 'data_quality_rule'"),
                     {"p": P}).scalar() == before


def test_a_fabricated_quote_or_an_id_the_clause_does_not_print_is_refused(c, world):
    clause = _clause(c, "NPB-DQ-TX-009")
    with pytest.raises(rules.RuleRefused, match="word for word"):
        rules.propose_dq_rule(c, project_id=P, rule_id="NPB-DQ-TX-009", title="t", statement="s",
                              clause=clause["id"], quote="Check: every payment is credited the same day.")
    with pytest.raises(rules.RuleRefused, match="not printed in clause"):
        rules.propose_dq_rule(c, project_id=P, rule_id="NPB-DQ-TX-099", title="t", statement="s",
                              clause=clause["id"], quote="Statement: A payment less than a periodic payment")
    # A retired rule's id appears only in Appendix C's table; its own card does not exist.
    retired = _clause(c, "C.2 Retired rules")
    assert "NPB-DQ-LN-019" in retired["body"]
    assert not c.execute(text("SELECT 1 FROM kg_nodes WHERE project_id = :p AND node_key = 'NPB-DQ-LN-019'"),
                         {"p": P}).first()


def test_a_project_without_the_dq_kinds_is_refused_in_words(c, world, monkeypatch):
    monkeypatch.setattr(ontology_packs, "effective_node_kinds",
                        lambda connection, *, project_id: NODE_KINDS | {"document", "clause"})
    with pytest.raises(rulebook_extract.ExtractionRefused, match="banking-dq ontology pack"):
        rulebook_extract.run_step(c, None, P, {"id": "extract"}, {}, {"document_id": world["written"]["document_id"]})
    assert dq_rulebook_sample.status(c, project_id=P)["can_extract"] is False


# ── G4: review ─────────────────────────────────────────────────────────────────────────────────

def test_rules_review_lists_dq_rules_and_approving_one_approves_its_thresholds(c, world):
    listed = rules.list_rules(c, project_id=P, status="proposed", scan_id=world["result"]["scan_id"])
    assert listed["counts"]["proposed"] == 96
    card = next(r for r in listed["items"] if r["node"]["key"] == "NPB-DQ-TX-009")
    assert card["dq"]["legal_reference"].startswith("12 CFR 1026.36(c)(1)(ii)")
    assert card["clause_evidence"][0]["section"] == "NPB-DQ-TX-009"
    rules.approve_rule(c, project_id=P, rule_id=card["node"]["id"], actor_ref="ceo", actor_name="Dana Whitfield")
    tiers = {r[0] for r in c.execute(text(
        "SELECT trust_tier FROM kg_nodes WHERE project_id = :p AND kind = 'dq_threshold' AND attrs->>'rule_id' = "
        "'NPB-DQ-TX-009'"), {"p": P})}
    assert tiers == {"approved"}
    assert rule_scan.open_proposals(c, P, world["result"]["scan_id"]) == 95


def test_editing_a_dq_rule_keeps_its_id_links_and_thresholds(c, world):
    rule = _dq(c, "NPB-DQ-LN-003")
    out = rules.edit_rule(c, project_id=P, rule_id=rule["id"], actor_ref="ceo",
                          statement="The reported loan balance equals disbursed less repaid less written off.")
    new = out["rule"]
    assert new["node"]["key"] == "NPB-DQ-LN-003.v2" and new["status"] == "approved"
    assert new["title"] == "Principal outstanding reconciles to its components"
    assert {t["level"] for t in new["dq"]["thresholds"]} == {"warning", "error"}
    assert new["clause_evidence"][0]["section"] == "NPB-DQ-LN-003"
    assert c.execute(text("SELECT count(*) FROM kg_edges WHERE src_id = :r AND kind = 'CHECKS'"),
                     {"r": new["node"]["id"]}).scalar() == 1


def test_rejecting_a_dq_rule_closes_its_thresholds(c, world):
    rule = _dq(c, "NPB-DQ-CH-001")
    rules.reject_rule(c, project_id=P, rule_id=rule["id"], actor_ref="ceo", note="Not ours.")
    live = c.execute(text("SELECT count(*) FROM kg_nodes WHERE project_id = :p AND kind = 'dq_threshold' "
                          "AND attrs->>'rule_id' = 'NPB-DQ-CH-001' AND valid_to IS NULL"), {"p": P}).scalar()
    assert live == 0


def test_a_rule_from_a_clause_is_a_business_rule_too(c, world):
    """G4: kg_propose_rule takes {clause, quote} evidence; the clause is evidence, not scope."""
    clause = _clause(c, "1.2 Severity only moves up")
    quote = clause["body"].split("\n")[1]
    rule = rules.propose_rule(c, project_id=P, statement="A rule's severity is never lowered by a re-grade.",
                              title="Severity only moves up", category="other", scope={},
                              evidence=[{"clause": clause["id"], "quote": quote}], rationale="", confidence=0.8,
                              actor_kind="agent", actor_ref="rulebook_analyst")
    assert rule["evidence"] == [] and rule["clause_evidence"][0]["section"] == "1.2 Severity only moves up"
    assert rule["scope"]["symbols"] == []


# ── G1: embeddings ─────────────────────────────────────────────────────────────────────────────

def test_clauses_documents_dq_rules_thresholds_and_cdes_are_embedded(c, world):
    embedder = world["embedder"]
    assert embeddings.embed_pending(c, STORE, project_id=P, embedder=embedder) == 0, "nothing left behind"
    kinds = {r[0]: r[1] for r in c.execute(text(
        "SELECT n.kind, count(*) FROM kg_embeddings e JOIN kg_nodes n ON n.id = e.node_id WHERE e.project_id = :p "
        "AND e.model_id = :m GROUP BY n.kind"), {"p": P, "m": embedder.model_id}).all()}
    assert kinds["clause"] == 136 and kinds["document"] == 1 and kinds["data_quality_rule"] == 96
    assert kinds["dq_threshold"] == 192 and kinds["cde"] == 2
    assert "database_table" not in kinds and "report_line" not in kinds


def test_the_panel_says_the_truth_about_a_node_that_has_no_vector(c, world):
    table = c.execute(text("SELECT * FROM kg_nodes WHERE project_id = :p AND node_key = 'm_loan'"),
                      {"p": P}).mappings().one()
    said = view._vector(c, P, table, "knowledge", frozenset(), None)
    assert said["embedded"] is False and said["waiting"] is False
    assert said["reason"] == "Not embedded: database tables are found by name, words and links, not by meaning"
    # An embedded kind with no vector yet: never "has not reached it" — it is waiting, or the
    # project has no embedding model and says why.
    new_id = STORE.upsert_node(c, project_id=P, kind="cde", node_key="CDE-ZZ-999", name="A new CDE", body="",
                               trust_tier="observed", actor_kind="system")
    row = c.execute(text("SELECT * FROM kg_nodes WHERE id = :i"), {"i": new_id}).mappings().one()
    said = view._vector(c, P, row, "domain", frozenset(), None)
    assert said["embedded"] is False and "has not reached it" not in said["reason"]
    assert said["reason"].startswith("Not embedded: ") or said["reason"].startswith("Waiting to be embedded")


# ── G6: agents search, walk and cite ───────────────────────────────────────────────────────────

def test_kg_search_answers_the_two_questions_by_meaning(c, world, monkeypatch):
    from boss_engine.tools import knowledge_tools

    embedder = world["embedder"]
    monkeypatch.setattr(embeddings, "configured_embedder", lambda *a, **k: (embedder, None))
    found = knowledge_tools._search(Ctx(c), project_id=P, actor="ceo",
                                    args={"query": "Which rules protect the loan balance?"})
    keys = [h.get("name", "") for h in found["hits"][:10]]
    assert found["retrieval"]["mode"] == "hybrid"
    assert any(k.startswith("NPB-DQ-LN-003") for k in keys), keys
    found = knowledge_tools._search(Ctx(c), project_id=P, actor="ceo", args={"query": DEMO_QUERY})
    top = [h.get("name", "") for h in found["hits"][:10]]
    assert any(n.startswith("NPB-DQ-TX-009") for n in top), top


def test_kg_neighbors_walks_a_rule_to_its_cde_thresholds_and_clause(c, world):
    rule = _dq(c, "NPB-DQ-LN-003")
    out = neighbors.typed_neighbors(c, project_id=P, node="NPB-DQ-LN-003")
    seen = {(n["edge"], n["kind"]) for n in out["neighbors"]}
    assert {("CHECKS", "cde"), ("APPLIES_TO", "database_column"), ("OF", "dq_threshold"),
            ("evidenced_by", "clause"), ("REACHES", "report_line")} <= seen
    assert out["node"]["id"] == rule["id"] and all(n["hop"] == 1 for n in out["neighbors"])
    two = neighbors.typed_neighbors(c, project_id=P, node=rule["id"], hops=5, edge_kinds=["checks"])
    assert two["hops"] == 2
    assert {n["kind"] for n in two["neighbors"] if n["hop"] == 1} == {"cde"}
    with pytest.raises(LookupError):
        neighbors.typed_neighbors(c, project_id=P, node="NPB-DQ-ZZ-999")


def test_a_stage_never_walks_to_a_proposed_rule(c, world):
    from boss_engine.services.knowledge import scopes

    policy = scopes.policy_for(stage="triage")
    cde = c.execute(text("SELECT id FROM kg_nodes WHERE project_id = :p AND node_key = 'CDE-LN-001'"),
                    {"p": P}).scalar()
    out = neighbors.typed_neighbors(c, project_id=P, node=cde, policy=policy)
    assert not [n for n in out["neighbors"] if n["kind"] == "data_quality_rule"]


def test_citing_a_clause_or_a_dq_rule_is_recorded(c, world):
    rule = _dq(c, "NPB-DQ-TX-009")
    clause = _clause(c, "NPB-DQ-TX-009")
    c.execute(text("INSERT INTO agent_threads (id, project_id, actor, surface, agent_role) "
                   "VALUES ('athr_rbkg_ask01', :p, 'ceo', 'chat', '')"), {"p": P})
    c.execute(text("INSERT INTO agent_messages (id, thread_id, role, content) VALUES "
                   "('amsg_rbkg_q1', 'athr_rbkg_ask01', 'user', :q)"), {"q": DEMO_QUERY})
    reply = (f"A short payment is held in suspense [R:{rule['id']}], as the rulebook says [D:{clause['id']}].")
    cited = lc.turn_done(c, project_id=P, thread_id="athr_rbkg_ask01", act_id=None, message_id="amsg_rbkg_a1",
                         reply=reply, tool_calls=[])
    assert sorted(cited) == sorted([rule["id"], clause["id"]])
    used = {r[0] for r in c.execute(text("SELECT fact_id FROM kg_usage WHERE project_id = :p AND use_kind = 'cited' "
                                         "AND agent_thread_id = 'athr_rbkg_ask01'"), {"p": P})}
    assert used == {rule["id"], clause["id"]}
    names = view.names(c, P, [rule["id"], clause["id"], "kgn_notreal000000"])
    assert names[rule["id"]]["title"].startswith("NPB-DQ-TX-009 — ") and names[clause["id"]]["kind"] == "clause"
    assert "kgn_notreal000000" not in names


def test_written_kinds_include_the_rulebook_and_a_stage_reads_only_approved_dq_rules(c, world):
    assert {"clause", "data_quality_rule", "dq_threshold", "cde"} <= find.WRITTEN_KINDS
    found = find.find_written(c, P, "NPB-DQ-LN-003", limit=5)
    assert found and found[0]["kind"] in ("data_quality_rule", "clause")
    gated = find.find_written(c, P, "NPB-DQ-LN-003", approved_rules_only=True, limit=10)
    assert not [f for f in gated if f["kind"] == "data_quality_rule"]


# ── G9: by words and by meaning ────────────────────────────────────────────────────────────────

def test_the_demo_query_is_a_meaning_hit_the_words_miss(c, world):
    out = view.compare_search(c, P, DEMO_QUERY, embedder=world["embedder"], limit=10)
    words = [h["node"]["label"] for h in out["words"]]
    meaning = [h["node"]["label"] for h in out["meaning"]]
    assert not any(w.startswith("NPB-DQ-TX-009") for w in words), words
    assert any(m.startswith("NPB-DQ-TX-009") for m in meaning[:3]), meaning
    tx009 = next(h["node"]["id"] for h in out["meaning"] if h["node"]["label"].startswith("NPB-DQ-TX-009"))
    assert tx009 in out["only_by_meaning"] and out["meaning_available"] is True


def test_without_an_embedding_model_the_meaning_column_says_why(c, world):
    out = view.compare_search(c, P, DEMO_QUERY, limit=5)
    assert out["meaning"] == [] and out["meaning_available"] is False
    assert out["reason"]
