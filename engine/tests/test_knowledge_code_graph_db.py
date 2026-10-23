"""The code graph resolves names across files (BOSSAPP-622; ported from PRs #11-#14).

A small multi-language repository and a TypeScript monorepo are written to a temp dir and indexed
into the disposable test database inside one rolled-back transaction. Breaks covered: a call into
another file that never links (the per-file indexer this replaces scored 3/30 and 0/15 on the
blind answer keys), a re-export or ``export *`` chain that stops at the barrel, a workspace
package name or tsconfig alias read as a third-party import, an npm scope's ``@`` dropped, a
symbol that went away left open, and an incremental index that does not converge on a full one.
"""
from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest
from boss_engine.db import ensure_schema
from boss_engine.services.knowledge.code_index import index_files, index_repository, symbol_key
from boss_engine.services.knowledge.service import KnowledgeService
from boss_engine.services.knowledge.store import PostgresKnowledgeStore
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

STORE = PostgresKnowledgeStore()
SERVICE = KnowledgeService(STORE)


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
            conn.execute(text("INSERT INTO orgs (id, name) VALUES ('org_cg', 'CG')"))
            conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_cg', 'org_cg', 'CG')"))
            conn.execute(text(
                "INSERT INTO projects (id, workspace_id, name, slug) VALUES "
                "('proj_cg', 'ws_cg', 'CG', 'cg'), ('proj_cg2', 'ws_cg', 'CG2', 'cg2')"))
            yield conn
        finally:
            transaction.rollback()


def _write(root: Path, files: dict) -> Path:
    for path, body in files.items():
        p = root / path
        if body is None:
            p.unlink()
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body).lstrip())
    return root


def _edges(connection, project_id: str, kind: str | None = None) -> set[tuple[str, str, str]]:
    rows = connection.execute(text(
        "SELECT s.node_key AS src, d.node_key AS dst, e.kind FROM kg_edges e "
        "JOIN kg_nodes s ON s.id = e.src_id JOIN kg_nodes d ON d.id = e.dst_id "
        "WHERE e.project_id = :p AND e.valid_to IS NULL" + (" AND e.kind = :k" if kind else "")),
        {"p": project_id, "k": kind}).all()
    return {(r.src, r.dst, r.kind) for r in rows}


def s(path: str, qual: str) -> str:
    return symbol_key(None, path, qual)


def f(path: str) -> str:
    return f"file:{path}"


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


def test_a_full_index_resolves_names_across_files_and_languages(connection, tmp_path):
    root = _write(tmp_path / "shop", SHOP)
    run = index_repository(connection, STORE, project_id="proj_cg", root=root)
    edges = _edges(connection, "proj_cg")
    assert run["files"] == 9 and run["symbols"] >= 9
    assert (s("app/api.py", "create"), s("app/service.py", "checkout"), "calls") in edges
    assert (s("app/service.py", "checkout"), s("app/models.py", "Order.mark_paid"), "calls") in edges
    assert (s("app/service.py", "checkout"), s("app/models.py", "Order.total_due"), "references") in edges
    assert (s("app/service.py", "checkout"), s("app/models.py", "Order"), "references") in edges
    assert (s("app/consumer.py", "build"), s("app/models.py", "Order"), "calls") in edges   # via the re-export
    assert (s("web/cart.ts", "total"), s("web/price.ts", "price"), "references") in edges
    assert (s("cmd/main.go", "main"), s("internal/stock/stock.go", "Reserve"), "calls") in edges
    assert (f("app/api.py"), s("app/service.py", "checkout"), "imports") in edges
    assert (s("app/service.py", "checkout"), "config:PAYMENT_PROVIDER", "reads") in edges
    assert (s("app/api.py", "create"), "endpoint:POST /orders", "exposes") in edges
    assert (s("app/service.py", "report"), "table:stock_reservation", "reads") in edges
    checkout = STORE.find_by_key(connection, project_id="proj_cg", kind="symbol",
                                 node_key=s("app/service.py", "checkout"))
    assert SERVICE.dependent_files(connection, project_id="proj_cg", node_id=checkout["id"]) == ["app/api.py"]


def test_an_unchanged_tree_is_not_parsed_again_and_nothing_closes(connection, tmp_path):
    root = _write(tmp_path / "shop", SHOP)
    first = index_repository(connection, STORE, project_id="proj_cg", root=root)
    again = index_repository(connection, STORE, project_id="proj_cg", root=root)
    assert first["parsed"] == 9 and again["parsed"] == 0
    assert again["closed"] == {"nodes": 0, "edges": 0}


