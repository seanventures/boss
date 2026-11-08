"""Telling people: contact points, the routing tree, the outbox (BOSSAPP-598, Phase 1a).

THE TREE IS ALERTMANAGER'S
--------------------------
A project's notification policies form a tree: depth first, the first matching child wins
unless it says `continue`, and a child inherits every timing and contact point it does not set.
The root (made on first use) matches everything: the console, plus email to the project's owner,
grouped by alertname and service, 30 s / 5 min / 4 h. Most people never see the tree: the rule
sheet's Notify column writes a one-level child for that rule. `route` is THE function: the
router and the console's preview (`preview`) both call it, so they cannot disagree.

NOTIFICATION NEVER WAITS FOR TRIAGE
-----------------------------------
`plan` runs on the monitor's tick: a firing alert is notified after its group_wait whether or
not the agent has answered; the agent's triage is appended as a follow-up when it lands. Once
alerts are in an incident, people get ONE message thread per incident (`group_key` is the
incident). Ack stops repeats. A flapping alert is told once as flapping, and repeats are held
until it settles. Low urgency respects quiet hours; high ignores them. A contact point is
verified (a test message acknowledged) before high urgency may use it.

THE OUTBOX
----------
Every message is a `notifications` row before it is sent (idempotent `dedupe_key`), then a
receipt. Six attempts with backoff, a per-contact-point rate, and a dead channel never blocks
another channel or the evaluator. Secrets (a webhook URL, a signing token) live in the secret
store by reference; they are never returned or logged.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Mapping, Optional

from ..ports import sql
from . import monitoring as mon
from .core import make_id

log = logging.getLogger(__name__)

CONTACT_KINDS = ("console", "email", "slack_webhook", "mattermost", "webhook")
DEFAULTS = {"group_by": ["alertname", "service"], "group_wait_s": 30, "group_interval_s": 300,
            "repeat_interval_s": 14400}
MAX_ATTEMPTS = 6


class NotConfigured(RuntimeError):
    """Nothing is set up to send this message: a setup gap, not a send that failed."""


#: Failures that mean NOTHING IS CONFIGURED to send with (the host may not send email, a
#: contact point's secret is missing, the contact point was removed). They are a setup prompt
#: ("Set up a contact point" -> Notify), never a page: BossOutboxBacklog fired SEV1 for 4.5 hours
#: on a host that had never been allowed to send email.
UNCONFIGURED_ERRORS = ("NotConfigured", "IntegrationConfigurationError")
UNCONFIGURED_SQL = ("split_part(COALESCE(error_word, ''), ':', 1) IN ("
                    + ", ".join(f"'{e}'" for e in UNCONFIGURED_ERRORS) + ")")


def not_configured(error_word: Optional[str]) -> bool:
    return str(error_word or "").split(":", 1)[0] in UNCONFIGURED_ERRORS
TRIAGE_WORDS = {None: "Not triaged yet: the agent's triage follows when it lands",
                "no_model": "Not triaged: no model is answering (this message did not wait)"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(v: Any, default: Any = None) -> Any:
    return mon._json(v, default)


# --------------------------------------------------------------------------- #
# Contact points
# --------------------------------------------------------------------------- #
def owner_email(connection, project_id: str) -> Optional[str]:
    point = connection.begin_nested()
    try:
        got = connection.execute(sql(
            "SELECT u.email FROM identity_role_bindings b JOIN identity_users u "
            " ON u.principal_id = b.subject_id JOIN projects p ON p.id = :p "
            " JOIN workspaces w ON w.id = p.workspace_id WHERE b.subject_type = 'principal' "
            " AND b.role_id = 'arl_owner' AND ((b.scope_type = 'org' AND b.scope_id = w.org_id) "
            " OR (b.scope_type = 'project' AND b.scope_id = :p)) ORDER BY b.granted_at LIMIT 1"),
            {"p": project_id}).scalar()
        point.commit()
        return str(got) if got else None
    except Exception:  # noqa: BLE001 — no identity tables (an old install): no owner email
        point.rollback()
        return None


def _cp_row(connection, project_id: str, cp_id: str) -> Optional[dict[str, Any]]:
    row = connection.execute(sql(
        "SELECT * FROM contact_points WHERE id = :i AND project_id = :p"),
        {"i": cp_id, "p": project_id}).mappings().first()
    if not row:
        return None
    out = dict(row)
    out["config"] = _json(out.get("config"), {}) or {}
    return out


def contact_point_view(cp: Mapping[str, Any]) -> dict[str, Any]:
    """What may be shown: never the secret, never any part of it."""
    return {"id": cp["id"], "name": cp["name"], "kind": cp["kind"],
            "shows": cp["config"].get("address") or cp["config"].get("host") or "",
            "verified": bool(cp.get("verified_at")), "verified_at": mon.iso(cp.get("verified_at")),
            "has_secret": bool(cp.get("secret_ref")), "rate_per_minute": cp["rate_per_minute"],
            "created_at": mon.iso(cp.get("created_at"))}


def ensure_console(connection, project_id: str) -> str:
    cid = connection.execute(sql(
        "SELECT id FROM contact_points WHERE project_id = :p AND kind = 'console' "
        "ORDER BY created_at LIMIT 1"), {"p": project_id}).scalar()
    if cid:
        return str(cid)
    cid = make_id("cp")
    connection.execute(sql(
        "INSERT INTO contact_points (id, project_id, name, kind, verified_at, created_by) "
        "VALUES (:i, :p, 'Console', 'console', now(), 'boss')"), {"i": cid, "p": project_id})
    return cid


def ensure_root(connection, project_id: str) -> dict[str, Any]:
    """The project's root policy: console + email to its owner (when one is on record)."""
    row = connection.execute(sql(
        "SELECT * FROM notification_policies WHERE project_id = :p AND parent_id IS NULL"),
        {"p": project_id}).mappings().first()
    if row:
        return dict(row)
    cps = [ensure_console(connection, project_id)]
    email = owner_email(connection, project_id)
    if email:
        cid = make_id("cp")
        connection.execute(sql(
            "INSERT INTO contact_points (id, project_id, name, kind, config, verified_at, "
            " created_by) VALUES (:i, :p, :n, 'email', CAST(:c AS JSONB), now(), 'boss')"),
            {"i": cid, "p": project_id, "n": "Project owner",
             "c": json.dumps({"address": email})})
        cps.append(cid)
    pid_ = make_id("np")
    connection.execute(sql(
        "INSERT INTO notification_policies (id, project_id, parent_id, name, matchers, group_by, "
        " group_wait_s, group_interval_s, repeat_interval_s, contact_point_ids) VALUES (:i, :p, "
        " NULL, 'Everything', '[]'::jsonb, CAST(:g AS JSONB), 30, 300, 14400, CAST(:c AS JSONB)) "
        "ON CONFLICT DO NOTHING"),
        {"i": pid_, "p": project_id, "g": json.dumps(DEFAULTS["group_by"]),
         "c": json.dumps(cps)})
    return dict(connection.execute(sql(
        "SELECT * FROM notification_policies WHERE project_id = :p AND parent_id IS NULL"),
        {"p": project_id}).mappings().first())


