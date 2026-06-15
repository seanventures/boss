#!/usr/bin/env python3
"""Seed the local BOSS database with realistic DEMO data so a fresh checkout
shows a populated, clickable console (Work, Decisions, Meetings, Code Tasks)
WITHOUT needing the worker or any model API key.

Offline + deterministic + idempotent: pure inserts with fixed demo ids and
ON CONFLICT DO NOTHING, safe to re-run. Targets the project from settings
(default `boss-dogfood`), which must already exist — run bootstrap first:

    PYTHONPATH=apps/api .venv/bin/python -m app.bootstrap     # baseline
    PYTHONPATH=apps/api .venv/bin/python scripts/seed_demo.py # demo data

Tasks stay `open` (as in real usage); the Work board derives each task's
lifecycle from its artifacts/jobs/decisions, so we seed those to show tasks
at different stages. Theme: "Northwind", a fictional B2B SaaS startup.
"""
from __future__ import annotations

import json
import sys

import sqlalchemy as sa

from app.config import get_settings
from app.db import get_engine


def _md(*lines: str) -> str:
    return "\n".join(lines)


# Each task: (id, title, description, priority, risk, stage). `stage` drives
# which artifacts/jobs/decisions we seed so the Work board shows it correctly.
#   fresh      → just created
#   planning   → plan job queued (worker would pick it up)
#   awaiting   → planned, review decision PENDING (+ a non-review pending decision)
#   executing  → planned, approved, follow-on running, code tasks attached
#   done       → planned, approved, executed
TASKS = [
    ("tsk_demo_sso", "Plan SSO / SAML for enterprise",
     "Phased rollout of enterprise SSO.", "p3", 2, "fresh"),
    ("tsk_demo_cloudspend", "Cut cloud spend 15% without hurting reliability",
     "Investigate spend and propose a savings plan.", "p2", 1, "planning"),
    ("tsk_demo_onboarding", "Trial onboarding email sequence",
     "4-email onboarding flow with a defined success metric.", "p2", 1, "awaiting"),
    ("tsk_demo_status_page", "Public status page wired to uptime monitor",
     "Customer-facing status page + on-call runbook.", "p1", 1, "executing"),
    ("tsk_demo_soc2", "SOC 2 access-control evidence checklist",
     "Prepare evidence for the security review.", "p1", 2, "done"),
]


