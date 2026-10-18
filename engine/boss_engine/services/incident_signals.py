"""An app tells BOSS it suspects an incident, and BOSS tells it how the incident ended.

THE HUMAN SIGNAL, KEPT IN ITS PLACE
-----------------------------------
A help desk sees customers before a dashboard does: "checkout fails when I order 12 cables",
four times in ten minutes. That is evidence an incident is happening and of whom it hurts. It is
not proof, and it is not an instruction: an app's report CREATES OR LINKS an incident and never
starts a change by itself. The response (a fix, a release) starts from BOSS's own monitor alert —
the reports corroborate it. A reported-only incident waits for the monitor or for a person, and
the monitor never closes an incident it did not raise.

THE CHANNEL
-----------
An app opts in by declaring, in its boss.deploy.yaml::

    signals: {path: /boss/signals, callback: /boss/incidents}

BOSS mints one token per deployment target (`BOSS_SIGNALS_TOKEN`, in its secret store, put in
the app's deploy environment at every deploy — never shown, never logged). Two directions:

* app -> BOSS, PULL: on the monitor's cadence BOSS GETs `<origin><path>` with
  `Authorization: Bearer <token>`; the app answers ``{"signals": [...]}``. Works when the app can
  be reached and BOSS cannot (a laptop install deploying to a public VM).
* app -> BOSS, PUSH: the app POSTs one signal to ``/v1/apps/<target id>/signals`` signed
  ``X-BOSS-Signature: v1=<hex HMAC-SHA256(token, "<timestamp>.<raw body>")>`` with
  ``X-BOSS-Timestamp``. Same ingest, same limits.
* BOSS -> app: ``POST <origin><callback>/<incident id>``, signed the same way, when a report is
  linked ("investigating") and when the incident is resolved (with the root cause and the fix).

Every signal is validated and bounded, deduplicated by the app's own id, and rate-limited per
app; every refusal is said in words. A signal is
``{"id", "title", "summary"?, "affects"?, "severity"?, "refs"?: [<the app's own ids>],
"count"?, "first_seen"?, "last_seen"?}`` — `affects` names the app that is failing (by its
BOSS app name); without it the reporter itself is the affected app.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
from urllib.parse import quote
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..ports import sql
from .core import emit_event, make_id

ENV_KEY = "BOSS_SIGNALS_TOKEN"
#: The token's name in BOSS's secret store (stack_deploy.ensure_generated_secret's naming).
_TOKEN_STACK, _TOKEN_KEY = "app_signals", ENV_KEY

POLL_INTERVAL_SECONDS = 30
MAX_BODY_BYTES = 64 * 1024
MAX_SIGNALS_PER_READ = 20
TIMEOUT_SECONDS = 5
TIMESTAMP_TOLERANCE_SECONDS = 300
#: Per app: a report storm is itself a symptom, not a reason to write a thousand rows.
RATE_PER_MINUTE = 10
RATE_PER_HOUR = 120
SEVERITIES = ("info", "warning", "critical")


class SignalRefused(ValueError):
    """A signal BOSS will not take, with the reason in words."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason, self.detail = reason, detail


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# The token: one per app, minted once, never shown.
# --------------------------------------------------------------------------- #
def channel_token(engine, settings: Any, *, project_id: str, target_id: str) -> str:
    """The app's signals token, minted on first use (its own committed transaction)."""
    from .stack_deploy import ensure_generated_secret

    return ensure_generated_secret(engine, settings, project_id=project_id, target_id=target_id,
                                   stack=_TOKEN_STACK, key=_TOKEN_KEY, generator="token",
                                   actor="boss")


def read_token(connection, settings: Any, project_id: str, target_id: str) -> Optional[str]:
    """The token if one was minted (by a deploy that declared signals); None otherwise."""
    from .secret_backends import resolve_secret_ref
    from .secrets_service import ref_key_for
    from .stack_deploy import _find_secret_ref, _generated_secret_name

    ref = _find_secret_ref(connection, project_id,
                           [ref_key_for(target_id, _generated_secret_name(_TOKEN_STACK,
                                                                          _TOKEN_KEY))])
    return resolve_secret_ref(connection, ref, settings) if ref else None


