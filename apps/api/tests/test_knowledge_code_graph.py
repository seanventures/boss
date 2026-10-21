"""Knowledge Graph Phase 1: the code graph and the lifecycle graph end to end (spec §3.3, §4, §5, §7).

A small multi-language repository is built with git in a temp dir and indexed into the isolated
boss_test database. The tests prove name resolution across files (imports, re-exports, Go packages,
TS modules), framework facts, cross-repository edges, incremental updates that converge on a full
re-index, scope, and the lifecycle records joined to code through kg_why.
"""
from __future__ import annotations

import subprocess
import textwrap
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from apps.api.app.db import begin
from boss_engine.services.knowledge import Scope
from boss_engine.services.knowledge.code_index import CodeIndexer
from boss_engine.services.knowledge.code_queries import KnowledgeQueries
from boss_engine.services.knowledge.lifecycle import LifecycleGraph

REPO = "acme/shop"
Q = KnowledgeQueries()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=ci@example.com", "-c", "user.name=ci", *args], cwd=root,
                          capture_output=True, text=True, check=True).stdout.strip()


def _write(root: Path, files: dict[str, str]) -> str:
    for path, body in files.items():
        p = root / path
        if body is None:
            p.unlink()
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body).lstrip())
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "change")
    return _git(root, "rev-parse", "HEAD")


SHOP = {
    "app/__init__.py": "from .models import Order\n",
    "app/models.py": """
        class Order:
            @property
            def total_due(self):
                return 1

            def mark_paid(self):
                return self
        """,
    "app/service.py": """
        from django.conf import settings
        from .models import Order

        def checkout(order: Order):
            order.mark_paid()
            return order.total_due, settings.PAYMENT_PROVIDER

        def report():
            return "SELECT id FROM stock_reservation"
        """,
    "app/api.py": """
        from app.service import checkout
        from .webhooks import WebhookEventAsyncType

        @router.post("/orders")
        def create(request):
            notify(WebhookEventAsyncType.ORDER_CREATED)
            return checkout(request.order)
        """,
    "app/consumer.py": """
        from app import Order          # through the package's re-export

        def build():
            return Order()
        """,
    "web/price.ts": "export function price(n: number) { return n * 2; }\n",
    "web/cart.ts": "import { price } from './price';\nexport function total(xs: number[]) { return xs.map(price); }\n",
    "go.mod": "module example.com/shop\n",
    "internal/stock/stock.go": "package stock\n\nfunc Reserve(n int) int { return n }\n",
    "cmd/main.go": 'package main\n\nimport "example.com/shop/internal/stock"\n\nfunc main() { stock.Reserve(1) }\n',
}

FULFILMENT = {
    "fulfil/handler.py": """
        @app.post("/webhooks/saleor/order-created")
        def order_created(request):
            db.execute("INSERT INTO stock_reservation (line) VALUES (%s)", [request.line])
        """,
}


@pytest.fixture
def scope(engine):
    with begin(engine) as c:
        pid = c.execute(text("SELECT id FROM projects ORDER BY created_at LIMIT 1")).scalar_one()
        other = "proj-kg-code-" + uuid.uuid4().hex[:6]
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) SELECT :o, workspace_id, :o, :o "
                       "FROM projects WHERE id = :p"), {"o": other, "p": pid})
    return Scope(other)


@pytest.fixture
def shop(tmp_path):
    root = tmp_path / "shop"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, SHOP)
    return root


def _edges(c, scope, kind=None):
    rows = c.execute(text(
        "SELECT s.node_key AS src, d.node_key AS dst, e.kind FROM kg_edges e JOIN kg_nodes s ON s.id = e.src_id "
        "JOIN kg_nodes d ON d.id = e.dst_id WHERE e.project_id = :p AND e.valid_to IS NULL"
        + (" AND e.kind = :k" if kind else "")), {"p": scope.project_id, "k": kind}).all()
    return {(r.src, r.dst, r.kind) for r in rows}


def k(path, qual=None):
    return f"{REPO}:{path}" + (f"::{qual}" if qual else "")


