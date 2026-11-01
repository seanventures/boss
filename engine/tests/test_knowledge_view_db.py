"""The Graph area's read path against a real database (KNOWLEDGE_GRAPH_PRODUCT_SPEC.md §2.4, §3).

Breaks covered: an area rule that makes one bubble of a single-module repo; a rollup that drifts
from the code graph it summarises (checked against a live aggregation of kg_edges, level by
level); a method hung on a class key nothing resolves; a package key without its area, so one
package name in two modules collides; a neighbourhood past its depth or limit, or that drops
what it hid without saying so, or draws a rule the layer chips turned off; a decision shown to a
caller without decision:read; a tree search that does not open to its matches; a type-ahead that
ranks a method before the class of the same name; and any payload drifting from the Phase 0 wire
types (apps/web/tests/fixtures/kg-*.json hold those shapes).
"""
from __future__ import annotations

import json
import os
import textwrap
from collections import defaultdict
from pathlib import Path

import pytest
from boss_engine.db import ensure_schema
from boss_engine.services.knowledge import view, view_rollup
from boss_engine.services.knowledge.code_index import index_repository, symbol_key
from boss_engine.services.knowledge.store import PostgresKnowledgeStore
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

STORE = PostgresKnowledgeStore()
P = "proj_kgv"
REPO = "bank/core-banking"
ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "apps" / "web" / "tests" / "fixtures"

TREE = {
    "core/src/bank/core/Ledger.java": """
        package bank.core;
        public class Ledger {
            public void post(String entry) { validate(entry); }
            void validate(String entry) { }
        }
        """,
    "core/src/bank/core/Account.java": """
        package bank.core;
        public interface Account { void credit(long amount); }
        """,
    "loan/src/bank/loan/Disbursement.java": """
        package bank.loan;
        import bank.core.Ledger;
        import bank.core.Account;
        public class Disbursement implements Account {
            private final Ledger ledger = new Ledger();
            public void credit(long amount) { ledger.post("credit"); }
            public void disburse(long amount) { ledger.post("disburse"); ledger.validate("d"); credit(amount); }
        }
        """,
    "loan/src/bank/loan/DisbursementPlan.java": """
        package bank.loan;
        public class DisbursementPlan extends Disbursement {
            public void schedule() { disburse(1L); }
        }
        """,
    # The same package name as core's, in another module: its key must not collide.
    "loan/src/bank/core/Rates.java": """
        package bank.core;
        public class Rates { public long rate() { return 1L; } }
        """,
    "web/src/bank/web/LoanController.java": """
        package bank.web;
        import bank.loan.Disbursement;
        public class LoanController {
            private final Disbursement disbursement = new Disbursement();
            public void disburse() { disbursement.disburse(5L); }
        }
        """,
}


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
            conn.execute(text("INSERT INTO orgs (id, name) VALUES ('org_kgv', 'KGV')"))
            conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_kgv', 'org_kgv', 'KGV')"))
            conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, 'ws_kgv', 'KGV', 'kgv')"),
                         {"p": P})
            yield conn
        finally:
            transaction.rollback()


@pytest.fixture()
def built(connection, tmp_path):
    for path, body in TREE.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(body).lstrip())
    index_repository(connection, STORE, project_id=P, root=tmp_path, repo=REPO)
    return view_rollup.rebuild(connection, P)


def _id(connection, path: str, qual: str) -> str:
    return connection.execute(text("SELECT id FROM kg_nodes WHERE project_id = :p AND node_key = :k AND valid_to IS NULL"),
                              {"p": P, "k": symbol_key(REPO, path, qual)}).scalar_one()


def _raw_node(connection, nid: str, kind: str, name: str, *, attrs: dict, body: str = "",
              trust: str = "inferred", created_by: str = "system", closed: bool = False) -> str:
    connection.execute(text(
        "INSERT INTO kg_nodes (id, project_id, kind, node_key, name, attrs, body, trust_tier, layer, created_by, valid_to) "
        "VALUES (:id, :p, :k, :key, :n, CAST(:a AS JSONB), :b, :t, 'inferred', :c, CASE WHEN :closed THEN now() END)"),
        {"id": nid, "p": P, "k": kind, "key": f"{kind}:{nid}", "n": name, "a": json.dumps(attrs), "b": body,
         "t": trust, "c": created_by, "closed": closed})
    return nid


