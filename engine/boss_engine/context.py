"""EngineContext — the BOSS engine dependency container.

This is the single object the tool layer, the engine-native agent and the MCP
server all use to reach the engine. It builds exactly what the API's `main.py`
built at import time (settings, the SQLAlchemy engine, and the service
singletons) — but with no FastAPI, no request objects, no HTTP.

    ctx = EngineContext.create()
    with ctx.begin() as conn:
        runs = ctx.list_code_task_runs(conn, project_id, limit=20)

Services that manage their own transactions (previews, discovery) are called
with `ctx.engine` / `ctx.settings`; services that take a live connection are
called with a connection from `ctx.begin()`. The context hides which is which
behind small helper methods so tools don't have to care.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from functools import cached_property
import os
from typing import Any, Iterator

from ._compat import app_module, ensure_monorepo_on_path, services_module


@dataclass
class EngineContext:
    """Wires the engine's settings, DB engine and service singletons.

    Construct with `EngineContext.create()`. Cheap to hold; the SQLAlchemy engine
    and service objects are built lazily on first use and then cached.
    """

    settings: Any
    engine: Any
    # The engine's Settings object has a development Postgres default.  Preserve
    # the difference between that default and an operator explicitly supplying a
    # durable workspace: without the latter, catalog/inspection mode must never
    # pretend that it can create governed work.
    durable_workspace_configured: bool = True

    # ---- construction -----------------------------------------------------
    @classmethod
    def create(cls, *, ensure_schema: bool = False) -> "EngineContext":
        ensure_monorepo_on_path()
        config = app_module("config")
        db = app_module("db")
        settings = config.get_settings()
        engine = db.get_engine(settings)
        if ensure_schema:
            db.ensure_schema(engine, settings)
        return cls(
            settings=settings,
            engine=engine,
            durable_workspace_configured=bool(os.environ.get("DATABASE_URL")),
        )

    # ---- connections ------------------------------------------------------
    @contextmanager
    def begin(self) -> Iterator[Any]:
        """A transactional connection (commits on exit, rolls back on error)."""
        db = app_module("db")
        with db.begin(self.engine) as conn:
            yield conn

    def database_reachable(self) -> bool:
        db = app_module("db")
        return bool(db.database_is_reachable(self.engine))

    # ---- service singletons (lazy, cached) --------------------------------
    @cached_property
    def jobs(self) -> Any:
        return services_module("jobs").JobLeaseService(self.settings)

    @cached_property
    def intake(self) -> Any:
        return services_module("intake").CommandIntakeService(self.settings)

    @cached_property
    def agent(self) -> Any:
        return services_module("agent_service").AgentService(self.settings)

    @cached_property
    def budget(self) -> Any:
        return services_module("budget").BudgetGovernor(self.settings)

    @cached_property
    def receipts(self) -> Any:
        return services_module("receipts").ReceiptService()

    @cached_property
    def canonical(self) -> Any:
        return services_module("canonical").CanonicalObjectService()

    @cached_property
    def members(self) -> Any:
        # MemberDirectoryService location varies; import defensively.
        try:
            return services_module("members").MemberDirectoryService(self.settings)
        except Exception:  # pragma: no cover - optional in some editions
            return None

    # ---- convenience passthroughs used by tools ---------------------------
    def list_code_task_runs(self, conn: Any, project_id: str, limit: int = 50) -> list[dict]:
        return services_module("code_task_service").list_code_task_runs(conn, project_id, limit)

    def list_discovery_runs(self, conn: Any, project_id: str, limit: int = 50) -> list[dict]:
        return services_module("discovery_service").list_discovery_runs(conn, project_id, limit)

    def list_previews(self, project_id: str, limit: int = 50) -> list[dict]:
        return services_module("preview_service").list_previews(
            self.engine, project_id, limit, self.settings
        )
