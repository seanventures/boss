"""WATCH THIS APP (docs/product/wireframes/agentic-factory/2-watch-an-app.html; Project › Health).

One input ("HelpDesk staging. Wake me if replies stop sending or the inbox gets slow. Tell
#support-eng on Slack.") → the SRE agent's read (the app it means, how it talks, what matters,
the watches with a 7-day baseline and backtest, one open question) → six boxes → Start watching
→ the app's Health page with the agent already on call.

ONE PATH, NO SECOND MONITORING STACK. Everything here is a thin layer over what Observability
already runs:

  * a watch IS one of the app's alert rules (services/app_alerts.py: ensure_rules,
    update_rule, render, condition_words); "Remove" switches the rule off;
  * the backtest is services/rule_preview.preview over 7 days instead of 24 h, on the same
    datasource the evaluator picks (Prometheus when the stack answers, else BOSS's records);
  * "Tell" is a contact point (services/notify.py: create_contact_point, send_test, verify)
    and each watch's routing is notify.set_rule_notify, with quiet hours for "Wait till 08:00";
  * "On call" is the SRE agent: every watch triages (app_alerts.triage_alert), and on an app
    that is not production each watch answers alone (auto_respond → the incident_response
    workflow, on the person's standing grant: 409 consent_required the first time). On
    production the same watch only triages and the fix waits for a person (Where people
    decide › Production deploy);
  * "Collector" is observability_stack's registered Prometheus/Grafana, or BOSS's own scraper.

WHO DECIDES. Only the compliance boundary waits for a person (services/decide_list.py): a
message to an outside channel needs its code entered once (Talking to the outside), and a
production rollback or deploy waits at Production deploy. Everything else runs alone.

The reading of the words is deterministic (like Add a server's): which app and environment,
which channel, what the person cares about. It never invents a rule kind BOSS cannot evaluate.
"""
from __future__ import annotations

import json
import re
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from ..ports import sql
from . import app_alerts as aa
from . import monitoring as mon
from . import notify

KINDS = ("project", "url", "elsewhere")
KIND_WORDS = {"project": "Project app", "url": "Just a URL", "elsewhere": "Alerts elsewhere"}
NIGHT = ("wake", "wait", "alone")
NIGHT_WORDS = {"wake": "Wake me", "wait": "Wait till 08:00", "alone": "Fix alone, tell me"}
QUIET = {"start": "22:00", "end": "08:00"}
BACKTEST = timedelta(days=7)
SPARK_POINTS = 28

#: What each watch is, in words, from its rule kind. `tone` is the default: wake (a page,
#: high urgency) or tell (a message, low urgency).
WATCH: dict[str, dict[str, Any]] = {
    "error_rate": {"icon": "paper-plane", "tone": "wake", "what": "Requests failing"},
    "latency_p95": {"icon": "timer", "tone": "tell", "what": "Slow responses"},
    "health_check": {"icon": "heartbeat", "tone": "wake", "what": "App down"},
    "no_traffic": {"icon": "wave", "tone": "tell", "what": "Traffic stops"},
    "deploy_health": {"icon": "rocket", "tone": "tell", "what": "A deploy goes bad"},
}
#: The watches every app gets, before the words add any.
DEFAULT_WATCHES = ("error_rate", "latency_p95", "health_check", "deploy_health")

#: What a clause of the person's words is about. A clause is matched to the ONE rule kind BOSS
#: evaluates for it; anything else stays a hint the agent shows ("not a watch yet").
_ABOUT = (
    ("latency_p95", re.compile(r"\b(slow|slower|lag|laggy|latency|p9[59]|takes? (too )?long|"
                               r"sluggish|speed)\b", re.I)),
    ("health_check", re.compile(r"\b(down|offline|unreachable|outage|not (load|answer|respond)"
                                r"\w*|dies|crash\w*)\b", re.I)),
    ("no_traffic", re.compile(r"\b(no traffic|quiet|nobody|no (one|requests|users)|"
                              r"traffic (stops|drops))\b", re.I)),
    ("error_rate", re.compile(r"\b(stop\w*|fail\w*|error\w*|broken|break\w*|5\d\d|bounce\w*|"
                              r"not (send|sav|work)\w*)\b", re.I)),
)
_WAKE = re.compile(r"\b(wake|page|call)\s+me\b", re.I)
_WAIT = re.compile(r"\b(wait|until|till)\s+(the\s+)?(morning|08|8)\b", re.I)
_ALONE = re.compile(r"\b(fix (it )?(alone|yourself|on your own)|don'?t wake)\b", re.I)
_SLACK = re.compile(r"(#[a-z0-9][\w-]{0,79})(?:[^.\n]*?\bslack\b)?", re.I)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_URL = re.compile(r"https?://[^\s,]+", re.I)
_ENV = re.compile(r"\b(prod(?:uction)?|staging|stage|preview|dev(?:elopment)?|test)\b", re.I)
_TOOLS = re.compile(r"\b(alertmanager|grafana|pagerduty|datadog|sentry|cloudwatch|opsgenie)\b",
                    re.I)