def _raw_edge(connection, src: str, dst: str, kind: str, attrs: dict | None = None) -> None:
    connection.execute(text(
        "INSERT INTO kg_edges (id, project_id, src_id, dst_id, kind, attrs, layer) "
        "VALUES (:id, :p, :s, :d, :k, CAST(:a AS JSONB), 'inferred')"),
        {"id": f"kge_{src}_{dst}_{kind}"[:60], "p": P, "s": src, "d": dst, "k": kind, "a": json.dumps(attrs or {})})


def _seed_rules_and_learning(connection) -> dict[str, str]:
    disburse = _id(connection, "loan/src/bank/loan/Disbursement.java", "Disbursement.disburse")
    ledger = _id(connection, "core/src/bank/core/Ledger.java", "Ledger")
    prov = {"agent_role": "chief_architect", "agent_name": "Scout", "run_id": "run_scout", "step": "scan",
            "proposed_at": "2026-10-24T09:00:00Z"}
    approved = _raw_node(connection, "kgn_rule_ok", "rule", "A loan is disbursed once", body="Disbursing twice is refused.",
                         trust="approved", created_by="chief_architect",
                         attrs={"status": "approved", "category": "validation", "provenance": prov, "version": 1,
                                "review": {"by": "usr_param", "at": "2026-10-24T09:30:00Z"}})
    proposed = _raw_node(connection, "kgn_rule_new", "rule", "The ledger rejects empty entries",
                         created_by="chief_architect", attrs={"status": "proposed", "category": "limit", "provenance": prov})
    _raw_node(connection, "kgn_rule_no", "rule", "Rates are negative", closed=True,
              attrs={"status": "rejected", "category": "calculation"})
    _raw_edge(connection, approved, disburse, "evidenced_by", {"lines": [5, 5]})
    _raw_edge(connection, approved, disburse, "governs")
    _raw_edge(connection, proposed, ledger, "evidenced_by")
    learned = _raw_node(connection, "kgn_learn_tf", "test_failure", "Disburse test fails without a ledger",
                        body="disburse() throws when the ledger is missing.", created_by="backend_engineer",
                        attrs={"learning_kind": "test_failure", "provenance": {
                            "agent_role": "backend_engineer", "agent_name": "Builder", "run_id": "run_b1",
                            "step": "implement-repair1", "at": "2026-10-24T10:00:00Z"}})
    _raw_edge(connection, learned, disburse, "about")
    decision = _raw_node(connection, "kgn_learn_dec", "decision", "Keep the ledger append-only",
                         attrs={"learning_kind": "decision"})
    _raw_edge(connection, decision, ledger, "about")
    run = _raw_node(connection, "kgn_run_b2", "run", "run_b2", trust="observed",
                    attrs={"agent_role": "sre", "agent_name": "Responder", "run_id": "run_b2"})
    _raw_edge(connection, run, learned, "consulted", {"how": "tool", "run_id": "run_b2"})
    return {"approved": approved, "proposed": proposed, "learned": learned, "decision": decision,
            "disburse": disburse, "ledger": ledger, "run": run}


# ── the area rule ───────────────────────────────────────────────────────────────────────────

def test_the_area_is_the_first_segment_unless_one_segment_holds_most_symbols():
    modules = view_rollup.area_rule([("core/a/X.java", 5), ("loan/b/Y.java", 5), ("setup.py", 1)])
    assert [modules(p) for p in ("core/a/X.java", "loan/b/Y.java", "setup.py")] == ["core", "loan", "(root)"]
    single = view_rollup.area_rule([("src/main/java/bank/loan/A.java", 40), ("src/main/java/bank/core/B.java", 50),
                                    ("src/main/java/bank/App.java", 5), ("tools/gen.py", 2)])
    assert single("src/main/java/bank/loan/A.java") == "loan"
    assert single("src/main/java/bank/core/B.java") == "core"
    assert single("src/main/java/bank/App.java") == "bank"      # a file in the common prefix itself
    assert single("tools/gen.py") == "tools"                    # outside the dominant segment


# ── the rollup ──────────────────────────────────────────────────────────────────────────────