def _store_secret(connection, settings: Any, project_id: str, cp_id: str, value: str,
                  actor: str) -> str:
    from .secrets_service import put_secret

    from .secret_backends import durable_secret_backend

    got = put_secret(connection, settings, project_id=project_id, name=f"contact_point_{cp_id}",
                     value=value, secret_kind="api_token", scope_type="contact_point",
                     scope_id=cp_id, actor=actor, backend=durable_secret_backend(settings))
    connection.execute(sql("UPDATE secret_refs SET metadata = metadata - 'last4' WHERE id = :i"),
                       {"i": got["secret_ref_id"]})
    return str(got["secret_ref_id"])


def _secret(connection, settings: Any, cp: Mapping[str, Any]) -> Optional[str]:
    if not cp.get("secret_ref"):
        return None
    from .secret_backends import resolve_secret_ref

    return resolve_secret_ref(connection, cp["secret_ref"], settings, cp["project_id"])


def create_contact_point(connection, settings: Any, project_id: str, *, kind: str, name: str,
                         address: Optional[str] = None, url: Optional[str] = None,
                         actor: str) -> dict[str, Any]:
    """A contact point. A webhook gets a signing token, shown ONCE in the return value."""
    from urllib.parse import urlsplit

    if kind not in CONTACT_KINDS:
        raise ValueError(f"kind is one of {', '.join(CONTACT_KINDS)}")
    if kind == "console":
        return {"contact_point": contact_point_view(_cp_row(
            connection, project_id, ensure_console(connection, project_id)))}
    cid = make_id("cp")
    config: dict[str, Any] = {}
    secret_value = None
    token = None
    if kind == "email":
        from email.utils import parseaddr

        _n, addr = parseaddr(address or "")
        if "@" not in addr:
            raise ValueError("Give an email address")
        config["address"] = addr
    else:
        parts = urlsplit(url or "")
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError("Give the webhook's full URL (https://...)")
        config["host"] = parts.hostname
        secret_value = url
        if kind == "webhook":
            token = "bwh_" + secrets.token_urlsafe(32)
            secret_value = json.dumps({"url": url, "token": token})
    connection.execute(sql(
        "INSERT INTO contact_points (id, project_id, name, kind, config, created_by) VALUES "
        " (:i, :p, :n, :k, CAST(:c AS JSONB), :by)"),
        {"i": cid, "p": project_id, "n": (name or kind)[:120], "k": kind,
         "c": json.dumps(config), "by": actor})
    if secret_value:
        ref = _store_secret(connection, settings, project_id, cid, secret_value, actor)
        connection.execute(sql("UPDATE contact_points SET secret_ref = :r WHERE id = :i"),
                           {"r": ref, "i": cid})
    mon.audit(connection, project_id, "contact_point", cid, "created",
              after={"kind": kind, "name": name, **config}, actor=actor)
    out = {"contact_point": contact_point_view(_cp_row(connection, project_id, cid))}
    if token:
        out["signing_token"] = token
    return out


