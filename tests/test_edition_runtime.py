"""Community must be able to RUN the product, not merely record it.

`build_ports` switched the entire engine boundary on edition, so a community control plane got
`InMemoryPersistence`. The result was not a missing feature — it was a control plane that
accepted work it could never perform:

    POST /v1/code-tasks       -> wrote the run to Postgres via raw SQL (console listed it)
    POST /v1/code-tasks/step  -> read through the engine port, found an empty in-memory
                                 store, returned {"status": "not_found", "done": true}

`done: true` and not an error, so the worker marked the job succeeded and the run sat at
`queued` for ever. Weeks of production runs died exactly that way, and every symptom-level
diagnosis (orphaned, starved, stalled, abandoned) was accurate and beside the point.

    PYTHONPATH=engine .venv/bin/python tests/test_edition_runtime.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, "engine")
sys.path.insert(0, ".")

from apps.api.app.bootstrap_ports import ENTERPRISE_EDITIONS, build_ports  # noqa: E402
from boss_engine.config import Settings  # noqa: E402
from boss_engine.db import get_engine  # noqa: E402

_failures: list[str] = []


def check(name: str, ok: bool, why: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        _failures.append(f"{name}{f' — {why}' if why else ''}")


def main() -> int:
    settings = Settings()
    engine = get_engine(settings)

    print("== durability is not a paid feature ==")
    for edition in ["community", "oss", "", "enterprise", "cloud"]:
        settings.boss_edition = edition
        ports = build_ports(settings, engine)
        kind = type(ports.persistence).__name__
        check(f"{edition or '(unset)'!r:24} persists to Postgres", kind == "PostgresPersistence",
              f"got {kind} — this edition can record a code task and never execute one")

    print("\n== an audit trail is not a paid feature either ==")
    settings.boss_edition = "community"
    community = build_ports(settings, engine)
    check("events are durable", type(community.events).__name__ == "PostgresEventSink")
    check("decisions are durable", type(community.decisions).__name__ == "PostgresDecisionSink")

    print("\n== governance IS the paid tier ==")
    check("community has no spend enforcement",
          type(community.budget).__name__ == "NoopBudgetPort")
    check("community has no entitlement gating",
          type(community.entitlement).__name__ == "AllowAllEntitlementPort")
    settings.boss_edition = "enterprise"
    paid = build_ports(settings, engine)
    check("enterprise enforces budget", type(paid.budget).__name__ == "PostgresBudgetPort")
    check("enterprise checks entitlements",
          type(paid.entitlement).__name__ == "PostgresEntitlementPort")
    check("every paid edition is covered",
          all(type(build_ports(_ed(settings, e), engine).budget).__name__
              == "PostgresBudgetPort" for e in ENTERPRISE_EDITIONS))

    print("\n== the library case, which in-memory was always right for ==")
    settings.boss_edition = "community"
    check("no database means in-memory",
          type(build_ports(settings, None).persistence).__name__ == "InMemoryPersistence")

    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("every edition with a database can run the product")
    return 0


def _ed(settings, edition):
    settings.boss_edition = edition
    return settings


if __name__ == "__main__":
    raise SystemExit(main())