def test_the_tree_runs_area_package_class_method_with_area_carrying_package_keys(connection, built):
    rows = {r["key"]: dict(r) for r in connection.execute(text(
        "SELECT * FROM kg_view_tree WHERE project_id = :p"), {"p": P}).mappings()}
    areas = {r["label"]: r for r in rows.values() if r["level"] == "area"}
    assert set(areas) == {"core", "loan", "web"}
    assert f"pkg:{REPO}:core:bank.core" in rows and f"pkg:{REPO}:loan:bank.core" in rows   # no collision
    assert areas["loan"]["child_count"] == 2 and areas["loan"]["class_count"] == 3
    disburse = rows[symbol_key(REPO, "loan/src/bank/loan/Disbursement.java", "Disbursement.disburse")]
    assert disburse["level"] == "method" and disburse["label"] == "disburse"
    assert disburse["parent_key"] == symbol_key(REPO, "loan/src/bank/loan/Disbursement.java", "Disbursement")
    account = rows[symbol_key(REPO, "core/src/bank/core/Account.java", "Account")]
    assert account["symbol_kind"] == "interface" and account["parent_key"] == f"pkg:{REPO}:core:bank.core"
    for row in rows.values():   # every parent resolves
        assert row["parent_key"] is None or row["parent_key"] in rows, row["key"]
    assert built["nodes"] == view_rollup.live_code_count(connection, P)
    assert not view_rollup.is_stale(connection, P)


def _live_aggregation(connection) -> dict[tuple[str, str, str, str], int]:
    """The rollup computed independently: every live kg_edge between symbols/files, lifted to the
    class, package and area of each end by the documented rules, in Python."""
    nodes = {}
    paths = defaultdict(int)
    for r in connection.execute(text(
            "SELECT id, kind, node_key, path, repo, attrs FROM kg_nodes WHERE project_id = :p AND valid_to IS NULL "
            "AND kind IN ('symbol', 'file')"), {"p": P}).mappings():
        nodes[r["id"]] = dict(r)
        paths[r["path"]] += 1 if r["kind"] == "symbol" else 0
    area_of = view_rollup.area_rule(paths.items())
    types = {n["node_key"] for n in nodes.values() if n["kind"] == "symbol" and n["attrs"].get("symbol_kind") != "method"}
    file_pkg = {}
    for n in sorted(nodes.values(), key=lambda n: n["attrs"].get("line") or 0):
        if n["kind"] == "symbol":
            file_pkg.setdefault(n["path"], n["attrs"].get("module"))

    def keys(n):
        area = area_of(n["path"])
        pkg = n["attrs"].get("module") if n["kind"] == "symbol" else file_pkg.get(n["path"])
        cls = None
        if n["kind"] == "symbol":
            cls = n["node_key"]
            if n["attrs"].get("symbol_kind") == "method":
                owner = n["node_key"].rsplit(".", 1)[0]
                cls = owner if owner in types else None
        return {"class": cls, "package": f"pkg:{n['repo']}:{area}:{pkg}", "area": f"area:{n['repo']}:{area}"}

    fold = {"calls": "calls", "dispatches_to": "calls", "imports": "imports", "extends": "extends", "implements": "extends"}
    out: dict[tuple[str, str, str, str], int] = defaultdict(int)
    for src, dst, kind in connection.execute(text(
            "SELECT src_id, dst_id, kind FROM kg_edges WHERE project_id = :p AND valid_to IS NULL"), {"p": P}).all():
        if kind not in fold or src not in nodes or dst not in nodes:
            continue
        s, d = keys(nodes[src]), keys(nodes[dst])
        for level in ("class", "package", "area"):
            if s[level] and d[level] and s[level] != d[level]:
                out[(level, s[level], d[level], fold[kind])] += 1
    return dict(out)


def test_the_rollup_equals_a_live_aggregation_of_the_code_graph(connection, built):
    stored = {(lv, s, d, k): w for lv, s, d, k, w in connection.execute(text(
        "SELECT level, src_key, dst_key, kind, weight FROM kg_view_edges WHERE project_id = :p"), {"p": P}).all()}
    live = _live_aggregation(connection)
    assert stored == live
    assert stored[("area", f"area:{REPO}:web", f"area:{REPO}:loan", "calls")] >= 1
    assert any(k[0] == "class" and k[3] == "extends" for k in stored)