def test_files_bosss_writes_resolve_against_the_rest_and_converge_on_a_full_index(connection, tmp_path):
    root = _write(tmp_path / "shop", SHOP)
    index_repository(connection, STORE, project_id="proj_cg", root=root)
    changed = {
        "app/service.py": textwrap.dedent("""
            from .models import Order

            def pay(order: Order):
                return order.mark_paid()
            """).lstrip(),
        "app/api.py": textwrap.dedent("""
            from app.service import pay

            def create(request):
                return pay(request.order)
            """).lstrip(),
    }
    run = index_files(connection, STORE, project_id="proj_cg",
                      files=[{"path": p, "language": "python", "source": body} for p, body in changed.items()])
    after = _edges(connection, "proj_cg")
    closed = connection.execute(text(
        "SELECT valid_to IS NOT NULL FROM kg_nodes WHERE project_id = 'proj_cg' AND node_key = :k"),
        {"k": s("app/service.py", "checkout")}).scalar_one()
    assert run["indexed"] == 2
    assert closed is True                                       # never deleted, validity closed
    assert (s("app/api.py", "create"), s("app/service.py", "pay"), "calls") in after
    assert (s("app/service.py", "pay"), s("app/models.py", "Order.mark_paid"), "calls") in after
    assert not any(dst == s("app/service.py", "checkout") for _, dst, _ in after)

    _write(root, changed)
    index_repository(connection, STORE, project_id="proj_cg2", root=root)
    assert after == _edges(connection, "proj_cg2")              # incremental == a full index


MONOREPO = {
    "package.json": '{"name": "root", "private": true, "workspaces": ["packages/*"]}',
    "packages/utils/package.json": '{"name": "@acme/utils", "main": "dist/index.js"}',
    "packages/utils/src/index.ts": 'export * from "./pg"\nexport * as Http from "./http"\n',
    "packages/utils/src/pg/index.ts": "export function parseConn(s: string) { return s }\n",
    "packages/utils/src/http/index.ts": 'export * from "./role"\n',
    "packages/utils/src/http/role.ts": "export interface RoleResponse { id: string }\n",
    "packages/framework/package.json":
        '{"name": "@acme/framework", "exports": {"./utils": "./dist/utils/index.js", '
        '"./types": {"types": "./dist/types/index.d.ts"}}}',
    "packages/framework/src/utils/index.ts": 'export * from "@acme/utils"\n',
    "packages/cli/src/create.ts": """
        import { parseConn, Http } from "@acme/framework/utils"
        export function create(s: string): Http.RoleResponse { return { id: parseConn(s) } }
        """,
    "apps/web/tsconfig.json": '{\n  // comments are allowed\n  "compilerOptions": {"paths": {"@/*": ["./src/*"],},},\n}\n',
    "apps/web/src/lib/modal.tsx": "export function useModal() { return 1 }\n",
    "apps/web/src/lib/index.ts": 'export { useModal as useDialog } from "./modal"\n',
    "apps/web/src/page.tsx": 'import { useDialog } from "@/lib"\nexport const Page = () => useDialog()\n',
}


def test_a_typescript_monorepo_resolves_workspace_packages_aliases_and_re_exports(connection, tmp_path):
    """`@acme/framework/utils` -> package exports (dist -> src) -> `export *` chains -> the definition;
    `Http.RoleResponse` through `export * as`; `@/lib` through tsconfig paths and a renamed re-export."""
    root = _write(tmp_path / "mono", MONOREPO)
    index_repository(connection, STORE, project_id="proj_cg", root=root)

    def deps(path: str, qual: str) -> set[str]:
        node = STORE.find_by_key(connection, project_id="proj_cg", kind="symbol", node_key=s(path, qual))
        assert node is not None, (path, qual)
        return set(SERVICE.dependent_files(connection, project_id="proj_cg", node_id=node["id"]))

    assert "packages/cli/src/create.ts" in deps("packages/utils/src/pg/index.ts", "parseConn")
    assert "packages/cli/src/create.ts" in deps("packages/utils/src/http/role.ts", "RoleResponse")
    assert deps("apps/web/src/lib/modal.tsx", "useModal") == {"apps/web/src/lib/index.ts", "apps/web/src/page.tsx"}


def test_a_written_typescript_file_resolves_through_the_stored_workspace(connection, tmp_path):
    """A pull request's files carry no package.json: the workspace saved by the full index is used."""
    root = _write(tmp_path / "mono", MONOREPO)
    index_repository(connection, STORE, project_id="proj_cg", root=root)
    index_files(connection, STORE, project_id="proj_cg", files=[{
        "path": "packages/cli/src/drop.ts", "language": "typescript",
        "source": 'import { parseConn } from "@acme/framework/utils"\nexport const drop = (s: string) => parseConn(s)\n'}])
    node = STORE.find_by_key(connection, project_id="proj_cg", kind="symbol",
                             node_key=s("packages/utils/src/pg/index.ts", "parseConn"))
    assert "packages/cli/src/drop.ts" in SERVICE.dependent_files(connection, project_id="proj_cg", node_id=node["id"])