def sign(token: str, timestamp: str, body: bytes) -> str:
    return "v1=" + hmac.new(token.encode(), timestamp.encode() + b"." + body,
                            hashlib.sha256).hexdigest()


def verify_signature(token: Optional[str], timestamp: str, body: bytes, signature: str, *,
                     now: Optional[float] = None) -> bool:
    if not token or not signature or not timestamp:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - ts) > TIMESTAMP_TOLERANCE_SECONDS:
        return False
    return hmac.compare_digest(sign(token, timestamp, body), signature.strip())


# --------------------------------------------------------------------------- #
# What an app declares, and where it is reached.
# --------------------------------------------------------------------------- #
def declarations(connection, target_ids: list[str]) -> dict[str, dict[str, Any]]:
    """target -> its `signals:` spec, from its newest HEALTHY stack deploy."""
    if not target_ids:
        return {}
    rows = connection.execute(sql(
        "SELECT DISTINCT ON (target_id) target_id, manifest FROM stack_deployments "
        "WHERE target_id = ANY(:ids) AND status = 'healthy' "
        "ORDER BY target_id, created_at DESC"), {"ids": list(target_ids)}).mappings().all()
    out = {}
    for r in rows:
        manifest = r["manifest"] if isinstance(r["manifest"], dict) else \
            json.loads(r["manifest"] or "{}")
        spec = manifest.get("signals")
        if spec and spec.get("path"):
            out[r["target_id"]] = dict(spec)
    return out


def _target(connection, target_id: str) -> Optional[dict[str, Any]]:
    from .deployment_health import STACK_OPEN_URL_COLUMN

    row = connection.execute(sql(
        "SELECT t.*, " + STACK_OPEN_URL_COLUMN.format(alias="t")
        + " FROM deployment_targets t WHERE t.id = :t"), {"t": target_id}).mappings().first()
    return dict(row) if row else None


def _no_redirects():
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):  # noqa: D401
            return None
    return urllib.request.build_opener(NoRedirect)


