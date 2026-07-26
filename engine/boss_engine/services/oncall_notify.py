"""RB-20 — the oncall conversational surface: investigation cards where oncall already lives.

Posts a compact, actionable card to a Mattermost/Slack incoming webhook (both accept
``{"text": <markdown>}``) whenever an alert-driven investigation completes: what fired, the ranked
root-cause hypothesis + confidence, what the autonomy policy decided, and the API refs to act.
Always **best-effort** — a dead webhook must never break intake — and configured by one env
(`RELIABILITY_ONCALL_WEBHOOK`), so pointing it at seannet #monitoring is a config change.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any, Optional

_TIMEOUT = 10
_SEV_EMOJI = {"critical": "🔴", "warning": "🟠", "info": "🔵"}


def _http_post_json(url: str, body: dict[str, Any]) -> bool:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:  # noqa: S310 — operator-configured URL
        return 200 <= r.status < 300


def format_card(incident_id: str, alert: dict[str, Any], hypothesis: Optional[dict[str, Any]],
                autonomy: Optional[dict[str, Any]], remediation: Optional[dict[str, Any]]) -> str:
    sev = alert.get("severity", "warning")
    lines = [
        f"{_SEV_EMOJI.get(sev, '⚪')} **[{sev.upper()}] {alert.get('title', 'alert')}**"
        + (f" · service `{alert['service']}`" if alert.get("service") else ""),
    ]
    if alert.get("description"):
        lines.append(f"> {alert['description']}")
    if hypothesis:
        lines.append(f"**Root cause (ranked):** {hypothesis.get('root_cause', 'unknown')} "
                     f"— confidence **{hypothesis.get('confidence', '?')}** "
                     f"(basis: {hypothesis.get('basis', '?')})")
        if hypothesis.get("rationale"):
            lines.append(f"_{hypothesis['rationale']}_")
    if autonomy:
        lines.append(f"**Autonomy:** Tier-{autonomy.get('tier')} → {autonomy.get('action')} "
                     f"({autonomy.get('reason', '')})")
    if remediation:
        lines.append(f"**Remediation auto-launched:** governed code task "
                     f"`{remediation.get('code_task_run_id', '')}` → gated PR incoming")
    lines.append(f"`incident {incident_id}` · diagnose/remediate/feedback via "
                 f"`/v1/reliability/incidents/{incident_id}/…`")
    return "\n".join(lines)


def notify_incident(settings, incident_id: str, alert: dict[str, Any],
                    hypothesis: Optional[dict[str, Any]] = None,
                    autonomy: Optional[dict[str, Any]] = None,
                    remediation: Optional[dict[str, Any]] = None) -> bool:
    """Post the card; best-effort by contract (False on any failure, never raises)."""
    url = (getattr(settings, "reliability_oncall_webhook", None) or "").strip()
    if not url:
        return False
    try:
        return _http_post_json(url, {"text": format_card(incident_id, alert, hypothesis,
                                                         autonomy, remediation)})
    except Exception:
        return False
