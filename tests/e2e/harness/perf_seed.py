"""Realistic volume for the performance walk (tests/e2e/harness/perf.py).

An e2e stack starts nearly empty, and an empty database hides exactly the costs an operator's
install pays: the Factory groups dozens of builds into apps, the Command Center counts decisions,
the activity feed pages through thousands of events. This writes that volume directly into the
stack's OWN database (never an operator's: the URL comes from the stack's state), deterministic so
two runs measure the same thing.

    python -m tests.e2e.harness.perf_seed <database-url> [--scale 1]
"""
from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta, timezone

PROJECT = "boss-dogfood"

APP_NAMES = ["invoice-portal", "team-wiki", "status-page", "booking-app", "crm-lite", "survey-kit",
             "expense-tracker", "news-digest", "inventory", "help-desk", "recipe-box", "fleet-map",
             "time-sheets", "event-rsvp", "photo-gallery", "quiz-maker", "habit-log", "job-board",
             "chat-bridge", "pricing-calc", "doc-signer", "link-shortener", "feedback-wall",
             "bug-tracker", "menu-builder", "parking-finder", "tutor-match", "bike-share",
             "garden-planner", "podcast-feed", "shift-swap", "lab-notebook", "gift-registry",
             "pet-clinic", "stock-alerts"]
STATES = [("succeeded", "done"), ("succeeded", "pr_open"), ("failed", "tests"), ("running", "build"),
          ("succeeded", "merged"), ("cancelled", "queued"), ("needs_plan", "plan")]
ROLES = ["vp_engineering", "staff_engineer", "qa_lead", "designer", "sre", "product_manager"]