def send_test(connection, project_id: str, cp_id: str, *, actor: str,
              now: Optional[datetime] = None) -> dict[str, Any]:
    """A test message carrying a one-time code; entering it verifies the contact point."""
    now = now or _now()
    cp = _cp_row(connection, project_id, cp_id)
    if not cp:
        raise LookupError("No such contact point")
    code = f"{secrets.randbelow(10 ** 6):06d}"
    connection.execute(sql("UPDATE contact_points SET verify_token_hash = :h WHERE id = :i"),
                       {"h": mon.token_hash(code), "i": cp_id})
    _enqueue(connection, project_id, cp_id=cp_id, group_key=f"test:{cp_id}", kind="test",
             urgency="high", alert_ids=[], incident_id=None,
             subject="BOSS test message",
             body=f"This is a test from BOSS. To verify this contact point, enter the code "
                  f"{code} in Observability › Notify.",
             dedupe=f"test:{cp_id}:{now.isoformat()}", now=now)
    return {"sent": True, "word": "Test sent; enter the code it carries to verify"}


def verify(connection, project_id: str, cp_id: str, code: str, *, actor: str) -> dict[str, Any]:
    cp = _cp_row(connection, project_id, cp_id)
    if not cp:
        raise LookupError("No such contact point")
    if not cp.get("verify_token_hash") or mon.token_hash((code or "").strip()) != \
            cp["verify_token_hash"]:
        raise ValueError("That is not the code in the test message")
    connection.execute(sql("UPDATE contact_points SET verified_at = now(), verify_token_hash = "
                           "NULL WHERE id = :i"), {"i": cp_id})
    mon.audit(connection, project_id, "contact_point", cp_id, "verified", actor=actor)
    return {"verified": True}


# --------------------------------------------------------------------------- #
# The tree, and THE routing function (the preview calls it too)
# --------------------------------------------------------------------------- #
def policies(connection, project_id: str) -> list[dict[str, Any]]:
    ensure_root(connection, project_id)
    out = []
    for r in connection.execute(sql(
            "SELECT * FROM notification_policies WHERE project_id = :p "
            "ORDER BY parent_id NULLS FIRST, position, created_at"), {"p": project_id}).mappings():
        d = dict(r)
        for k in ("matchers", "group_by", "contact_point_ids", "quiet_hours"):
            d[k] = _json(d.get(k), None)
        out.append(d)
    return out


def _inherit(parent: dict, child: dict) -> dict:
    eff = dict(parent)
    for k in ("group_by", "group_wait_s", "group_interval_s", "repeat_interval_s",
              "contact_point_ids", "quiet_hours"):
        if child.get(k) is not None:
            eff[k] = child[k]
    eff["policy_id"], eff["name"] = child["id"], child.get("name") or ""
    eff["path"] = parent.get("path", []) + [child.get("name") or child["id"]]
    return eff


def route(tree: list[dict[str, Any]], labels: Mapping[str, str]) -> list[dict[str, Any]]:
    """Every policy this alert ends at (Alertmanager's: depth first, first match unless
    `continue`, inheriting). Never empty: the root matches everything."""
    root = next(p for p in tree if p["parent_id"] is None)
    kids: dict[str, list[dict]] = {}
    for p in tree:
        if p["parent_id"]:
            kids.setdefault(p["parent_id"], []).append(p)
    base = {**DEFAULTS, "contact_point_ids": [], "quiet_hours": None}
    eff_root = _inherit({**base, "path": []}, root)

    def walk(node: dict, eff: dict) -> list[dict]:
        out: list[dict] = []
        for child in kids.get(node["id"], []):
            if not mon.matches(labels, child.get("matchers") or []):
                continue
            out += walk(child, _inherit(eff, child))
            if not child.get("continue_matching"):
                break
        return out or [eff]
    return walk(root, eff_root)


def quiet_now(quiet: Optional[Mapping[str, Any]], now: datetime) -> Optional[datetime]:
    """When quiet hours end, if `now` is inside them."""
    from zoneinfo import ZoneInfo

    if not quiet or not quiet.get("start") or not quiet.get("end"):
        return None
    try:
        tz = ZoneInfo(str(quiet.get("timezone") or "UTC"))
        start = dtime.fromisoformat(str(quiet["start"]))
        end = dtime.fromisoformat(str(quiet["end"]))
    except Exception:  # noqa: BLE001 — a malformed window is no window
        return None
    local = now.astimezone(tz)
    t = local.timetz().replace(tzinfo=None)
    inside = (start <= t < end) if start < end else (t >= start or t < end)
    if not inside:
        return None
    end_day = local.date() if t < end else local.date() + timedelta(days=1)
    return datetime.combine(end_day, end, tzinfo=tz).astimezone(timezone.utc)