def test_a_rebuild_replaces_the_rows_and_a_drift_marks_it_stale_until_reconciled(connection, built, pg_engine):
    _raw_node(connection, "kgn_extra_table", "table", "loans", trust="observed", attrs={})
    assert view_rollup.is_stale(connection, P)
    again = view_rollup.rebuild(connection, P)
    assert again["nodes"] == built["nodes"] + 1 and again["tree_rows"] == built["tree_rows"]
    assert not view_rollup.is_stale(connection, P)
    # Learning and rules never make it stale: they are read live.
    _raw_node(connection, "kgn_some_fact", "fact", "x", attrs={})
    assert not view_rollup.is_stale(connection, P)
    # A re-index that removes a class: the rebuild applies the difference, and still equals the
    # live aggregation (its rows and its edges go; nothing else is left behind).
    plan = symbol_key(REPO, "loan/src/bank/loan/DisbursementPlan.java", "DisbursementPlan")
    connection.execute(text("UPDATE kg_nodes SET valid_to = now() WHERE project_id = :p AND node_key LIKE :k"),
                       {"p": P, "k": plan + "%"})
    view_rollup.rebuild(connection, P)
    keys = set(connection.execute(text("SELECT key FROM kg_view_tree WHERE project_id = :p"), {"p": P}).scalars())
    assert plan not in keys and plan + ".schedule" not in keys
    stored = {(lv, s, d, k): w for lv, s, d, k, w in connection.execute(text(
        "SELECT level, src_key, dst_key, kind, weight FROM kg_view_edges WHERE project_id = :p"), {"p": P}).all()}
    assert stored == _live_aggregation(connection)
    # The sync row holds the project's foreign key; deleting it takes the project's rows with it.
    connection.execute(text("DELETE FROM kg_view_sync WHERE project_id = :p"), {"p": P})
    assert connection.execute(text("SELECT (SELECT count(*) FROM kg_view_tree WHERE project_id = :p) + "
                                   "(SELECT count(*) FROM kg_view_edges WHERE project_id = :p)"), {"p": P}).scalar() == 0
    assert view_rollup.is_stale(connection, P)


# ── overview ────────────────────────────────────────────────────────────────────────────────

def test_the_overview_draws_areas_their_calls_and_the_layer_bubbles(connection, built):
    ids = _seed_rules_and_learning(connection)
    out = view.overview(connection, P)
    assert out["level"] == "area" and out["repo"] == REPO and out["repos"] == [REPO]
    assert [a["label"] for a in out["areas"]][0] == "loan"            # most classes first
    loan = next(a for a in out["areas"] if a["label"] == "loan")
    assert loan["sub"] == "3 classes" and set(loan["counts"]) == {"classes", "methods", "packages"}
    calls = [e for e in out["edges"] if e["kind"] == "calls"]
    assert calls and all(e["label"] == f"{e['weight']:,} call{'' if e['weight'] == 1 else 's'}" for e in calls)
    rules, learning = out["layers"]
    assert rules["counts"] == {"proposed": 1, "approved": 1, "rejected": 1, "superseded": 0}
    # The one count's words (rules.rule_count_words), as Rules review and the checklist say it.
    assert rules["sub"] == "1 approved · 1 waiting for review · 1 rejected"
    assert learning["counts"] == {"test_failure": 1, "decision": 1} and learning["sub"] == "2 learned"
    governs = {e["dst"]: e for e in out["edges"] if e["src"] == "layer:rules"}
    assert governs[f"area:{REPO}:loan"]["label"] == "1 rule" and governs[f"area:{REPO}:core"]["weight"] == 1
    assert {e["dst"] for e in out["edges"] if e["src"] == "layer:learning"} == {f"area:{REPO}:loan", f"area:{REPO}:core"}
    assert out["totals"]["nodes"] == built["nodes"] and out["stale"] is False and out["built_at"]
    # Without decision:read, the decision is neither counted nor drawn.
    hidden = view.overview(connection, P, withheld={"decision", "precedent"})
    assert hidden["layers"][1]["counts"] == {"test_failure": 1}
    assert {e["dst"] for e in hidden["edges"] if e["src"] == "layer:learning"} == {f"area:{REPO}:loan"}
    # The chips: code only.
    code = view.overview(connection, P, layers={"code"})
    assert code["layers"] == [] and all(e["kind"] == "calls" for e in code["edges"])
    assert ids


def test_the_overview_drills_into_an_area_as_its_packages(connection, built):
    out = view.overview(connection, P, area="loan")
    assert out["level"] == "package" and out["parent"] == f"area:{REPO}:loan"
    assert {a["id"] for a in out["areas"]} == {f"pkg:{REPO}:loan:bank.loan", f"pkg:{REPO}:loan:bank.core"}
    assert all(a["kind"] == "package" for a in out["areas"])
    with pytest.raises(LookupError):
        view.overview(connection, P, area="nowhere")


# ── neighbourhood ───────────────────────────────────────────────────────────────────────────

