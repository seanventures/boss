"""Operational cost-anomaly detector (spec §22.4): flags retry storms and spend spikes, opens a
review Decision/incident for each (transparency), and — when enabled — trips auto-lite-mode by
lowering the budget intensity. Runs on demand or on a schedule. Provider/infra cost spikes are
handled separately by the provider cost path; this covers the model/job operational signals.
"""
from __future__ import annotations

from sqlalchemy import text

from ..db import begin
from .budget import BudgetGovernor
from .core import create_blocking_decision, record_policy_eval

RETRY_STORM_THRESHOLD = 5     # deadlettered jobs in the window trips a retry-storm flag
SPEND_SPIKE_FACTOR = 3.0      # recent 5-min spend vs the trailing 60-min per-5-min baseline
AUTO_LITE_INTENSITY = 25      # intensity to drop to when auto-lite-mode trips


def scan(engine, settings, project_id: str, *, auto_lite: bool = True) -> dict:
    anomalies: list[dict] = []
    with begin(engine) as c:
        deadlettered = c.execute(
            text(
                "SELECT count(*) FROM jobs WHERE project_id = :p AND status = 'deadlettered' "
                "AND created_at >= now() - interval '15 minutes'"
            ),
            {"p": project_id},
        ).scalar() or 0
        if int(deadlettered) >= RETRY_STORM_THRESHOLD:
            anomalies.append({"type": "retry_storm",
                              "detail": f"{int(deadlettered)} deadlettered jobs in 15m"})

        recent = c.execute(
            text(
                "SELECT COALESCE(SUM(amount_usd_micros), 0) FROM budget_reservations "
                "WHERE project_id = :p AND created_at >= now() - interval '5 minutes'"
            ),
            {"p": project_id},
        ).scalar() or 0
        hour = c.execute(
            text(
                "SELECT COALESCE(SUM(amount_usd_micros), 0) FROM budget_reservations "
                "WHERE project_id = :p AND created_at >= now() - interval '60 minutes'"
            ),
            {"p": project_id},
        ).scalar() or 0
        baseline = int(hour) / 12.0  # per 5-min
        if baseline > 0 and int(recent) > baseline * SPEND_SPIKE_FACTOR:
            anomalies.append({"type": "spend_spike",
                              "detail": f"5m spend {int(recent)} vs ~{int(baseline)}/5m baseline"})

        for a in anomalies:
            decision_id = create_blocking_decision(
                c, project_id, None, f"Cost anomaly detected: {a['type']}",
                {"action": "review_cost_anomaly", "anomaly": a["type"]},
                {"detail": a["detail"]},
                f"Anomaly detector flagged {a['type']}: {a['detail']}.", risk_tier=2)
            a["decision_id"] = decision_id
            record_policy_eval(c, project_id, "anomaly.detect", "deny", action=a["type"],
                               reason=a["detail"], detail={"decision_id": decision_id})

        auto_lited = False
        if anomalies and auto_lite:
            BudgetGovernor(settings).set_intensity(
                c, "project", project_id, AUTO_LITE_INTENSITY, actor="anomaly_detector")
            auto_lited = True

    return {"anomalies": anomalies, "auto_lite_applied": auto_lited}