def _call(method: str, url: str, *, headers: dict[str, str],
          body: Optional[bytes] = None) -> tuple[Optional[int], bytes, str]:
    """(status, body, word). Never raises; never follows a redirect; the body is capped."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with _no_redirects().open(req, timeout=TIMEOUT_SECONDS) as resp:
            data = resp.read(MAX_BODY_BYTES + 1)
            if len(data) > MAX_BODY_BYTES:
                return resp.status, b"", "The app's answer is larger than 64 KB"
            return resp.status, data, ""
    except urllib.error.HTTPError as exc:
        return exc.code, b"", f"The app answered {exc.code}"
    except Exception as exc:  # noqa: BLE001 — unreachable IS the answer
        return None, b"", f"The app could not be reached ({type(exc).__name__})"


# --------------------------------------------------------------------------- #
# Validation and ingest.
# --------------------------------------------------------------------------- #
def _text(raw: Any, limit: int) -> str:
    return " ".join(str(raw or "").split())[:limit]


def validate(raw: Any) -> dict[str, Any]:
    """One signal, bounded and typed — or SignalRefused saying which field is wrong."""
    if not isinstance(raw, dict):
        raise SignalRefused("invalid_shape", "a signal must be a JSON object")
    sid = _text(raw.get("id"), 100)
    title = _text(raw.get("title"), 200)
    if not sid:
        raise SignalRefused("invalid_shape", "a signal needs the app's own `id` for it")
    if not title:
        raise SignalRefused("invalid_shape", "a signal needs a `title`")
    severity = str(raw.get("severity") or "warning").lower()
    if severity not in SEVERITIES:
        raise SignalRefused("invalid_shape", f"severity must be one of {list(SEVERITIES)}")
    refs = raw.get("refs") or []
    if not isinstance(refs, list):
        raise SignalRefused("invalid_shape", "`refs` must be a list of the app's own ids")
    try:
        count = max(0, min(100000, int(raw.get("count") or len(refs) or 1)))
    except (TypeError, ValueError):
        raise SignalRefused("invalid_shape", "`count` must be a number") from None
    return {"id": sid, "title": title, "summary": _text(raw.get("summary"), 2000),
            "affects": _text(raw.get("affects"), 100), "severity": severity,
            "refs": [_text(r, 100) for r in refs[:50] if _text(r, 100)], "count": count,
            "first_seen": _text(raw.get("first_seen"), 40),
            "last_seen": _text(raw.get("last_seen"), 40)}


def _affected_target(connection, project_id: str, reporter: dict, affects: str) -> dict:
    """The app the signal says is failing: by its BOSS app name, else the reporter itself."""
    if affects:
        from .naming import identity_key

        want = identity_key(affects)
        for row in connection.execute(sql(
                "SELECT id, app_name, environment, project_id FROM deployment_targets "
                "WHERE project_id = :p AND status = 'active' ORDER BY created_at DESC"),
                {"p": project_id}).mappings().all():
            if want and identity_key(str(row["app_name"] or "")) == want:
                return dict(row)
    return {k: reporter.get(k) for k in ("id", "app_name", "environment", "project_id")}


def _rate_refusal(connection, reporter_id: str, now: datetime) -> Optional[str]:
    minute, hour = connection.execute(sql(
        "SELECT count(*) FILTER (WHERE received_at > :m), count(*) FROM app_signal_receipts "
        "WHERE reporter_target_id = :t AND received_at > :h"),
        {"t": reporter_id, "m": now - timedelta(minutes=1),
         "h": now - timedelta(hours=1)}).one()
    if int(minute) >= RATE_PER_MINUTE:
        return f"more than {RATE_PER_MINUTE} signals a minute from this app"
    if int(hour) >= RATE_PER_HOUR:
        return f"more than {RATE_PER_HOUR} signals an hour from this app"
    return None


def _refresh(connection, seen: dict, signal: dict, now: datetime) -> dict[str, Any]:
    """The same problem, reported again: the app keeps one id while its cluster grows.

    The receipt takes the union of refs, the larger count and the later last_seen, so every
    ticket the app linked gets the RCA and the card counts them all. Nothing new is opened and
    the rate limit is not spent: it is one report, kept current. Unchanged: a duplicate."""
    old = seen["payload"] if isinstance(seen["payload"], dict) else json.loads(seen["payload"] or "{}")
    refs = list(dict.fromkeys([*(old.get("refs") or []), *(signal.get("refs") or [])]))
    merged = {**old, **signal, "refs": refs,
              "count": max(int(old.get("count") or 0), int(signal.get("count") or 0), len(refs)),
              "first_seen": old.get("first_seen") or signal.get("first_seen") or "",
              "last_seen": max(str(old.get("last_seen") or ""), str(signal.get("last_seen") or ""))}
    out = {"incident_id": seen["incident_id"], "signal_id": signal["id"]}
    if merged == old:
        return {"action": "duplicate", **out}
    connection.execute(sql(
        "UPDATE app_signal_receipts SET payload = CAST(:p AS JSONB) WHERE id = :id"),
        {"p": json.dumps(merged), "id": seen["id"]})
    connection.execute(sql("UPDATE incidents SET last_seen = :now WHERE id = :i"),
                       {"i": seen["incident_id"], "now": now})
    return {"action": "updated", **out}


def ingest(connection, reporter: dict, raw: Any, *, via: str,
           now: Optional[datetime] = None) -> dict[str, Any]:
    """Take one signal from `reporter` (a deployment target): create or link the incident.

    Returns {action: linked|opened|updated|duplicate, incident_id, signal_id}. Raises SignalRefused."""
    from . import app_metrics, incident_response as ir

    now = now or _now()
    signal = validate(raw)
    project_id = reporter["project_id"]
    seen = connection.execute(sql(
        "SELECT id, incident_id, payload FROM app_signal_receipts WHERE reporter_target_id = :t "
        "AND signal_id = :s"), {"t": reporter["id"], "s": signal["id"]}).mappings().first()
    if seen:
        return _refresh(connection, seen, signal, now)
    refused = _rate_refusal(connection, reporter["id"], now)
    if refused:
        raise SignalRefused("rate_limited", refused)

    affected = _affected_target(connection, project_id, reporter, signal["affects"])
    key = app_metrics.INCIDENT_PREFIX + str(affected["id"])
    inc = connection.execute(sql(
        "SELECT id, status FROM incidents WHERE project_id = :p AND dedupe_key = :k"),
        {"p": project_id, "k": key}).mappings().first()
    app = {"target_id": affected["id"], "app_name": affected.get("app_name"),
           "environment": affected.get("environment")}
    if inc and inc["status"] not in ("resolved", "dismissed"):
        incident_id, action = inc["id"], "linked"
    elif inc:
        # A NEW OCCURRENCE, REPORTED BEFORE THE MONITOR SAW IT: reopened as reported-only. The
        # last occurrence's alert, response and verification are not this one's.
        incident_id, action = inc["id"], "opened"
        connection.execute(sql(
            "UPDATE incidents SET status = 'open', first_seen = :now, last_seen = :now, "
            " title = :title, finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) || "
            " CAST(:ctx AS JSONB) WHERE id = :i"),
            {"i": incident_id, "now": now,
             "title": f"{app['app_name']}: reported — {signal['title']}"[:200],
             "ctx": json.dumps({"app": app, "alert": None, "response": None,
                                "verification": None, "occurrence": ir._iso(now),
                                "reported_only": True})})
    else:
        incident_id, action = make_id("inc"), "opened"
        connection.execute(sql(
            "INSERT INTO incidents (id, project_id, dedupe_key, title, service, severity, "
            " status, signal_count, first_seen, last_seen, finalize_ctx) VALUES (:id, :p, :k, "
            " :title, :svc, :sev, 'open', 0, :now, :now, CAST(:ctx AS JSONB))"),
            {"id": incident_id, "p": project_id, "k": key,
             "title": f"{app['app_name']}: reported — {signal['title']}"[:200],
             "svc": app["app_name"] or "", "sev": signal["severity"], "now": now,
             "ctx": json.dumps({"app": app, "occurrence": ir._iso(now),
                                "reported_only": True})})
    connection.execute(sql(
        "UPDATE incidents SET signal_count = signal_count + 1, last_seen = :now WHERE id = :i"),
        {"i": incident_id, "now": now})
    connection.execute(sql(
        "INSERT INTO app_signal_receipts (id, project_id, reporter_target_id, signal_id, "
        " incident_id, via, payload, received_at) VALUES (:id, :p, :t, :s, :i, :via, "
        " CAST(:payload AS JSONB), :now)"),
        {"id": make_id("sgr"), "p": project_id, "t": reporter["id"], "s": signal["id"],
         "i": incident_id, "via": via, "payload": json.dumps(signal), "now": now})
    reporter_name = reporter.get("app_name")
    ir._evidence(connection, incident_id, "customer_reports", ref=str(reporter["id"]),
                 payload={"reporter": {"target_id": reporter["id"], "app_name": reporter_name},
                          "via": via, **signal}, at=now)
    emit_event(connection, project_id, "incident.reported", f"app:{reporter['id']}", "incident",
               incident_id, {"incident_id": incident_id, "reporter_target_id": reporter["id"],
                             "reporter": reporter_name, "action": action,
                             "signal_id": signal["id"], "title": signal["title"],
                             "refs": signal["refs"], "affected_target_id": affected["id"]})
    return {"action": action, "incident_id": incident_id, "signal_id": signal["id"]}


def _record_poll(connection, target_id: str, project_id: str, word: str, now: datetime) -> None:
    connection.execute(sql(
        "INSERT INTO app_signal_channels (target_id, project_id, last_polled_at, last_word) "
        "VALUES (:t, :p, :now, :w) ON CONFLICT (target_id) DO UPDATE SET "
        "last_polled_at = EXCLUDED.last_polled_at, last_word = EXCLUDED.last_word"),
        {"t": target_id, "p": project_id, "now": now, "w": word[:200]})


def _due(connection, target_id: str, now: datetime) -> bool:
    last = connection.execute(sql(
        "SELECT last_polled_at FROM app_signal_channels WHERE target_id = :t"),
        {"t": target_id}).scalar()
    return not last or (now - last).total_seconds() >= POLL_INTERVAL_SECONDS


def poll_target(engine, settings: Any, target: dict, spec: dict, *,
                now: Optional[datetime] = None) -> dict[str, Any]:
    """Read one app's suspected incidents, take each, and tell it which were linked."""
    from ..db import begin
    from .app_metrics import public_origin

    now = now or _now()
    origin = public_origin(target)
    with begin(engine) as c:
        token = read_token(c, settings, target["project_id"], target["id"])
    if not origin or not token:
        word = ("No public address to read signals from" if not origin else
                "No signals token yet: deploy the app with `signals:` declared")
        with begin(engine) as c:
            _record_poll(c, target["id"], target["project_id"], word, now)
        return {"ok": False, "word": word, "taken": []}
    status, body, word = _call("GET", origin + spec["path"],
                               headers={"Authorization": f"Bearer {token}",
                                        "Accept": "application/json"})
    taken: list[dict[str, Any]] = []
    if status == 200:
        try:
            items = (json.loads(body.decode("utf-8", "replace") or "{}") or {}).get("signals")
            if not isinstance(items, list):
                raise ValueError("no `signals` list")
        except (ValueError, AttributeError) as exc:
            word, items = f"The app's signals page is not usable: {exc}"[:200], []
        for raw in items[:MAX_SIGNALS_PER_READ]:
            with begin(engine) as c:
                try:
                    taken.append(ingest(c, target, raw, via="pull", now=now))
                except SignalRefused as refused:
                    taken.append({"action": "refused", "reason": refused.reason,
                                  "detail": refused.detail})
        word = word or f"Read {len(items)} signal(s)"
    with begin(engine) as c:
        _record_poll(c, target["id"], target["project_id"], word or f"HTTP {status}", now)
    for got in taken:
        if got.get("action") in ("linked", "opened"):
            with begin(engine) as c:
                notify_reporters(c, settings, target["project_id"], got["incident_id"], None,
                                 only_target=target["id"])
    return {"ok": status == 200, "word": word, "taken": taken}