def test_a_class_neighbourhood_walks_the_class_rollup_within_depth_and_limit(connection, built):
    ledger = _id(connection, "core/src/bank/core/Ledger.java", "Ledger")
    one = view.neighbourhood(connection, P, ledger, depth=1)
    assert one["level"] == "class" and one["center"]["id"] == ledger and one["center"]["hop"] == 0
    labels = {n["label"] for n in one["nodes"] if n["hop"] == 1}
    assert labels == {"Disbursement"}
    two = view.neighbourhood(connection, P, ledger, depth=2)
    assert {n["label"] for n in two["nodes"] if n["hop"] == 2} >= {"LoanController", "DisbursementPlan"}
    assert all(0 <= n["hop"] <= 2 for n in two["nodes"])
    capped = view.neighbourhood(connection, P, ledger, depth=2, limit=2)
    assert len(capped["nodes"]) <= 3 and capped["truncated"] and capped["hidden"]["nodes"] >= 1
    ids = {n["id"] for n in two["nodes"]}
    assert all(e["src"] in ids and e["dst"] in ids for e in two["edges"])
    for bad in ({"depth": 0}, {"depth": 4}, {"limit": 0}, {"limit": 500}):
        with pytest.raises(ValueError):
            view.neighbourhood(connection, P, ledger, **bad)


def test_a_method_walks_its_calls_with_its_class_as_context(connection, built):
    disburse = _id(connection, "loan/src/bank/loan/Disbursement.java", "Disbursement.disburse")
    out = view.neighbourhood(connection, P, disburse, depth=1)
    assert out["level"] == "method" and out["center"]["label"] == "disburse()"
    labels = {n["label"] for n in out["nodes"]}
    assert {"post()", "validate()", "credit()", "Disbursement"} <= labels
    assert any(e["kind"] == "defines" for e in out["edges"])
    assert all(e["label"] is None for e in out["edges"] if e["weight"] == 1)


def test_rules_and_learnings_join_a_code_centre_only_when_their_chip_is_on(connection, built):
    ids = _seed_rules_and_learning(connection)
    disbursement = _id(connection, "loan/src/bank/loan/Disbursement.java", "Disbursement")
    on = view.neighbourhood(connection, P, disbursement)
    kinds = {n["kind"] for n in on["nodes"]}
    assert {"rule", "test_failure"} <= kinds
    assert any(e["src"] == ids["approved"] and e["dst"] == disbursement for e in on["edges"])
    off = view.neighbourhood(connection, P, disbursement, layers={"code"})
    assert {n["kind"] for n in off["nodes"]} == {"symbol"}
    rule = view.neighbourhood(connection, P, ids["approved"])
    assert rule["level"] == "graph" and {n["label"] for n in rule["nodes"]} >= {"disburse()"}
    without = view.neighbourhood(connection, P, ids["ledger"], withheld={"decision", "precedent"})
    assert "decision" not in {n["kind"] for n in without["nodes"]}
    with pytest.raises(PermissionError):
        view.neighbourhood(connection, P, ids["decision"], withheld={"decision"})
    area = view.neighbourhood(connection, P, f"area:{REPO}:loan")
    assert area["level"] == "area" and area["center"]["kind"] == "area"


# ── node detail ─────────────────────────────────────────────────────────────────────────────

def test_node_detail_groups_connections_pages_them_and_names_what_is_not_embedded(connection, built):
    ids = _seed_rules_and_learning(connection)
    out = view.node_detail(connection, P, ids["disburse"])
    assert out["node"]["symbol_kind"] == "method" and out["area"] == "loan" and out["package"] == "bank.loan"
    assert out["line"] and out["language"] == "java" and out["statement"] is None
    groups = {(g["kind"], g["direction"]): g for g in out["connections"]}
    assert groups[("calls", "out")]["label"] == "Calls" and groups[("calls", "out")]["total"] >= 3
    assert groups[("evidenced_by", "in")]["label"] == "Evidence for"
    page = view.node_detail(connection, P, ids["disburse"], edge="calls", direction="out", offset=1)
    assert len(page["connections"]) == 1
    assert len(page["connections"][0]["items"]) == groups[("calls", "out")]["total"] - 1
    assert out["vector"] == {"embedded": False,
                             "reason": "Not embedded: code symbols are matched by structure, not meaning"}
    assert out["provenance"]["created_by_kind"] == "system" and out["provenance"]["events"][0]["what"] == "indexed"
    rule = view.node_detail(connection, P, ids["approved"])
    assert rule["statement"] == "Disbursing twice is refused." and rule["node"]["status"] == "approved"
    assert rule["provenance"]["created_by_kind"] == "agent" and rule["provenance"]["approved_by"] == "usr_param"
    learned = view.node_detail(connection, P, ids["learned"])
    assert learned["usage"]["consulted_runs"] == 1
    assert learned["lineage"]["learned"]["agent_name"] == "Builder"
    assert learned["lineage"]["used"][0]["run_id"] == "run_b2" and learned["lineage"]["used"][0]["how"] == "consulted"
    area = view.node_detail(connection, P, f"area:{REPO}:loan")
    assert area["node"]["kind"] == "area" and area["connections"]
    with pytest.raises(LookupError):
        view.node_detail(connection, P, "kgn_nope")
    hidden = view.node_detail(connection, P, ids["ledger"], withheld={"decision"})
    assert all(i["kind"] != "decision" for g in hidden["connections"] for i in g["items"])


