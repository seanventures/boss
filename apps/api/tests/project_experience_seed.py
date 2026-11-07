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
from typing import Any

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
         gate_kind: str = "", env: str = "staging", merge_waits: bool = False) -> dict[str, Any]:
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
        db.execute(text(
            "INSERT INTO decisions (id, project_id, question, recommendation, status, approved_by, "
            "approved_at, created_at) VALUES (:i, :p, :q, CAST(:r AS JSONB), 'approved', 'ceo', :t, :t)"),
            {"i": _id("dec"), "p": project_id, "q": f"Merge PR #{number}: {title}?",
             "t": now - timedelta(days=days) + timedelta(hours=2),
             "r": json.dumps({"action": "review_pull_request", "pull_request_url": got["pr_url"]})})
        merged.append(got)
    running = _run(db, project_id, did, vid, spec, title="Ticket search filters", status="running",
                   upto=build_step, at=now - timedelta(minutes=12))
    merge = _run(db, project_id, did, vid, spec, title="Visual refresh finish", status="running",
                 upto=by_cap.get("report") or build_step, at=now - timedelta(hours=3), pr=3,
                 merge_waits=True)
    deploy = _run(db, project_id, did, vid, spec, title="Email-to-ticket intake", status="waiting",
                  upto=deploy_step, at=now - timedelta(hours=1), pr=5,
                  gate="Deploy Email-to-ticket intake to staging?", env="staging")

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
    shipping = seed_ship_story(db, project_id, target_id=target_id, host_id=host_id,
                               merged=merged, merge=merge, live_sdep=sdep, now=now)
    built = dossier.build(db, project_id=project_id, repo=REPO, paths=list(TREE), actor="seed",
                          seed_signals=False)
    return {"project_id": project_id, "binding_id": binding, "merged": merged, "running": running,
            "merge": merge, "deploy": deploy, "target_id": target_id, "host_id": host_id,
            "stack_deployment_id": sdep, "recommendations": recs, "dossier_id": built["id"],
            "chain": chain, "shipping": shipping}


VISUAL_BRIEF = ("Finish the visual refresh the last change promised but did not deliver. The ticket "
                "list still looks plain. Do all of this in the existing web app: 1) An app header "
                "with the HelpDesk name and navigation. 2) Coloured priority and SLA chips.")
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
    return {"first": first, "pr1": pr1, "pr2": pr2, "superseded": stale,
            "superseded_decision": stale_decision}


__all__ = ["DEMO_USERS", "HOST_ADDRESS", "REPO", "STAGING_URL", "TREE", "seed"]