def sweep(engine, settings: Any, targets: list[dict], *,
          now: Optional[datetime] = None) -> dict[str, Any]:
    """Poll every deployed app that declares `signals:` and is due. Never raises."""
    from ..db import begin

    now = now or _now()
    polled = 0
    try:
        with begin(engine) as c:
            decls = declarations(c, [t["id"] for t in targets])
        for t in targets:
            spec = decls.get(t["id"])
            if not spec:
                continue
            with begin(engine) as c:
                if not _due(c, t["id"], now):
                    continue
                _record_poll(c, t["id"], t["project_id"], "Reading", now)   # claim the slot
            poll_target(engine, settings, t, spec, now=now)
            polled += 1
    except Exception as exc:  # noqa: BLE001
        return {"signals_polled": polled, "signals_error": f"{type(exc).__name__}: {exc}"[:200]}
    return {"signals_polled": polled}


def receive(connection, settings: Any, target_id: str, raw_body: bytes,
            headers: dict[str, str], *, now: Optional[datetime] = None) -> dict[str, Any]:
    """The PUSH door: one signed signal from the app itself. Never trusts the body before the
    signature; every refusal is said."""
    target = _target(connection, target_id)
    if not target or target.get("status") != "active":
        return {"status": "not_found"}
    if not declarations(connection, [target_id]).get(target_id):
        return {"status": "refused", "reason": "not_declared",
                "detail": "this app does not declare `signals:` in its boss.deploy.yaml"}
    if len(raw_body) > MAX_BODY_BYTES:
        return {"status": "refused", "reason": "body_too_large", "detail": "over 64 KB"}
    token = read_token(connection, settings, target["project_id"], target_id)
    if not verify_signature(token, headers.get("x-boss-timestamp", ""), raw_body,
                            headers.get("x-boss-signature", "")):
        return {"status": "refused", "reason": "bad_signature",
                "detail": "X-BOSS-Signature does not verify (or the timestamp is stale)"}
    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return {"status": "refused", "reason": "invalid_json", "detail": "not a JSON object"}
    try:
        got = ingest(connection, target, payload, via="push", now=now)
    except SignalRefused as refused:
        return {"status": "refused", "reason": refused.reason, "detail": refused.detail}
    return {"status": "accepted", **got}


