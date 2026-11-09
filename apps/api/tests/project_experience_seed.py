"""A realistic project for the Project experience (BOSSAPP-913/914): HelpDesk AI Demo-like.

NON-PRODUCTION ONLY. Used by the API tests and by scripts/seed-project-experience.py (which
refuses a database that is not a test or dev one). It writes the rows BOSS itself writes, so the
console and the tester see exactly what a live project shows:

  * a primary repository (`acme/helpdesk-ai-demo`) with its access checks,
  * 3 merged pull requests (PR #1 SLA timers, PR #2 UI refresh, PR #4 ticket export), each a
    completed Minor Feature run with its build,
  * 1 running build (Search filters, at its build step),
  * 2 things that need a person: Merge PR #3 (a pull-request decision) and a deploy approval,
  * a healthy staging deploy on a verified host, with probe samples and a demo login password,
    whose boss.deploy.yaml names the demo users customer@ / agent@ / admin@demo.test,
  * 2 open roadmap recommendations, and the repository's dossier,
  * one build with its retries and three stacked checks-failing follow-ups, the newest stuck
    `repairing` at phase 'done' (seed_followup_chain): one row, Stalled, attempts inside.

Addresses are documentation ranges (203.0.113.0/24): the public tree names no private host.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text

REPO = "acme/helpdesk-ai-demo"
HOST_ADDRESS = "203.0.113.10"
STAGING_URL = f"http://{HOST_ADDRESS}:8004"
DEMO_USERS = [{"user": "customer@demo.test", "label": "Customer", "password": "DEMO_PASSWORD"},
              {"user": "agent@demo.test", "label": "Agent", "password": "DEMO_PASSWORD"},
              {"user": "admin@demo.test", "label": "Admin", "password": "DEMO_PASSWORD"}]
TREE = ["package.json", "pnpm-lock.yaml", "vite.config.ts", "README.md", "boss.deploy.yaml",
        ".github/workflows/ci.yml", "server/index.mjs", "server/sla.mjs", "server/seed.mjs",
        "web/src/App.tsx", "web/src/pages/Login.tsx", "tests/sla.test.mjs", "tests/api.test.mjs"]


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _minor_feature(db, project_id: str) -> tuple[str, str, dict[str, Any]]:
    row = db.execute(text(
        "SELECT d.id AS d, v.id AS v, v.spec FROM workflow_definitions d JOIN workflow_versions v "
        "ON v.id = d.active_version_id WHERE d.project_id = :p AND d.workflow_key = 'minor_feature' "
        "LIMIT 1"), {"p": project_id}).mappings().first()
    if row is None:
        raise RuntimeError("the project has no Minor Feature workflow (seed it like a new project)")
    spec = row["spec"] if isinstance(row["spec"], dict) else json.loads(row["spec"])
    return row["d"], row["v"], spec


def _caps(spec: dict[str, Any]) -> list[tuple[str, str, str]]:
    out = []
    for step in spec.get("steps") or []:
        binding = step.get("binding") if isinstance(step.get("binding"), dict) else {}
        out.append((str(step.get("id")), str(step.get("kind") or "capability"),
                    str(binding.get("capability") or "")))
    return out


def _run(db, project_id: str, did: str, vid: str, spec: dict[str, Any], *, title: str,
         status: str, upto: str, at: datetime, pr: int = 0, gate: str = "",
         gate_kind: str = "", env: str = "staging", merge_waits: bool = False,
         prd_by: Optional[str] = "ceo") -> dict[str, Any]:
    """One Minor Feature run: steps before `upto` succeeded, `upto` in `status`-dependent state,
    the rest pending. `gate` makes `upto` a waiting step with an open decision."""
    rid = _id("wfr")
    db.execute(text(
        "INSERT INTO workflow_runs (id, project_id, workflow_definition_id, workflow_version_id, "
        "actor, status, input, created_at, updated_at, completed_at) VALUES (:i, :p, :d, :v, "
        "'ceo', :s, CAST(:in AS JSONB), :t, :t, :c)"),
        {"i": rid, "p": project_id, "d": did, "v": vid, "s": status,
         "in": json.dumps({"title": title, "directive": title, "repository": REPO}),
         "t": at, "c": at + timedelta(minutes=40) if status == "completed" else None})
    pr_url = f"https://github.com/{REPO}/pull/{pr}" if pr else None
    reached = False
    decision_id = None
    for sid, kind, cap in _caps(spec):
        if status == "completed":
            st = "succeeded"
        elif sid == upto:
            reached = True
            st = "waiting" if gate else "running"
        else:
            st = "pending" if reached else "succeeded"
        output: dict[str, Any] = {}
        if pr_url and cap in ("build", "review", "test", "preview") and st == "succeeded":
            output["pull_request_url"] = pr_url
        if cap == "meeting" and st == "succeeded" and prd_by:
            # THE PRD A PERSON APPROVED, as the runner records it after holding for the spec
            # (workflow_capability_runner: spec_decision_id while it waits, spec_approved_by once
            # approved) — the work row's D station and the drawer's "You approved" read it.
            spec_dec = _id("dec")
            db.execute(text(
                "INSERT INTO decisions (id, project_id, question, recommendation, status, "
                "approved_by, approved_at, created_at) VALUES (:i, :p, :q, CAST(:r AS JSONB), "
                "'approved', :by, :at, :t)"),
                {"i": spec_dec, "p": project_id, "q": f"Approve the PRD: {title}?", "by": prd_by,
                 "at": at + timedelta(minutes=4), "t": at + timedelta(minutes=3),
                 "r": json.dumps({"action": "approve_spec", "workflow_run_id": rid})})
            output.update(spec_decision_id=spec_dec, spec_approved_by=prd_by)
        did_dec = None
        if sid == upto and gate:
            did_dec = decision_id = _id("dec")
            db.execute(text(
                "INSERT INTO decisions (id, project_id, question, recommendation, status, risk_tier, "
                "created_at) VALUES (:i, :p, :q, CAST(:r AS JSONB), 'pending', 2, :t)"),
                {"i": did_dec, "p": project_id, "q": gate, "t": at + timedelta(minutes=30),
                 "r": json.dumps({"action": gate_kind or "approve_workflow_step",
                                  "workflow_run_id": rid, "step_id": sid, "capability": cap,
                                  "environment": env,
                                  **({"pull_request_url": pr_url} if pr_url else {})})})
        db.execute(text(
            "INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, step_id, step_kind, "
            "status, output, decision_id, started_at, completed_at) VALUES (:i, :p, :r, :s, :k, "
            ":st, CAST(:o AS JSONB), :d, :t, :c)"),
            {"i": _id("wsr"), "p": project_id, "r": rid, "s": sid, "k": kind, "st": st,
             "o": json.dumps(output), "d": did_dec, "t": at,
             "c": at + timedelta(minutes=5) if st == "succeeded" else None})
    build_id = _id("crun")
    merge_id = None
    if merge_waits:
        # The build's own pull-request decision, as the build path raises it: "Merge PR #n?"
        merge_id = decision_id = _id("dec")
        db.execute(text(
            "INSERT INTO decisions (id, project_id, question, recommendation, status, risk_tier, "
            "created_at) VALUES (:i, :p, :q, CAST(:r AS JSONB), 'pending', 2, :t)"),
            {"i": merge_id, "p": project_id, "q": f"Merge PR #{pr}: {title}?",
             "t": at + timedelta(minutes=20),
             "r": json.dumps({"action": "review_pull_request", "pull_request_url": pr_url})})
    db.execute(text(
        "INSERT INTO code_task_runs (id, project_id, status, phase, title, pull_request_url, "
        "pull_request_number, decision_id, finalize_ctx, created_at, updated_at) VALUES (:i, :p, "
        ":s, :ph, :t, :u, :n, :d, CAST(:f AS JSONB), :at, :at)"),
        {"i": build_id, "p": project_id, "s": "completed" if pr else "running",
         "ph": "done" if pr else "coding", "t": title, "u": pr_url, "n": pr or None,
         "d": merge_id, "f": json.dumps({"workflow_run_id": rid}), "at": at + timedelta(minutes=2)})
    db.execute(text(
        "INSERT INTO llm_calls (id, project_id, code_task_run_id, stage, model, cost_usd_micros, "
        "created_at) VALUES (:i, :p, :b, 'build', 'claude-sonnet', :c, :t)"),
        {"i": _id("llm"), "p": project_id, "b": build_id, "c": 2_100_000,
         "t": at + timedelta(minutes=3)})
    return {"run_id": rid, "build_id": build_id, "decision_id": decision_id, "pr_url": pr_url}


CHAIN_TITLE = "AML flagged line by kind"


def seed_followup_chain(db, project_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """One build and everything that came after it, as the build path writes them: the build's
    repair round handed to a retry, whose PR opened with checks failing; a checks-failing
    follow-up, its retry, a follow-up of THAT (title leads stacked), its retry, and a third
    follow-up left `repairing` at phase 'done' two days ago with no job — the stuck row the Work
    tab once showed as Running, nineteen times over.

    root ─retry→ r1 (PR #6) ─follow-up→ f1 ─retry→ f1r ─follow-up→ f2 ─retry→ f2r ─follow-up→ f3
    """
    now = now or datetime.now(timezone.utc)
    lead = "Follow-up: checks failing — "
    t0 = now - timedelta(days=3)
    ids: dict[str, str] = {}

    def build(key: str, title: str, *, status: str, at: datetime, parent: str = "",
              attempt: int = 0, deferred: str = "", pr: int = 0, cost: int = 0,
              updated: datetime | None = None) -> str:
        rid = ids[key] = _id("crun")
        root = ids.get("root", rid)
        fctx: dict[str, Any] = {}
        if parent:
            fctx.update({"parent_run_id": ids[parent], "root_run_id": root})
        if attempt:
            fctx["attempt"] = attempt
        if deferred:
            fctx.update({"deferred_from_run": ids[deferred], "is_followup": True})
        if status == "repairing":
            fctx.update({"failing_gates": ["tests_pass"], "review_repair_round": 1})
        pr_url = f"https://github.com/{REPO}/pull/{pr}" if pr else None
        db.execute(text(
            "INSERT INTO code_task_runs (id, project_id, status, phase, title, pull_request_url, "
            "pull_request_number, finalize_ctx, created_at, updated_at) VALUES (:i, :p, :s, "
            "'done', :t, :u, :n, CAST(:f AS JSONB), :at, :up)"),
            {"i": rid, "p": project_id, "s": status, "t": title, "u": pr_url, "n": pr or None,
             "f": json.dumps(fctx), "at": at, "up": updated or at + timedelta(minutes=40)})
        if cost:
            db.execute(text(
                "INSERT INTO llm_calls (id, project_id, code_task_run_id, stage, model, "
                "cost_usd_micros, created_at) VALUES (:i, :p, :b, 'build', 'claude-sonnet', :c, :t)"),
                {"i": _id("llm"), "p": project_id, "b": rid, "c": cost, "t": at})
        return rid

    build("root", CHAIN_TITLE, status="repairing", at=t0, cost=1_400_000)
    build("r1", f"{CHAIN_TITLE} · retry 1", status="completed", at=t0 + timedelta(hours=1),
          parent="root", attempt=2, pr=6, cost=900_000)
    build("f1", f"{lead}{CHAIN_TITLE}", status="repairing", at=t0 + timedelta(hours=2),
          parent="r1", deferred="r1", cost=700_000)
    # A retry of a follow-up, as retries were written before they carried the follow-up mark.
    build("f1r", f"{lead}{CHAIN_TITLE} · retry 1", status="completed",
          at=t0 + timedelta(hours=3), parent="f1", attempt=2, cost=500_000)
    build("f2", f"{lead}{lead}{CHAIN_TITLE}", status="repairing", at=t0 + timedelta(hours=4),
          parent="f1r", deferred="f1r", cost=600_000)
    build("f2r", f"{lead}{lead}{CHAIN_TITLE} · retry 1", status="completed",
          at=t0 + timedelta(hours=5), parent="f2", attempt=2, cost=400_000)
    build("f3", f"{lead}{lead}{lead}{CHAIN_TITLE}", status="repairing",
          at=now - timedelta(days=2), parent="f2r", deferred="f2r", cost=300_000,
          updated=now - timedelta(days=2) + timedelta(minutes=30))
    return dict(ids)


ALERT_RULES = [
    # (kind, alert, expr, params, severity label, summary, source)
    ("error_rate", "AppErrorRate", 'sum(rate(http_requests_total{app="helpdesk-ai-demo",status=~"5.."}[5m])) '
     '/ sum(rate(http_requests_total{app="helpdesk-ai-demo"}[5m])) * 100 > 5',
     {"threshold": 5, "window_minutes": 5, "min_requests": 20}, "sev1", None, "yaml"),
    ("latency_p95", "AppLatencyP95", 'histogram_quantile(0.95, sum by (le) (rate('
     'http_request_duration_seconds_bucket{app="helpdesk-ai-demo"}[5m]))) * 1000 > 800',
     {"threshold": 800, "window_minutes": 5}, "sev3", None, "yaml"),
    ("health_check", "AppHealthCheckFailing", 'sum(count_over_time(probe_success{app="helpdesk-ai-demo"}[10m])) '
     '- sum(sum_over_time(probe_success{app="helpdesk-ai-demo"}[10m])) >= 3',
     {"min_failures": 3, "window_minutes": 10}, "sev1", None, "yaml"),
    ("custom", "TicketsApi5xx", 'sum(rate(http_requests_total{app="helpdesk-ai-demo",route="/api/tickets",'
     'status=~"5.."}[5m])) > 0.5', {}, "sev1", "5xx on /api/tickets", "yaml"),
    ("custom", "HostDiskFilling", 'predict_linear(node_filesystem_avail_bytes{app="helpdesk-ai-demo"}[6h], '
     '24*3600) < 0', {}, "sev3", "Disk filling up", "yaml"),
    ("custom", "PgConnectionsHigh", 'pg_stat_activity_count{app="helpdesk-ai-demo"} / '
     'pg_settings_max_connections > 0.8', {}, "sev3", "DB connections high", "yaml"),
    ("custom", "TlsCertExpiring", '(probe_ssl_earliest_cert_expiry{app="helpdesk-ai-demo"} - time()) '
     '/ 86400 < 14', {}, "sev3", "TLS cert expiring", "yaml"),
]


def seed_alert_history(db, project_id: str, target_id: str, *, now: datetime | None = None
                       ) -> dict[str, Any]:
    """Health (screen 12) as a live project shows it: Beacon's pack (six rules from the repo's
    boss.deploy.yaml), the app's own metrics (requests, 5xx, p95 every 10 min for 24 h), two
    incidents the SRE squad worked — "5xx on /api/tickets" this morning (pool exhausted, pool
    10→20, recovered) and "Slow ticket list" (a missing index, the fix waiting on a person) — the
    alerts they came from, an older latency alert that cleared three days ago, and one of BOSS's
    OWN platform alerts (layer boss) that must never show on a project's Health."""
    from boss_engine.services.ambient import ledger

    now = now or datetime.now(timezone.utc)
    rules: dict[str, str] = {}
    for kind, alert_name, expr, params, sev, summary, source in ALERT_RULES:
        rid = _id("arule")
        rules[alert_name] = rid
        db.execute(text(
            "INSERT INTO app_alert_rules (id, project_id, target_id, kind, alert, expr, "
            "for_seconds, params, labels, annotations, source, managed_by, created_at) VALUES (:i, :p, "
            ":t, :k, :a, :e, 0, CAST(:pa AS JSONB), CAST(:l AS JSONB), CAST(:an AS JSONB), :s, "
            "'code', :at)"),
            {"i": rid, "p": project_id, "t": target_id, "k": kind, "a": alert_name, "e": expr,
             "pa": json.dumps(params), "l": json.dumps({"severity": sev}),
             "an": json.dumps({"summary": summary} if summary else {}), "s": source,
             "at": now - timedelta(days=5)})
    # The app's own metrics: 144 scrapes (24 h), a 5xx burst this morning, p95 ~180 ms.
    for i in range(144):
        burst = 2 <= i <= 4
        db.execute(text(
            "INSERT INTO app_metrics_scrapes (id, project_id, deployment_target_id, scraped_at, ok, "
            "requests, errors, p95_ms, p50_ms, interval_s) VALUES (:i, :p, :t, :at, true, :r, :e, "
            ":p95, :p50, 600)"),
            {"i": _id("ams"), "p": project_id, "t": target_id, "at": now - timedelta(minutes=10 * i),
             "r": 240 + (i * 13) % 60, "e": 9 if burst else (1 if i % 17 == 0 else 0),
             "p95": 150 + (i * 11) % 60 + (260 if burst else 0), "p50": 60 + (i * 5) % 20})

    def incident(title: str, *, at: datetime, status: str) -> str:
        iid = _id("inc")
        db.execute(text(
            "INSERT INTO incidents (id, project_id, dedupe_key, title, severity, status, first_seen, "
            "last_seen) VALUES (:i, :p, :d, :t, 'critical', :s, :at, :at)"),
            {"i": iid, "p": project_id, "d": f"seed:{iid}", "t": title, "s": status, "at": at})
        return iid

    def alert(alertname: str, kind: str, *, at: datetime, cleared: datetime | None,
              incident_id: str | None = None, value: float | None = None,
              labels: dict[str, Any] | None = None, summary: str | None = None) -> str:
        aid = _id("alr")
        db.execute(text(
            "INSERT INTO app_alerts (id, project_id, target_id, rule_id, rule_kind, "
            "fingerprint, alertname, labels, annotations, state, active_at, fired_at, cleared_at, "
            "incident_id, value, unit) VALUES (:i, :p, :t, :r, :k, :f, :a, CAST(:l AS JSONB), "
            "CAST(:an AS JSONB), :s, :at, :at, :c, :inc, :v, :u)"),
            {"i": aid, "p": project_id, "t": target_id, "r": rules.get(alertname), "k": kind,
             "f": uuid.uuid4().hex, "a": alertname,
             "l": json.dumps(labels or {"severity": "sev3" if kind == "latency_p95" else "sev1"}),
             "an": json.dumps({"summary": summary} if summary else {}),
             "s": "resolved" if cleared else "firing", "at": at, "c": cleared, "inc": incident_id,
             "v": value, "u": "ms" if kind == "latency_p95" else ""})
        return aid

    morning = (now - timedelta(minutes=95)).replace(second=0, microsecond=0)
    inc_5xx = incident("5xx on /api/tickets", at=morning, status="resolved")
    alert("TicketsApi5xx", "custom", at=morning, cleared=morning + timedelta(minutes=14),
          incident_id=inc_5xx, summary="5xx on /api/tickets")
    slow_at = now - timedelta(days=3, hours=2)
    inc_slow = incident("Slow ticket list", at=slow_at, status="remediating")
    alert("AppLatencyP95", "latency_p95", at=slow_at, cleared=slow_at + timedelta(hours=1),
          incident_id=inc_slow, value=1400)
    # BOSS's own evaluator lag: a platform alert, never the app's.
    alert("BossEvaluatorLag", "custom", at=now - timedelta(hours=5), cleared=now - timedelta(hours=4),
          labels={"layer": "boss", "severity": "critical", "boss_meta": "true"},
          summary="1 rule group(s) are evaluating late (more than twice their interval)")

    items: dict[str, str] = {}
    for key, title, life, inc, at, answers in (
            ("5xx", "5xx on /api/tickets", "resolved", inc_5xx, morning,
             [("What caused it?", "Pool exhausted"), ("How do we stop the bleeding?", "Pool 10→20"),
              ("Did it recover?", "Recovered")]),
            ("slow", "Slow ticket list", "needs_you", inc_slow, slow_at,
             [("What caused it?", "Missing index")])):
        wid = _id("wi")
        items[key] = wid
        db.execute(text(
            "INSERT INTO work_items (id, project_id, subject_type, subject_id, title, ambient, "
            "lifecycle, task_class, created_by, created_at, updated_at, resolved_at, app) VALUES "
            "(:i, :p, 'incident', :inc, :t, true, :l, 'incident_staging_mitigation', 'seed', :at, "
            ":up, CASE WHEN :l = 'resolved' THEN CAST(:up AS timestamptz) END, 'helpdesk-ai-demo')"),
            {"i": wid, "p": project_id, "inc": inc, "t": title, "l": life, "at": at,
             "up": at + timedelta(minutes=14)})
        db.execute(text(
            "INSERT INTO work_item_links (work_item_id, kind, ref, project_id) VALUES (:w, "
            "'incident', :r, :p)"), {"w": wid, "r": inc, "p": project_id})
        for question, chosen in answers:
            ledger.record_decision(db, project_id=project_id, decided_by="agent:sre",
                                   question=question, chosen=chosen, rationale=f"{chosen}.",
                                   reversibility="compensable", options=[chosen],
                                   evidence=[{"kind": "incident", "ref": inc}],
                                   work_item_id=wid, task_class="incident_staging_mitigation")
    return {"rules": rules, "incidents": {"5xx": inc_5xx, "slow": inc_slow}, "items": items}