def seed_demo() -> None:
    settings = get_settings()
    project_id = settings.default_project_id
    engine = get_engine(settings)

    with engine.begin() as c:
        if not c.execute(sa.text("SELECT 1 FROM projects WHERE id=:p"), {"p": project_id}).first():
            print(f"Project '{project_id}' not found. Run the bootstrap seed first:\n"
                  "  PYTHONPATH=apps/api .venv/bin/python -m app.bootstrap", file=sys.stderr)
            sys.exit(1)

        def task(tid, title, desc, prio, risk, age_h):
            c.execute(sa.text("""
                INSERT INTO tasks (id, project_id, title, description, status, priority, risk_tier,
                                   data_boundary, dedupe_key, created_at, updated_at)
                VALUES (:id,:p,:t,:d,'open',:prio,:risk,'internal',:dk,
                        now()-make_interval(hours=>:a), now()-make_interval(mins=>:a))
                ON CONFLICT (id) DO NOTHING
            """), {"id": tid, "p": project_id, "t": title, "d": desc, "prio": prio,
                   "risk": risk, "dk": f"demo-{tid}", "a": age_h})

        def artifact(aid, tid, atype, title, content):
            c.execute(sa.text("""
                INSERT INTO artifacts (id, project_id, task_id, artifact_type, title, content, created_at)
                VALUES (:id,:p,:tid,:ty,:t,CAST(:c AS jsonb), now()-interval '3 hours')
                ON CONFLICT (id) DO NOTHING
            """), {"id": aid, "p": project_id, "tid": tid, "ty": atype, "t": title,
                   "c": json.dumps(content)})

        def job(jid, tid, kind, status):
            c.execute(sa.text("""
                INSERT INTO jobs (id, project_id, task_id, kind, status, payload, created_at, updated_at)
                VALUES (:id,:p,:tid,:k,:s,'{}'::jsonb, now()-interval '3 hours', now()-interval '2 hours')
                ON CONFLICT (id) DO NOTHING
            """), {"id": jid, "p": project_id, "tid": tid, "k": kind, "s": status})

        def decision(did, tid, q, rec, status, approver=None):
            c.execute(sa.text("""
                INSERT INTO decisions (id, project_id, task_id, question, recommendation, options,
                                       risk_tier, status, approved_by,
                                       approved_at, default_action, created_at, updated_at)
                VALUES (:id,:p,:tid,:q,CAST(:rec AS jsonb),
                        '[{"id":"approve","label":"Approve"},{"id":"revise","label":"Request changes"}]'::jsonb,
                        1,:s,:ab,
                        CASE WHEN :s='approved' THEN now()-interval '2 hours' END,
                        'approve', now()-interval '5 hours', now()-interval '90 minutes')
                ON CONFLICT (id) DO NOTHING
            """), {"id": did, "p": project_id, "tid": tid, "q": q, "rec": json.dumps(rec),
                   "s": status, "ab": approver})

        # ---- Tasks + their lifecycle (artifacts / jobs / decisions) -------
        for i, (tid, title, desc, prio, risk, stage) in enumerate(TASKS):
            task(tid, title, desc, prio, risk, age_h=i * 6 + 2)
            short = title.split()[0].lower()
            if stage in ("awaiting", "executing", "done"):
                artifact(f"art_demo_plan_{short}", tid, "plan_draft", f"Plan: {title}",
                         {"objective": desc, "steps": ["Scope", "Build", "Validate", "Ship"]})
                job(f"job_demo_plan_{short}", tid, "openai.plan", "succeeded")
            if stage == "planning":
                job(f"job_demo_plan_{short}", tid, "openai.plan", "queued")
            if stage == "awaiting":
                decision(f"dec_demo_review_{short}", tid, f"Review plan: {title}",
                         {"summary": "Plan ready for approval.", "recommendation": "Approve to execute."},
                         "pending")
                decision(f"dec_demo_act_{short}", tid,
                         "Approve the onboarding send schedule (4 emails / 14 days)?",
                         {"summary": "Behavior-triggered; success = trial→paid activation."}, "pending")
            if stage in ("executing", "done"):
                decision(f"dec_demo_review_{short}", tid, f"Review plan: {title}",
                         {"summary": "Plan reviewed.", "recommendation": "Approved."}, "approved", "ceo")
                job(f"job_demo_exec_{short}", tid, "software_factory.propose_pr",
                    "succeeded" if stage == "done" else "running")

        # ---- A completed multi-agent PRD meeting (linked to onboarding) ---
        role_keys = ["product_manager", "engineer", "ux_designer", "privacy_safety", "growth"]
        round1 = {
            "Product Manager": _md("## PM Perspective",
                "The core job is **trial→paid activation**, not a redesign. Anchor the team on the one "
                "metric that matters first.", "### Priorities",
                "- Instrument the activation funnel", "- Ship onboarding behind a flag",
                "- Define 'activated' precisely"),
            "Engineer / Architect": _md("## Engineering view",
                "Feasible on the current stack. Keep it **event-driven**.",
                "- Reuse the email service", "- Add an `activation_events` table", "- Feature-flag rollout"),
            "UX Designer": _md("## UX", "**Reduce time-to-value** — the first session should hit one win.",
                "- Progressive onboarding checklist", "- Plain-language empty states", "- Accessible forms"),
            "Privacy & Safety": _md("## Privacy & Safety",
                "Email automation touches PII — **least-privilege, auditable**.",
                "- Honor consent + unsubscribe", "- No PII in logs", "- Retention policy"),
            "Growth / Marketing": _md("## Growth", "The sequence should **earn** the next email.",
                "- A/B subject lines", "- Trigger on behavior", "- One CTA per email"),
        }
        round2 = {
            "Product Manager": _md("## PM — debate",
                "Agree on event-driven. Pushing back on Growth: **don't gate activation on email opens** — "
                "in-product is primary, email supports."),
            "Engineer / Architect": _md("## Eng — debate",
                "Aligned. The `activation_events` table serves UX's checklist and Growth's lift — one source of truth."),
            "UX Designer": _md("## UX — debate",
                "In-product-first is right. Checklist drives activation; email nudges the lapsed."),
            "Privacy & Safety": _md("## Privacy — debate",
                "Fine with a shared events table **if** it carries no message content and respects consent."),
            "Growth / Marketing": _md("## Growth — debate",
                "Concede: in-product leads, email supports. I keep per-step lift from the shared events."),
        }
        synthesis = _md("# Converged direction", "*Facilitator's reconciliation of the panel.*", "---",
            "## Agreed goal",
            "Lift **trial→paid activation** via an in-product onboarding checklist (primary) plus a "
            "behavior-triggered email sequence (support).",
            "## Decisions", "- One `activation_events` table = shared source of truth",
            "- In-product checklist leads; email nudges lapsed users",
            "- Event-driven, flagged rollout; consent + deletion respected",
            "## Open questions", "- Exact definition of 'activated'", "- A/B framework for subjects")
        prd_markdown = _md("# PRD — Trial Activation", "## Problem",
            "Trial users don't reliably reach first value, so trial→paid conversion lags.",
            "## Goals", "- Define and instrument 'activation'", "- In-product onboarding checklist",
            "- Behavior-triggered support emails", "## Requirements",
            "- **P0**: `activation_events` table; funnel instrumentation",
            "- **P1**: onboarding checklist UI; flagged rollout",
            "- **P2**: A/B email subjects; per-step lift dashboard",
            "## Success metrics", "- +X% trial→paid activation within 2 quarters",
            "## Privacy", "- Consent-respecting, no PII in logs, deletion path")
        state = {"round1": round1, "round2": round2, "synthesis": synthesis,
                 "prd_markdown": prd_markdown,
                 "research": {"pages_studied": 0, "note": "demo meeting — no live site studied"}}
        c.execute(sa.text("""
            INSERT INTO meeting_runs (id, project_id, task_id, status, phase, directive, url,
                                      role_keys, state, usage, finalize_ctx, created_at, updated_at)
            VALUES (:id,:p,:tid,'completed','done',:directive,'', CAST(:rk AS jsonb),
                    CAST(:state AS jsonb), CAST(:usage AS jsonb), '{}'::jsonb,
                    now()-interval '3 hours', now()-interval '174 minutes')
            ON CONFLICT (id) DO NOTHING
        """), {"id": "mrun_demo_activation", "p": project_id, "tid": "tsk_demo_onboarding",
               "directive": _md("Produce a PRD to lift trial→paid activation for Northwind.", "",
                                "- Define and instrument what 'activated' means.",
                                "- Propose an in-product onboarding experience.",
                                "- Decide where email fits vs in-product.", "",
                                "Publish the final PRD as a PDF pull request."),
               "rk": json.dumps(role_keys), "state": json.dumps(state),
               "usage": json.dumps({"model": "claude-opus-4-8[1m]", "cost_usd_micros": 1_591_374})})

        # ---- Code tasks (one completed w/ PR, one running) ----------------
        spec = {"repo": "northwind-api", "owner": "northwind", "base_branch": "main",
                "instructions": "Add /healthz + an activation_events migration.",
                "test_commands": ["mvn -q test"],
                "acceptance_criteria": ["/healthz returns 200", "migration applies"]}
        c.execute(sa.text("""
            INSERT INTO code_task_runs (id, project_id, task_id, status, phase, title, spec, state, usage,
                                        finalize_ctx, pull_request_url, pull_request_number, created_at, updated_at)
            VALUES (:id,:p,:tid,'completed','opened_pr',:title, CAST(:spec AS jsonb), CAST(:state AS jsonb),
                    CAST(:usage AS jsonb), '{}'::jsonb, :url, :num,
                    now()-interval '90 minutes', now()-interval '78 minutes')
            ON CONFLICT (id) DO NOTHING
        """), {"id": "ctr_demo_healthz", "p": project_id, "tid": "tsk_demo_status_page",
               "title": "Add /healthz + activation_events migration", "spec": json.dumps(spec),
               "state": json.dumps({"engine": "native", "summary": "Added /healthz and migration.",
                                    "files_changed": 3, "additions": 84, "deletions": 2, "tests": "mvn -q test",
                                    "tests_passed": True, "turns": 4, "cost_usd_micros": 110_522,
                                    "paths": ["src/HealthController.java", "db/V12__activation_events.sql"]}),
               "usage": json.dumps({"model": "claude-opus-4-8[1m]", "cost_usd_micros": 110_522}),
               "url": "https://github.com/example/northwind/pull/43", "num": 43})
        c.execute(sa.text("""
            INSERT INTO code_task_runs (id, project_id, task_id, status, phase, title, spec, state, usage,
                                        finalize_ctx, created_at, updated_at)
            VALUES (:id,:p,:tid,'running','coding',:title, CAST(:spec AS jsonb), '{}'::jsonb, '{}'::jsonb,
                    '{}'::jsonb, now()-interval '4 minutes', now()-interval '1 minute')
            ON CONFLICT (id) DO NOTHING
        """), {"id": "ctr_demo_status_ui", "p": project_id, "tid": "tsk_demo_status_page",
               "title": "Build the public status page UI",
               "spec": json.dumps({**spec, "repo": "northwind-web",
                                   "instructions": "Build the public status page bound to the uptime API."})})

    print("✓ Demo data seeded into project", project_id)
    print("  5 tasks (fresh → done) · review + pending decisions · 1 PRD meeting · 2 code tasks")
    print("  Open the console — Work, Decisions, Meetings, and Code Tasks are populated.")


if __name__ == "__main__":
    seed_demo()