# --------------------------------------------------------------------------- #
# BOSS -> the app: what became of what it reported.
# --------------------------------------------------------------------------- #
def notify_reporters(connection, settings: Any, project_id: str, incident_id: str,
                     rep: Optional[dict[str, Any]], *, only_target: Optional[str] = None
                     ) -> list[dict[str, Any]]:
    """POST the incident's state to every app that reported it and declares a callback.
    `rep` is the incident report (resolved, root cause, fix) or None (linked: investigating).

    ADDRESSED BY THE APP'S OWN ID: one call per suspected incident the app reported, at
    `<callback>/<the id it reported under>`, with BOSS's incident id in the body. D2: BOSS posted
    to `<callback>/<BOSS's incident id>`, an id the help desk never saw, and both calls (linked,
    resolved) were answered 404 — the root cause never reached the tickets."""
    from . import app_metrics, incident_response as ir

    receipts = connection.execute(sql(
        "SELECT reporter_target_id, signal_id, payload FROM app_signal_receipts "
        "WHERE incident_id = :i ORDER BY received_at"), {"i": incident_id}).mappings().all()
    inc = ir._incident(connection, incident_id) or {}
    told: list[dict[str, Any]] = []
    for r in receipts:
        tid = r["reporter_target_id"]
        if only_target and tid != only_target:
            continue
        spec = declarations(connection, [tid]).get(tid) or {}
        target = _target(connection, tid)
        origin = app_metrics.public_origin(target) if target else None
        token = read_token(connection, settings, project_id, tid)
        if not spec.get("callback") or not origin or not token:
            continue
        signal = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"] or "{}")
        refs = list(signal.get("refs") or [])
        body = {"incident_id": incident_id, "signal_id": r["signal_id"],
                "title": inc.get("title"),
                "status": "resolved" if rep and rep.get("resolved_at") else
                ("investigating" if rep is None else inc.get("status")),
                "refs": refs}
        if rep:
            body.update({"root_cause": (rep.get("root_cause") or {}).get("root_cause"),
                         "pull_request_url": rep.get("pull_request_url"),
                         "resolved_at": rep.get("resolved_at"),
                         "minutes_to_recover": rep.get("minutes_to_recover")})
        raw = json.dumps(body, default=str).encode()
        ts = str(int(time.time()))
        path = f"{spec['callback']}/{quote(str(r['signal_id']), safe='')}"
        status, _resp, word = _call("POST", f"{origin}{path}",
                                    headers={"Content-Type": "application/json",
                                             "X-BOSS-Timestamp": ts,
                                             "X-BOSS-Signature": sign(token, ts, raw)},
                                    body=raw)
        receipt = {"target_id": tid, "signal_id": r["signal_id"], "status": body["status"],
                   "http": status, "ok": bool(status and 200 <= status < 300), "word": word,
                   "refs": len(refs)}
        ir._evidence(connection, incident_id, "reporter_notified", ref=tid, payload=receipt)
        told.append(receipt)
    return told