def seed(db, project_id: str, *, now: datetime | None = None, connection_id: str | None = None
         ) -> dict[str, Any]:
    """Seed `project_id` (an app project created like any other) with the HelpDesk story."""
    from boss_engine.services.ambient import dossier

    now = now or datetime.now(timezone.utc)
    ok = {"ok": True, "state": "working"}
    if connection_id is None:
        connection_id = _id("ghc")
        db.execute(text(
            "INSERT INTO github_connections (id, project_id, connection_label, auth_method, "
            "credential_ref, status, created_by) VALUES (:c, :p, 'seed', 'personal_access_token', "
            "'secret:none', 'active', 'seed')"), {"c": connection_id, "p": project_id})
    binding = db.execute(text(
        "SELECT id FROM github_repository_bindings WHERE project_id = :p AND lower(full_name) = "
        "lower(:r)"), {"p": project_id, "r": REPO}).scalar()
    if binding is None:
        binding = _id("ghb")
        db.execute(text(
            "INSERT INTO github_repository_bindings (id, project_id, connection_id, owner, name, "
            "full_name, role, default_branch, checks, checked_at, created_by) VALUES (:i, :p, :c, "
            "'acme', 'helpdesk-ai-demo', :r, 'primary', 'main', CAST(:k AS JSONB), now(), 'seed')"),
            {"i": binding, "p": project_id, "c": connection_id, "r": REPO,
             "k": json.dumps({"read": ok, "write": ok, "pull_requests": ok, "state": "working"})})
    # THE MODEL the onboarding agent found connected ("Claude connected"), as Repo and models
    # stores a working subscription: the project's Ready card reads it LIVE (round 3, 10-20).
    db.execute(text(
        "INSERT INTO entitlement_sources (id, project_id, provider, source_id, source_kind, "
        "entitlement_kind, owner_type, owner_id, cost_mode, status, auth_method, "
        "connection_status, connection_metadata, observed_capacity, runtime_supported) VALUES "
        "(:i, :p, 'anthropic', 'anthropic_claude_code', 'official_cli', 'subscription', "
        "'workspace_operator', 'workspace_operator', 'subscription', 'declared', "
        "'official_cli_oauth', 'connected', CAST(:m AS JSONB), CAST(:c AS JSONB), true) "
        "ON CONFLICT (project_id, source_id) DO UPDATE SET connection_status = 'connected', "
        "connection_metadata = EXCLUDED.connection_metadata, "
        "observed_capacity = EXCLUDED.observed_capacity"),
        {"i": _id("es"), "p": project_id,
         "m": json.dumps({"last_check": {"ok": True, "state": "working",
                                         "at": now.isoformat()}}),
         "c": json.dumps({"credential_present": True, "credential_state": "ok"})})
    did, vid, spec = _minor_feature(db, project_id)
    caps = _caps(spec)
    by_cap = {cap: sid for sid, _k, cap in caps}
    build_step = by_cap.get("build") or caps[len(caps) // 2][0]
    deploy_step = by_cap.get("deploy") or build_step

    merged = []
    for n, (title, days) in enumerate((("SLA timers", 6), ("UI refresh", 4),
                                       ("Ticket export", 2)), start=1):
        number = n if n < 3 else 4
        got = _run(db, project_id, did, vid, spec, title=title, status="completed", upto="",
                   at=now - timedelta(days=days), pr=number)
        merge_dec = _id("dec")
        db.execute(text(
            "INSERT INTO decisions (id, project_id, question, recommendation, status, approved_by, "
            "approved_at, created_at) VALUES (:i, :p, :q, CAST(:r AS JSONB), 'approved', 'ceo', :t, :t)"),
            {"i": merge_dec, "p": project_id, "q": f"Merge PR #{number}: {title}?",
             "t": now - timedelta(days=days) + timedelta(hours=2),
             "r": json.dumps({"action": "review_pull_request", "pull_request_url": got["pr_url"]})})
        # The build's own pull-request decision, as the build path links it (the drawer's
        # "<who> approved · merged").
        db.execute(text("UPDATE code_task_runs SET decision_id = :d WHERE id = :b"),
                   {"d": merge_dec, "b": got["build_id"]})
        merged.append(got)
    running = _run(db, project_id, did, vid, spec, title="Ticket search filters", status="running",
                   upto=build_step, at=now - timedelta(minutes=12))
    merge = _run(db, project_id, did, vid, spec, title="Visual refresh finish", status="running",
                 upto=by_cap.get("report") or build_step, at=now - timedelta(hours=3), pr=3,
                 merge_waits=True)
    # (Email-to-ticket intake is the PLANNED next-up item of screens 04/05/07, not this run.)
    deploy = _run(db, project_id, did, vid, spec, title="Agent reply macros", status="waiting",
                  upto=deploy_step, at=now - timedelta(hours=1), pr=5,
                  gate="Deploy Agent reply macros to staging?", env="staging")

    host_id = _id("dhost")
    db.execute(text(
        "INSERT INTO deployment_hosts (id, project_id, name, address, ssh_user, status, created_by) "
        "VALUES (:i, :p, 'demo-host', :a, 'deploy', 'verified', 'seed')"),
        {"i": host_id, "p": project_id, "a": HOST_ADDRESS})
    target_id = _id("dept")
    db.execute(text(
        "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, "
        "target_ref, domains, status, created_by, host_id, public_url) VALUES (:i, :p, "
        "'helpdesk-ai-demo', :r, 'staging', 'host', :h, CAST(:d AS JSONB), 'active', 'seed', :h, :u)"),
        {"i": target_id, "p": project_id, "r": REPO, "h": host_id,
         "d": json.dumps([STAGING_URL]), "u": STAGING_URL})
    manifest = {"name": "helpdesk-ai-demo", "kind": "compose", "generated_env_keys": ["DEMO_PASSWORD",
                                                                                      "SESSION_SECRET"],
                "secret_env_keys": [], "demo_logins": DEMO_USERS}
    sdep = _id("sdep")
    db.execute(text(
        "INSERT INTO stack_deployments (id, project_id, target_id, host_id, repository, ref, "
        "commit_sha, manifest, status, open_url, created_by, created_at, finished_at, "
        "workflow_run_id) VALUES (:i, :p, :t, :h, :r, 'main', 'f3f130c9a1b2', CAST(:m AS JSONB), "
        "'healthy', :u, 'ceo', :at, :at, :w)"),
        {"i": sdep, "p": project_id, "t": target_id, "h": host_id, "r": REPO,
         "m": json.dumps(manifest), "u": STAGING_URL, "at": now - timedelta(days=2),
         "w": merged[-1]["run_id"]})
    for i in range(30):
        db.execute(text(
            "INSERT INTO deployment_health_samples (id, project_id, deployment_target_id, checked_at, "
            "ok, status_code, latency_ms) VALUES (:i, :p, :t, :at, true, 200, :ms)"),
            {"i": _id("dhs"), "p": project_id, "t": target_id,
             "at": now - timedelta(minutes=10 * i), "ms": 120 + (i * 7) % 90})

    recs = []
    for title, why, kind in (("Email-to-ticket intake", "Customers email support today; tickets "
                              "are retyped by hand.", "feature"),
                             ("Test the SLA engine", "server/sla.mjs changed 9 times in 30 days "
                              "and has 1 test.", "test_gap")):
        wid = _id("wi")
        fp = f"seed:{uuid.uuid4().hex[:8]}"
        db.execute(text(
            "INSERT INTO work_items (id, project_id, subject_type, subject_id, title, ambient, "
            "fingerprint, lifecycle, task_class, created_by) VALUES (:i, :p, 'roadmap', :s, :t, true, "
            ":f, 'proposed', 'roadmap_item', 'seed')"),
            {"i": wid, "p": project_id, "s": fp, "t": title, "f": fp})
        db.execute(text(
            "INSERT INTO ambient_recommendations (work_item_id, project_id, detector, kind, "
            "fingerprint, title, why, proposed_shape, score, priority_score) VALUES (:w, :p, "
            "'seed', :k, :f, :t, :y, 'ticket', :s, :s)"),
            {"w": wid, "p": project_id, "k": kind, "f": fp, "t": title, "y": why,
             "s": 80 if kind == "feature" else 60})
        recs.append(wid)

    chain = seed_followup_chain(db, project_id, now=now)
    alerting = seed_alert_history(db, project_id, target_id, now=now)
    planned = seed_plan(db, project_id, now=now)
    shipping = seed_ship_story(db, project_id, target_id=target_id, host_id=host_id,
                               merged=merged, merge=merge, live_sdep=sdep, now=now)
    built = dossier.build(db, project_id=project_id, repo=REPO, paths=list(TREE), actor="seed",
                          seed_signals=False)
    knowledge_story = seed_knowledge(db, project_id, binding_id=binding, merged=merged, now=now)
    seed_work_story(db, project_id, runs=[*merged, merge], now=now)
    settings_story = seed_settings_story(db, project_id, live_sdep=sdep, now=now)
    seed_project_sources(db, project_id, host_id=host_id, now=now)
    return {"knowledge": knowledge_story,
            "project_id": project_id, "binding_id": binding, "merged": merged, "running": running,
            "merge": merge, "deploy": deploy, "target_id": target_id, "host_id": host_id,
            "stack_deployment_id": sdep, "recommendations": recs, "dossier_id": built["id"],
            "chain": chain, "alerting": alerting, "shipping": shipping,
            "settings": settings_story, "plan": planned}

VISUAL_BRIEF = ("Finish the visual refresh the last change promised but did not deliver. The ticket "
                "list still looks plain. Do all of this in the existing web app: an app header "
                "with the HelpDesk name and navigation, and coloured priority and SLA chips.")
MERGE_SHAS = ["c1a7e0d4b2f94a1e8c3d", "6ba65005c585441eddfc", "f3f130c9a1b2"]


def seed_ship_story(db, project_id: str, *, target_id: str, host_id: str, merged: list,
                    merge: dict, live_sdep: str, now: datetime | None = None) -> dict[str, Any]:
    """How code shipped this week (screen 08), as the deploy path writes it:

      * each merged PR's recorded merge commit (pr_merge.record's finalize_ctx.merge.sha), so a
        deploy names "PR #2", not a hash;
      * the deploy history on staging: a first deploy that failed on a port clash 7 days ago,
        PR #1 and PR #2 live (with apply durations), then the live PR #4 (the seed's healthy one);
      * a SUPERSEDED request: PR #2's commit asked to ship again 3 days ago, still pending —
        BOSS withdraws it (cleared_causes: a later deploy went live), so it never shows in Needs
        you, and the run parked on it follows the live deploy;
      * the open brief of "Visual refresh finish" as the build's title, the way the start door
        used to cut it (80 characters of the brief): the page shows its short title.
    """
    now = now or datetime.now(timezone.utc)
    builds = [m["build_id"] for m in merged]
    for build_id, sha in zip(builds, MERGE_SHAS):
        db.execute(text(
            "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) || "
            "jsonb_build_object('merge', jsonb_build_object('sha', CAST(:s AS TEXT))) WHERE id = :i"),
            {"s": sha, "i": build_id})
    db.execute(text(
        "UPDATE code_task_runs SET title = :t, spec = CAST(:sp AS JSONB) WHERE id = :i"),
        {"t": VISUAL_BRIEF[:80], "sp": json.dumps({"instructions": VISUAL_BRIEF}),
         "i": merge["build_id"]})
    db.execute(text(
        "UPDATE workflow_runs SET input = input || jsonb_build_object('title', CAST(:t AS TEXT), "
        "'directive', CAST(:d AS TEXT)) WHERE id = :i"),
        {"t": VISUAL_BRIEF[:80], "d": VISUAL_BRIEF, "i": merge["run_id"]})
    db.execute(text("UPDATE stack_deployments SET commit_sha = :c, apply_started_at = created_at - "
                    "interval '3 minutes', finished_at = created_at WHERE id = :i"),
               {"c": MERGE_SHAS[2], "i": live_sdep})

    def deploy(commit: str, *, status: str, days: float, minutes: float = 3.0, reason: str = "",
               decision: str = "", run_id: str | None = None, sid: str = "") -> str:
        sid = sid or _id("sdep")
        at = now - timedelta(days=days)
        db.execute(text(
            "INSERT INTO stack_deployments (id, project_id, target_id, host_id, repository, ref, "
            "commit_sha, manifest, status, open_url, created_by, created_at, apply_started_at, "
            "finished_at, failure_reason, decision_id, workflow_run_id) VALUES (:i, :p, :t, :h, :r, "
            "'main', :c, CAST(:m AS JSONB), :s, :u, 'ceo', :at, :st, :fin, :why, :d, :w)"),
            {"i": sid, "p": project_id, "t": target_id, "h": host_id, "r": REPO, "c": commit,
             "m": json.dumps({"name": "helpdesk-ai-demo", "kind": "compose",
                              "generated_env_keys": ["DEMO_PASSWORD"], "demo_logins": DEMO_USERS}),
             "s": status, "u": STAGING_URL if status == "healthy" else None, "at": at,
             "st": None if status == "awaiting_approval" else at,
             "fin": None if status == "awaiting_approval" else at + timedelta(minutes=minutes),
             "why": reason or None, "d": decision or None, "w": run_id})
        return sid

    first = deploy("0a1b2c3d4e5f", status="failed", days=7, minutes=1.2,
                   reason="Port 8000 was taken on the host. Beacon moved the app to 8004.")
    pr1 = deploy(MERGE_SHAS[0], status="healthy", days=6, minutes=2 + 50 / 60)
    pr2 = deploy(MERGE_SHAS[1], status="healthy", days=4, minutes=3 + 10 / 60)
    # The overtaken request: PR #2 asked to ship AGAIN three days ago (before PR #4 went live
    # two days ago), parked on a workflow step and never answered.
    stale_decision, stale = _id("dec"), _id("sdep")
    db.execute(text(
        "INSERT INTO decisions (id, project_id, question, recommendation, status, risk_tier, "
        "created_at) VALUES (:i, :p, :q, CAST(:r AS JSONB), 'pending', 2, :t)"),
        {"i": stale_decision, "p": project_id, "t": now - timedelta(days=3),
         "q": f"Deploy {REPO}@{MERGE_SHAS[1][:12]} to {HOST_ADDRESS} (/opt/boss-apps/helpdesk)?",
         "r": json.dumps({"action": "deploy_stack", "deployment_id": stale, "target_id": target_id,
                          "repository": REPO, "commit": MERGE_SHAS[1], "host": HOST_ADDRESS,
                          "workflow_run_id": merged[1]["run_id"]})})
    deploy(MERGE_SHAS[1], status="awaiting_approval", days=3, decision=stale_decision,
           run_id=merged[1]["run_id"], sid=stale)
    # What BOSS does at boot and on every tick (cleared_causes): the overtaken request is
    # withdrawn and closed, so the story reads as a running install shows it.
    from boss_engine.services.cleared_causes import withdraw_cleared

    withdraw_cleared(db, project_id, actor="boss")
    return {"first": first, "pr1": pr1, "pr2": pr2, "superseded": stale,
            "superseded_decision": stale_decision}


#: The onboarding agent's ranked backlog (screen 10): issues, CI and scan findings, with scores.
BACKLOG = [
    ("Fix 6 failing tests", "ci", "bug", "red_main", "high", "ci:main", "CI fails on main: 6 tests."),
    ("SLA timer ignores weekends", "bug", "bug", "issue_triage", "medium", "issue:12",
     "#12 SLA timers keep counting through the weekend."),
    ("SLA timers per priority", "ticket", "feature", "issue_triage", "low", "issue:9",
     "#9 Each priority gets its own response deadline."),
    ("UI refresh", "roadmap", "feature", "todo_triage", "low", "todo:web", "7 TODOs in web/src."),
    ("Email-to-ticket intake", "ticket", "feature", "issue_triage", "low", "issue:14",
     "#14 Turn support emails into tickets."),
    ("Weekend SLA pause", "bug", "bug", "issue_triage", "medium", "issue:15",
     "#15 Pause the SLA clock outside business hours."),
    ("Reply suggestions", "ticket", "feature", "issue_triage", "low", "issue:16",
     "#16 Suggest replies from past tickets."),
    ("Status index", "roadmap", "chore", "missing_tests", "low", "scan:index",
     "tickets(status) has no index."),
    ("Patch express 4.17", "roadmap", "bug", "security_patch", "high", "osv:express",
     "express 4.17.1 has a known advisory."),
    ("Add a README", "roadmap", "chore", "doc_drift", "low", "scan:readme", "No README at the root."),
    ("Test sla.mjs", "roadmap", "chore", "missing_tests", "medium", "scan:sla",
     "server/sla.mjs changed 9 times and has 1 test."),
    ("Finish PR #7", "ticket", "ticket", "issue_triage", "low", "pr:7", "Open PR: ticket export CSV."),
]

#: Open recommendations with their evidence (screen 07's ranked cards).
RECOMMENDATIONS = [
    ("Regression test for SLA weekend pause", "incident_cause", "hardening",
     [{"kind": "incident", "ref": "inc_1"}, {"kind": "incident", "ref": "inc_2"},
      {"kind": "kg_node", "ref": "server/sla.mjs"}], ["incident", "code"], 1.9, 2, 2_000_000),
    ("Alert rule for /api/tickets 5xx", "design_drift", "design_fix",
     [{"kind": "commit", "ref": "a41c9e2"}], ["code"], 1.6, 1, 1_000_000),
    ("Agent reply suggestions (GPT)", "user_friction", "feature",
     [{"kind": "meeting", "ref": "mtg_1"}, {"kind": "meeting", "ref": "mtg_2"},
      {"kind": "decision", "ref": "dec_1"}], ["design"], 1.2, 3, 4_000_000),
    ("Index tickets(status, created_at)", "slow_stage", "process",
     [{"kind": "query", "ref": "list p95 1.4s"}], ["code"], 1.0, 1, 500_000),
]


def seed_plan(db, project_id: str, *, now: datetime | None = None, actor: str = "ceo"
              ) -> dict[str, Any]:
    """The Roadmap and Onboarding screens' own data, as BOSS writes it: a finished onboarding
    run with a 12-item ranked backlog (onboarding._candidate scores), three of them accepted
    onto Next and two moved to Later through the real accept / move paths (nothing starts),
    four more open recommendations with evidence, and one each rejected and snoozed."""
    from boss_engine.services.project_experience import onboarding, plan

    now = now or datetime.now(timezone.utc)
    cands = [onboarding._candidate(label, source=src, task_class=tc, severity=sev, kind=kind,
                                   detail=detail, ref=ref, score_source=src,
                                   # the GitHub link the agent read it from; the failing file
                                   external_url=(f"https://github.com/{REPO}/"
                                                 f"{'issues' if ref.startswith('issue:') else 'pull'}"
                                                 f"/{ref.split(':', 1)[1]}"
                                                 if ref.startswith(("issue:", "pr:")) else None),
                                   where="tests/test_notify.py" if src == "ci" else None)
             for label, src, kind, tc, sev, ref, detail in BACKLOG]
    cands.sort(key=lambda c: -c["score"])
    checklist = {
        "repo_access": {"key": "repo_access", "state": "done", "owner": "agent",
                        "detail": "Read, branch and pull requests work.", "sub": "read · branch · PR"},
        "models": {"key": "models", "state": "done", "owner": "agent", "detail": "Claude connected",
                   "sub": "Claude connected"},
        "knowledge": {"key": "knowledge", "state": "done", "owner": "agent",
                      "detail": "214 files mapped.", "sub": "214 / 214 files"},
        # Tests RAN: the check is done and the six failures went to the backlog (10-16), exactly
        # as the onboarding agent records it — never a red Discover.
        "tests": {"key": "tests", "state": "done", "owner": "agent",
                  "detail": "142 of 148 tests pass on main; 6 fail — fixing them is in the backlog.",
                  "sub": "6 failing → backlog"},
        "baseline_build": {"key": "baseline_build", "state": "done", "owner": "agent",
                           "detail": "main builds clean.", "sub": "make setup · 1m 40s"},
        "deploy_target": {"key": "deploy_target", "state": "done", "owner": "agent",
                          "detail": "Staging deploys to demo-host.", "sub": "port 8004"},
        "secrets": {"key": "secrets", "state": "todo", "owner": "you",
                    "detail": "OPENAI_API_KEY is only yours to give.",
                    "sub": "OPENAI_API_KEY — only you",
                    "action": {"label": "Add", "href": f"?project_id={project_id}&area=project"
                                                       "&tab=settings&section=deploy_values"}},
    }
    for item in checklist.values():
        item.setdefault("action", None)
        item["at"] = (now - timedelta(days=5, minutes=-3)).isoformat()
    discover = [{"key": "files", "label": "214 files", "state": "done", "sub": ""},
                {"key": "stack", "label": "Express+Node", "state": "done", "sub": ""},
                {"key": "tests", "label": "142/148 tests", "state": "done", "sub": ""},
                {"key": "hotspots", "label": "3 hotspots", "state": "done", "sub": ""},
                {"key": "docs", "label": "4 docs", "state": "done", "sub": ""}]
    # The run creation started is replaced by its finished record (what it found).
    db.execute(text("DELETE FROM project_onboarding_runs WHERE project_id = :p"), {"p": project_id})
    run_id = _id("onb")
    db.execute(text(
        "INSERT INTO project_onboarding_runs (id, project_id, state, trigger, started_by, "
        "checklist, discover, facts, backlog, first_work, started_at, updated_at, finished_at) "
        "VALUES (:i, :p, 'needs_you', 'created', 'boss', CAST(:c AS JSONB), CAST(:d AS JSONB), "
        "CAST(:f AS JSONB), CAST(:b AS JSONB), CAST(:w AS JSONB), :s, :e, :e)"),
        {"i": run_id, "p": project_id, "c": json.dumps(checklist), "d": json.dumps(discover),
         "f": json.dumps({"demo_users": [{"user": d["user"], "label": d["label"]}
                                         for d in DEMO_USERS], "demo_users_rule": 2,
                          "stack": {"framework": "Express", "language": "Node"},
                          # What only a person can give, as onboarding._secrets records it.
                          "secrets": {"yours": ["OPENAI_API_KEY"]},
                          "tests": {"passed": 142, "total": 148, "failing": 6}}),
         "b": json.dumps(cands), "w": json.dumps({"label": cands[0]["label"],
                                                   "detail": cands[0]["detail"],
                                                   "source": cands[0]["source"],
                                                   "kind": cands[0]["kind"],
                                                   "estimate_min": 15, "estimate_usd": 1.5}),
         "s": now - timedelta(days=5), "e": now - timedelta(days=5, minutes=-6)})
    # What the onboarding agent's own model calls cost while it ran (the Onboarding row's $1.20).
    db.execute(text(
        "INSERT INTO llm_calls (id, project_id, stage, model, cost_usd_micros, created_at) "
        "VALUES (:i, :p, 'onboarding', 'claude-sonnet', 1200000, :t)"),
        {"i": _id("llm"), "p": project_id, "t": now - timedelta(days=5, minutes=-4)})
    by_label = {c["label"]: c["id"] for c in cands}
    took = onboarding.accept_backlog(
        db, project_id, [by_label["Email-to-ticket intake"], by_label["SLA timer ignores weekends"],
                         by_label["Fix 6 failing tests"], by_label["Reply suggestions"],
                         by_label["Status index"]], actor=actor)
    lanes = plan.lanes(db, project_id, work_items=[])
    for card in lanes["next"]:
        if card["title"] in ("Reply suggestions", "Status index"):
            plan.move(db, project_id, card["id"], lane="later", actor=actor)
    recs = []
    for i, (title, det, kind, evidence, sources, value, effort, cost) in enumerate(RECOMMENDATIONS):
        wid = _id("wi")
        fp = f"seed:{uuid.uuid4().hex[:8]}"
        db.execute(text(
            "INSERT INTO work_items (id, project_id, subject_type, subject_id, title, ambient, "
            "fingerprint, lifecycle, task_class, created_by) VALUES (:i, :p, 'roadmap', :s, :t, "
            "true, :f, 'proposed', 'roadmap_item', 'seed')"),
            {"i": wid, "p": project_id, "s": fp, "t": title, "f": fp})
        db.execute(text(
            "INSERT INTO ambient_recommendations (work_item_id, project_id, detector, detectors, "
            "kind, fingerprint, subject, title, why, proposed_shape, evidence, evidence_count, "
            "source_kinds, value, effort_units, estimated_cost_usd_micros, score, priority_score, "
            "evidence_at) VALUES (:w, :p, :d, CAST(:ds AS JSONB), :k, :f, :sub, :t, :y, :shape, "
            "CAST(:ev AS JSONB), :n, CAST(:sk AS JSONB), :v, :eff, :c, :sc, :sc, :at)"),
            {"w": wid, "p": project_id, "d": det, "ds": json.dumps([det]), "k": kind, "f": fp,
             "sub": "file:server/sla.mjs" if i == 0 else f"stage:{det}", "t": title,
             "y": f"{title}: the evidence says it is worth it.",
             "shape": "prd" if kind == "feature" else "ticket", "ev": json.dumps(evidence),
             "n": len(evidence), "sk": json.dumps(sources), "v": value, "eff": effort, "c": cost,
             "sc": 95 - i * 5, "at": now - timedelta(hours=6 + i)})
        recs.append(wid)
    for title, status in (("Rewrite the ticket store", "rejected"), ("Dark mode", "snoozed")):
        wid = _id("wi")
        fp = f"seed:{uuid.uuid4().hex[:8]}"
        db.execute(text(
            "INSERT INTO work_items (id, project_id, subject_type, subject_id, title, ambient, "
            "fingerprint, lifecycle, task_class, created_by) VALUES (:i, :p, 'roadmap', :s, :t, "
            "true, :f, 'resolved', 'roadmap_item', 'seed')"),
            {"i": wid, "p": project_id, "s": fp, "t": title, "f": fp})
        db.execute(text(
            "INSERT INTO ambient_recommendations (work_item_id, project_id, detector, kind, "
            "fingerprint, title, why, proposed_shape, status, snoozed_until, reject_category, "
            "decided_by, decided_at) VALUES (:w, :p, 'hotspot', 'refactor', :f, :t, :t, 'ticket', "
            ":st, :until, :cat, :a, now())"),
            {"w": wid, "p": project_id, "f": fp, "t": title, "st": status, "a": actor,
             "until": now + timedelta(days=14) if status == "snoozed" else None,
             "cat": "not_valuable" if status == "rejected" else None})
    return {"onboarding_run_id": run_id, "backlog": [c["id"] for c in cands],
            "accepted": took["accepted"], "recommendations": recs}


def shape_more_projects(db, onboarding_now: str, never_onboarded: str) -> None:
    """One project onboarding right now (a running run), one never onboarded (no run)."""
    for pid in (onboarding_now, never_onboarded):
        db.execute(text("DELETE FROM project_onboarding_runs WHERE project_id = :p"), {"p": pid})
    db.execute(text(
        "INSERT INTO project_onboarding_runs (id, project_id, state, trigger, checklist, discover) "
        "VALUES (:i, :p, 'running', 'created', CAST(:c AS JSONB), '[]'::jsonb)"),
        {"i": _id("onb"), "p": onboarding_now, "c": json.dumps({
            "repo_access": {"key": "repo_access", "state": "done", "owner": "agent",
                            "detail": "Access checked", "sub": "read · branch · PR"},
            "knowledge": {"key": "knowledge", "state": "now", "owner": "agent",
                          "detail": "Mapping code", "sub": "118 / 214 files"}})})


def seed_more_projects(db, settings, *, prefix: str) -> dict[str, str]:
    """Two more projects for the Projects list: NorthPeak Retail onboarding right now, Hello
    World never onboarded."""
    from boss_engine.services import projects

    out = {}
    for name in ("NorthPeak Retail", "Hello World"):
        made = projects.create(db, settings, auth=None, name=name,
                               slug=f"{prefix}-{name.lower().replace(' ', '-')}", actor="seed")
        out[name] = made["project_id"]
    shape_more_projects(db, out["NorthPeak Retail"], out["Hello World"])
    return out

__all__ = ["DEMO_USERS", "HOST_ADDRESS", "REPO", "STAGING_URL", "TREE", "seed"]


def seed_knowledge(db, project_id: str, *, binding_id: str, merged: list[dict[str, Any]],
                   now: datetime | None = None) -> dict[str, Any]:
    """Repo & knowledge (screen 06), as BOSS writes it: the code graph's index row, a graph with
    a hub table, its business rules, code and endpoints; what agents LEARNED on the job (the
    learning layer: a known test failure, a workaround and a fix, each `produced` by a merged
    build, `proposed_by` an agent and, for two of them, reused by later runs); the dossier's
    hotspots as a checkout read writes them; and the newest build's test run (142 / 148)."""
    now = now or datetime.now(timezone.utc)
    ids: dict[str, str] = {}

    def node(key: str, kind: str, name: str, *, layer: str = "code", source: str = "indexer",
             path: str | None = None, attrs: dict[str, Any] | None = None, by: str = "system",
             days: float = 3) -> str:
        nid = ids[key] = _id("kgn")
        db.execute(text(
            "INSERT INTO kg_nodes (id, project_id, kind, node_key, name, attrs, layer, source, "
            "knowledge_layer, repo, path, created_by, first_seen, last_seen) VALUES (:i, :p, :k, "
            ":nk, :n, CAST(:a AS JSONB), :ly, :s, :kl, :r, :path, :by, :t, :t)"),
            {"i": nid, "p": project_id, "k": kind, "nk": f"{kind}:{key}", "n": name,
             "a": json.dumps(attrs or {}), "ly": "inferred" if layer in ("knowledge", "rules")
             else "deterministic", "s": source, "kl": layer, "r": REPO, "path": path, "by": by,
             "t": now - timedelta(days=days)})
        return nid

    def edge(a: str, b: str, kind: str) -> None:
        db.execute(text(
            "INSERT INTO kg_edges (id, project_id, src_id, dst_id, kind) VALUES (:i, :p, :a, :b, :k) "
            "ON CONFLICT DO NOTHING"),
            {"i": _id("kge"), "p": project_id, "a": ids[a], "b": ids[b], "k": kind})

    node("tickets", "table", "tickets")
    node("customers", "table", "customers")
    node("orgs", "table", "orgs")
    node("sla_rule", "rule", "SLA policy: urgent 1h, high 4h", layer="rules", source="rules")
    node("esc_rule", "rule", "Escalation on SLA breach", layer="rules", source="rules")
    node("sla_file", "file", "server/sla.mjs", path="server/sla.mjs")
    node("post_tickets", "endpoint", "POST /api/tickets", path="server/routes/tickets.mjs")
    for a, b, k in (("sla_file", "tickets", "reads"), ("post_tickets", "tickets", "writes"),
                    ("tickets", "customers", "references"), ("customers", "orgs", "references"),
                    ("sla_rule", "tickets", "governs"), ("sla_rule", "esc_rule", "escalates"),
                    ("esc_rule", "tickets", "governs"), ("sla_file", "sla_rule", "implements")):
        edge(a, b, k)
    # Agents and the runs that taught them (lifecycle layer, as learning_writeback writes them).
    for key, name in (("forge", "Forge"), ("vera", "Vera"), ("rook", "Rook")):
        node(key, "agent", name, layer="lifecycle", source="learning")
    builds = [m["build_id"] for m in merged]
    for i, build_id in enumerate(builds):
        node(f"run{i}", "run", f"Build {i + 1}", layer="lifecycle", source="learning",
             attrs={"source_id": build_id})
    node("tz", "test_failure", "SLA timers drift outside UTC fails: tests need TZ=UTC",
         layer="knowledge", source="learning", by="software_engineer", days=5,
         attrs={"detail": {"test": "SLA timers drift outside UTC"}})
    node("migrate", "workaround", "npm start: what worked after it failed", layer="knowledge",
         source="learning", by="qa", days=3,
         attrs={"detail": {"problem": "`npm start` failed: relation \"tickets\" does not exist",
                           "workaround": "`npm run migrate && npm start`"}})
    node("theme", "fix", "What made npm run build pass", layer="knowledge", source="learning",
         by="software_engineer", days=1, attrs={"detail": {"command": "npm run build"}})
    for fact, run, agent in (("tz", "run0", "forge"), ("migrate", "run1", "vera"),
                             ("theme", "run2", "rook")):
        edge(run, fact, "produced")
        edge(fact, agent, "proposed_by")
        edge(fact, "sla_file" if fact == "tz" else "tickets", "about")
    # Reused by later runs: the TZ fact twice, the migrate workaround once.
    for fact, runs in (("tz", builds[1:3]), ("migrate", builds[2:3])):
        for run in runs:
            db.execute(text(
                "INSERT INTO kg_usage (id, project_id, run_id, step, fact_id, use_kind, "
                "code_task_run_id) VALUES (:i, :p, :r, 'implement', :f, 'cited', :r)"),
                {"i": _id("kgu"), "p": project_id, "r": run, "f": ids[fact]})
    db.execute(text(
        "INSERT INTO kg_repo_index (project_id, binding_id, repo, branch, status, due, nodes, "
        "files, parsed, indexed_at) VALUES (:p, :b, :r, 'main', 'indexed', false, 412, 214, 214, "
        ":t) ON CONFLICT (project_id, binding_id) DO UPDATE SET status = 'indexed', nodes = 412, "
        "files = 214, parsed = 214, indexed_at = EXCLUDED.indexed_at"),
        {"p": project_id, "b": binding_id, "r": REPO, "t": now - timedelta(hours=3)})
    # Each merged build's merge, as pr_merge.record writes it: the repo's "last push".
    for i, m in enumerate(merged):
        db.execute(text(
            "UPDATE code_task_runs SET finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) || "
            "jsonb_build_object('merge', jsonb_build_object('sha', CAST(:sha AS TEXT), 'source', "
            "'boss') || COALESCE(finalize_ctx->'merge', '{}'::jsonb) || jsonb_build_object('at', "
            "CAST(:at AS TEXT))) WHERE id = :i AND COALESCE(finalize_ctx->'merge'->>'at', '') = ''"),
            {"i": m["build_id"], "sha": f"seedmerge{i}{uuid.uuid4().hex[:8]}",
             "at": (now - timedelta(hours=3 + 30 * (len(merged) - 1 - i))).isoformat()})
    # Two PRDs the panel wrote, and the repo's ADR (docs/), as Discovery lists them.
    for title, prd, days in (("Add SLA timers to tickets: each priority gets a deadline",
                              "# PRD: SLA timers\nEach priority gets a response deadline.", 4),
                             ("Finish the visual refresh the last change promised",
                              "# PRD: Visual refresh — ticket list and dashboard\nColoured chips.", 2)):
        # A person approved each PRD at its gate (06-14 "Juno · approved Nov 3").
        approval = _id("dec")
        db.execute(text(
            "INSERT INTO decisions (id, project_id, question, recommendation, status, approved_by, "
            "approved_at, created_at) VALUES (:i, :p, :q, CAST(:r AS JSONB), 'approved', 'ceo', "
            ":t, :t)"),
            {"i": approval, "p": project_id, "q": f"Approve the PRD: {title}?",
             "r": json.dumps({"action": "approve_prd"}), "t": now - timedelta(days=days)})
        db.execute(text(
            "INSERT INTO meeting_runs (id, project_id, directive, title, status, state, updated_at, "
            "role_keys, decision_id) VALUES (:i, :p, :d, :d, 'completed', CAST(:s AS JSONB), :t, "
            "CAST('[\"product_manager\", \"engineer\"]' AS JSONB), :dec)"),
            {"i": _id("mrun"), "p": project_id, "d": title, "t": now - timedelta(days=days),
             "s": json.dumps({"prd_markdown": prd}), "dec": approval})
    # The dossier as a checkout read leaves it: hotspots by churn, the code map by directory.
    db.execute(text(
        "UPDATE project_dossiers SET facts = facts || CAST(:f AS JSONB) WHERE project_id = :p"),
        {"p": project_id, "f": json.dumps({
            "files": 214,
            "commands": {"test": ["npm test"], "build": ["npm run build"], "lint": []},
            "docs": {"readme": "README.md", "runbooks": ["docs/adr-001-postgres-queue.md"]},
            "structure": [{"dir": "server", "files": 92}, {"dir": "web", "files": 71},
                          {"dir": "tests", "files": 38}, {"dir": "infra", "files": 13}],
            "hotspots": [{"file": "server/sla.mjs", "churn": 9, "lines": 210, "has_tests": True},
                         {"file": "web/src/pages/TicketList.tsx", "churn": 6, "lines": 180,
                          "has_tests": False},
                         {"file": "server/notify/email.mjs", "churn": 4, "lines": 95,
                          "has_tests": False}]})})
    # The newest build's own test run (node:test's summary).
    db.execute(text(
        "UPDATE code_task_runs SET state = COALESCE(state, '{}'::jsonb) || CAST(:s AS JSONB), "
        "updated_at = :t WHERE id = :i"),
        {"i": builds[-1], "t": now - timedelta(hours=2),
         "s": json.dumps({"last_verify_output": "1..148\n# tests 148\n# suites 0\n# pass 142\n"
                                                "# fail 6\n# cancelled 0\n"})})
    return {"nodes": dict(ids)}


def seed_work_story(db, project_id: str, *, runs: list[dict[str, Any]],
                    now: datetime | None = None) -> None:
    """What the work drawer (screen 05b) reads, as the build path records it: each build's
    files changed, tests, recorded stage costs, review rounds and a deferred finding; each run's
    plan as an agreed PRD (a completed panel) approved by a person."""
    now = now or datetime.now(timezone.utc)
    for n, got in enumerate(runs):
        build = got.get("build_id")
        if not build:
            continue
        state = {"files_changed": 11 - n, "tests_passed": True, "tests": [{"ok": True}],
                 "stage_costs": {"design": 210_000, "implement": 980_000, "write_tests": 310_000,
                                 "review_code": 420_000, "security_check": 120_000},
                 "deferred_findings": [{"summary": "Dashboard render test"}] if n == 0 else []}
        rounds = [{"round": 0, "at": None, "seats": {}}, {"round": 1, "at": None, "seats": {}}]
        db.execute(text(
            "UPDATE code_task_runs SET state = COALESCE(state, '{}'::jsonb) || CAST(:s AS JSONB), "
            "finalize_ctx = COALESCE(finalize_ctx, '{}'::jsonb) || CAST(:f AS JSONB), "
            "updated_at = created_at + interval '31 minutes' WHERE id = :i"),
            {"i": build, "s": json.dumps(state), "f": json.dumps({"review_rounds": rounds})})
        run_id = got.get("run_id")
        if not run_id:
            continue
        created = db.execute(text("SELECT created_at FROM workflow_runs WHERE id = :r"),
                             {"r": run_id}).scalar() or now
        meeting = _id("mtg")
        db.execute(text(
            "INSERT INTO meeting_runs (id, project_id, status, directive, title, created_at, "
            "updated_at) VALUES (:i, :p, 'completed', :d, :t, :at, :at)"),
            {"i": meeting, "p": project_id, "d": "Plan the change", "t": "PRD agreed",
             "at": created})
        spec = _id("dec")
        db.execute(text(
            "INSERT INTO decisions (id, project_id, question, recommendation, status, approved_by, "
            "approved_at, created_at) VALUES (:i, :p, 'Approve the PRD?', CAST(:r AS JSONB), "
            "'approved', 'ceo', :at, :at)"),
            {"i": spec, "p": project_id, "at": created + timedelta(minutes=8),
             "r": json.dumps({"action": "approve_workflow_step", "workflow_run_id": run_id,
                              "capability": "spec_review"})})
        db.execute(text(
            "UPDATE workflow_step_runs SET output = COALESCE(output, '{}'::jsonb) || CAST(:o AS JSONB) "
            "WHERE id = (SELECT id FROM workflow_step_runs WHERE workflow_run_id = :r "
            "ORDER BY started_at, id LIMIT 1)"),
            {"r": run_id, "o": json.dumps({"meeting_run_id": meeting, "spec_decision_id": spec,
                                           "prd_markdown": "# PRD\n4 changes agreed."})})


def seed_project_sources(db, project_id: str, *, host_id: str, now: datetime | None = None) -> None:
    """Add a project (screen 03) and Infra › server (screen 14): the org's GitHub registry with
    four repositories and their last pushes (the project's own repo is "in use"), and what the
    demo server said about itself at its last preflight (cpus, memory, disk, OS, running images)."""
    from boss_engine.multirepo.common import org_of_project
    from boss_engine.services.project_experience.host_overview import capacity_from, store_capacity

    now = now or datetime.now(timezone.utc)
    org = org_of_project(db, project_id)
    ok = {"ok": True, "state": "working"}
    for name, lang, ago in (("helpdesk-ai-demo", "JavaScript", timedelta(hours=3)),
                            ("hello-world", "TypeScript", timedelta(days=2)),
                            ("kaizen-notes", "Python", timedelta(days=12)),
                            ("ledger-api", "Go", timedelta(days=34))):
        db.execute(text(
            "INSERT INTO github_repos (id, org_id, source, full_name, default_branch, language, "
            "private, allowed, allowed_by, allowed_at, checks, checked_at, pushed_at) VALUES (:i, :o, "
            "'token', :f, 'main', :l, true, true, 'seed', now(), CAST(:k AS JSONB), now(), :p) "
            "ON CONFLICT (org_id, lower(full_name)) DO UPDATE SET pushed_at = EXCLUDED.pushed_at"),
            {"i": _id("ghr"), "o": org, "f": f"acme/{name}", "l": lang, "p": now - ago,
             "k": json.dumps({"read": ok, "write": ok, "pull_requests": ok, "state": "working"})})
    db.execute(text(
        "UPDATE deployment_hosts SET last_preflight = jsonb_build_object('reachable', true, "
        "'facts', jsonb_build_object('docker', true, 'versions', jsonb_build_object("
        "'docker', 'Docker version 27.3.1, build ce12230', 'compose', '2.29.7'))), "
        "last_preflight_at = :at WHERE id = :h"), {"h": host_id, "at": now - timedelta(hours=2)})
    store_capacity(db, host_id, capacity_from(
        "BOSS_CAP cpus 8\nBOSS_CAP load 2.72\nBOSS_CAP mem 16384000 6389760\n"
        "BOSS_CAP disk 209715200 150994944\nBOSS_CAP os Ubuntu 22.04.4 LTS\n"
        "BOSS_IMG caddy:2\nBOSS_IMG prom/node-exporter:v1.8\nBOSS_IMG helpdesk-ai-app\n",
        at=now - timedelta(hours=2)) or {})



def seed_settings_story(db, project_id: str, *, live_sdep: str, now: datetime | None = None
                        ) -> dict[str, Any]:
    """Project › Settings (screen 13) as the app's own writes leave it:

      * deploy values, names only (the vault ids are placeholders; no value is ever seeded):
        DATABASE_URL and SESSION_SECRET generated for the deploy, DEMO_PASSWORD generated for the
        deploy AND the preview ("per env"), SMTP_URL set by a person 12 days ago, and
        OPENAI_API_KEY read by the live deploy's manifest but never set ("missing");
      * contact points (console, the owner's email, a Slack webhook) with the alert routing root
        on email + Slack (Incidents), Needs you → email + Slack, PR ready → email;
      * the project's autonomy at A1 Propose for every class, and an ambient budget of $50 a week
        with $14 of ambient spend this week.
    """
    from boss_engine.services.ambient import task_classes as tc

    now = now or datetime.now(timezone.utc)
    app = "helpdesk-ai-demo"
    values = [("DATABASE_URL", "deploy", "generated", 9), ("SESSION_SECRET", "deploy", "generated", 9),
              ("DEMO_PASSWORD", "deploy", "generated", 9), ("DEMO_PASSWORD", "preview", "generated", 3),
              ("SMTP_URL", "deploy", "test", 12)]
    for name, env, mode, days in values:
        db.execute(text(
            "INSERT INTO app_secret_bindings (id, project_id, app_key, env, name, vault_item_id, mode, "
            "set_by, set_at) VALUES (:i, :p, :a, :e, :n, :v, :m, :by, :at) "
            "ON CONFLICT (project_id, app_key, env, name) DO NOTHING"),
            {"i": _id("asb"), "p": project_id, "a": app, "e": env, "n": name, "v": _id("vlt_seed"),
             "m": mode, "by": "ceo" if mode == "test" else "boss", "at": now - timedelta(days=days)})
    db.execute(text(
        "UPDATE stack_deployments SET manifest = manifest || CAST(:m AS JSONB) WHERE id = :i"),
        {"i": live_sdep, "m": json.dumps({"secret_env_keys": ["OPENAI_API_KEY", "SMTP_URL"]})})
    cps = {}
    for key, kind, name, config in (("console", "console", "Console", {}),
                                    ("email", "email", "Project owner", {"address": "owner@example.test"}),
                                    ("slack", "slack_webhook", "#helpdesk", {})):
        cps[key] = _id("cp")
        db.execute(text(
            "INSERT INTO contact_points (id, project_id, name, kind, config, verified_at, created_by) "
            "VALUES (:i, :p, :n, :k, CAST(:c AS JSONB), now(), 'seed')"),
            {"i": cps[key], "p": project_id, "n": name, "k": kind, "c": json.dumps(config)})
    db.execute(text("DELETE FROM notification_policies WHERE project_id = :p AND parent_id IS NULL"),
               {"p": project_id})
    db.execute(text(
        "INSERT INTO notification_policies (id, project_id, parent_id, name, matchers, group_by, "
        "group_wait_s, group_interval_s, repeat_interval_s, contact_point_ids) VALUES (:i, :p, NULL, "
        "'Everything', '[]'::jsonb, CAST(:g AS JSONB), 30, 300, 14400, CAST(:c AS JSONB))"),
        {"i": _id("np"), "p": project_id, "g": json.dumps(["alertname", "service"]),
         "c": json.dumps([cps["console"], cps["email"], cps["slack"]])})
    for event, ids in (("needs_you", [cps["email"], cps["slack"]]), ("pr_ready", [cps["email"]])):
        db.execute(text(
            "INSERT INTO project_notification_routes (project_id, event, contact_point_ids, updated_by) "
            "VALUES (:p, :e, CAST(:c AS JSONB), 'seed') ON CONFLICT (project_id, event) DO UPDATE "
            "SET contact_point_ids = EXCLUDED.contact_point_ids"),
            {"p": project_id, "e": event, "c": json.dumps(ids)})
    for key, klass in tc.TASK_CLASSES.items():
        level = "A1" if tc.level_rank(klass.ceiling) >= 1 else klass.ceiling
        db.execute(text(
            "INSERT INTO ambient_autonomy (project_id, task_class, level, ceiling, set_by) VALUES "
            "(:p, :c, :l, :ce, 'seed') ON CONFLICT (project_id, task_class) DO UPDATE SET "
            "level = EXCLUDED.level"),
            {"p": project_id, "c": key, "l": level, "ce": klass.ceiling})
    db.execute(text(
        "INSERT INTO ambient_settings (project_id, daily_cap_usd_micros, updated_by) VALUES "
        "(:p, :c, 'seed') ON CONFLICT (project_id) DO UPDATE SET daily_cap_usd_micros = "
        "EXCLUDED.daily_cap_usd_micros"), {"p": project_id, "c": round(50_000_000 / 7)})
    for i, usd in enumerate((4.10, 6.40, 3.50)):
        db.execute(text(
            "INSERT INTO cost_receipts (id, project_id, category, skill_or_model_id, "
            "estimated_usd_micros, actual_usd_micros, status, created_at) VALUES (:i, :p, 'ambient', "
            "'claude-sonnet', :u, :u, 'actual', :at)"),
            {"i": _id("cr"), "p": project_id, "u": int(usd * 1_000_000),
             "at": now - timedelta(days=1 + i * 2)})
    return {"contact_points": cps}


def seed_round_two(db, project_id: str, *, now: datetime | None = None,
                   other_project: str | None = None) -> dict[str, Any]:
    """Round two's elements (BOSSAPP-913/914), on a project `seed()` made:

    * 24 hours of hourly health probes on staging, one hour degraded (08-08's strip);
    * one merged run that ran on a subscription (its list price, never a bare $0.00);
    * the "Alert rule for /api/tickets 5xx" card read by the Alert gap detector, its evidence the
      live staging target (07-12);
    * another project's app with an alert firing, for Observability's rows across projects (15).
    """
    now = now or datetime.now(timezone.utc)
    target = db.execute(text(
        "SELECT id, host_id FROM deployment_targets WHERE project_id = :p AND status = 'active' "
        "ORDER BY created_at LIMIT 1"), {"p": project_id}).mappings().first()
    out: dict[str, Any] = {}
    if target:
        hour0 = now.replace(minute=0, second=0, microsecond=0)
        for h in range(1, 24):
            for k in range(4):
                bad = h == 7 and k < 2
                db.execute(text(
                    "INSERT INTO deployment_health_samples (id, project_id, deployment_target_id, "
                    "checked_at, ok, status_code, latency_ms) VALUES (:i, :p, :t, :at, :ok, :c, :ms)"),
                    {"i": _id("dhs"), "p": project_id, "t": target["id"],
                     "at": hour0 - timedelta(hours=h) + timedelta(minutes=5 + 12 * k), "ok": not bad,
                     "c": 502 if bad else 200, "ms": 140 + (h * 13 + k * 7) % 110})
        out["target_id"] = target["id"]
        db.execute(text(
            "UPDATE ambient_recommendations SET detector = 'alert_gap', detectors = '[\"alert_gap\"]', "
            "kind = 'hardening', squad = '[\"sre\"]', evidence = CAST(:e AS JSONB), evidence_count = 1, "
            "source_kinds = '[\"alerts\"]' WHERE project_id = :p AND title LIKE 'Alert rule for%'"),
            {"p": project_id, "e": json.dumps([{"kind": "deployment_target", "ref": target["id"],
                                                "note": "live, no alert rule"}])})
    first = db.execute(text(
        "SELECT c.id FROM code_task_runs c WHERE c.project_id = :p AND c.pull_request_number IS NOT NULL "
        "ORDER BY c.created_at LIMIT 1"), {"p": project_id}).scalar()
    if first:
        db.execute(text("UPDATE llm_calls SET included_in_subscription = true WHERE code_task_run_id = :b"),
                   {"b": first})
        out["subscription_build"] = first
    if other_project:
        host = _id("dhost")
        db.execute(text(
            "INSERT INTO deployment_hosts (id, project_id, name, address, ssh_user, status, created_by) "
            "VALUES (:i, :p, :i, '203.0.113.40', 'deploy', 'verified', 'seed')"), {"i": host, "p": other_project})
        tgt = _id("dept")
        db.execute(text(
            "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, "
            "target_ref, domains, status, created_by, host_id, public_url) VALUES (:i, :p, 'northpeak', "
            "'acme/northpeak', 'preview', 'host', :h, '[]'::jsonb, 'active', 'seed', :h, "
            "'http://203.0.113.40:8110')"), {"i": tgt, "p": other_project, "h": host})
        db.execute(text(
            "INSERT INTO stack_deployments (id, project_id, target_id, host_id, repository, ref, commit_sha, "
            "manifest, status, open_url, created_by, created_at, finished_at) VALUES (:i, :p, :t, :h, "
            "'acme/northpeak', 'main', 'b7e1c0d', '{}'::jsonb, 'healthy', 'http://203.0.113.40:8110', 'seed', "
            ":at, :at)"), {"i": _id("sdep"), "p": other_project, "t": tgt, "h": host, "at": now - timedelta(days=1)})
        for i in range(48):
            slow = i % 6 == 0
            db.execute(text(
                "INSERT INTO deployment_health_samples (id, project_id, deployment_target_id, checked_at, "
                "ok, status_code, latency_ms) VALUES (:i, :p, :t, :at, :ok, :c, :ms)"),
                {"i": _id("dhs"), "p": other_project, "t": tgt, "at": now - timedelta(minutes=30 * i),
                 "ok": not slow, "c": 500 if slow else 200, "ms": 650 + (i * 37) % 600})
        db.execute(text(
            "INSERT INTO app_alerts (id, project_id, target_id, rule_kind, fingerprint, alertname, state, "
            "active_at) VALUES (:i, :p, :t, 'threshold', :f, 'p95 latency', 'firing', :at)"),
            {"i": _id("aal"), "p": other_project, "t": tgt, "f": uuid.uuid4().hex, "at": now - timedelta(minutes=40)})
        out["other_target_id"] = tgt
    return out


def seed_captain_conversation(engine, settings, project_id: str, *, actor: str = "executive_console",
                              role: str | None = None) -> str:
    """Screen 09's conversation, through THE ONE AGENT TURN (AgentService.chat) with the model's
    side stood in for at the router — so the stored messages carry the real `proposal` and
    `citations` turn_cards derives: a status answer that names PR #3, the running Run, Staging
    and the demo logins; then "add a VIP customer flag" answered with an offer to build it, naming
    issue #12 and sla.mjs. Returns the thread id."""
    from types import SimpleNamespace

    from boss_engine.services.agent_service import AgentService
    from boss_engine.services.project_experience import work

    from boss_engine.services.project_experience import captain as captain_service

    with engine.connect() as db:
        items = work.everything(db, project_id)
        # The dock addresses the captain by its name (AgentChat's `role`), as the console does.
        role = role or str((captain_service.view(db, project_id, with_last_said=False) or {})
                           .get("name") or "Helm")
    run_no = next((w["run_number"] for w in items if w.get("run_number") and w["state"] == "running"),
                  None) or next((w["run_number"] for w in items if w.get("run_number")), 1)
    service = AgentService(settings)
    replies = [
        (f"! Merge PR #3 → ● Search filters → ✓ Staging → Demo ready\n"
         f"Two things. **Merge PR #3** (tests pass, review clean) — then Beacon redeploys staging in "
         f"about 3 minutes. **Search filters** (Run #{run_no}) finishes building soon; I'd demo it too. "
         f"Prod isn't needed for a demo, and the demo logins are ready.", [], {"action": "answer"}),
        ("I'll plan it as a minor feature. It touches the same SLA code as the open weekend bug "
         "(issue #12, in `sla.mjs`), so I'd run them together.",
         [{"id": "act_seed_vip", "tool": "create_code_task", "label": "Build the VIP customer flag",
           "args": {"title": "VIP customer flag",
                    "instructions": "Add a VIP customer flag on customers; tickets from VIP "
                                    "customers jump the queue and their SLA clock is halved."}}],
         {"action": "new_code_change"}),
    ]
    asks = ["What's left before I can demo this to the support team on Monday?",
            "Also add a \"VIP customer\" flag that jumps the queue."]
    thread_id = None
    for ask, (reply, offers, decision) in zip(asks, replies):
        service.router.route_turn = (lambda *_a, _r=reply, _o=offers, _d=decision, **_k: SimpleNamespace(
            reply=_r, tool_calls=[], actions=[], proposed_actions=list(_o), pending_confirmation=None,
            next_steps=[], decision=_d))
        # One transaction per turn, as the API does: the question and its answer share a
        # timestamp, the next turn comes after them.
        with engine.begin() as db:
            got = service.chat(db, project_id=project_id, actor=actor, message=ask,
                               thread_id=thread_id, surface="chat",
                               external_ref=f"project:{project_id}", role=role)
        thread_id = got["thread_id"]
    return str(thread_id)