class WatchRefused(ValueError):
    """A refusal, said in words (the router answers 422 with it)."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


# ── reading the words ─────────────────────────────────────────────────────────────────────

def _clauses(raw: str) -> list[str]:
    parts = re.split(r"[\n.;!?]+|\b(?:and|or)\b(?=\s+(?:the|if|when|it|replies|inbox|\w+\s+"
                     r"(?:gets|stops|is|are|goes)))", raw or "", flags=re.I)
    return [p.strip(" ,") for p in parts if p and p.strip(" ,")]


def _subject_of(clause: str) -> str:
    """'Wake me if replies stop sending' → 'replies stop sending'."""
    c = re.sub(r"^(?:.*?\b(?:if|when|whenever)\s+)", "", clause, flags=re.I)
    c = re.sub(r"^(?:wake|tell|page|call)\s+me\s+", "", c, flags=re.I)
    return c.strip()[:80]


def read_words(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    matters: list[dict[str, str]] = []
    seen: set[str] = set()
    for clause in _clauses(text):
        if re.search(r"\b(slack|tell|email|mattermost)\b", clause, re.I) and not any(
                rx.search(clause) for _k, rx in _ABOUT[:3]):
            continue
        for kind, rx in _ABOUT:
            if rx.search(clause):
                if kind not in seen:
                    seen.add(kind)
                    matters.append({"kind": kind, "words": _subject_of(clause)})
                break
    channel = None
    slack = re.search(r"(#[a-z0-9][\w-]{0,79})", text, re.I)
    if slack and re.search(r"\bslack\b", text, re.I):
        channel = {"kind": "slack_webhook", "name": slack.group(1)}
    elif slack and re.search(r"\bmattermost\b", text, re.I):
        channel = {"kind": "mattermost", "name": slack.group(1)}
    else:
        mail = _EMAIL.search(text)
        if mail:
            channel = {"kind": "email", "name": mail.group(0), "address": mail.group(0)}
    night = "alone" if _ALONE.search(text) else "wait" if _WAIT.search(text) else \
        "wake" if _WAKE.search(text) else None
    env = _ENV.search(text)
    url = _URL.search(text)
    tool = _TOOLS.search(text)
    return {"text": text, "matters": matters, "channel": channel, "night": night,
            "env": _env_key(env.group(1)) if env else None,
            "url": url.group(0).rstrip(".") if url else None,
            "tool": tool.group(1).lower() if tool else None}


def _env_key(word: str) -> str:
    w = word.lower()
    return "prod" if w.startswith("prod") else "staging" if w.startswith("stag") else \
        "dev" if w.startswith("dev") else w


def _is_prod(env: Any) -> bool:
    return bool(re.match(r"^(prod|production|live)$", str(env or ""), re.I))


# ── the app it means ──────────────────────────────────────────────────────────────────────

def targets(connection, project_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in connection.execute(sql(
        "SELECT * FROM deployment_targets WHERE project_id = :p AND status NOT IN "
        "('deleted', 'retired') ORDER BY (environment ~* '^prod') , created_at"),
        {"p": project_id}).mappings().all()]


def _target(connection, project_id: str, target_id: str) -> dict[str, Any]:
    row = connection.execute(sql("SELECT * FROM deployment_targets WHERE id = :t AND "
                                 "project_id = :p"), {"t": target_id, "p": project_id}
                             ).mappings().first()
    if not row:
        raise WatchRefused("That app is not in this project", 404)
    return dict(row)


def _address(t: Mapping[str, Any]) -> str:
    doms = t.get("domains")
    if isinstance(doms, str):
        try:
            doms = json.loads(doms)
        except ValueError:
            doms = []
    for d in doms or []:
        if d:
            return str(d).replace("https://", "").replace("http://", "").rstrip("/")
    return str(t.get("target_ref") or "").split("@")[-1]


def match(connection, project_id: str, words: Mapping[str, Any],
          target_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    ts = targets(connection, project_id)
    if target_id:
        return next((t for t in ts if t["id"] == target_id), None)
    if not ts:
        return None
    text = str(words.get("text") or "").lower()
    env = words.get("env")
    scored = []
    for i, t in enumerate(ts):
        s = 0
        if env and _env_key(str(t["environment"])) == env:
            s += 4
        name = str(t["app_name"] or "").lower()
        if name and (name in text or any(w in text for w in re.findall(r"[a-z]{4,}", name))):
            s += 2
        if words.get("url") and _address(t) and _address(t) in str(words["url"]):
            s += 6
        scored.append((s, -i, t))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return scored[0][2]


# ── what the agent reads from the running app ─────────────────────────────────────────────

def signals(connection, target: dict) -> dict[str, Any]:
    """How the app talks: its scraped metric families, BOSS's probe, its logs."""
    from . import alert_samples

    try:
        fams = [f for f in alert_samples.families(connection, target)
                if f["name"] != "probe_success"]
    except Exception:  # noqa: BLE001 — no monitored service yet
        fams = []
    probes = connection.execute(sql(
        "SELECT count(*) FROM deployment_health_samples WHERE deployment_target_id = :t AND "
        "checked_at > now() - interval '7 days'"), {"t": target["id"]}).scalar() or 0
    logs = str(target.get("target_kind") or "") in ("ssh", "docker", "local", "compose")
    found = bool(fams)
    return {"metrics": found, "families": len(fams), "probes": int(probes), "logs": logs,
            "traces": False,
            "word": (f"/metrics · {len(fams)} series families" if found else
                     "BOSS's health probe" + (f" · {probes} checks in 7 d" if probes else "")),
            "value": "/metrics + logs" if found else "probe + logs" if logs else "probe",
            "sub": "no app change"}