def test_a_full_index_resolves_names_across_files_and_languages(engine, scope, shop):
    with begin(engine) as c:
        run = CodeIndexer(workers=1).index_full(c, scope, REPO, shop)
        edges = _edges(c, scope)
    assert run["files"] == 9 and run["symbols"] >= 9
    assert (k("app/api.py", "create"), k("app/service.py", "checkout"), "calls") in edges
    assert (k("app/service.py", "checkout"), k("app/models.py", "Order.mark_paid"), "calls") in edges   # unique method
    assert (k("app/service.py", "checkout"), k("app/models.py", "Order.total_due"), "references") in edges  # a property read
    assert (k("app/service.py", "checkout"), k("app/models.py", "Order"), "references") in edges         # a type annotation
    assert (k("app/consumer.py", "build"), k("app/models.py", "Order"), "calls") in edges                 # via the re-export
    assert (k("web/cart.ts", "total"), k("web/price.ts", "price"), "references") in edges
    assert (k("cmd/main.go", "main"), k("internal/stock/stock.go", "Reserve"), "calls") in edges          # go.mod module path
    assert (k("app/api.py"), k("app/service.py", "checkout"), "imports") in edges
    assert (k("app/service.py", "checkout"), "config:PAYMENT_PROVIDER", "reads") in edges
    assert (k("app/api.py", "create"), f"{REPO}:endpoint:POST /orders", "exposes") in edges
    assert (k("app/service.py", "report"), "table:stock_reservation", "reads") in edges


def test_two_repositories_meet_at_shared_tables_and_webhooks(engine, scope, shop, tmp_path):
    fulfil = tmp_path / "fulfilment"
    fulfil.mkdir()
    _git(fulfil, "init", "-q")
    _write(fulfil, FULFILMENT)
    with begin(engine) as c:
        CodeIndexer(workers=1).index_full(c, scope, REPO, shop)
        CodeIndexer(workers=1).index_full(c, scope, "acme/fulfilment", fulfil)
        edges = _edges(c, scope)
    writer = ("acme/fulfilment:fulfil/handler.py::order_created", "table:stock_reservation", "writes")
    reader = (k("app/service.py", "report"), "table:stock_reservation", "reads")
    assert writer in edges and reader in edges
    assert (k("app/api.py", "create"), "queue:webhook:ORDER_CREATED", "publishes") in edges
    assert ("acme/fulfilment:fulfil/handler.py::order_created", "queue:webhook:ORDER_CREATED", "subscribes") in edges


def test_an_incremental_update_closes_what_went_away_and_converges_on_a_full_index(engine, scope, shop):
    indexer = CodeIndexer(workers=1)
    with begin(engine) as c:
        indexer.index_full(c, scope, REPO, shop)
    _write(shop, {
        "app/service.py": """
            from .models import Order

            def pay(order: Order):
                return order.mark_paid()
            """,
        "app/api.py": """
            from app.service import pay

            def create(request):
                return pay(request.order)
            """,
        "app/consumer.py": None,
    })
    with begin(engine) as c:
        run = indexer.index_incremental(c, scope, REPO, shop)
        after = _edges(c, scope)
        closed = c.execute(text("SELECT valid_to IS NOT NULL FROM kg_nodes WHERE project_id = :p AND node_key = :k"),
                           {"p": scope.project_id, "k": k("app/service.py", "checkout")}).scalar_one()
    assert run["mode"] == "incremental" and run["changed"] == 2 and run["deleted"] == 1
    assert closed is True                                                   # never deleted, validity closed
    assert (k("app/api.py", "create"), k("app/service.py", "pay"), "calls") in after
    assert not any(dst == k("app/service.py", "checkout") for _, dst, _ in after)
    assert not any(src.startswith(k("app/consumer.py")) for src, _, _ in after)

    fresh = Scope(scope.project_id + "-fresh")
    with begin(engine) as c:
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) SELECT :f, workspace_id, :f, :f "
                       "FROM projects WHERE id = :p"), {"f": fresh.project_id, "p": scope.project_id})
        CodeIndexer(workers=1).index_full(c, fresh, REPO, shop)
        rebuilt = _edges(c, fresh)
    assert after == rebuilt                                                 # incremental == full re-index


def test_an_unchanged_file_is_not_parsed_again(engine, scope, shop):
    indexer = CodeIndexer(workers=1)
    with begin(engine) as c:
        first = indexer.index_full(c, scope, REPO, shop)
        again = indexer.index_full(c, scope, REPO, shop)
    assert first["parsed"] == 9 and again["parsed"] == 0 and again["closed"] == {"nodes": 0, "edges": 0}


