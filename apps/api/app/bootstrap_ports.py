from __future__ import annotations

from typing import Any

from boss_engine.ports import EnginePorts


# Editions that get the PAID governance ports on top of the shared runtime.
ENTERPRISE_EDITIONS = {
    "cloud",
    "enterprise",
    "enterprise_cloud",
    "enterprise_self_hosted",
}


def build_ports(settings: Any, engine: Any = None) -> EnginePorts:
    """Resolve the engine boundary: durable runtime for everyone, governance for the paid tier.

    This used to switch the WHOLE boundary on edition, so a community control plane ran on
    `InMemoryPersistence`. The effect was not a missing feature — it was a control plane that
    accepted work it could never perform. `POST /v1/code-tasks` wrote the run to Postgres
    through raw SQL, so the console listed it; `POST /v1/code-tasks/step` read through the
    engine port, found an empty in-memory store, and returned `{"status": "not_found",
    "done": true}`. Not an error, so the worker marked the job succeeded and moved on, and the
    run sat at `queued` for ever. Weeks of runs on the production box died exactly that way,
    and every symptom-level diagnosis — orphaned, starved, stalled, abandoned — was true and
    beside the point.

    Persistence, events and decisions are what it means to RUN the product: record a code
    task, execute it, deploy the result, keep an audit trail. None of that is a paid feature,
    and an open-source BOSS that cannot do it is not a product. They are Postgres-backed
    wherever a database exists, in every edition.

    SPEND CAPS ARE PART OF RUNNING IT TOO. Community used to get the no-op budget port here,
    while chat, meetings and connection proofs were governed by the same caps through
    BudgetGovernor directly — so builds were the ONE spend no cap could stop: their calls were
    recorded and never reserved or settled ("Builds $284.01 not capped" in Govern while the caps
    counted $8.36). Every edition with a database meters builds now (services/build_budget.py).

    What stays paid is entitlement gating ON TOP of that runtime. Community gets allow-all
    entitlements — a coherent free tier rather than a crippled one.

    With no engine at all (the OSS engine used standalone as a library, no database) the
    in-memory defaults still apply. That is the only case they were ever right for.
    """

    if engine is None:
        return EnginePorts.oss_defaults(settings)

    from .adapters_postgres import (
        PostgresBudgetPort,
        PostgresDecisionSink,
        PostgresEntitlementPort,
        PostgresEventSink,
        PostgresPersistence,
    )
    from boss_engine.defaults import AllowAllEntitlementPort

    edition = str(getattr(settings, "boss_edition", "community")).strip().lower()
    paid = edition in ENTERPRISE_EDITIONS

    return EnginePorts(
        # Every edition: the project's caps count and stop build spend like any other spend.
        budget=PostgresBudgetPort(settings),
        # Paid: entitlement checks.
        entitlement=(
            PostgresEntitlementPort(settings) if paid
            else AllowAllEntitlementPort(
                model_id=getattr(settings, "openai_model", "user-supplied-model"))
        ),
        # Shared runtime: durable in every edition that has a database.
        persistence=PostgresPersistence(engine),
        events=PostgresEventSink(),
        decisions=PostgresDecisionSink(),
    )