def test_a_class_detail_lists_its_members_from_the_tree_and_its_methods_edges_rolled_up(connection, built):
    """The index draws ``defines`` file -> symbol only: a class's members come from the tree
    rollup and its calls from the class-level rollup (tester round 3: "declares no methods")."""
    ledger = _id(connection, "core/src/bank/core/Ledger.java", "Ledger")
    d = view.node_detail(connection, P, ledger)
    groups = {(g["kind"], g["direction"]): g for g in d["connections"]}
    members = groups[("defines", "out")]
    assert members["total"] == 2 and [i["label"] for i in members["items"]] == ["post()", "validate()"]
    assert all(i["symbol_kind"] == "method" for i in members["items"])
    called = groups[("calls", "in")]
    assert called["label"] == "Called by" and {i["label"] for i in called["items"]} == {"Disbursement"}
    # The file that defines it is still there; no group is listed twice.
    assert ("defines", "in") in groups
    assert len(groups) == len(d["connections"])
    page = view.node_detail(connection, P, ledger, edge="defines", direction="out", offset=1)
    assert [g["kind"] for g in page["connections"]] == ["defines"]
    assert [i["label"] for i in page["connections"][0]["items"]] == ["validate()"]
    plan = view.node_detail(connection, P, _id(connection, "loan/src/bank/loan/DisbursementPlan.java", "DisbursementPlan"))
    plan_groups = {(g["kind"], g["direction"]): g for g in plan["connections"]}
    assert {i["label"] for i in plan_groups[("calls", "out")]["items"]} == {"Disbursement"}
    assert {i["label"] for i in plan_groups[("extends", "out")]["items"]} == {"Disbursement"}
    focus = view.neighbourhood(connection, P, ledger)
    assert focus["center"]["counts"] == {"methods": 2}


# ── tree ────────────────────────────────────────────────────────────────────────────────────

def test_the_tree_loads_lazily_and_a_search_opens_to_its_matches(connection, built):
    areas = view.tree_children(connection, P)
    assert [r["label"] for r in areas["rows"]] == ["core", "loan", "web"] and areas["next_offset"] is None
    loan = next(r for r in areas["rows"] if r["label"] == "loan")
    packages = view.tree_children(connection, P, parent=loan["key"])
    assert {r["label"] for r in packages["rows"]} == {"bank.core", "bank.loan"}
    classes = view.tree_children(connection, P, parent=f"pkg:{REPO}:loan:bank.loan")
    assert [r["label"] for r in classes["rows"]] == ["Disbursement", "DisbursementPlan"]
    methods = view.tree_children(connection, P, parent=classes["rows"][0]["key"])
    assert [r["label"] for r in methods["rows"]] == ["credit", "disburse"] and not methods["rows"][0]["has_children"]
    found = view.tree_children(connection, P, q="disburse")
    keys = {r["key"]: r for r in found["rows"]}
    assert keys[f"area:{REPO}:loan"]["match"] is False and keys[f"pkg:{REPO}:loan:bank.loan"]["match"] is False
    assert any(r["match"] and r["label"] == "disburse" for r in found["rows"])
    assert all(r["parent_key"] is None or r["parent_key"] in keys for r in found["rows"])


def test_the_rules_and_learning_tabs_group_by_status_category_and_kind(connection, built):
    ids = _seed_rules_and_learning(connection)
    top = view.tree_children(connection, P, tab="rules")
    assert [r["key"] for r in top["rows"]] == ["rules:proposed", "rules:approved", "rules:rejected"]
    cats = view.tree_children(connection, P, tab="rules", parent="rules:approved")
    assert [r["label"] for r in cats["rows"]] == ["Validation"]
    items = view.tree_children(connection, P, tab="rules", parent="rules:approved:validation")
    assert [r["node_id"] for r in items["rows"]] == [ids["approved"]] and items["rows"][0]["kind"] == "rule"
    learning = view.tree_children(connection, P, tab="learning")
    assert [r["label"] for r in learning["rows"]] == ["Test failures", "Decisions", "Runs it came from"]
    hidden = view.tree_children(connection, P, tab="learning", withheld={"decision", "precedent"})
    assert "Decisions" not in [r["label"] for r in hidden["rows"]]
    search = view.tree_children(connection, P, tab="rules", q="ledger")
    assert {r["key"] for r in search["rows"] if not r.get("match")} == {"rules:proposed", "rules:proposed:limit"}


