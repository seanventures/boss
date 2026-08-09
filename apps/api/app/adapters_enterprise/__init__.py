"""Backwards-compatible alias for `adapters_postgres`.

The package was named for the edition that used it, and then community started using the
same Postgres persistence — because a control plane that cannot durably record a code task
is not a control plane. A name that says "enterprise" over adapters both editions rely on is
the kind of thing that makes a capability look paid when it is not.

Kept as a shim so external imports do not break; new code should import adapters_postgres.
"""
from ..adapters_postgres import (  # noqa: F401
    PostgresBudgetPort,
    PostgresDecisionSink,
    PostgresEntitlementPort,
    PostgresEventSink,
    PostgresPersistence,
)

__all__ = [
    "PostgresBudgetPort",
    "PostgresDecisionSink",
    "PostgresEntitlementPort",
    "PostgresEventSink",
    "PostgresPersistence",
]
