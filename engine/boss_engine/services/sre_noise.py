"""AIOps noise-reduction report from persisted incidents and signals.

Computes clustered count vs raw signal count from the reliability tables.
Never invents a reduction percentage. Empty projects are honest zeros.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection


def noise_report(connection: Connection, project_id: str) -> dict[str, Any]:
    """First-class noise view over existing anomaly_signals / incidents."""
    pid = str(project_id or "").strip()
    raw = int(
        connection.execute(
            text("SELECT COUNT(*) FROM anomaly_signals WHERE project_id = :p"),
            {"p": pid},
        ).scalar()
        or 0
    )
    clustered = int(
        connection.execute(
            text("SELECT COUNT(*) FROM incidents WHERE project_id = :p"),
            {"p": pid},
        ).scalar()
        or 0
    )
    suppressed = max(0, raw - clustered)
    ratio = (suppressed / raw) if raw else None

    rows = connection.execute(
        text(
            """
            SELECT signature, kind,
                   COUNT(*) AS signal_rows,
                   COALESCE(SUM(count), 0) AS event_count
            FROM anomaly_signals
            WHERE project_id = :p
            GROUP BY signature, kind
            ORDER BY signal_rows DESC, event_count DESC, signature ASC
            LIMIT 10
            """
        ),
        {"p": pid},
    ).mappings().all()

    return {
        "project_id": pid,
        "public_ga": False,
        "raw_signal_count": raw,
        "clustered_count": clustered,
        "suppressed_duplicates": suppressed,
        "reduction": {
            "numerator": suppressed,
            "denominator": raw,
            "ratio": ratio,
        },
        "top_signatures": [
            {
                "signature": row["signature"],
                "kind": row["kind"],
                "signal_rows": int(row["signal_rows"]),
                "event_count": int(row["event_count"]),
            }
            for row in rows
        ],
        "notes": (
            "Computed from persisted anomaly_signals and incidents. "
            "Not an invented reduction."
        ),
    }
