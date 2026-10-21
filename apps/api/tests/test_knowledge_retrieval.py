"""Knowledge Graph Phase 3: hybrid retrieval, context packs, the Passport filter (KNOWLEDGE_GRAPH_SPEC.md §3.5)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from apps.api.app.db import begin
from boss_engine.services.knowledge import Scope
from boss_engine.services.knowledge import retrieval as rt
from boss_engine.services.knowledge.facts import FactStore, Origin
from boss_engine.services.token_efficiency import estimate_tokens

ALL = None   # BOSS itself
AGENT_READS = frozenset({"repository:read", "code_task:read", "decision:read", "reliability:read"})


def _project(c, base_pid=None) -> Scope:
    pid = base_pid or c.execute(text("SELECT id FROM projects ORDER BY created_at LIMIT 1")).scalar_one()
    new = "proj-kg-ret-" + uuid.uuid4().hex[:6]
    c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) SELECT :n, workspace_id, :n, :n "
                   "FROM projects WHERE id = :p"), {"n": new, "p": pid})
    return Scope(new)


def _node(c, scope, kind, key, name, path=None) -> str:
    nid = "n-" + uuid.uuid4().hex[:10]
    c.execute(text("INSERT INTO kg_nodes (id, project_id, kind, node_key, name, path, repo) "
                   "VALUES (:i, :p, :k, :key, :n, :path, 'acme/shop')"),
              {"i": nid, "p": scope.project_id, "k": kind, "key": key, "n": name, "path": path})
    return nid


def _edge(c, scope, a, b, kind="calls"):
    c.execute(text("INSERT INTO kg_edges (id, project_id, src_id, dst_id, kind) VALUES (:i, :p, :a, :b, :k)"),
              {"i": "e-" + uuid.uuid4().hex[:10], "p": scope.project_id, "a": a, "b": b, "k": kind})


def _fact(c, scope, subject, statement, kind, origin_kind, source_kind, node=None) -> str:
    out = FactStore().write(c, scope, {"subject": subject, "predicate": "is", "statement": statement, "kind": kind},
                            Origin(origin_kind, source_kind, "src-" + uuid.uuid4().hex[:6]))
    fid = out["id"]
    if node:
        c.execute(text("UPDATE kg_facts SET subject_node_id = :n WHERE id = :f"), {"n": node, "f": fid})
    return fid


@pytest.fixture
def world(engine):
    """A small shop: checkout calls pricing calls tax; facts of every tier and source."""
    with begin(engine) as c:
        s = _project(c)
        checkout = _node(c, s, "symbol", "shop.checkout.total", "checkout.total", "shop/checkout.py")
        pricing = _node(c, s, "symbol", "shop.pricing.price", "pricing.price", "shop/pricing.py")
        tax = _node(c, s, "symbol", "shop.tax.vat", "tax.vat", "shop/tax.py")
        far = _node(c, s, "symbol", "shop.admin.export", "admin.export", "shop/admin.py")
        svc = _node(c, s, "service", "svc:checkout", "checkout-api")
        db = _node(c, s, "datastore", "db:orders", "orders-db")
        inc = _node(c, s, "incident", "incident:1", "checkout 500s after VAT change")
        _edge(c, s, checkout, pricing)
        _edge(c, s, pricing, tax)
        _edge(c, s, svc, db, "depends_on")
        ids = {
            # graph-only: no word of the task, but about a neighbour of the touched symbol
            "graph": _fact(c, s, "tax.vat", "Rounding happens once, at the end, half-even.", "invariant",
                           "decision", "decision", tax),
            # text-only: about nothing touched, but names the task's words
            "text": _fact(c, s, "refunds", "Refund totals must reuse the discount engine.", "coupling",
                          "distilled", "run"),
            "governed": _fact(c, s, "pricing.price", "Prices are integers in minor units.", "invariant",
                              "decision", "decision", pricing),
            "inferred": _fact(c, s, "pricing.price", "Prices may be floats for display.", "invariant",
                              "agent", "agent", pricing),
            "finding": _fact(c, s, "checkout.total", "A review found discount applied twice in checkout totals.",
                             "lesson", "distilled", "review", checkout),
            "security": _fact(c, s, "checkout.total", "Checkout requires an auth token scoped to the cart.",
                              "decision", "decision", "decision", checkout),
            "cause": _fact(c, s, "tax.vat", "A VAT table reload caused checkout 500s.", "risk", "distilled",
                           "incident", tax),
            "runbook": _fact(c, s, "checkout-api", "Restart the pricing cache, then replay failed orders.",
                             "pattern", "distilled", "incident"),
            "unrelated": _fact(c, s, "admin.export", "Exports run nightly.", "pattern", "distilled", "run", far),
        }
        # two past decisions, one whose run failed
        for did, q, status, run_status in (("dec-a-" + s.project_id, "Ship the VAT rounding change to checkout?",
                                            "approved", "failed"),
                                           ("dec-b-" + s.project_id, "Rotate the payment webhook secret?",
                                            "approved", "completed")):
            c.execute(text("INSERT INTO decisions (id, project_id, question, status, resolution_note, approved_by) "
                           "VALUES (:i, :p, :q, :s, 'operator agreed', 'ops-lead')"),
                      {"i": did, "p": s.project_id, "q": q, "s": status})
            c.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, spec, state, finalize_ctx, "
                           "decision_id) VALUES (:r, :p, 'run', :st, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, :d)"),
                      {"r": "run-" + uuid.uuid4().hex[:8], "p": s.project_id, "st": run_status, "d": did})
    return {"scope": s, "ids": ids, "nodes": {"checkout": checkout, "tax": tax, "svc": svc, "inc": inc}}


TASK = {"text": "Fix refund totals in checkout when a discount applies", "touched": ["checkout.total"]}


def test_hybrid_search_fuses_graph_and_text_candidates(engine, world):
    ids = world["ids"]
    with begin(engine) as c:
        out = rt.search(c, rt.Reader(world["scope"]), TASK["text"], touched=TASK["touched"])
    got = [f["id"] for f in out["facts"]]
    assert ids["graph"] in got          # reached only through the graph (two hops from checkout.total)
    assert ids["text"] in got           # reached only through full text
    assert ids["unrelated"] not in got  # neither
    via = {f["id"]: f["via"] for f in out["facts"]}
    assert "graph" in via[ids["graph"]] and "graph" not in via[ids["text"]]
    assert {"graph", "text", "trusted"} <= set(out["sources"])


def test_an_inferred_fact_never_outranks_a_governed_one(engine, world):
    ids = world["ids"]

    def hostile_reranker(query, items):     # puts the inferred fact first
        return [ids["inferred"]] + [f["id"] for f in items if f["id"] != ids["inferred"]]

    with begin(engine) as c:
        for reranker in (None, hostile_reranker):
            got = [f["id"] for f in rt.search(c, rt.Reader(world["scope"]), "pricing prices integers floats",
                                              touched=["pricing.price"], reranker=reranker)["facts"]]
            assert got.index(ids["governed"]) < got.index(ids["inferred"])


def test_the_reranker_runs_only_when_the_knowledge_budget_allows(engine, world):
    calls = []

    def reranker(query, items):
        calls.append(len(items))
        return [f["id"] for f in items]

    s = world["scope"]
    with begin(engine) as c:
        assert rt.search(c, rt.Reader(s), TASK["text"], touched=TASK["touched"], reranker=reranker)["reranked"]
        org = c.execute(text("SELECT w.org_id FROM projects p JOIN workspaces w ON w.id = p.workspace_id "
                             "WHERE p.id = :p"), {"p": s.project_id}).scalar()
        c.execute(text("INSERT INTO kg_knowledge_settings (org_id, daily_cap_usd_micros) VALUES (:o, 1) "
                       "ON CONFLICT (org_id) DO UPDATE SET daily_cap_usd_micros = 1"), {"o": org})
        try:
            out = rt.search(c, rt.Reader(s), TASK["text"], touched=TASK["touched"], reranker=reranker)
        finally:
            c.execute(text("DELETE FROM kg_knowledge_settings WHERE org_id = :o"), {"o": org})
    assert len(calls) == 1 and not out["reranked"]


@pytest.mark.parametrize("role,stage", [("software_engineer", "implement"), ("adversarial_reviewer", "review"),
                                        ("sre", "incident"), ("jev", "decide")])
@pytest.mark.parametrize("budget", [5, 40, 90, 200, 600, 4000])
def test_a_pack_never_exceeds_its_budget_and_logs_what_it_included(engine, world, role, stage, budget):
    s = world["scope"]
    with begin(engine) as c:
        pack = rt.knowledge_pack(c, rt.Reader(s, permissions=AGENT_READS), role, stage, TASK, budget,
                                 run_id="crun-pack-test", step=f"{role}:{budget}")
        logged = c.execute(text("SELECT fact_ids, node_ids, decision_ids, tokens FROM kg_pack_log "
                                "WHERE project_id = :p AND step = :s"), {"p": s.project_id, "s": f"{role}:{budget}"}).one()
    assert estimate_tokens(pack.text) == pack.tokens <= budget
    assert (list(logged.fact_ids), list(logged.node_ids), list(logged.decision_ids), logged.tokens) == \
        (pack.fact_ids, pack.node_ids, pack.decision_ids, pack.tokens)
    for fid in pack.fact_ids:
        assert f"fact:{fid}" in pack.text
    if budget >= 600:
        assert pack.fact_ids or pack.decision_ids or pack.node_ids


def test_each_recipe_carries_what_the_spec_says(engine, world):
    ids, s = world["ids"], world["scope"]
    with begin(engine) as c:
        r = rt.Reader(s)
        build = rt.knowledge_pack(c, r, "software_engineer", "implement", TASK, 4000)
        review = rt.knowledge_pack(c, r, "security_reviewer", "review", TASK, 4000)
        sre = rt.knowledge_pack(c, r, "sre", "incident", {"text": "checkout 500s VAT", "touched": ["tax.vat"]}, 4000)
        jev = rt.knowledge_pack(c, r, "jev", "decide", {"text": "Ship a VAT rounding change to checkout?"}, 4000)
    assert build.recipe == "build" and {"invariants", "module_map"} <= set(build.sections)
    assert ids["graph"] in build.sections["invariants"] and world["nodes"]["checkout"] in build.sections["module_map"]
    assert review.recipe == "review" and ids["finding"] in review.sections["findings"]
    assert ids["security"] in review.sections["security"]
    assert sre.recipe == "sre" and world["nodes"]["svc"] in sre.sections["topology"]
    assert ids["cause"] in sre.sections["known_causes"] and world["nodes"]["inc"] in sre.sections["recent_incidents"]
    assert jev.recipe == "jev" and jev.decision_ids and jev.decision_ids[0] == "dec-a-" + s.project_id
    assert "outcome: failed" in jev.text            # the precedent says how it went


def test_every_read_is_filtered_by_project_org_team_and_the_passport_ceiling(engine, world):
    ids, s = world["ids"], world["scope"]
    with begin(engine) as c:
        everything = {f["id"] for f in rt.search(c, rt.Reader(s), TASK["text"] + " VAT", touched=["checkout.total",
                                                                                                    "tax.vat"])["facts"]}
        assert ids["cause"] in everything
        # an agent whose Passport has no reliability:read sees no incident-derived fact and no incident node
        no_rel = rt.Reader(s, permissions=frozenset({"repository:read", "code_task:read", "decision:read"}))
        seen = {f["id"] for f in rt.search(c, no_rel, TASK["text"] + " VAT", touched=["checkout.total", "tax.vat"])["facts"]}
        assert ids["cause"] not in seen and ids["runbook"] not in seen and ids["graph"] in seen
        sre = rt.knowledge_pack(c, no_rel, "sre", "incident", {"text": "checkout 500s", "touched": ["tax.vat"]}, 4000)
        assert world["nodes"]["inc"] not in sre.node_ids and world["nodes"]["svc"] not in sre.node_ids
        # without decision:read: no governed decision facts, and no precedent
        no_dec = rt.Reader(s, permissions=frozenset({"repository:read", "code_task:read", "reliability:read"}))
        assert ids["governed"] not in {f["id"] for f in rt.search(c, no_dec, "prices", touched=["pricing.price"])["facts"]}
        assert rt.precedent(c, no_dec, "VAT rounding checkout") == []
        # an empty ceiling reads nothing
        nothing = rt.Reader(s, permissions=frozenset())
        assert rt.search(c, nothing, TASK["text"], touched=TASK["touched"])["facts"] == []
        assert rt.knowledge_pack(c, nothing, "software_engineer", "implement", TASK, 4000).text == ""
        # another project sees none of this one's facts
        other = _project(c)
        assert rt.search(c, rt.Reader(other), TASK["text"], touched=TASK["touched"])["facts"] == []
        # a reader naming another org is refused outright
        with pytest.raises(rt.ScopeRefused):
            rt.search(c, rt.Reader(Scope(s.project_id, org_id="org-somebody-else")), "x")
        # a team-scoped fact is visible to that team only
        c.execute(text("UPDATE kg_facts SET team_id = 'team-pay' WHERE id = :f"), {"f": ids["text"]})
        assert ids["text"] not in {f["id"] for f in rt.search(c, rt.Reader(s, team_id="team-ops"), TASK["text"])["facts"]}
        assert ids["text"] in {f["id"] for f in rt.search(c, rt.Reader(s, team_id="team-pay"), TASK["text"])["facts"]}


def test_a_fact_carries_the_permissions_of_its_most_restrictive_source(engine, world):
    ids = world["ids"]
    with begin(engine) as c:
        perms = dict(c.execute(text("SELECT id, read_permissions FROM kg_facts WHERE id = ANY(:i)"),
                               {"i": [ids["cause"], ids["finding"], ids["governed"]]}).all())
    assert perms[ids["cause"]] == ["reliability:read"]
    assert perms[ids["finding"]] == ["code_task:read"]
    assert perms[ids["governed"]] == ["decision:read"]


def test_precedent_returns_similar_past_decisions_with_outcomes(engine, world):
    s = world["scope"]
    with begin(engine) as c:
        got = rt.precedent(c, rt.Reader(s), "Should we ship the VAT rounding change?")
    assert got[0]["id"] == "dec-a-" + s.project_id and got[0]["outcome"] == "failed"
    assert got[0]["decided"] == "approved" and got[0]["by"] == "ops-lead"