def collector(connection, project_id: str) -> dict[str, Any]:
    from .observability_stack import endpoint_state, prometheus_base

    try:
        prom = prometheus_base(connection, project_id)
    except Exception:  # noqa: BLE001
        prom = None
    try:
        graf = endpoint_state(connection, project_id, "grafana")
    except Exception:  # noqa: BLE001
        graf = {}
    if prom:
        return {"kind": "prometheus", "value": "Prometheus + Grafana" if graf.get("ok") else
                "Prometheus", "sub": "already running", "base": prom}
    return {"kind": "boss", "value": "BOSS's own scraper", "sub": "already running",
            "base": None}


def _subject(connection, target: dict) -> dict[str, Any]:
    return aa.subject(connection, target["id"]) or {**target, "subject_kind": "target"}


def backtest(connection, target: dict, rule: Mapping[str, Any], *,
             now: Optional[datetime] = None) -> dict[str, Any]:
    """Would this watch have fired in the last 7 days, with the 7-day line of its value: the
    same replay the rule editor's 24 h preview runs (rule_preview), on the same datasource."""
    from . import rule_preview

    now = now or datetime.now(timezone.utc)
    subj = _subject(connection, target)
    prom = None
    try:
        prom = aa.prometheus_for(connection, target["project_id"])
    except Exception:  # noqa: BLE001
        prom = None
    fired: Optional[int] = None
    word = ""
    try:
        got = rule_preview.preview(connection, subj, rule["expr"], rule.get("for_seconds") or 0,
                                   now=now, step_s=600, prometheus=prom,
                                   scope={"project": target["project_id"]} if prom else None,
                                   window=BACKTEST)
        fired, word = got.get("fired"), str(got.get("word") or "")
    except Exception as exc:  # noqa: BLE001 — a backtest that cannot run says why
        word = f"No backtest: {str(exc)[:120]}"
    spark: list[Optional[float]] = []
    value_expr = None
    try:
        value_expr = aa.render(rule["kind"], rule.get("params") or {}, target["app_name"],
                               target["environment"], aa._dialect(subj))["value_expr"]
    except Exception:  # noqa: BLE001 — a custom rule draws no line
        value_expr = None
    if value_expr and prom is None:
        try:
            from . import promql_lite as pq

            store = aa.local_store(connection, subj, now, int(BACKTEST.total_seconds() * 1000)
                                   + aa._max_range_ms(value_expr))
            node = pq.parse(value_expr)
            step = BACKTEST / SPARK_POINTS
            for i in range(SPARK_POINTS):
                at = now - BACKTEST + step * (i + 1)
                got = pq.instant_vector(node, store, int(at.timestamp() * 1000))
                v = got[0].value if got else None
                spark.append(None if v is None or v != v else round(float(v), 2))
        except Exception:  # noqa: BLE001
            spark = []
    vals = [v for v in spark if v is not None]
    baseline = round(statistics.median(vals), 2) if vals else None
    return {"fired": fired, "word": word, "spark": spark, "baseline": baseline}


def _title(kind: str, params: Mapping[str, Any]) -> str:
    p = {**aa.BUILTIN[kind]["params"], **(params or {})}
    if kind == "error_rate":
        return f"Errors > {aa.num(p['threshold'])}% for {aa.num(p['window_minutes'])} min"
    if kind == "latency_p95":
        return f"p95 > {aa.num(p['threshold'])} ms for {aa.num(p['window_minutes'])} min"
    if kind == "health_check":
        return f"App down · {aa.num(p['min_failures'])} failed probes in " \
               f"{aa.num(p['window_minutes'])} min"
    if kind == "no_traffic":
        return f"No traffic for {aa.num(p['window_minutes'])} min"
    return "A deploy fails its health check"


