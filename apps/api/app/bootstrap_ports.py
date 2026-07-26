from __future__ import annotations

from typing import Any

from boss_engine.ports import EnginePorts


ENTERPRISE_EDITIONS = {
    "cloud",
    "enterprise",
    "enterprise_cloud",
    "enterprise_self_hosted",
}


def build_ports(settings: Any, engine: Any = None) -> EnginePorts:
    """Resolve the engine boundary once from the configured BOSS edition."""

    edition = str(getattr(settings, "boss_edition", "community")).strip().lower()
    if edition not in ENTERPRISE_EDITIONS:
        return EnginePorts.oss_defaults(settings)

    if engine is None:
        raise ValueError("enterprise engine ports require a Postgres engine")

    from .adapters_enterprise import (
        PostgresBudgetPort,
        PostgresDecisionSink,
        PostgresEntitlementPort,
        PostgresEventSink,
        PostgresPersistence,
    )

    return EnginePorts(
        budget=PostgresBudgetPort(settings),
        entitlement=PostgresEntitlementPort(settings),
        persistence=PostgresPersistence(engine),
        events=PostgresEventSink(),
        decisions=PostgresDecisionSink(),
    )