def preview(connection, project_id: str, labels: Mapping[str, str],
            now: Optional[datetime] = None) -> dict[str, Any]:
    """Where an alert with these labels would go, with what timing (the same `route`)."""
    now = now or _now()
    tree = policies(connection, project_id)
    labels = {str(k): str(v) for k, v in labels.items()}
    labels.setdefault("severity", "warning")
    from . import severity as sev

    labels.setdefault("sev", sev.level(labels))
    urgency = labels.get("urgency") or sev.urgency(labels)
    out = []
    for eff in route(tree, labels):
        cps = []
        for cid in eff.get("contact_point_ids") or []:
            cp = _cp_row(connection, project_id, cid)
            if not cp:
                continue
            skipped = urgency == "high" and not cp.get("verified_at")
            cps.append({**contact_point_view(cp),
                        "skipped": "Not verified: high urgency skips it" if skipped else None})
        quiet = quiet_now(eff.get("quiet_hours"), now) if urgency == "low" else None
        out.append({"policy_id": eff["policy_id"], "path": eff["path"],
                    "group_by": eff["group_by"], "group_wait_s": eff["group_wait_s"],
                    "group_interval_s": eff["group_interval_s"],
                    "repeat_interval_s": eff["repeat_interval_s"], "contact_points": cps,
                    "quiet_until": mon.iso(quiet)})
    words = "; ".join(f"{' › '.join(r['path'])}: " + (", ".join(
        c["name"] for c in r["contact_points"] if not c["skipped"]) or "nobody")
        + f" after {r['group_wait_s']} s" for r in out)
    return {"labels": labels, "urgency": urgency, "routes": out, "word": words}