# ── search ──────────────────────────────────────────────────────────────────────────────────

def test_search_ranks_exact_then_prefix_then_text_classes_before_methods(connection, built):
    _seed_rules_and_learning(connection)
    out = view.search(connection, P, "disburse")
    order = [(h["match"], h["node"]["label"]) for h in out["hits"]]
    assert order[0] == ("exact", "disburse()")
    prefix = [label for match, label in order if match == "prefix"]
    assert prefix.index("Disbursement") < prefix.index("disburse()") if "disburse()" in prefix else True
    assert prefix[:2] == ["Disbursement", "DisbursementPlan"]
    assert [h["score"] for h in out["hits"]] == sorted((h["score"] for h in out["hits"]), reverse=True)
    assert out["meaning"] is False and out["meaning_available"] is False
    rules = view.search(connection, P, "ledger", layers={"rules"})
    assert [h["node"]["kind"] for h in rules["hits"]] == ["rule"]
    with pytest.raises(ValueError):
        view.search(connection, P, "disburse", meaning=True)   # no embedding model: said in words
    with pytest.raises(ValueError):
        view.search(connection, P, "  ")


def test_search_finds_a_learning_by_an_incident_id_in_its_evidence_or_its_conversation(connection):
    """Tester round 4: "INC-2041" and "INC-2088" found nothing. INC-2041 sat in a root cause's
    evidence (attrs, not its name); INC-2088 only in the operator's brief — the conversation the
    root cause was recorded from — never in the root cause itself. No code index needed."""
    _raw_node(connection, "kgn_rc_2041", "root_cause", "Standard strategy overbooks principal",
              body="principal is paid from the pre-interest amount",
              attrs={"learning_kind": "root_cause",
                     "evidence": [{"ref": "INC-2041 reconciliation rows", "type": "metric"}]})
    rc = _raw_node(connection, "kgn_rc_2088", "root_cause", "Advance-payment handler never decrements the remaining amount",
                   attrs={"learning_kind": "root_cause"})
    connection.execute(text("INSERT INTO agent_threads (id, project_id, actor) VALUES ('athr_kgv', :p, 'ceo')"), {"p": P})
    connection.execute(text(
        "INSERT INTO agent_messages (id, thread_id, role, content) VALUES "
        "('amsg_kgv1', 'athr_kgv', 'user', '# B4 · Incident: early repayments\n**Incident INC-2088 · Severity 2**')"))
    conv = _raw_node(connection, "kgn_conv_b4", "run", "B4 · Incident: early repayments · Oct 25, 07:13", trust="observed",
                     attrs={"subtype": "conversation", "thread_id": "athr_kgv", "source_id": "athr_kgv"})
    _raw_edge(connection, conv, rc, "produced")

    by_evidence = view.search(connection, P, "INC-2041")
    assert [h["node"]["id"] for h in by_evidence["hits"]][:1] == ["kgn_rc_2041"]
    by_brief = [h["node"]["id"] for h in view.search(connection, P, "inc-2088")["hits"]]
    assert by_brief[0] == rc and conv in by_brief          # the learning first, then where it came from
    # Their text, not only code symbols: a body's words find it.
    assert "kgn_rc_2041" in [h["node"]["id"] for h in view.search(connection, P, "pre-interest")["hits"]]
    # A layer turned off stays off, and provenance is never what matches.
    assert view.search(connection, P, "INC-2041", layers={"code"})["hits"] == []