def test_queries_answer_symbol_callers_blast_radius_and_stay_in_scope(engine, scope, shop):
    with begin(engine) as c:
        CodeIndexer(workers=1).index_full(c, scope, REPO, shop)
        sym = Q.symbol(c, scope, "checkout")["symbols"]
        callers = Q.callers(c, scope, "checkout")
        radius = Q.blast_radius(c, scope, k("app/models.py", "Order"), depth=2)
        by_file = Q.blast_radius(c, scope, "app/models.py", depth=1)
        with pytest.raises(LookupError):
            Q.symbol(c, Scope("no-such-project"), "checkout")
        other = Scope("proj-kg-code-empty")
        c.execute(text("INSERT INTO projects (id, workspace_id, name, slug) SELECT :o, workspace_id, :o, :o "
                       "FROM projects WHERE id = :p ON CONFLICT DO NOTHING"), {"o": other.project_id, "p": scope.project_id})
        assert Q.symbol(c, other, "checkout")["symbols"] == []
    assert sym[0]["path"] == "app/service.py" and sym[0]["callers"] >= 1
    assert {x["key"] for x in callers["callers"]} == {k("app/api.py", "create")}
    # Order -> checkout/consumer (depth 1) -> api.create (depth 2) and its endpoint.
    assert set(radius["files_by_depth"][1]) >= {"app/service.py", "app/consumer.py", "app/__init__.py"}
    assert "app/api.py" in radius["files_by_depth"][2]
    assert {e["name"] for e in radius["endpoints"]} == {"POST /orders"}
    assert "app/service.py" in by_file["files_by_depth"][1]


def test_kg_why_joins_specs_prs_decisions_findings_and_incidents_to_code(engine, scope, shop):
    p = scope.project_id
    with begin(engine) as c:
        CodeIndexer(workers=1).index_full(c, scope, REPO, shop)
        c.execute(text("INSERT INTO decisions (id, project_id, question, status, approved_by, resolution_note) "
                       "VALUES (:d, :p, 'Ship checkout refactor?', 'approved', 'operator', 'Payments signed off')"),
                  {"d": f"dec-{p}", "p": p})
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, title, status, spec, state, finalize_ctx, pull_request_url, "
            "pull_request_number, decision_id) VALUES (:r, :p, 'Refactor checkout', 'completed', CAST(:spec AS JSONB), "
            "CAST(:state AS JSONB), '{}'::jsonb, 'https://github.example.com/acme/shop/pull/7', 7, :d)"),
            {"r": f"run-{p}", "p": p, "d": f"dec-{p}",
             "spec": '{"owner": "acme", "repo": "shop", "instructions": "Split checkout out of the API."}',
             "state": '{"paths": ["app/service.py"], "deferred_findings": [{"severity": "minor", '
                      '"path": "app/service.py", "line": 4, "text": "checkout returns a tuple"}]}'})
        c.execute(text("INSERT INTO incidents (id, project_id, dedupe_key, title, service) "
                       "VALUES (:i, :p, :i, 'Orders oversold', 'checkout')"), {"i": f"inc-{p}", "p": p})
        from boss_engine.services.knowledge_graph import KnowledgeGraphService
        KnowledgeGraphService().record_root_cause(c, p, "checkout", "root_cause", "rc-paid-twice", "paid twice",
                                                  confidence=0.9, evidence={"repo": REPO, "path": "app/service.py"})
        c.execute(text("INSERT INTO learning_examples (id, project_id, agent, kind, label, outcome, ref, payload) "
                       "VALUES (:id, :p, 'sre', 'incident_feedback', 'real', 'confirmed', :i, CAST(:pl AS JSONB))"),
                  {"id": f"le-{p}", "p": p, "i": f"inc-{p}", "pl": '{"cause": {"kind": "root_cause", "key": "rc-paid-twice"}}'})
        c.execute(text("INSERT INTO remediations (id, project_id, incident_id, code_task_run_id) VALUES (:m, :p, :i, :r)"),
                  {"m": f"rem-{p}", "p": p, "i": f"inc-{p}", "r": f"run-{p}"})
        first = LifecycleGraph().sync(c, scope)
        again = LifecycleGraph().sync(c, scope)
        why = Q.why(c, scope, "checkout")
    assert first["runs"] == 1 and again["nodes"] == first["nodes"]            # idempotent
    assert [x["key"] for x in why["pull_requests"]] == ["pull_request:acme/shop#7"]
    assert why["specs"][0]["instructions"].startswith("Split checkout")
    assert why["decisions"][0]["approved_by"] == "operator" and why["decisions"][0]["reason"] == "Payments signed off"
    assert why["review_findings"][0]["text"] == "checkout returns a tuple"
    assert [x["key"] for x in why["root_causes"]] == ["rc-paid-twice"]
    assert [x["key"] for x in why["incidents"]] == [f"incident:inc-{p}"]
    assert any(f["key"] == "pull_request:acme/shop#7" for f in why["fixes"])
