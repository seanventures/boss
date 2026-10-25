"""THE KNOWLEDGE GRAPH SAYS IT IN WORDS (BOSSAPP-768 project flow, tester round 2).

A run node was titled "Run wfr_6267…" in the tree, the canvas, the panel and its attributes; the
Rules review's Scan filter listed `rscan_caa44184979a_scan`; a learning's provenance read
"Indexed by the code index" and "knowledge.learning.record by system"; the Learning tree counted
the runs it came from as if they were learnings. Real database.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from boss_engine.config import get_settings
from boss_engine.services.knowledge import learning_writeback, rules, view
from boss_engine.services.knowledge.store import PostgresKnowledgeStore as KnowledgeStore


@pytest.fixture
def run(engine):
    """A real workflow run of the default project, titled as a scan is."""
    pid = get_settings().default_project_id
    rid = f"wfr_{uuid.uuid4().hex[:20]}"
    tag = uuid.uuid4().hex[:10]
    with engine.begin() as db:
        if db.execute(text("SELECT 1 FROM projects WHERE id = :p"), {"p": pid}).first() is None:
            pytest.skip("no default project in this database")
        db.execute(text("INSERT INTO workflow_definitions (id, project_id, workflow_key, name, created_by) "
                        "VALUES (:d, :p, :k, 'Scan for Business Rules', 'test')"),
                   {"d": f"wfd_{tag}", "p": pid, "k": f"business_rules_scan_{tag}"})
        db.execute(text("INSERT INTO workflow_versions (id, project_id, workflow_definition_id, version, spec, content_hash, created_by) "
                        "VALUES (:v, :p, :d, 1, '{}'::jsonb, :h, 'test')"),
                   {"v": f"wfv_{tag}", "p": pid, "d": f"wfd_{tag}", "h": tag})
        db.execute(text("INSERT INTO workflow_runs (id, project_id, workflow_definition_id, workflow_version_id, actor, input) "
                        "VALUES (:r, :p, :d, :v, 'ceo', CAST(:i AS JSONB))"),
                   {"r": rid, "p": pid, "d": f"wfd_{tag}", "v": f"wfv_{tag}",
                    "i": '{"title": "Business rules scan: loan repayment schedule"}'})
    return pid, rid


def test_a_run_is_named_for_its_run_and_an_old_raw_name_is_replaced(engine, run):
    pid, rid = run
    with engine.begin() as db:
        title = learning_writeback.run_title(db, rid)
        assert title.startswith("Business rules scan: loan repayment schedule · ") and rid not in title
        # One written before names were kept is renamed the next time it is anchored.
        db.execute(text("DELETE FROM kg_nodes WHERE project_id = :p AND node_key = :k"), {"p": pid, "k": f"run:{rid}"})
        node = KnowledgeStore().upsert_node(db, project_id=pid, kind="run", node_key=f"run:{rid}", name=f"Run {rid}",
                                            body="", trust_tier="observed", actor_kind="system", source="learning",
                                            attrs={"source_id": rid})
        assert learning_writeback.run_anchor(db, pid, rid) == node
        row = db.execute(text("SELECT name FROM kg_nodes WHERE id = :i"), {"i": node}).one()
        assert row[0] == title
        detail = view.node_detail(db, pid, node) if hasattr(view, "node_detail") else None
        if detail is not None:
            assert detail["node"]["label"] == title and "source_id" not in detail["attrs"]


def test_words_for_who_added_it_and_what_was_done():
    assert view.action_words("knowledge.learning.record") == "recorded"
    assert view.action_words("knowledge.rule.approve") == "approved"
    assert view.action_words("knowledge.repository.index_failed") == "failed to index"
    blank = {"agent_role": None, "agent_name": None}
    assert view._who("learning", "agent", blank, "system") == view.NOT_RECORDED == "an agent, before BOSS kept agents' names"
    assert view._who("code", "system", blank, "system") == view.CODE_WHO
    assert "code index" not in view.CODE_WHO and "not recorded" not in view.NOT_RECORDED
    assert view._who("learning", "agent", {"agent_role": "sre", "agent_name": "Beacon"}, "sre") == "Beacon"
    assert view._who("rules", "agent", {"agent_role": "boss", "agent_name": "BOSS (general)"}, "boss") == "BOSS (general)"


def test_a_scan_is_listed_by_what_it_read_and_when(engine, run):
    pid, rid = run
    with engine.begin() as db:
        KnowledgeStore().upsert_node(db, project_id=pid, kind="rule", node_key=f"rule:t{uuid.uuid4().hex[:12]}",
                                     name="A rule", body="Statement", trust_tier="inferred", actor_kind="agent",
                                     actor_ref="chief_architect", source="rules",
                                     attrs={"status": "proposed", "scan_id": f"rscan_{rid[-12:]}_scan",
                                            "provenance": {"workflow_run_id": rid}})
        labels = {s["id"]: s for s in rules.scan_labels(db, pid)}
    found = labels[f"rscan_{rid[-12:]}_scan"]
    assert found["label"].startswith("Business rules scan: loan repayment schedule · ")
    assert "rscan_" not in found["label"] and found["rules"] >= 1


def test_provenance_names_the_run_the_reviewer_once_and_never_a_key(engine, run):
    """Tester round 4: "Run wfr_6267c5d3… ↗ · step scan", "approved by person" then "approved by
    an owner (legacy record)", and an "Approved by" node named "ceo". The provenance carries the
    run's title and start, the review once, and a person node named for a person."""
    pid, rid = run
    store = KnowledgeStore()
    with engine.begin() as db:
        rule = store.upsert_node(db, project_id=pid, kind="rule", node_key=f"rule:t{uuid.uuid4().hex[:12]}",
                                 name="Charge-payment allocation is capped", body="Statement", trust_tier="inferred",
                                 actor_kind="agent", actor_ref="chief_architect", source="rules",
                                 attrs={"status": "proposed", "category": "limit",
                                        "provenance": {"workflow_run_id": rid, "step": "scan", "agent_role": "chief_architect",
                                                       "agent_name": "Chief Architect", "agent_id": "agt_test123"}})
        rules.approve_rule(db, project_id=pid, rule_id=rule, actor_ref="ceo", actor_kind="person")
        detail = view.node_detail(db, pid, rule)
        prov = detail["provenance"]
        assert prov["run_title"] == "Business rules scan: loan repayment schedule" and prov["run_at"]
        approvals = [e for e in prov["events"] if e["what"] == "approved"]
        assert len(approvals) == 1 and approvals[0]["by"] == "ceo", prov["events"]
        assert all(e["by"] not in ("person", "agent") for e in prov["events"])
        people = [n for g in detail["connections"] for n in g["items"] if n["kind"] == "person"]
        assert people and {n["label"] for n in people} == {view.OPERATOR}
        db.execute(text("DELETE FROM kg_nodes WHERE project_id = :p AND (id = :r OR node_key = 'person:ceo')"),
                   {"p": pid, "r": rule})