def _baseline_word(kind: str, baseline: Optional[float]) -> str:
    if baseline is None:
        return "no baseline yet"
    if kind == "deploy_health":
        return "deploys healthy" if baseline >= 1 else "a deploy failed"
    if kind == "health_check":
        return f"baseline {aa.num(baseline)} failed probes"
    unit = "%" if kind == "error_rate" else " ms" if kind == "latency_p95" else " req"
    return f"baseline {aa.num(baseline)}{unit}"


def _fired_word(fired: Optional[int]) -> str:
    return "not backtested" if fired is None else f"would have fired {fired}× in 7 d"


def watch_view(connection, target: dict, rule: Mapping[str, Any], *, tone: str,
               words: Optional[str], now: Optional[datetime] = None,
               with_backtest: bool = True) -> dict[str, Any]:
    kind = str(rule["kind"])
    bt = backtest(connection, target, rule, now=now) if with_backtest else \
        {"fired": None, "word": "", "spark": [], "baseline": None}
    params = {**aa.BUILTIN.get(kind, {}).get("params", {}), **(rule.get("params") or {})}
    return {
        "kind": kind, "rule_id": rule.get("id"), "icon": WATCH.get(kind, {}).get("icon", "bell"),
        "title": _title(kind, params), "what": WATCH.get(kind, {}).get("what", kind),
        "words": words, "from": "your words" if words else "BOSS default",
        "tone": tone, "params": params, "for_s": int(rule.get("for_seconds") or 0),
        "condition": aa.condition_words(kind, params, int(rule.get("for_seconds") or 0)),
        "expr": rule["expr"], "fired": bt["fired"], "backtest": bt["word"],
        "spark": bt["spark"], "baseline": bt["baseline"],
        "sub": f"{_baseline_word(kind, bt['baseline'])} · {_fired_word(bt['fired'])}",
        "runbook": (rule.get("annotations") or {}).get("runbook_url")
        or aa.BUILTIN.get(kind, {}).get("runbook"),
        "state": rule.get("last_state"), "enabled": bool(rule.get("enabled", True)),
    }


def _proposed_rule(target: dict, subj: dict, kind: str, params: Optional[dict] = None,
                   for_s: int = 0) -> dict[str, Any]:
    p = {**aa.BUILTIN[kind]["params"], **(params or {})}
    return {"kind": kind, "params": p, "for_seconds": for_s,
            "expr": aa.render(kind, p, target["app_name"], target["environment"],
                              aa._dialect(subj))["expr"],
            "annotations": {"runbook_url": aa.BUILTIN[kind]["runbook"]}}


# ── the boundary: what waits for a person ─────────────────────────────────────────────────