def told_reporters(connection, incident_id: str) -> list[dict[str, Any]]:
    """What each reporting app was last told, per suspected incident it reported: status,
    whether it took it, and BOSS's word for the answer. A callback that failed is SAID (D2: the
    help desk answered 404 twice and only an evidence row knew)."""
    rows = connection.execute(sql(
        "SELECT DISTINCT ON (ref, COALESCE(payload->>'signal_id', '')) ref, payload, created_at "
        "FROM incident_evidence WHERE incident_id = :i AND kind = 'reporter_notified' "
        "ORDER BY ref, COALESCE(payload->>'signal_id', ''), created_at DESC"),
        {"i": incident_id}).mappings().all()
    names = {r["id"]: r["app_name"] for r in connection.execute(sql(
        "SELECT id, app_name FROM deployment_targets WHERE id = ANY(:t)"),
        {"t": [r["ref"] for r in rows]}).mappings().all()} if rows else {}
    out = []
    for r in rows:
        p = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"] or "{}")
        out.append({"target_id": r["ref"], "app_name": names.get(r["ref"]) or r["ref"],
                    "signal_id": p.get("signal_id"), "status": p.get("status"),
                    "ok": bool(p.get("ok")), "http": p.get("http"), "word": p.get("word"),
                    "refs": p.get("refs"), "at": r["created_at"].isoformat()})
    # A call recorded before calls were addressed per signal (no signal_id) is superseded by any
    # later call to the same app: it is the same report, told again (D2: the 404 line stayed
    # beside the "Told" one after Send again).
    latest = {}
    for t in out:
        latest[t["target_id"]] = max(latest.get(t["target_id"], ""), t["at"])
    return [t for t in out if t["signal_id"] or t["at"] >= latest[t["target_id"]]]