def seed(url: str, scale: int = 1) -> dict[str, int]:
    import psycopg

    url = url.replace("postgresql+psycopg://", "postgresql://")
    rnd = random.Random(7)
    now = datetime.now(timezone.utc)
    counts: dict[str, int] = {}
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        # tasks + decisions (some pending: the inbox and the Command Center count them)
        n_tasks = 60 * scale
        for i in range(n_tasks):
            cur.execute(
                "INSERT INTO tasks (id, project_id, title, description, status, owner_role_id, "
                "priority, dedupe_key, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (f"task_perf_{i}", PROJECT, f"Perf task {i}: {rnd.choice(APP_NAMES)}",
                 "Seeded for the performance walk. " * 4, rnd.choice(["open", "in_progress", "done"]),
                 None, rnd.choice(["p1", "p2", "p3"]), f"perf-{i}",
                 now - timedelta(hours=i), now - timedelta(hours=i)))
        counts["tasks"] = n_tasks
        n_dec = 80 * scale
        for i in range(n_dec):
            status = "pending" if i % 4 == 0 else rnd.choice(["approved", "rejected", "executed"])
            cur.execute(
                "INSERT INTO decisions (id, project_id, task_id, question, recommendation, options, "
                "risk_tier, status, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (f"dec_perf_{i}", PROJECT, f"task_perf_{i % n_tasks}",
                 f"Approve the plan for {rnd.choice(APP_NAMES)} (#{i})?",
                 json.dumps({"action": "approve", "why": "Tests green, low risk. " * 3}),
                 json.dumps([{"id": "approve", "label": "Approve"}, {"id": "reject", "label": "Reject"}]),
                 rnd.randint(0, 3), status, now - timedelta(hours=i * 2), now - timedelta(hours=i)))
        counts["decisions"] = n_dec
        # builds: 35 apps, 1-8 attempts each
        n_runs = 0
        for a, app in enumerate(APP_NAMES[: 35 if scale >= 1 else 10]):
            for attempt in range(1 + (a * 3) % 8):
                status, phase = STATES[(a + attempt) % len(STATES)]
                rid = f"crun_perf_{a}_{attempt}"
                created = now - timedelta(hours=a * 5 + attempt)
                pr = 100 + n_runs if phase in ("pr_open", "merged", "done") else None
                cur.execute(
                    "INSERT INTO code_task_runs (id, project_id, status, phase, title, spec, state, usage, "
                    "finalize_ctx, pull_request_url, pull_request_number, created_at, updated_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (rid, PROJECT, status, phase, f"Build {app}: add the {rnd.choice(['login', 'search', 'export', 'billing'])} page",
                     json.dumps({"owner": "acme", "repo": app, "app_name": app, "brief": f"Build {app}. " * 10,
                                 "kind": "new_app"}),
                     json.dumps({"stations": [{"id": s, "status": "done"} for s in
                                              ("plan", "build", "test", "review", "pr")],
                                 "log": ["step " + str(k) for k in range(30)]}),
                     json.dumps({"calls": rnd.randint(5, 80), "metered_usd": round(rnd.random() * 3, 4)}),
                     json.dumps({"origin": {"kind": "console"}}),
                     f"https://github.com/acme/{app}/pull/{pr}" if pr else None, pr, created, created))
                for s, stage in enumerate(("plan", "build", "test", "review")):
                    cur.execute(
                        "INSERT INTO agent_acts (act_id, project_id, role_key, kind, status, root_act_id, "
                        "handed_by_kind, how, accountable, code_task_run_id, label, started_at, ended_at, "
                        "tokens_in, tokens_out, cost_usd) VALUES (%s,%s,%s,'stage','complete',%s,'person','next_stage',"
                        "'ceo',%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (f"act_perf_{rid}_{s}", PROJECT, ROLES[(a + s) % len(ROLES)], f"act_perf_{rid}_0",
                         rid, stage, created + timedelta(minutes=s * 5), created + timedelta(minutes=s * 5 + 4),
                         rnd.randint(1000, 9000), rnd.randint(200, 3000), round(rnd.random() / 5, 4)))
                n_runs += 1
        counts["code_task_runs"] = n_runs
        # workflow runs on the installed definitions
        cur.execute("SELECT d.id, v.id FROM workflow_definitions d JOIN workflow_versions v "
                    "ON v.workflow_definition_id = d.id WHERE d.project_id = %s LIMIT 12", (PROJECT,))
        defs = cur.fetchall()
        n_wf = 0
        for i in range(40 * scale if defs else 0):
            did, vid = defs[i % len(defs)]
            wid = f"wfr_perf_{i}"
            st = rnd.choice(["completed", "completed", "failed", "running", "waiting"])
            created = now - timedelta(hours=i * 3)
            cur.execute(
                "INSERT INTO workflow_runs (id, project_id, workflow_definition_id, workflow_version_id, "
                "actor, status, input, created_at, updated_at) VALUES (%s,%s,%s,%s,'ceo',%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (wid, PROJECT, did, vid, st, json.dumps({"brief": "Seeded run " * 5}), created, created))
            for s in range(6):
                cur.execute(
                    "INSERT INTO workflow_step_runs (id, project_id, workflow_run_id, step_id, step_kind, "
                    "status, output, created_at, updated_at) VALUES (%s,%s,%s,%s,'agent',%s,%s,%s,%s) "
                    "ON CONFLICT DO NOTHING",
                    (f"wsr_perf_{i}_{s}", PROJECT, wid, f"step{s}",
                     "succeeded" if s < 4 else rnd.choice(["pending", "running", "succeeded"]),
                     json.dumps({"summary": "did the thing " * 10}), created, created))
            n_wf += 1
        counts["workflow_runs"] = n_wf
        # activity
        n_ev = 3000 * scale
        cur.executemany(
            "INSERT INTO events (id, project_id, event_type, actor, entity_type, entity_id, data, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            [(f"evt_perf_{i}", PROJECT, rnd.choice(["task.updated", "decision.created", "code_task.phase",
                                                     "workflow.step_completed", "deploy.started"]),
              rnd.choice(ROLES), "task", f"task_perf_{i % n_tasks}",
              json.dumps({"note": "seeded", "i": i}), now - timedelta(minutes=i * 3)) for i in range(n_ev)])
        counts["events"] = n_ev
        conn.commit()
    return counts


if __name__ == "__main__":
    args = sys.argv[1:]
    scale = 1
    if "--scale" in args:
        k = args.index("--scale")
        scale = int(args[k + 1])
        del args[k:k + 2]
    print(json.dumps(seed(args[0], scale)))