def test_a_hop_that_needs_less_than_its_share_leaves_the_rest_to_the_next(connection):
    """Tester round 4: Show 200 at 2 hops read 147 of 809 — each hop was held to 100 although the
    first had only 47. The limit is the whole view's: what one hop leaves, the next may use."""
    centre = _raw_node(connection, "kgn_hop_c", "investigation", "Why the allocator overbooks", attrs={})
    for i in range(3):
        a = _raw_node(connection, f"kgn_hop_a{i}", "investigation", f"First {i}", attrs={})
        _raw_edge(connection, centre, a, "about")
        for j in range(6):
            b = _raw_node(connection, f"kgn_hop_b{i}{j}", "investigation", f"Second {i}{j}", attrs={})
            _raw_edge(connection, a, b, "about")
    out = view.neighbourhood(connection, P, centre, depth=2, limit=10)
    hops = [n.get("hop") for n in out["nodes"] if n["id"] != centre]
    assert len(hops) == 10 and hops.count(1) == 3 and hops.count(2) == 7
    assert out["truncated"] and out["hidden"]["nodes"] == 18 - 7


# ── the wire contract ───────────────────────────────────────────────────────────────────────

_NODE_KEYS = {"id", "key", "kind", "symbol_kind", "layer", "label", "sub", "trust_tier", "status", "counts"}


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"kg-{name}.json").read_text())


def _nodes_hold(nodes) -> None:
    for n in nodes:
        assert set(n) - {"hop"} <= _NODE_KEYS, set(n) - _NODE_KEYS
        assert {"id", "key", "kind", "layer", "label", "trust_tier"} <= set(n)
        assert n["layer"] in ("code", "rules", "learning", "domain", "context")


def test_every_payload_has_exactly_the_phase_0_wire_shape(connection, built):
    ids = _seed_rules_and_learning(connection)
    top = lambda d: set(d) - {"_fixture"}   # noqa: E731
    o = view.overview(connection, P)
    assert top(o) == top(_fixture("overview"))
    _nodes_hold(o["areas"] + o["layers"])
    assert all(set(e) == {"src", "dst", "kind", "weight", "label"} for e in o["edges"])
    n = view.neighbourhood(connection, P, ids["disburse"])
    assert top(n) == top(_fixture("neighbourhood"))
    _nodes_hold(n["nodes"])
    d = view.node_detail(connection, P, ids["disburse"])
    assert top(d) == top(_fixture("node-detail")) | {"node"}
    assert set(d["connections"][0]) == set(_fixture("node-detail")["connections"][0])
    assert set(d["usage"]) == set(_fixture("node-detail")["usage"])
    t = view.tree_children(connection, P)
    assert top(t) == set(_fixture("tree")["areas"])
    assert set(t["rows"][0]) == set(_fixture("tree")["areas"]["rows"][0])
    s = view.search(connection, P, "disburse")
    assert top(s) == top(_fixture("search"))
    assert set(s["hits"][0]) == set(_fixture("search")["hits"][0])


# ── when it runs ────────────────────────────────────────────────────────────────────────────

def test_the_index_end_and_the_worker_reconcile_keep_the_rollup_current(pg_engine, tmp_path):
    """Committed, in its own project: both callers open their own transactions."""
    import uuid

    from boss_engine.services.knowledge.ingest import reconcile_projections

    tag = uuid.uuid4().hex[:8]
    pid = f"proj_kgvr{tag}"
    for path, body in TREE.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(body).lstrip())
    with pg_engine.begin() as c:
        c.execute(text("INSERT INTO orgs (id, name) VALUES (:o, 'KGVR')"), {"o": f"org_kgvr{tag}"})
        c.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'KGVR')"),
                  {"w": f"ws_kgvr{tag}", "o": f"org_kgvr{tag}"})
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, :p, :p)"),
                  {"p": pid, "w": f"ws_kgvr{tag}"})
        index_repository(c, STORE, project_id=pid, root=tmp_path, repo=REPO)
    try:
        # The worker's reconcile finds it never built, and builds it; then it is current.
        first = reconcile_projections(pg_engine, project_id=pid)["views"]
        assert first == {"current": 0, "rebuilt": 1, "failed": 0}
        assert reconcile_projections(pg_engine, project_id=pid)["views"] == {"current": 1, "rebuilt": 0, "failed": 0}
        # The end of an index that changed nothing leaves a current rollup alone; any other rebuilds.
        assert view_rollup.rebuild_after_index(pg_engine, pid, {"mode": "unchanged"})["mode"] == "current"
        again = view_rollup.rebuild_after_index(pg_engine, pid, {"mode": "incremental"})
        assert again["nodes"] > 0 and "error" not in again
    finally:
        with pg_engine.begin() as c:
            c.execute(text("DELETE FROM kg_view_sync WHERE project_id = :p"), {"p": pid})
            c.execute(text("DELETE FROM kg_edges WHERE project_id = :p"), {"p": pid})
            c.execute(text("DELETE FROM kg_nodes WHERE project_id = :p"), {"p": pid})