def tell_reporters_again(connection, settings: Any, project_id: str,
                         incident_id: str) -> list[dict[str, Any]]:
    """A person sends the incident's current state to the apps that reported it once more —
    the root cause and fix when the response has resolved it, else that it is being handled."""
    stored = connection.execute(sql(
        "SELECT payload FROM sre_postmortems WHERE incident_id = :i AND project_id = :p AND "
        "payload->>'kind' = 'incident_report'"), {"i": incident_id, "p": project_id}).scalar()
    rep = stored if isinstance(stored, dict) else (json.loads(stored) if stored else None)
    return notify_reporters(connection, settings, project_id, incident_id,
                            rep if rep and rep.get("resolved_at") else None)


SIGNALS_STANDARD = """
## Incident signals (BOSS platform standard — for an app that reports incidents to BOSS)

An app that notices users failing (a help desk seeing similar complaints, a queue backing up)
may tell BOSS. BOSS links what it reports to the incident its own monitor raises — the report
corroborates; it never starts a code change by itself — and tells the app how it ended.

DECLARE IT in the app's boss.deploy.yaml (paths on the app itself):

    signals: {path: /boss/signals, callback: /boss/incidents}

BOSS puts `BOSS_SIGNALS_TOKEN` in the app's deploy environment. With docker compose, pass it to
the service that serves these routes (`environment: {BOSS_SIGNALS_TOKEN: ${BOSS_SIGNALS_TOKEN}}`).
Never log it, never return it, never put it in a client bundle.

1. `GET /boss/signals` — BOSS reads it every 30 s with `Authorization: Bearer <token>`.
   Compare the token in constant time; anything else is 401. Answer
   `{"signals": [{"id": "<your stable id for this suspected incident>", "title": "...",
   "summary": "...", "affects": "<the failing app's name, e.g. order-api>",
   "severity": "warning", "refs": ["<your record ids, e.g. ticket ids>"], "count": 4,
   "first_seen": "<ISO>", "last_seen": "<ISO>"}]}` — only suspected incidents that are still
   open on your side; the same `id` while it is the same problem (BOSS deduplicates by it and keeps its refs and count current).
2. `POST /boss/incidents/<your id>` — `<your id>` is the `id` YOU reported the suspected
   incident under in (1), never BOSS's. BOSS tells you what happened, JSON
   `{"incident_id": "<BOSS's id>", "signal_id": "<your id>", "title",
   "status": "investigating"|"resolved", "refs": [...],
   "root_cause"?, "pull_request_url"?, "resolved_at"?}` with headers `X-BOSS-Timestamp` and
   `X-BOSS-Signature: v1=<hex HMAC-SHA256(token, "<timestamp>.<raw body>")>`. Verify the
   signature over the RAW body before parsing, refuse a timestamp more than 300 s old, and
   answer 2xx. Show it on the records named in `refs` (e.g. post the root cause on each linked
   ticket and resolve them when status is "resolved").

Detecting the incident is the app's own logic and must be explainable: e.g. N similar records
(same symptom words, same affected product) within a time window. Test it: similar records
cluster, unrelated ones do not; the signals route refuses a missing or wrong token; the
callback refuses a bad signature and a stale timestamp, and applies a good one.
""".strip()