def set_rule_notify(connection, project_id: str, rule_id: str, contact_point_ids: list[str], *,
                    actor: str, quiet_hours: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """The rule sheet's Notify column: a one-level child policy {alert_rule_id="<id>"}.
    `quiet_hours` ({start, end, timezone}) holds the rule's low-urgency messages overnight
    (Watch this app's "Wait till 08:00"); None inherits the root's."""
    root = ensure_root(connection, project_id)
    for cid in contact_point_ids:
        if not _cp_row(connection, project_id, cid):
            raise ValueError("No such contact point in this project")
    connection.execute(sql("DELETE FROM notification_policies WHERE project_id = :p "
                           "AND rule_id = :r"), {"p": project_id, "r": rule_id})
    pid_ = None
    if contact_point_ids:
        pid_ = make_id("np")
        connection.execute(sql(
            "INSERT INTO notification_policies (id, project_id, parent_id, position, name, "
            " matchers, contact_point_ids, rule_id, created_by, quiet_hours) VALUES (:i, :p, "
            " :root, -1, :n, CAST(:m AS JSONB), CAST(:c AS JSONB), :r, :by, CAST(:q AS JSONB))"),
            {"i": pid_, "p": project_id, "root": root["id"], "n": f"Rule {rule_id}",
             "m": json.dumps([{"name": "alert_rule_id", "op": "=", "value": rule_id}]),
             "c": json.dumps(contact_point_ids), "r": rule_id, "by": actor,
             "q": json.dumps(dict(quiet_hours)) if quiet_hours else None})
    connection.execute(sql("UPDATE app_alert_rules SET notify = :n WHERE id = :r"),
                       {"n": ",".join(contact_point_ids) or "console", "r": rule_id})
    mon.audit(connection, project_id, "notification_policy", pid_ or rule_id, "rule_notify",
              after={"rule_id": rule_id, "contact_point_ids": contact_point_ids,
                     **({"quiet_hours": dict(quiet_hours)} if quiet_hours else {})}, actor=actor)
    return {"policy_id": pid_}


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #
def _base_url(settings: Any) -> str:
    return str(getattr(settings, "public_url", "") or getattr(settings, "app_base_url", "")
               or "").rstrip("/")


def _triage_line(a: Mapping[str, Any]) -> str:
    t = _json(a.get("triage"), {}) or {}
    st = a.get("triage_state")
    if st == "no_model":
        from .app_alerts import NO_MODEL_WORD, no_model_display

        word = no_model_display(t)
        if word == NO_MODEL_WORD:
            return TRIAGE_WORDS["no_model"]
        if "this message did not wait" in word:
            return word
        return f"{word} (this message did not wait)"
    if st != "done" or not t:
        return TRIAGE_WORDS[None]
    line = f"Agent's triage: {t.get('word') or t.get('outcome')}. {t.get('reason') or ''}".strip()
    if t.get("outcome") == "asked":
        # THE QUESTION AND ITS ANSWERS, so the person can answer from the message's link.
        opts = " · ".join(o.get("label") or o.get("action") for o in t.get("options") or [])
        line += (f"\n  The agent asks: {t.get('question') or 'what should BOSS do?'}"
                 + (f"\n  Answer in BOSS: {opts}" if opts else ""))
    if t.get("answered"):
        line += f"\n  Answered by {t['answered'].get('name') or 'a person'}."
    return line


def _alert_lines(a: Mapping[str, Any], names: Mapping[str, str]) -> list[str]:
    ann = _json(a.get("annotations"), {}) or {}
    value = a.get("value")
    from . import severity as sev

    out = [f"• {sev.label(_json(a.get('labels'), {}) or {})} "
           f"{names.get(a.get('subject_id') or '', '')}: {ann.get('summary') or a['alertname']}"
           + (" (flapping)" if a.get("flapping") else "")]
    if a.get("expr"):
        out.append(f"  Query: {a['expr']}")
    if value is not None:
        out.append(f"  Value at breach: {round(float(value), 3)}"
                   + (f" {a.get('unit')}" if a.get("unit") else "")
                   + (f" (threshold {a.get('threshold')})" if a.get("threshold") is not None
                      else ""))
    runbook = ann.get("runbook_url")
    if runbook and not str(runbook).startswith("boss:"):
        out.append(f"  Runbook: {runbook}")
    for link in (_json(a.get("links"), []) or [])[:3]:
        out.append(f"  {link.get('text') or 'Link'}: {link.get('href')}")
    out.append("  " + _triage_line(a))
    return out


def _render(kind: str, alerts: list[dict], names: Mapping[str, str], incident: Optional[dict],
            *, ack_url: Optional[str], console_url: Optional[str]) -> tuple[str, str]:
    firing = [a for a in alerts if a["state"] == "firing"]
    head = (incident or {}).get("title") or (
        f"{names.get(alerts[0].get('subject_id') or '', '')}: "
        + ((_json(alerts[0].get("annotations"), {}) or {}).get("summary")
           or alerts[0]["alertname"])) if alerts else "BOSS"
    tag = {"firing": "FIRING", "resolved": "RESOLVED", "flapping": "FLAPPING",
           "triage": "UPDATE", "meta": "BOSS SELF-CHECK"}.get(kind, kind.upper())
    from . import severity as sev

    # The worst level leads the subject: "[SEV1 FIRING:2] …" (meta self-checks carry none).
    worst = sev.worst([_json(a.get("labels"), {}) or {} for a in alerts]) if alerts else None
    level = f"{sev.label(worst)} " if worst and kind != "meta" else ""
    subject = f"[{level}{tag}{':' + str(len(firing)) if kind == 'firing' and len(firing) > 1 else ''}] " \
              f"{head}"[:200]
    lines = []
    if kind == "resolved":
        lines.append("Resolved: every alert in it has cleared.")
    elif kind == "flapping":
        lines.append("Flapping: it fires and clears again and again. You are told once; repeats "
                     "are held until it settles. The agent proposes a tuning.")
    elif kind == "triage":
        lines.append("The agent's triage landed:")
    for a in alerts[:10]:
        lines += _alert_lines(a, names)
    if len(alerts) > 10:
        lines.append(f"…and {len(alerts) - 10} more")
    if ack_url and kind in ("firing", "flapping"):
        lines.append(f"\nAcknowledge (stops repeats, does not silence): {ack_url}")
    if console_url:
        lines.append(f"Open in BOSS: {console_url}")
    return subject, "\n".join(lines)


def _enqueue(connection, project_id: str, *, cp_id: str, group_key: str, kind: str, urgency: str,
             alert_ids: list[str], incident_id: Optional[str], subject: str, body: str,
             dedupe: str, now: datetime, policy_id: Optional[str] = None,
             hold_until: Optional[datetime] = None, payload: Optional[dict] = None) -> bool:
    got = connection.execute(sql(
        "INSERT INTO notifications (id, project_id, group_key, policy_id, contact_point_id, "
        " alert_ids, incident_id, kind, urgency, dedupe_key, subject, body, payload, status, "
        " next_attempt_at, created_at) VALUES (:i, :p, :g, :pol, :cp, CAST(:a AS JSONB), :inc, "
        " :k, :u, :d, :s, :b, CAST(:pl AS JSONB), :st, :next, :now) "
        "ON CONFLICT (dedupe_key) DO NOTHING RETURNING id"),
        {"i": make_id("ntf"), "p": project_id, "g": group_key, "pol": policy_id, "cp": cp_id,
         "a": json.dumps(alert_ids), "inc": incident_id, "k": kind, "u": urgency,
         "d": hashlib.sha256(dedupe.encode()).hexdigest()[:40], "s": subject, "b": body,
         "pl": json.dumps(payload or {}, default=str), "st": "held" if hold_until else "pending",
         "next": hold_until or now, "now": now}).first()
    return got is not None


# --------------------------------------------------------------------------- #
# Planning: which groups are due a message (the monitor's tick)
# --------------------------------------------------------------------------- #
def _names(connection, project_id: str) -> dict[str, str]:
    out = {r[0]: r[1] for r in connection.execute(sql(
        "SELECT id, app_name || ' (' || environment || ')' FROM deployment_targets "
        "WHERE project_id = :p"), {"p": project_id}).all()}
    out.update({r[0]: r[1] for r in connection.execute(sql(
        "SELECT id, name || ' (' || environment || ')' FROM monitored_services "
        "WHERE project_id = :p AND deployment_target_id IS NULL"), {"p": project_id}).all()})
    return out


#: Triage outcomes that end a hold: the agent decided it is real, or asked a person.
RELEASES_HOLD = ("page", "asked", "opened", "joined", "split")


def _held(a: Mapping[str, Any], now: datetime) -> Optional[datetime]:
    """A hold (deploy window, maintenance `hold`) delays the message until the agent says page
    or the hold ends; mute never reaches here (silenced alerts are skipped)."""
    triage = _json(a.get("triage"), {}) or {}
    # The hold waits for the agent's verdict, never for a person: once the agent has said it is
    # real (page, opened, joined, split) or ASKED a person (2026-10-19: release 1.2's SEV1 sat
    # "Held for the agent" while the agent's question waited, and nobody was told), the hold
    # ends. Only noise or the agent's own "hold and look again" keeps it.
    if triage.get("outcome") in RELEASES_HOLD:
        return None
    holds = [x for x in (_json(a.get("suppressed_by"), []) or []) if x.get("mode") == "hold"]
    if not holds:
        return None
    ends = []
    for x in holds:
        try:
            ends.append(datetime.fromisoformat(str(x.get("until"))))
        except ValueError:
            continue
    end = max(ends) if ends else None
    return end if end and end > now else None


def plan(connection, settings: Any, project_id: str, now: Optional[datetime] = None
         ) -> dict[str, Any]:
    """Enqueue every message due in the project. Never waits for triage."""
    now = now or _now()
    tree = policies(connection, project_id)
    names = _names(connection, project_id)
    rows = [dict(r) for r in connection.execute(sql(
        "SELECT * FROM app_alerts WHERE project_id = :p AND inhibited_by IS NULL "
        "AND silenced_by IS NULL AND source_kind <> 'signal' AND ("
        " state = 'firing' OR (state = 'resolved' AND notified_at IS NOT NULL "
        "  AND cleared_at > :since))"),
        {"p": project_id, "since": now - timedelta(days=1)}).mappings().all()]
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for a in rows:
        triage = _json(a.get("triage"), {}) or {}
        if a["state"] == "firing" and triage.get("outcome") in ("noise",) and not a.get(
                "notified_at"):
            continue            # the agent said noise before anyone was told
        if a["state"] == "firing" and _held(a, now):
            continue
        lab = mon.alert_labels(a, {"app_name": names.get(a.get("subject_id") or "", ""),
                                   "environment": ""})
        meta = lab.get("boss_meta") == "true"
        for eff in ([_meta_route(connection, project_id)] if meta else route(tree, lab)):
            gk = (f"inc:{a['incident_id']}" if a.get("incident_id") else
                  f"{eff['policy_id']}:" + ",".join(lab.get(k, "") for k in eff["group_by"]))
            for cid in eff.get("contact_point_ids") or []:
                g = groups.setdefault((cid, gk), {"eff": eff, "alerts": [], "cp": cid})
                g["alerts"].append(a)
    sent = 0
    for (cid, gk), g in groups.items():
        cp = _cp_row(connection, project_id, cid)
        if not cp:
            continue
        sent += _plan_group(connection, settings, project_id, cp, gk, g, names, now)
    return {"groups": len(groups), "enqueued": sent}


def _meta_route(connection, project_id: str) -> dict[str, Any]:
    """Meta-alerts go to the console AND the owner's email, whatever the tree says."""
    root = ensure_root(connection, project_id)
    cps = [ensure_console(connection, project_id)]
    email = connection.execute(sql(
        "SELECT id FROM contact_points WHERE project_id = :p AND kind = 'email' "
        "ORDER BY created_at LIMIT 1"), {"p": project_id}).scalar()
    if email:
        cps.append(email)
    return {**DEFAULTS, "policy_id": root["id"], "path": ["BOSS self-check"],
            "contact_point_ids": cps, "quiet_hours": None, "group_wait_s": 0}


def _plan_group(connection, settings, project_id: str, cp: dict, gk: str, g: dict,
                names: dict, now: datetime) -> int:
    eff, alerts = g["eff"], g["alerts"]
    urgency = "high" if any((a.get("urgency") or "high") == "high" for a in alerts) else "low"
    if urgency == "high" and not cp.get("verified_at"):
        return 0            # an unverified point is never trusted with high urgency
    ng = connection.execute(sql(
        "SELECT * FROM notification_groups WHERE contact_point_id = :c AND group_key = :g"),
        {"c": cp["id"], "g": gk}).mappings().first()
    if ng is None:
        connection.execute(sql(
            "INSERT INTO notification_groups (id, project_id, policy_id, contact_point_id, "
            " group_key, incident_id, first_seen_at) VALUES (:i, :p, :pol, :c, :g, :inc, :now) "
            "ON CONFLICT DO NOTHING"),
            {"i": make_id("ngr"), "p": project_id, "pol": eff["policy_id"], "c": cp["id"],
             "g": gk, "inc": alerts[0].get("incident_id"),
             # group_wait counts from when the first alert FIRED, not from when the planner
             # first looked (a slow tick must not delay the first message further)
             "now": min([a.get("fired_at") or now for a in alerts] + [now])})
        ng = connection.execute(sql(
            "SELECT * FROM notification_groups WHERE contact_point_id = :c AND group_key = :g"),
            {"c": cp["id"], "g": gk}).mappings().first()
    ng = dict(ng)
    firing = [a for a in alerts if a["state"] == "firing"]
    steady = [a for a in firing if not a.get("flapping")]
    flapping = [a for a in firing if a.get("flapping")]
    digest = ",".join(sorted(a["id"] for a in steady)) or ("flapping" if flapping else "")
    incident = None
    inc_id = alerts[0].get("incident_id")
    if inc_id:
        incident = dict(connection.execute(sql(
            "SELECT id, title, acked_at FROM incidents WHERE id = :i"),
            {"i": inc_id}).mappings().first() or {}) or None
    all_acked = firing and all(mon.acked_now(a, now) for a in firing) or bool(
        incident and incident.get("acked_at"))
    kind = None
    last = ng.get("last_sent_at")
    waited = (now - ng["first_seen_at"]).total_seconds() >= int(eff["group_wait_s"])
    if firing:
        if last is None and waited and steady:
            kind = "firing"
        elif last is not None and digest != (ng.get("last_digest") or "") and steady and (
                now - last).total_seconds() >= int(eff["group_interval_s"]):
            kind = "firing"
        elif last is not None and steady and not all_acked and (
                now - last).total_seconds() >= int(eff["repeat_interval_s"]):
            kind = "firing"
    elif last is not None and ng.get("last_digest") not in ("", "resolved", None):
        kind = "resolved"
    out = 0
    # flapping: told ONCE per alert (after the group's wait), never repeated while it flaps
    for a in flapping if waited else []:
        if _send(connection, settings, project_id, cp, gk, "flapping", [a], names,
                 incident, eff, urgency, now, dedupe=f"flap:{cp['id']}:{a['id']}"):
            out += 1
            connection.execute(sql(
                "UPDATE notification_groups SET last_sent_at = COALESCE(last_sent_at, :now), "
                " last_digest = COALESCE(last_digest, 'flapping') WHERE id = :i"),
                {"now": now, "i": ng["id"]})
    if kind:
        bucket = "" if kind != "firing" else (now.isoformat() if last is not None
                                              and digest == (ng.get("last_digest") or "")
                                              else digest)
        chosen = steady if kind == "firing" else alerts
        if _send(connection, settings, project_id, cp, gk, kind, chosen or alerts, names,
                 incident, eff, urgency, now, dedupe=f"{cp['id']}:{gk}:{kind}:{bucket}"):
            out += 1
            connection.execute(sql(
                "UPDATE notification_groups SET last_sent_at = :now, last_digest = :d "
                "WHERE id = :i"),
                {"now": now, "d": "resolved" if kind == "resolved" else digest, "i": ng["id"]})
    # the agent's triage, appended once per alert when it lands (never awaited)
    if last is not None or kind:
        for a in firing:
            t = _json(a.get("triage"), {}) or {}
            if a.get("triage_state") == "done" and t.get("outcome") not in (None, "storm") \
                    and a.get("notified_at"):
                out += _send(connection, settings, project_id, cp, gk, "triage", [a], names,
                             incident, eff, urgency, now,
                             dedupe=f"triage:{cp['id']}:{a['id']}:{t.get('at')}")
    return out


def _send(connection, settings, project_id, cp, gk, kind, alerts, names, incident, eff,
          urgency, now, *, dedupe: str) -> int:
    base = _base_url(settings)
    ack_url = None
    if cp["kind"] == "email" and kind in ("firing", "flapping") and base:
        token = mon.mint_action_token(connection, project_id, "ack",
                                      alert_id=None if incident else alerts[0]["id"],
                                      incident_id=(incident or {}).get("id"),
                                      contact_point_id=cp["id"], now=now)
        ack_url = f"{base}/v1/alert-actions/{token}"
    console_url = None
    if base:
        from ..app_links.console import build_console_link

        console_url = build_console_link("observability", project_id=project_id, base=base,
                                          view="alerts")
    subject, body = _render(kind, alerts, names, incident, ack_url=ack_url,
                            console_url=console_url)
    hold = quiet_now(eff.get("quiet_hours"), now) if urgency == "low" else None
    ok = _enqueue(connection, project_id, cp_id=cp["id"], group_key=gk, kind=kind,
                  urgency=urgency, alert_ids=[a["id"] for a in alerts],
                  incident_id=(incident or {}).get("id"), subject=subject, body=body,
                  dedupe=dedupe, now=now, policy_id=eff["policy_id"], hold_until=hold,
                  payload={"alerts": [{"id": a["id"], "alertname": a["alertname"],
                                       "state": a["state"], "value": a.get("value"),
                                       "labels": _json(a.get("labels"), {}),
                                       "expr": a.get("expr")} for a in alerts],
                           "incident": incident, "kind": kind})
    if ok:
        connection.execute(sql(
            "UPDATE app_alerts SET notified_at = COALESCE(notified_at, :now) WHERE id = ANY(:i)"),
            {"now": now, "i": [a["id"] for a in alerts]})
    return 1 if ok else 0


# --------------------------------------------------------------------------- #
# Delivery (outbox -> channel -> receipt)
# --------------------------------------------------------------------------- #
def _post_json(url: str, payload: dict, headers: Optional[dict] = None) -> str:
    from ..identity.http import fetch

    body = json.dumps(payload).encode()
    resp = fetch("POST", url, headers={"Content-Type": "application/json", **(headers or {})},
                 data=body, purpose="monitoring.notify")
    if resp.status >= 300:
        raise RuntimeError(f"The channel answered {resp.status}")
    return f"HTTP {resp.status}"


def deliver(connection, settings: Any, n: Mapping[str, Any]) -> str:
    """Send one message. Returns the receipt; raises with the reason in words."""
    cp = _cp_row(connection, n["project_id"], n["contact_point_id"])
    if not cp:
        raise NotConfigured("The contact point was removed")
    text = f"*{n['subject']}*\n{n['body']}"
    if cp["kind"] == "console":
        return "shown in the console"
    if cp["kind"] == "email":
        from .mail_transport import send

        return send(cp["config"]["address"], n["subject"], n["body"], connection=connection,
                    settings=settings, project_id=n["project_id"])
    secret = _secret(connection, settings, cp)
    if not secret:
        raise NotConfigured("The contact point's webhook is not in the secret store")
    if cp["kind"] in ("slack_webhook", "mattermost"):
        return _post_json(secret, {"text": text})
    spec = json.loads(secret)
    ts = str(int(_now().timestamp()))
    payload = {"kind": n["kind"], "subject": n["subject"], "body": n["body"],
               "incident_id": n.get("incident_id"), "group_key": n["group_key"],
               **(_json(n.get("payload"), {}) or {})}
    raw = json.dumps(payload).encode()
    return _post_json(spec["url"], payload, headers={
        "X-BOSS-Timestamp": ts, "X-BOSS-Signature": mon.sign(spec["token"], ts, raw)})


def deliver_due(engine, settings: Any, *, now: Optional[datetime] = None,
                limit: int = 50, sender: Any = None) -> dict[str, int]:
    """Claim due messages and send each OUTSIDE the claiming transaction, so one slow channel
    holds no lock and blocks nothing else."""
    from ..db import begin

    now = now or _now()
    sender = sender or deliver
    with begin(engine) as c:
        c.execute(sql("UPDATE notifications SET status = 'pending' WHERE status = 'held' "
                      "AND next_attempt_at <= :now"), {"now": now})
        rows = [dict(r) for r in c.execute(sql(
            "UPDATE notifications SET next_attempt_at = :lease WHERE id IN (SELECT id FROM "
            " notifications WHERE status IN ('pending', 'failed') AND next_attempt_at <= :now "
            " ORDER BY created_at LIMIT :n FOR UPDATE SKIP LOCKED) RETURNING *"),
            {"now": now, "lease": now + timedelta(minutes=2), "n": limit}).mappings().all()]
    sent = failed = 0
    for n in rows:
        with begin(engine) as c:
            cp = _cp_row(c, n["project_id"], n["contact_point_id"])
            recent = c.execute(sql(
                "SELECT count(*) FROM notifications WHERE contact_point_id = :c AND status = "
                "'sent' AND sent_at > :since"),
                {"c": n["contact_point_id"], "since": now - timedelta(minutes=1)}).scalar()
            if cp and int(recent or 0) >= int(cp["rate_per_minute"] or 20):
                c.execute(sql("UPDATE notifications SET next_attempt_at = :t WHERE id = :i"),
                          {"t": now + timedelta(minutes=1), "i": n["id"]})
                continue
            try:
                receipt = sender(c, settings, n)
            except Exception as exc:  # noqa: BLE001 — a dead channel is a receipt, not a crash
                attempts = int(n["attempts"]) + 1
                word = f"{type(exc).__name__}: {exc}"[:300]
                c.execute(sql(
                    "UPDATE notifications SET attempts = :a, status = :s, error_word = :w, "
                    " next_attempt_at = :t WHERE id = :i"),
                    {"a": attempts, "s": "dead" if attempts >= MAX_ATTEMPTS else "failed",
                     "w": word, "t": now + timedelta(seconds=30 * (2 ** attempts)),
                     "i": n["id"]})
                failed += 1
                continue
            c.execute(sql(
                "UPDATE notifications SET status = 'sent', sent_at = :now, receipt = :r, "
                " attempts = attempts + 1, error_word = NULL WHERE id = :i"),
                {"now": now, "r": str(receipt)[:300], "i": n["id"]})
            sent += 1
    return {"sent": sent, "failed": failed}


def outbox(connection, project_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """The notifications BOSS sent (or holds), newest first, each with the worst SEV level of
    the alerts it carried (`sev`, null for BOSS's own self-checks)."""
    from . import severity as sev

    rows = [dict(r) for r in connection.execute(sql(
        "SELECT n.*, c.name AS contact_point, c.kind AS channel FROM notifications n "
        "LEFT JOIN contact_points c ON c.id = n.contact_point_id WHERE n.project_id = :p "
        "ORDER BY n.created_at DESC LIMIT :n"), {"p": project_id, "n": limit}).mappings()]
    ids = sorted({str(i) for r in rows for i in (_json(r.get("alert_ids"), []) or [])})
    labels = {r["id"]: (_json(r["labels"], {}) or {}) for r in connection.execute(sql(
        "SELECT id, labels FROM app_alerts WHERE id = ANY(:i)"), {"i": ids}).mappings()} \
        if ids else {}
    out = []
    for r in rows:
        got = [labels[i] for i in (_json(r.get("alert_ids"), []) or []) if i in labels
               and labels[i].get("boss_meta") != "true"]
        out.append({**{k: (mon.iso(v) if isinstance(v, datetime) else v) for k, v in r.items()
                       if k not in ("body", "payload", "dedupe_key")},
                    "sev": sev.worst(got)})
    return out


__all__ = ["CONTACT_KINDS", "contact_point_view", "create_contact_point", "deliver",
           "deliver_due", "ensure_console", "ensure_root", "outbox", "plan", "policies",
           "preview", "quiet_now", "route", "send_test", "set_rule_notify", "verify"]
