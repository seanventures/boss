"""Business rules: agents propose, people approve (BOSSAPP-768, KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §2.3, §4.5).

Breaks covered: an agent approving a rule (through the service, the store or the tool registry);
a rule whose evidence names a symbol that is not in the code graph, or lines outside it; a
proposal that takes its provenance from the arguments rather than the run; an approval that does
not record who approved (approved_by edge, kg_audit.actor_ref); an edit that overwrites the rule
instead of superseding it; a correction that leaves the corrected rule standing once approved;
a proposed rule reaching a stage's reads; kg_rules handing an agent an unapproved rule unlabelled;
the projection without the Rules label; rules missing from the embedding pass.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

import pytest
from boss_engine.db import ensure_schema
from boss_engine.services.knowledge import rules, scopes
from boss_engine.services.knowledge.code_index import index_files, symbol_key
from boss_engine.services.knowledge.service import KnowledgeService
from boss_engine.services.knowledge.store import PostgresKnowledgeStore
from boss_engine.services.llm_telemetry import clear_llm_context, set_llm_context
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

STORE = PostgresKnowledgeStore()
SERVICE = KnowledgeService(STORE)
P = "proj_kgr"
REPO = "acme/bank"
PATH = "loan-core/src/loans/validate.py"
SOURCE = '''class LoanValidator:
    def validate_disburse_date(self, disbursed_on, today):
        if disbursed_on > today:
            raise ValueError("cannot.be.a.future.date")
        return True

    def validate_amount(self, amount, limit):
        if amount > limit:
            raise ValueError("amount.exceeds.limit")
'''
DISBURSE = symbol_key(REPO, PATH, "LoanValidator.validate_disburse_date")
AMOUNT = symbol_key(REPO, PATH, "LoanValidator.validate_amount")
PACKAGE = "loan-core.src.loans.validate"


def _database_url() -> str:
    return os.environ.get("BOSS_KG_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


@pytest.fixture(scope="module")
def pg_engine():
    url = _database_url()
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if not url or not any(marker in name for marker in ("test", "testing", "ci", "scratch", "tmp")):
        pytest.skip("BOSS_KG_TEST_DATABASE_URL must name a disposable database")
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        pytest.skip(f"disposable postgres is not reachable: {exc.__class__.__name__}")
    ensure_schema(engine)
    return engine


@pytest.fixture()
def connection(pg_engine):
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text("INSERT INTO orgs (id, name) VALUES ('org_kgr', 'KGR')"))
            conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_kgr', 'org_kgr', 'KGR')"))
            conn.execute(text(f"INSERT INTO projects (id, workspace_id, name, slug) VALUES ('{P}', 'ws_kgr', 'KGR', 'kgr')"))
            conn.execute(text(
                "INSERT INTO roles (id, project_id, role_key, display_name, spec) VALUES "
                f"('role_kgr_ca', '{P}', 'chief_architect', 'Chief Architect', "
                "CAST('{\"persona\": {\"callsign\": \"Scout\"}}' AS JSONB))"))
            index_files(conn, STORE, project_id=P, repo=REPO,
                        files=[{"path": PATH, "language": "python", "source": SOURCE}])
            set_llm_context(role_key="chief_architect", run_id="ctr_scan_1", workflow_run_id="wfr_scan_1",
                            stage="scan", model_id="scripted-model")
            yield conn
        finally:
            transaction.rollback()
            clear_llm_context()


def _propose(connection, **overrides):
    args = dict(
        project_id=P, title="A loan cannot be disbursed on a future date",
        statement="Disbursing a loan dated after today is refused with cannot.be.a.future.date.",
        category="validation", scope={"symbols": [DISBURSE]},
        evidence=[{"symbol": DISBURSE, "lines": [3, 4], "excerpt": 'if disbursed_on > today: raise ...'}],
        rationale="validate_disburse_date raises when the date is in the future.", confidence=0.9,
    )
    args.update(overrides)
    return rules.propose_rule(connection, **args)


def _edges(connection, src: str, kind: str) -> list[dict]:
    return [dict(r) for r in connection.execute(text(
        "SELECT e.dst_id, e.attrs, e.valid_to, d.kind AS dst_kind, d.node_key AS dst_key FROM kg_edges e "
        "JOIN kg_nodes d ON d.id = e.dst_id WHERE e.src_id = :s AND e.kind = :k"), {"s": src, "k": kind}).mappings()]


def _node(connection, node_id: str) -> dict:
    return dict(connection.execute(text(
        "SELECT kind, node_key, trust_tier, created_by, attrs, valid_to, knowledge_layer FROM kg_nodes WHERE id = :i"),
        {"i": node_id}).mappings().one())


# --- proposing -------------------------------------------------------------------------------------
def test_an_agent_proposes_an_inferred_rule_with_evidence_and_the_runs_provenance(connection):
    rule = _propose(connection)
    rid = rule["node"]["id"]
    node = _node(connection, rid)
    assert (node["kind"], node["trust_tier"], node["knowledge_layer"]) == ("rule", "inferred", "rules")
    assert node["node_key"].startswith("rule:") and len(node["node_key"]) == len("rule:") + 16
    assert node["created_by"] == "chief_architect"                 # the agent's role key, not 'system'
    assert rule["status"] == "proposed" and rule["version"] == 1 and rule["review"] is None
    assert rule["node"]["layer"] == "rules" and rule["node"]["sub"] == "proposed · validation"
    prov = rule["provenance"]
    assert (prov["agent_role"], prov["agent_name"], prov["run_id"], prov["workflow_run_id"], prov["step"],
            prov["model_id"]) == ("chief_architect", "Scout", "ctr_scan_1", "wfr_scan_1", "scan", "scripted-model")
    assert prov["created_by_kind"] == "agent" and prov["events"][0]["what"] == "proposed"
    [evidence] = rule["evidence"]
    assert (evidence["symbol_key"], evidence["path"], evidence["lines"]) == (DISBURSE, PATH, [3, 4])
    assert evidence["label"] == "LoanValidator.validate_disburse_date"
    [ev_edge] = _edges(connection, rid, "evidenced_by")
    assert ev_edge["attrs"]["lines"] == [3, 4] and "disbursed_on" in ev_edge["attrs"]["excerpt"]
    [by] = _edges(connection, rid, "proposed_by")
    assert (by["dst_kind"], by["dst_key"], by["attrs"]["run_id"]) == ("agent", "agent:chief_architect", "ctr_scan_1")
    run = STORE.find_by_key(connection, project_id=P, kind="run", node_key="run:ctr_scan_1")
    [produced] = _edges(connection, run["id"], "produced")
    assert produced["dst_id"] == rid and produced["attrs"]["step"] == "scan"
    assert not _edges(connection, rid, "governs")                  # only an approval derives these
    audit = connection.execute(text(
        "SELECT action, actor_kind, actor_ref FROM kg_audit WHERE entity_id = :i"), {"i": rid}).one()
    assert tuple(audit) == ("knowledge.rule.propose", "agent", "chief_architect")


def test_provenance_never_comes_from_the_arguments(connection):
    with pytest.raises(TypeError):
        _propose(connection, provenance={"agent_role": "ceo"})      # no such argument
    rule = _propose(connection, scan_id="scan_7")
    assert rule["provenance"]["agent_role"] == "chief_architect" and rule["scan_id"] == "scan_7"


def test_evidence_must_resolve_to_real_code_in_this_project(connection):
    before = connection.execute(text("SELECT count(*) FROM kg_nodes WHERE kind = 'rule'")).scalar_one()
    with pytest.raises(rules.RuleRefused, match="not a symbol in this project's code graph"):
        _propose(connection, evidence=[{"symbol": "LoanValidator.validate_nonexistent", "lines": [3, 4]}])
    with pytest.raises(rules.RuleRefused, match="needs evidence"):
        _propose(connection, evidence=[])
    with pytest.raises(rules.RuleRefused, match="outside LoanValidator.validate_disburse_date"):
        _propose(connection, evidence=[{"symbol": DISBURSE, "lines": [7, 9]}])
    with pytest.raises(rules.RuleRefused, match="not a symbol"):
        _propose(connection, scope={"symbols": ["symbol:acme/bank:nowhere.py:ghost"]})
    with pytest.raises(rules.RuleRefused, match="not a package"):
        _propose(connection, scope={"packages": ["com.acme.nothing"]})
    with pytest.raises(rules.RuleRefused, match="not an area"):
        _propose(connection, scope={"areas": ["no-such-module"]})
    with pytest.raises(rules.RuleRefused, match="category is one of"):
        _propose(connection, category="vibes")
    with pytest.raises(rules.RuleRefused, match="secret"):
        _propose(connection, statement="Use the key AKIAIOSFODNN7EXAMPLE to disburse.")
    # A code node of ANOTHER project is not evidence here.
    connection.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES ('proj_kgr2', 'ws_kgr', 'B', 'b')"))
    index_files(connection, STORE, project_id="proj_kgr2", repo=REPO,
                files=[{"path": "other/x.py", "language": "python", "source": "def elsewhere():\n    return 1\n"}])
    other = symbol_key(REPO, "other/x.py", "elsewhere")
    with pytest.raises(rules.RuleRefused, match="not a symbol"):
        _propose(connection, evidence=[{"symbol": other}])
    after = connection.execute(text("SELECT count(*) FROM kg_nodes WHERE kind = 'rule'")).scalar_one()
    assert after == before                                          # nothing refused was written


def test_names_resolve_and_the_scope_defaults_to_the_evidence(connection):
    rule = _propose(connection, scope={}, evidence=[{"symbol": "LoanValidator.validate_disburse_date"}])
    assert rule["scope"] == {"repo": REPO, "areas": [], "packages": [], "symbols": [DISBURSE]}
    scoped = _propose(connection, statement="Amounts above the product limit are refused.",
                      scope={"packages": [PACKAGE], "areas": ["area:acme/bank:loan-core"]},
                      evidence=[{"symbol": "validate_amount", "lines": [8, 9]}])
    assert scoped["scope"]["packages"] == [PACKAGE] and scoped["scope"]["areas"] == ["loan-core"]


def test_the_same_scope_and_statement_is_the_same_rule(connection):
    first = _propose(connection)
    again = _propose(connection, title="Another title", confidence=0.4)
    assert again["node"]["id"] == first["node"]["id"]
    rules.reject_rule(connection, project_id=P, rule_id=first["node"]["id"], actor_ref="usr_param",
                      note="Back-dated disbursals are allowed for migrations.")
    with pytest.raises(rules.RuleRefused, match="rejected by usr_param: Back-dated"):
        _propose(connection)


# --- agents never approve --------------------------------------------------------------------------
def test_an_agent_cannot_approve_edit_or_reject_a_rule(connection):
    rid = _propose(connection)["node"]["id"]
    for action in (rules.approve_rule, rules.reject_rule):
        with pytest.raises(PermissionError, match="Only a person reviews a rule"):
            action(connection, project_id=P, rule_id=rid, actor_ref="chief_architect", actor_kind="agent")
    with pytest.raises(PermissionError):
        rules.edit_rule(connection, project_id=P, rule_id=rid, actor_ref="chief_architect", actor_kind="agent",
                        statement="Changed by an agent.")
    with pytest.raises(PermissionError):                            # the store holds the line too
        STORE.set_trust(connection, project_id=P, node_id=rid, trust_tier="approved", actor_kind="agent")
    with pytest.raises(PermissionError):
        STORE.upsert_node(connection, project_id=P, kind="rule", node_key="rule:agent", name="x", body="y",
                          trust_tier="approved", actor_kind="agent")
    assert _node(connection, rid)["trust_tier"] == "inferred"
    from boss_engine.identity.catalogue import NEVER_DELEGABLE
    from boss_engine.identity.permissions_gen import PASSPORT_PRESETS, PERMISSION_META

    assert "knowledge:review" in NEVER_DELEGABLE and not PERMISSION_META["knowledge:review"].delegable
    assert set(PERMISSION_META["knowledge:review"].roles) == {"owner", "admin"}
    assert not any("knowledge:review" in p.permissions for p in PASSPORT_PRESETS.values())
    from boss_engine.tools.builtin import build_default_registry

    names = set(build_default_registry().names())
    assert {"kg_rules", "kg_propose_rule"} <= names
    assert not [n for n in names if n.startswith("kg_") and any(w in n for w in ("approve", "review", "promote"))]


# --- a person reviews ----------------------------------------------------------------------------
def test_approve_writes_approved_by_governs_and_the_audit_actor(connection):
    rid = _propose(connection, scope={"symbols": [DISBURSE], "packages": [PACKAGE]})["node"]["id"]
    out = rules.approve_rule(connection, project_id=P, rule_id=rid, actor_ref="usr_param", note="Matches policy.")
    rule = out["rule"]
    assert out["previous"] is None
    assert (rule["status"], rule["node"]["trust_tier"]) == ("approved", "approved")
    assert rule["review"]["by"] == "usr_param" and rule["review"]["note"] == "Matches policy."
    assert rule["provenance"]["approved_by"] == "usr_param" and rule["provenance"]["approved_at"]
    assert [e["what"] for e in rule["provenance"]["events"]] == ["proposed", "approved"]
    [approved_by] = _edges(connection, rid, "approved_by")
    assert (approved_by["dst_kind"], approved_by["dst_key"]) == ("person", "person:usr_param")
    governs = {e["dst_key"] for e in _edges(connection, rid, "governs")}
    assert governs == {DISBURSE, f"module:{REPO}:{PACKAGE}"}
    audit = connection.execute(text(
        "SELECT actor_kind, actor_ref, detail FROM kg_audit WHERE entity_id = :i AND action = 'knowledge.rule.approve'"),
        {"i": rid}).mappings().one()
    assert (audit["actor_kind"], audit["actor_ref"], audit["detail"]["note"]) == ("person", "usr_param", "Matches policy.")
    assert _node(connection, rid)["created_by"] == "chief_architect"   # still the proposer's
    with pytest.raises(rules.RuleRefused, match="is approved"):
        rules.approve_rule(connection, project_id=P, rule_id=rid, actor_ref="usr_param")


def test_edit_supersedes_the_old_version_and_is_approved_in_the_act(connection):
    old = _propose(connection)["node"]["id"]
    out = rules.edit_rule(connection, project_id=P, rule_id=old, actor_ref="usr_ops",
                          statement="A loan cannot be disbursed later than today, in the office's time zone.",
                          category="state_transition", note="Time zone matters.")
    new, previous = out["rule"], out["previous"]
    nid = new["node"]["id"]
    assert nid != old and new["version"] == 2 and new["previous_id"] == old
    assert (new["status"], new["node"]["trust_tier"], new["category"]) == ("approved", "approved", "state_transition")
    assert new["node"]["key"].endswith(".v2")
    assert new["provenance"]["created_by"] == "usr_ops" and new["provenance"]["created_by_kind"] == "person"
    assert new["provenance"]["agent_role"] == "chief_architect"     # the lineage stays
    assert [e["symbol_key"] for e in new["evidence"]] == [DISBURSE]  # evidence carries over
    assert previous["status"] == "superseded" and _node(connection, old)["valid_to"] is not None
    [sup] = _edges(connection, nid, "supersedes")
    assert sup["dst_id"] == old
    assert {e["dst_key"] for e in _edges(connection, nid, "approved_by")} == {"person:usr_ops"}
    assert {e["dst_key"] for e in _edges(connection, nid, "governs")} == {DISBURSE}
    assert connection.execute(text(
        "SELECT actor_ref FROM kg_audit WHERE entity_id = :i AND action = 'knowledge.rule.edit'"),
        {"i": nid}).scalar_one() == "usr_ops"
    with pytest.raises(rules.RuleRefused, match="is superseded"):
        rules.approve_rule(connection, project_id=P, rule_id=old, actor_ref="usr_ops")
    with pytest.raises(rules.RuleRefused, match="Nothing changed"):
        rules.edit_rule(connection, project_id=P, rule_id=nid, actor_ref="usr_ops")


def test_a_correction_supersedes_the_rule_it_corrects_once_a_person_approves_it(connection):
    approved = _propose(connection)["node"]["id"]
    rules.approve_rule(connection, project_id=P, rule_id=approved, actor_ref="usr_param")
    with pytest.raises(rules.RuleRefused, match="only an approved rule is corrected"):
        _propose(connection, statement="x", corrects=_propose(connection, statement="Another rule.")["node"]["id"])
    fix = _propose(connection, statement="A loan cannot be disbursed on a future date, except a "
                   "back-dated migration import.", corrects=approved, confidence=0.7)
    fid = fix["node"]["id"]
    assert fix["status"] == "proposed" and fix["version"] == 2 and fix["previous_id"] == approved
    [corrects] = _edges(connection, fid, "corrects")
    assert corrects["dst_id"] == approved
    assert _node(connection, approved)["valid_to"] is None           # stands until a person approves
    rules.approve_rule(connection, project_id=P, rule_id=fid, actor_ref="usr_param")
    old = rules.rule_payload(connection, P, approved)
    assert old["status"] == "superseded" and _node(connection, approved)["valid_to"] is not None
    assert [e["dst_id"] for e in _edges(connection, fid, "supersedes")] == [approved]


def test_reject_closes_the_rule_with_the_note(connection):
    rid = _propose(connection)["node"]["id"]
    rule = rules.reject_rule(connection, project_id=P, rule_id=rid, actor_ref="usr_param", note="Not a rule.")["rule"]
    assert rule["status"] == "rejected" and rule["review"]["note"] == "Not a rule."
    assert _node(connection, rid)["valid_to"] is not None


def test_the_review_queue_lists_by_status_scan_and_area_with_counts(connection):
    a = _propose(connection, scan_id="scan_1")["node"]["id"]
    b = _propose(connection, statement="Amounts above the limit are refused.", scan_id="scan_1",
                 scope={"areas": ["loan-core"]}, evidence=[{"symbol": AMOUNT, "lines": [8, 9]}])["node"]["id"]
    _propose(connection, statement="Another scan's rule.", scan_id="scan_2")
    rules.approve_rule(connection, project_id=P, rule_id=a, actor_ref="usr_param")
    queue = rules.list_rules(connection, project_id=P, status="proposed", scan_id="scan_1")
    assert [r["node"]["id"] for r in queue["items"]] == [b] and queue["scan_id"] == "scan_1"
    assert queue["counts"] == {"proposed": 1, "approved": 1, "rejected": 0, "superseded": 0}
    assert [r["node"]["id"] for r in rules.list_rules(connection, project_id=P, area="area:acme/bank:loan-core")["items"]] == [b]
    assert len(rules.list_rules(connection, project_id=P)["items"]) == 3
    with pytest.raises(rules.RuleRefused):
        rules.list_rules(connection, project_id=P, status="maybe")


# --- what agents read ----------------------------------------------------------------------------
class _Ctx:
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def begin(self):
        yield self.connection


def test_a_stage_reads_approved_rules_only(connection):
    proposed = _propose(connection, statement="Proposed: ledger amounts round half-even.")["node"]["id"]
    approved = _propose(connection, statement="Approved: a loan cannot be disbursed on a future date.")["node"]["id"]
    rules.approve_rule(connection, project_id=P, rule_id=approved, actor_ref="usr_param")
    triage = scopes.policy_for(stage="triage")
    assert "rules" in triage.layers
    found = {h["node_id"] for h in SERVICE.search(connection, project_id=P, query="disbursed loan", policy=triage)["hits"]}
    assert approved in found and proposed not in found
    unscoped = {h["node_id"] for h in SERVICE.search(connection, project_id=P, query="ledger amounts round")["hits"]}
    assert proposed in unscoped                                      # a person's own search sees it
    no_rules = scopes.apply_declaration(triage, {"layers": ["ops", "lifecycle", "knowledge"]})
    assert not scopes.offers_tool(no_rules, "kg_rules") and scopes.offers_tool(triage, "kg_rules")
    with pytest.raises(PermissionError, match="rules layer isn't available"):
        STORE.assert_readable(connection, project_id=P, node_id=approved, policy=no_rules)


def test_kg_rules_returns_approved_rules_for_a_symbol_and_labels_proposed_ones(connection):
    from boss_engine.tools.builtin import build_default_registry

    approved = _propose(connection, scope={"packages": [PACKAGE]})["node"]["id"]
    rules.approve_rule(connection, project_id=P, rule_id=approved, actor_ref="usr_param")
    proposed = _propose(connection, statement="Amounts above the limit are refused.",
                        evidence=[{"symbol": AMOUNT, "lines": [8, 9]}])["node"]["id"]
    set_llm_context(stage="implement")
    tool = build_default_registry().get("kg_rules")
    # The package rule reaches a method of that package (symbol -> its package).
    out = tool.handler(_Ctx(connection), project_id=P, actor="software_engineer", args={"symbols": [AMOUNT]})
    assert [r["id"] for r in out["rules"]] == [approved] and out["trust"].startswith("Approved by a person")
    used = connection.execute(text(
        "SELECT use_kind, run_id, agent_role FROM kg_usage WHERE fact_id = :i"), {"i": approved}).one()
    assert tuple(used) == ("consulted", "ctr_scan_1", "chief_architect")
    pending = tool.handler(_Ctx(connection), project_id=P, actor="software_engineer",
                           args={"symbols": [AMOUNT], "status": "proposed"})
    assert [r["id"] for r in pending["rules"]] == [proposed] and pending["trust"].startswith("PROPOSED, NOT APPROVED")
    by_text = tool.handler(_Ctx(connection), project_id=P, actor="x", args={"query": "disbursed future date"})
    assert [r["id"] for r in by_text["rules"]] == [approved]
    assert "error" in tool.handler(_Ctx(connection), project_id=P, actor="x", args={"symbols": ["ghost_fn"]})


def test_kg_propose_rule_tool_refuses_in_words_and_never_takes_a_tier(connection):
    from boss_engine.tools.builtin import build_default_registry

    set_llm_context(stage="investigate")
    tool = build_default_registry().get("kg_propose_rule")
    assert tool.kind == "write" and tool.reversibility == "safe"
    refused = tool.handler(_Ctx(connection), project_id=P, actor="sre", args={
        "title": "t", "statement": "s", "category": "limit", "confidence": 0.5,
        "evidence": [{"symbol": "nope_nothing"}]})
    assert "not a symbol in this project's code graph" in refused["error"]
    made = tool.handler(_Ctx(connection), project_id=P, actor="sre", args={
        "title": "Limit on amounts", "statement": "Amounts above the product limit are refused.",
        "category": "limit", "confidence": 0.8, "scope": {"symbols": [AMOUNT]},
        "evidence": [{"symbol": AMOUNT, "lines": [8, 9], "excerpt": "if amount > limit"}]})
    assert made["rule"]["status"] == "proposed" and "person reviews it" in made["note"]
    node = _node(connection, made["rule"]["id"])
    assert node["trust_tier"] == "inferred" and node["created_by"] == "chief_architect"   # the context's role


def test_the_projection_has_the_rules_label_and_product_layer(connection):
    from boss_engine.services.knowledge import age

    if not age.available(connection):
        pytest.skip("apache age is not installed")
    rid = _propose(connection)["node"]["id"]
    rules.approve_rule(connection, project_id=P, rule_id=rid, actor_ref="usr_param")
    age.rebuild(connection, P)
    rows = age.run_cypher(connection, project_id=P, columns=["id", "layer", "status"],
                          query="MATCH (r:Rules) RETURN r.node_id, r.product_layer, r.status")["rows"]
    assert rows == [{"id": rid, "layer": "rules", "status": "approved"}]
    code = age.run_cypher(connection, project_id=P, columns=["layer"],
                          query="MATCH (s:Code {kind: 'symbol'}) RETURN DISTINCT s.product_layer")["rows"]
    assert code == [{"layer": "code"}]
    governs = age.run_cypher(connection, project_id=P, columns=["n"],
                             query="MATCH (:Rules)-[g:governs]->(:Code) RETURN count(g)")["rows"]
    assert governs == [{"n": 1}]
    no_rules = scopes.apply_declaration(scopes.policy_for(stage="triage"), {"layers": ["ops"]})
    with pytest.raises(PermissionError, match="The Rules label isn't available"):
        age.check_scope("MATCH (r:Rules) RETURN r.name", no_rules)
    # A graph projected with an older shape is rebuilt even when its counts match.
    connection.execute(text("UPDATE kg_age_sync SET shape = 0 WHERE project_id = :p"), {"p": P})
    assert age.reconcile(connection, P)["mode"] == "rebuild"
    assert age.reconcile(connection, P)["mode"] == "current"


def test_rules_are_embedded_with_the_org_embedding_model(connection):
    from boss_engine.services.knowledge.embeddings import EMBEDDED_KINDS, embed_pending

    assert "rule" in EMBEDDED_KINDS
    rid = _propose(connection)["node"]["id"]

    class _Embedder:
        model_id = "test:embed:3"

        def embed(self, texts):
            return [[1.0, 0.0, 0.5] for _ in texts]

    assert embed_pending(connection, STORE, project_id=P, embedder=_Embedder()) >= 1
    assert connection.execute(text(
        "SELECT model_id FROM kg_embeddings WHERE node_id = :i"), {"i": rid}).scalar_one() == "test:embed:3"


def test_promoting_a_fact_audits_who_did_it(connection):
    fact = STORE.upsert_node(connection, project_id=P, kind="fact", node_key="fact:a", name="a", body="b",
                             trust_tier="inferred", actor_kind="agent", actor_ref="sre")
    assert _node(connection, fact)["created_by"] == "sre"
    SERVICE.promote(connection, project_id=P, fact_id=fact, actor_kind="person", actor_ref="usr_param")
    assert connection.execute(text(
        "SELECT actor_ref FROM kg_audit WHERE entity_id = :i AND action = 'knowledge.promote'"),
        {"i": fact}).scalar_one() == "usr_param"