def boundary(target: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Alone vs waits, from Where people decide (decide_list) and what incident_response does
    on its own: it investigates, builds the fix as a PR and verifies the recovery; the release
    approval and a production deploy wait for a person; so does anything sent outside."""
    from .decide_list import BY_KEY

    prod = _is_prod((target or {}).get("environment"))
    alone = [{"key": "triage", "label": "triage", "icon": "magnifying-glass"},
             {"key": "investigate", "label": "find the cause", "icon": "graph"},
             {"key": "fix_pr", "label": "open a fix PR", "icon": "git-pull-request"},
             {"key": "verify", "label": "verify recovery", "icon": "check-circle"},
             {"key": "team", "label": "tell your team", "icon": "chat-circle"}]
    if prod:
        alone = alone[:1] + alone[-1:]
    waits = [{"key": "prod_deploy", "label": "prod rollback · deploy",
              "title": BY_KEY["prod_deploy"].title, "why": BY_KEY["prod_deploy"].why},
             {"key": "outside", "label": "status page · customers",
              "title": BY_KEY["outside"].title, "why": BY_KEY["outside"].why}]
    if prod:
        waits.insert(0, {"key": "fix", "label": "any fix on prod",
                         "title": BY_KEY["prod_deploy"].title, "why": BY_KEY["prod_deploy"].why})
    return {"prod": prod, "alone": alone, "waits": waits,
            "word": ("Production: the agent triages and waits for you" if prod else
                     "Below the line the agent acts alone; prod and customers stay with you")}


# ── the read ──────────────────────────────────────────────────────────────────────────────

def understand(connection, settings: Any, project_id: str, raw: str, *,
               kind: Optional[str] = None, target_id: Optional[str] = None,
               now: Optional[datetime] = None) -> dict[str, Any]:
    """The sheet's read. Creates nothing."""
    w = read_words(raw)
    t = match(connection, project_id, w, target_id)
    detected = "elsewhere" if w["tool"] else ("project" if t else "url" if w["url"] else
                                              "project")
    k = kind if kind in KINDS else detected
    out: dict[str, Any] = {"kind": k, "detected": detected, "kind_words": KIND_WORDS[k],
                           "words": w, "apps": [
                               {"target_id": x["id"], "label": f"{x['app_name']} · "
                                f"{x['environment']}", "env": x["environment"],
                                "address": _address(x)} for x in targets(connection, project_id)]}
    if k != "project" or t is None:
        out.update({"target": None, "watches": [], "boxes": None,
                    "word": ("No app of this project yet: deploy it first, or wire it yourself"
                             if t is None and k == "project" else
                             "This one is wired by hand: Wire it myself keeps every path")})
        return out
    subj = _subject(connection, t)
    existing = {r["kind"]: r for r in aa.rules(connection, t["id"])} if connection.execute(sql(
        "SELECT 1 FROM app_alert_rules WHERE subject_id = :t LIMIT 1"), {"t": t["id"]}).first() \
        else {}
    from_words = {m["kind"]: m["words"] for m in w["matters"]}
    kinds = list(DEFAULT_WATCHES) + [k2 for k2 in from_words if k2 not in DEFAULT_WATCHES]
    watches = []
    for kd in kinds:
        have = existing.get(kd)
        rule = have if have and have.get("source") == "console" else _proposed_rule(
            t, subj, kd, (have or {}).get("params"), int((have or {}).get("for_seconds") or 0))
        if kd == "error_rate" and kd in from_words and not (have and have.get("source") ==
                                                            "console"):
            # A failure the person named is watched closer than the default 5%.
            rule = _proposed_rule(t, subj, kd, {"threshold": 2.0})
        tone = WATCH[kd]["tone"]
        if kd in from_words and kd == "latency_p95":
            tone = "tell"
        watches.append(watch_view(connection, t, rule, tone=tone, words=from_words.get(kd),
                                  now=now))
    sig = signals(connection, t)
    col = collector(connection, project_id)
    on_call = aa.on_call_agent(connection, project_id) or {"role_key": "sre", "role": "SRE"}
    tell = _tell_box(connection, project_id, w["channel"])
    bnd = boundary(t)
    out.update({
        "target": {"target_id": t["id"], "app": t["app_name"], "env": t["environment"],
                   "label": f"{t['app_name']} · {t['environment']}", "address": _address(t),
                   "prod": bnd["prod"]},
        "signals": sig, "collector": col, "watches": watches,
        "question": {"key": "night", "ask": "At night, wake you or wait until 08:00?",
                     "options": [{"key": k2, "label": NIGHT_WORDS[k2]} for k2 in NIGHT],
                     "answer": w["night"]},
        "on_call": {"agent": on_call.get("role") or "SRE", "role_key": "sre",
                    "alone": not bnd["prod"],
                    "word": "triage + fix alone" if not bnd["prod"] else "triage · waits for you"},
        "tell": tell, "boundary": bnd,
        "runbooks": sorted({wv["runbook"] for wv in watches if wv.get("runbook")}),
        "cost": "≈ $0.20 a day · first data ≈ 1 min",
        "watching": bool(connection.execute(sql("SELECT 1 FROM app_watches WHERE target_id = :t"),
                                            {"t": t["id"]}).first()),
    })
    return out


def _tell_box(connection, project_id: str, channel: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    you = notify.owner_email(connection, project_id)
    out = {"you": you or "the console", "channel": dict(channel) if channel else None,
           "outside": bool(channel), "verified": False, "needs_url": False}
    if channel:
        row = connection.execute(sql(
            "SELECT id, verified_at, secret_ref FROM contact_points WHERE project_id = :p AND "
            "kind = :k AND name = :n ORDER BY created_at DESC LIMIT 1"),
            {"p": project_id, "k": channel["kind"], "n": channel["name"]}).mappings().first()
        out["verified"] = bool(row and row["verified_at"])
        out["contact_point_id"] = row["id"] if row else None
        out["needs_url"] = channel["kind"] in ("slack_webhook", "mattermost", "webhook") and not (
            row and row["secret_ref"])
    return out


# ── Start watching ────────────────────────────────────────────────────────────────────────

def _contact(connection, settings: Any, project_id: str, tell: Mapping[str, Any], *,
             actor: str) -> Optional[dict[str, Any]]:
    """The outside channel: the one already made for this name, or a new one (a webhook URL
    goes to the secret store, never back). Unverified, it gets a test message with a code."""
    kind, name = str(tell.get("kind") or ""), str(tell.get("name") or "").strip()
    if not kind:
        return None
    if kind not in ("slack_webhook", "mattermost", "webhook", "email"):
        raise WatchRefused("Tell is your inbox, Slack, Mattermost, a webhook or an email address")
    row = connection.execute(sql(
        "SELECT * FROM contact_points WHERE project_id = :p AND kind = :k AND name = :n "
        "ORDER BY created_at DESC LIMIT 1"), {"p": project_id, "k": kind, "n": name or kind}
    ).mappings().first()
    if row is None or (kind != "email" and not row["secret_ref"]):
        url, address = tell.get("url"), tell.get("address") or (name if kind == "email" else None)
        if kind != "email" and not url:
            raise WatchRefused(f"Paste {name or 'the channel'}'s incoming webhook URL in Tell, "
                               "or tell only you")
        try:
            made = notify.create_contact_point(connection, settings, project_id, kind=kind,
                                               name=name or kind, address=address, url=url,
                                               actor=actor)
        except ValueError as exc:
            raise WatchRefused(str(exc)) from None
        cp_id = made["contact_point"]["id"]
        verified = False
    else:
        cp_id, verified = row["id"], bool(row["verified_at"])
    if not verified:
        notify.send_test(connection, project_id, cp_id, actor=actor)
    return {"id": cp_id, "verified": verified, "name": name or kind, "kind": kind}


def start(connection, settings: Any, project_id: str, *, actor: str,
          spec: Mapping[str, Any], now: Optional[datetime] = None) -> dict[str, Any]:
    """Start watching: the app's rules set from the watches (one update_rule each, audited with
    the person's words as the why), the outside channel made and sent its code, each watch
    routed (console + you, and the channel), the night answered with quiet hours, and the
    app_watches row the landing reads. Respond automatically on a non-prod app asks for the
    person's standing grant (ConsentRequired → 409) before anything is written."""
    now = now or datetime.now(timezone.utc)
    t = _target(connection, project_id, str(spec.get("target_id") or ""))
    watches = list(spec.get("watches") or [])
    if not any(w.get("enabled", True) for w in watches):
        raise WatchRefused("Keep at least one watch: an app with none is not watched")
    night = str(spec.get("night") or "wake")
    if night not in NIGHT:
        raise WatchRefused("At night is Wake me, Wait till 08:00, or Fix alone, tell me")
    prod = _is_prod(t["environment"])
    alone = bool(spec.get("alone", True)) and not prod
    words = str(spec.get("words") or "")[:2000]
    reason = (str(spec.get("reason") or "").strip() or f"Watch this app: {words}".strip())[:500]
    if alone:
        # FIXING ALONE IS THE incident_response WORKFLOW (app_alerts._respond starts it): it is
        # installed from the catalogue when it is not here yet, as a boot's seed would.
        from .workflow_catalogue import seed as seed_catalogue

        seed_catalogue(connection, project_id, actor=actor, only=frozenset({aa.RESPONSE_WORKFLOW}))
    rules = {r["kind"]: r for r in aa.ensure_rules(connection, t) if r["kind"] != "custom"}
    wanted: dict[str, Mapping[str, Any]] = {}
    for w in watches:
        kd = str(w.get("kind") or "")
        if kd not in aa.KINDS:
            raise WatchRefused(f"BOSS has no watch called {kd or 'that'}")
        wanted[kd] = w
    applied = []
    try:
        for kd, rule in rules.items():
            w = wanted.get(kd)
            if w is None or not w.get("enabled", True):
                if kd in wanted or (spec.get("removed") and kd in spec["removed"]):
                    aa.update_rule(connection, t, rule["id"], {"enabled": False}, actor=actor,
                                   reason=reason, now=now)
                continue
            tone = "wake" if w.get("tone") == "wake" else "tell"
            change: dict[str, Any] = {
                "enabled": True, "triage": True, "auto_respond": alone,
                "severity": "critical" if tone == "wake" else "warning",
                "urgency": "high" if tone == "wake" and night == "wake" else "low"}
            params = {k: v for k, v in (w.get("params") or {}).items()
                      if k in aa.PARAM_BOUNDS.get(kd, {})}
            if params:
                change["params"] = params
            if w.get("for_s") is not None:
                change["for"] = f"{int(w['for_s'])}s"
            applied.append(aa.update_rule(connection, t, rule["id"], change, actor=actor,
                                          reason=reason, now=now))
    except aa.RuleRefused as refused:
        raise WatchRefused(str(refused)) from None
    tell = dict(spec.get("tell") or {})
    cp = _contact(connection, settings, project_id, tell, actor=actor) if tell.get("kind") \
        else None
    root = notify.ensure_root(connection, project_id)
    base = list(json.loads(root["contact_point_ids"]) if isinstance(root["contact_point_ids"], str)
                else root["contact_point_ids"] or [])
    quiet = {**QUIET, "timezone": str(spec.get("timezone") or "UTC")} if night != "wake" else None
    for r in applied:
        notify.set_rule_notify(connection, project_id, r["id"],
                               base + ([cp["id"]] if cp else []), actor=actor, quiet_hours=quiet)
    plan = {"watches": [{"kind": r["kind"], "rule_id": r["id"],
                         "tone": "wake" if (wanted[r["kind"]].get("tone") == "wake") else "tell",
                         "words": wanted[r["kind"]].get("words")} for r in applied],
            "signals": spec.get("signals"), "tell": {k: v for k, v in tell.items() if k != "url"}}
    connection.execute(sql(
        "INSERT INTO app_watches (target_id, project_id, words, night, alone, tell_cp_id, plan, "
        " started_by, started_at, updated_at) VALUES (:t, :p, :w, :n, :a, :cp, "
        " CAST(:plan AS JSONB), :by, :now, :now) ON CONFLICT (target_id) DO UPDATE SET "
        " words = EXCLUDED.words, night = EXCLUDED.night, alone = EXCLUDED.alone, "
        " tell_cp_id = EXCLUDED.tell_cp_id, plan = EXCLUDED.plan, updated_at = EXCLUDED.updated_at"),
        {"t": t["id"], "p": project_id, "w": words, "n": night, "a": alone,
         "cp": cp["id"] if cp else None, "plan": json.dumps(plan, default=str), "by": actor,
         "now": now})
    mon.audit(connection, project_id, "app_watch", t["id"], "started",
              after={"watches": [p["kind"] for p in plan["watches"]], "night": night,
                     "alone": alone, "tell": cp["name"] if cp else None},
              actor=actor, reason=reason, at=now)
    return landing(connection, settings, project_id, t["id"], now=now)


def change(connection, project_id: str, target_id: str, rule_id: str, *, actor: str,
           reason: str, edit: Mapping[str, Any]) -> dict[str, Any]:
    """A later edit of one watch (the popover's Done): the same update_rule, and the why is
    required, as the rule sheet keeps it."""
    if len((reason or "").strip()) < 3:
        raise WatchRefused("Say why in a few words: every later change keeps its reason")
    t = _target(connection, project_id, target_id)
    ch: dict[str, Any] = {}
    if edit.get("remove"):
        ch["enabled"] = False
    if edit.get("params"):
        ch["params"] = dict(edit["params"])
    if edit.get("for_s") is not None:
        ch["for"] = f"{int(edit['for_s'])}s"
    if edit.get("tone") in ("wake", "tell"):
        ch["severity"] = "critical" if edit["tone"] == "wake" else "warning"
        ch["urgency"] = "high" if edit["tone"] == "wake" else "low"
    if not ch:
        raise WatchRefused("Nothing to change")
    try:
        aa.update_rule(connection, t, rule_id, ch, actor=actor, reason=reason.strip())
    except aa.RuleRefused as refused:
        raise WatchRefused(str(refused)) from None
    except LookupError as missing:
        raise WatchRefused(str(missing), 404) from None
    return landing(connection, None, project_id, target_id)


def verify_tell(connection, project_id: str, target_id: str, code: str, *,
                actor: str) -> dict[str, Any]:
    cp_id = _watch_row(connection, project_id, target_id).get("tell_cp_id")
    if not cp_id:
        raise WatchRefused("This app tells only you: nothing to verify")
    if not re.fullmatch(r"\s*\d{6}\s*", code or ""):
        raise WatchRefused("The code is the 6 digits in the test message")
    try:
        notify.verify(connection, project_id, cp_id, code, actor=actor)
    except ValueError as exc:
        raise WatchRefused(str(exc)) from None
    return landing(connection, None, project_id, target_id)


def resend(connection, project_id: str, target_id: str, *, actor: str) -> dict[str, Any]:
    cp_id = _watch_row(connection, project_id, target_id).get("tell_cp_id")
    if not cp_id:
        raise WatchRefused("This app tells only you: nothing to verify")
    notify.send_test(connection, project_id, cp_id, actor=actor)
    return landing(connection, None, project_id, target_id)


def _watch_row(connection, project_id: str, target_id: str) -> dict[str, Any]:
    row = connection.execute(sql("SELECT * FROM app_watches WHERE target_id = :t AND "
                                 "project_id = :p"), {"t": target_id, "p": project_id}
                             ).mappings().first()
    if not row:
        raise WatchRefused("This app is not watched yet", 404)
    return dict(row)


# ── where it lands ────────────────────────────────────────────────────────────────────────

def landing(connection, settings: Any, project_id: str, target_id: str, *,
            now: Optional[datetime] = None) -> dict[str, Any]:
    """The Health page's watched card: the diagram (read → collector → first data → watches →
    verify the channel → on call), the Now line, the one gate, the watches as they stand."""
    now = now or datetime.now(timezone.utc)
    w = _watch_row(connection, project_id, target_id)
    t = _target(connection, project_id, target_id)
    plan = w["plan"] if isinstance(w["plan"], dict) else json.loads(w["plan"] or "{}")
    tone = {p["rule_id"]: p.get("tone", "tell") for p in plan.get("watches") or []}
    words = {p["rule_id"]: p.get("words") for p in plan.get("watches") or []}
    rules = [r for r in aa.rules(connection, target_id) if r["enabled"] and r["kind"] != "custom"
             and r["id"] in tone]
    sig = signals(connection, t)
    col = collector(connection, project_id)
    firing = [r for r in rules if r.get("last_state") == "firing"]
    cp = None
    if w.get("tell_cp_id"):
        row = connection.execute(sql("SELECT id, name, kind, verified_at FROM contact_points "
                                     "WHERE id = :i"), {"i": w["tell_cp_id"]}).mappings().first()
        cp = dict(row) if row else None
    first_data = sig["metrics"] or sig["probes"] > 0
    steps = [
        {"key": "read", "label": "Read", "state": "done", "detail": sig["value"]},
        {"key": "collector", "label": "Collector", "state": "done", "detail": col["value"]},
        {"key": "data", "label": "First data", "state": "done" if first_data else "now",
         "detail": (f"{sig['families']} families" if sig["metrics"] else
                    f"{sig['probes']} probes" if sig["probes"] else "waiting")},
        {"key": "watches", "label": f"{len(rules)} watches", "state": "done",
         "detail": f"{len(firing)} firing" if firing else "all quiet"},
    ]
    gate = None
    if cp:
        verified = bool(cp["verified_at"])
        steps.append({"key": "verify", "label": f"Verify {cp['name']}",
                      "state": "done" if verified else "you",
                      "detail": "verified" if verified else "only you"})
        if not verified:
            gate = {"contact_point_id": cp["id"], "name": cp["name"], "kind": cp["kind"],
                    "word": f"Beacon sent a 6-digit code to {cp['name']}. Sending outside your "
                            "own inbox is yours to allow."}
    steps.append({"key": "oncall", "label": "On call", "state": "run", "detail": "from now"})
    bnd = boundary(t)
    from .app_metrics import INCIDENT_PREFIX

    incidents = [dict(r) for r in connection.execute(sql(
        "SELECT id, title, status, first_seen AS created_at FROM incidents WHERE project_id = :p "
        "AND (dedupe_key LIKE :k OR dedupe_key = :serving) ORDER BY first_seen DESC LIMIT 3"),
        {"p": project_id, "k": f"app_alert:{target_id}:%",
         "serving": INCIDENT_PREFIX + target_id}).mappings().all()]
    if firing:
        now_text = f"{len(firing)} watch{'es' if len(firing) > 1 else ''} firing · the SRE agent " \
                   f"is on it" + ("" if w["alone"] else "; a fix waits for you")
        tone_now = "amber"
    elif gate:
        now_text = f"Learning a 7-day baseline · you hear in the console now; {gate['name']} " \
                   "waits for the code"
        tone_now = "accent"
    else:
        now_text = "All quiet · the SRE agent is on call" + (
            " and fixes what it can alone" if w["alone"] else ", triaging; fixes wait for you")
        tone_now = "green"
    return {
        "target_id": target_id, "label": f"{t['app_name']} · {t['environment']}",
        "app": t["app_name"], "env": t["environment"], "night": w["night"],
        "night_word": NIGHT_WORDS[w["night"]], "alone": bool(w["alone"]), "steps": steps,
        "now": {"text": now_text, "tone": tone_now}, "gate": gate, "boundary": bnd,
        "tell": ({"name": cp["name"], "kind": cp["kind"], "verified": bool(cp["verified_at"])}
                 if cp else None),
        "watches": [watch_view(connection, t, r, tone=tone.get(r["id"], "tell"),
                               words=words.get(r["id"]), now=now, with_backtest=False)
                    for r in rules],
        "incidents": [{"id": i["id"], "title": i["title"], "status": i["status"],
                       "at": mon.iso(i["created_at"]), **_said(connection, i["id"])}
                      for i in incidents],
        "started_at": mon.iso(w["started_at"]), "words": w["words"],
    }


def _said(connection, incident_id: str) -> dict[str, Any]:
    """What the on-call agent decided and what its response did, from the incident's newest
    alert (app_alerts.triage / response_word): the flow's words."""
    row = connection.execute(sql(
        "SELECT triage, triage_state, response_word, response_run_id FROM app_alerts "
        "WHERE incident_id = :i ORDER BY fired_at DESC NULLS LAST LIMIT 1"), {"i": incident_id}
    ).mappings().first()
    if not row:
        return {"triage": None, "response": None, "response_run_id": None}
    t = row["triage"] if isinstance(row["triage"], dict) else json.loads(row["triage"] or "{}")
    return {"triage": (t or {}).get("word") or (row["triage_state"] or None),
            "response": row["response_word"], "response_run_id": row["response_run_id"]}


def landings(connection, project_id: str) -> list[dict[str, Any]]:
    """Every watched app of the project, for the Health read."""
    ids = [r[0] for r in connection.execute(sql(
        "SELECT target_id FROM app_watches WHERE project_id = :p ORDER BY started_at"),
        {"p": project_id}).all()]
    out = []
    for tid in ids:
        try:
            out.append(landing(connection, None, project_id, tid))
        except WatchRefused:
            continue
    return out
