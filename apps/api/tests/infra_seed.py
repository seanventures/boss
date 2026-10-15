"""One Infra world, seeded from SQL, shared by the Infra view tests (engine and API).

The ids are the parity fixture ids (apps/web/lib/infra-obs-parity.ts) so the lead can reuse the
same world for the local stack seed:

  machines   h_ready (Ready), h_setup (Setting up · 1/5), h_down (Unreachable), h_retired
  apps       t_live   invoice · prod     on h_ready   Up, delivery deploys, newest succeeded
             t_slow   team-inbox · dev   on h_ready   Slow (latency over 2 s)
             t_down   api · prod         on h_down    Down; newest delivery deploy failed
             t_stack  shop · production  on h_ready   stack: sd_ok healthy, sd_failed newest
             t_wait   shop · staging     on h_ready   stack: sd_wait awaiting approval
             t_cloud  billing · prod     no machine   registry run rr_created on account pa_aws
             t_never  notes · staging    no machine   never deployed
  previews   pv_live (running :4127), pv_fail (port_timeout), pv_ended (stopped)
  this computer: hardware place hp_local, hostname boss-1 (matches no machine)
  monitoring: an `observability.stack_deployed` event borrowing t_live (so it sits on h_ready)

Every timestamp is relative to now() so the 24 h strips are full.
"""
from __future__ import annotations

from sqlalchemy import text

KEY = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIG9vb2Jvc3NrZXlmb3J0ZXN0aW5nb25seTEyMzQ1Njc4 "
       "boss@h_ready")
SHA = {k: (k * 40)[:40] for k in "abcdef"}
SHA["live"] = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
SHA["old"] = "77aa01b4e5f60718293a4b5c6d7e8f9012345678"
SHA["fail"] = "e1f2a3b4e5f60718293a4b5c6d7e8f9012345678"

HOSTS = ("h_ready", "h_setup", "h_down", "h_retired")
TARGETS = ("t_live", "t_slow", "t_down", "t_stack", "t_wait", "t_cloud", "t_never")


def _x(c, sql: str, **p) -> None:
    c.execute(text(sql), p)


def _ensure_project(c, project_id: str) -> None:
    """The project the world is seeded into, made if absent (a fresh test database has none; an
    existing one is left exactly as it is). Never removed by clear: other tests share it."""
    _x(c, "INSERT INTO orgs (id, name) VALUES ('org_infra_seed', 'infra seed') "
          "ON CONFLICT DO NOTHING")
    _x(c, "INSERT INTO workspaces (id, org_id, name) VALUES ('ws_infra_seed', 'org_infra_seed', "
          "'infra seed') ON CONFLICT DO NOTHING")
    _x(c, "INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, 'ws_infra_seed', :p, "
          ":p) ON CONFLICT DO NOTHING", p=project_id)


def seed(engine, project_id: str = "boss-dogfood", *, samples: bool = True) -> dict:
    """Seed the world into `project_id`. Returns the ids. Idempotent per test (delete first)."""
    with engine.begin() as c:
        _ensure_project(c, project_id)
        clear(c, project_id)
        _x(c, "INSERT INTO provider_accounts (id, project_id, provider, account_label, "
              "created_by) VALUES ('pa_aws', :p, 'aws', 'prod-aws', 'test') "
              "ON CONFLICT (id) DO NOTHING", p=project_id)
        for hid, name, addr, status, pf, ref_key, maxa in (
            ("h_ready", "edge-ready", "203.0.113.10", "verified",
             '{"reachable": true, "problems": [], "findings": []}', True, 4),
            ("h_setup", "db-box", "10.0.0.9", "registered", "{}", False, None),
            ("h_down", "edge-2", "203.0.113.12", "degraded",
             '{"reachable": false, "problems": ["Permission denied (publickey)"]}', True, 4),
            ("h_retired", "old-box", "10.0.0.99", "retired", "{}", False, None),
        ):
            sref = None
            if ref_key:
                sref = f"sref_{hid}"
                _x(c, "INSERT INTO secret_refs (id, project_id, scope_type, scope_id, ref_kind, "
                      "ref_key, secret_kind, storage_backend, created_by) VALUES "
                      "(:i, :p, 'deployment_host', :h, 'database', :k, 'private_key', "
                      "'database', 'test') ON CONFLICT (id) DO NOTHING",
                   i=sref, p=project_id, h=hid, k=f"K_{project_id}_{hid}")
            _x(c, "INSERT INTO deployment_hosts (id, project_id, name, address, ssh_user, arch, "
                  "port_min, port_max, max_apps, status, last_preflight, last_preflight_at, "
                  "key_secret_ref, key_public, created_by) VALUES (:i, :p, :n, :a, 'deploy', "
                  "'amd64', 8000, 8099, :m, :s, CAST(:pf AS JSONB), "
                  "CASE WHEN CAST(:pf AS text) = '{}' THEN NULL ELSE now() - interval '2 minutes' END, "
                  ":r, :k, 'test')",
               i=hid, p=project_id, n=name, a=addr, m=maxa, s=status, pf=pf, r=sref,
               k=KEY if ref_key else None)
        for tid, app, env, kind, host, domains, account in (
            ("t_live", "invoice", "prod", "ssh", "h_ready", '["203.0.113.10:8001"]', None),
            ("t_slow", "team-inbox", "dev", "ssh", "h_ready", '["203.0.113.10:8002"]', None),
            ("t_down", "api", "prod", "ssh", "h_down", '["203.0.113.12:8003"]', None),
            ("t_stack", "shop", "production", "stack", "h_ready", "[]", None),
            ("t_wait", "shop", "staging", "stack", "h_ready", "[]", None),
            ("t_cloud", "billing", "prod", "aws_ecs", None, '["billing.example.test"]', "pa_aws"),
            ("t_never", "notes", "staging", "ssh", None, "[]", None),
        ):
            _x(c, "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, "
                  "target_kind, target_ref, domains, host_id, provider_account_id, status, "
                  "created_by) VALUES (:i, :p, :a, :r, :e, :k, 'deploy@x:22', "
                  "CAST(:d AS JSONB), :h, :acc, 'active', 'test')",
               i=tid, p=project_id, a=app, r=f"acme/{app}", e=env, k=kind, d=domains, h=host,
               acc=account)
        # Delivery deploys (trigger set), oldest first.
        for rid, tid, status, sha, trig, ago_min, steps in (
            ("dr_live_old", "t_live", "succeeded", SHA["old"], "manual", 3000, 6),
            ("dr_live", "t_live", "succeeded", SHA["live"], "merge", 3, 6),
            ("dr_down_ok", "t_down", "succeeded", SHA["old"], "manual", 2000, 6),
            ("dr_down_fail", "t_down", "failed", SHA["fail"], "manual", 60, 3),
            ("dr_slow", "t_slow", "succeeded", SHA["b"], "workflow", 2880, 6),
        ):
            step_json = ",".join(
                '{"step": "s%d", "label": "Step %d", "status": "%s"}'
                % (i, i, "failed" if status == "failed" and i == steps - 1 else "ok")
                for i in range(steps))
            _x(c, "INSERT INTO deployment_runs (id, project_id, deployment_target_id, app_name, "
                  "environment, repo, target_kind, target_ref, commit_ref, status, trigger, steps, "
                  "created_by, created_at, started_at, finished_at, duration_ms, failure_reason) "
                  "SELECT :i, :p, t.id, t.app_name, t.environment, t.repo, t.target_kind, "
                  "t.target_ref, :sha, :s, :tr, CAST(:steps AS JSONB), 'ceo', "
                  "now() - make_interval(mins => :ago + 2), now() - make_interval(mins => :ago + 2), "
                  "now() - make_interval(mins => :ago), 112000, "
                  "CASE WHEN CAST(:s AS text) = 'failed' THEN 'health check failed: GET / returned 502' END "
                  "FROM deployment_targets t WHERE t.id = :t",
               i=rid, p=project_id, t=tid, s=status, sha=sha, tr=trig,
               steps=f"[{step_json}]", ago=ago_min)
        # A registry run (no trigger): planned through POST /v1/deployment-runs.
        _x(c, "INSERT INTO deployment_runs (id, project_id, deployment_target_id, app_name, "
              "environment, repo, target_kind, target_ref, commit_ref, status, created_by, "
              "created_at) SELECT 'rr_created', :p, t.id, t.app_name, t.environment, t.repo, "
              "t.target_kind, t.target_ref, 'main', 'created', 'ceo', now() - interval '20 minutes' "
              "FROM deployment_targets t WHERE t.id = 't_cloud'", p=project_id)
        # Stack deploys.
        _x(c, "INSERT INTO decisions (id, project_id, question, status, risk_tier, "
              "approved_by, approved_at) VALUES ('dec_sd_ok', :p, 'Deploy shop?', 'approved', 2, "
              "'ceo', now() - interval '2 days'), ('dec_sd_failed', :p, 'Deploy shop?', "
              "'approved', 2, 'ceo', now() - interval '2 hours'), ('dec_sd_wait', :p, "
              "'Deploy shop to staging?', 'pending', 2, NULL, NULL)", p=project_id)
        for sid, tid, status, sha, dec, ago_min, applied in (
            ("sd_ok", "t_stack", "healthy", SHA["old"], "dec_sd_ok", 2880, True),
            ("sd_failed", "t_stack", "failed", SHA["fail"], "dec_sd_failed", 100, True),
            ("sd_wait", "t_wait", "awaiting_approval", SHA["live"], "dec_sd_wait", 2, False),
        ):
            _x(c, "INSERT INTO stack_deployments (id, project_id, target_id, host_id, repository, "
                  "ref, commit_sha, status, decision_id, steps, log_tail, manifest, preflight, "
                  "error, failure_reason, created_by, created_at, apply_started_at, finished_at) "
                  "VALUES (:i, :p, :t, 'h_ready', 'acme/shop', 'main', :sha, :s, :d, "
                  "CAST(:steps AS JSONB), :log, CAST(:m AS JSONB), "
                  "'{\"ok\": true, \"host\": \"edge-ready\"}'::jsonb, CAST(:err AS JSONB), :fr, "
                  "'ceo', now() - make_interval(mins => :ago + 3), "
                  "CASE WHEN CAST(:ap AS boolean) THEN now() - make_interval(mins => :ago + 2) END, "
                  "CASE WHEN CAST(:s AS text) IN ('healthy', 'failed') THEN now() - make_interval(mins => :ago) END)",
               i=sid, p=project_id, t=tid, s=status, sha=sha, d=dec, ago=ago_min, ap=applied,
               steps=('[{"name": "prepare", "status": "ok"}, {"name": "upload", "status": "ok"},'
                      ' {"name": "health", "status": "%s"}, {"name": "finalize", "status": '
                      '"pending"}]' % ("failed" if status == "failed" else
                                       "ok" if status == "healthy" else "pending")),
               log="line 1\nline 2\nTRACE manifest secret value never here\n",
               m='{"steps": {"install": ["./install.sh"]}, "env_keys": ["DB_PASSWORD"], '
                 '"health": [{"url": "http://127.0.0.1:8000/health", "expect": 200}]}',
               err=('{"headline": "Health check failed", "action": "Open the log"}'
                    if status == "failed" else None),
               fr="Health check failed" if status == "failed" else None)
        # Previews (BOSS machine).
        for pid_, status, port, fk, ago, exp in (
            ("pv_live", "running", 4127, None, 19, 41),
            ("pv_fail", "failed", 4120, "port_timeout", 40, None),
            ("pv_ended", "stopped", 4119, None, 180, None),
        ):
            _x(c, "INSERT INTO app_previews (id, project_id, run_id, repo, branch, port, url, "
                  "mode, status, failure_kind, created_at, expires_at, last_seen_at) VALUES "
                  "(:i, :p, :r, 'acme/search', 'feat/search', :port, :u, 'docker', :s, :fk, "
                  "now() - make_interval(mins => :ago), "
                  "now() + make_interval(mins => CAST(:exp AS int)), "
                  "now() - interval '1 minute')",
               i=pid_, p=project_id, r=f"run_{pid_}", port=port,
               u=f"http://127.0.0.1:{port}/" if status == "running" else None, s=status, fk=fk,
               ago=ago, exp=exp)
        # The builds the live and failed previews ran (pv_ended's build is gone: its Restart is
        # not offered).
        for run in ("run_pv_live", "run_pv_fail"):
            _x(c, "INSERT INTO code_task_runs (id, project_id, title, status, phase, spec) VALUES "
                  "(:i, :p, 'Search', 'completed', 'done', '{}'::jsonb) ON CONFLICT (id) DO NOTHING",
               i=run, p=project_id)
        # This computer.
        _x(c, "INSERT INTO hardware_places (id, project_id, kind, name, status, agent_state, "
              "is_default, hostname, arch, os_name, created_by) VALUES ('hp_local', :p, 'local', "
              "'This computer', 'ready', 'installed', true, 'boss-1', 'arm64', 'linux', 'test') "
              "ON CONFLICT DO NOTHING", p=project_id)
        _x(c, "UPDATE hardware_places SET hostname = 'boss-1', arch = 'arm64', status = 'ready', "
              "agent_state = 'installed' WHERE project_id = :p AND is_default", p=project_id)
        # The monitoring stack, as BOSS recorded deploying it.
        _x(c, "INSERT INTO events (id, project_id, event_type, actor, entity_type, entity_id, "
              "data) VALUES (:i, :p, 'observability.stack_deployed', 'ceo', "
              "'deployment_target', 't_live', CAST(:d AS JSONB))",
           i=f"ev_mon_{project_id}", p=project_id,
           d='{"host": "203.0.113.10", "grafana": "http://203.0.113.10:3006", "apps": 2}')
        if samples:
            seed_samples(c, project_id)
    return {"hosts": HOSTS, "targets": TARGETS}


def seed_samples(c, project_id: str) -> None:
    """24 h of 120 s probes: t_live all ok and fast; t_slow ok but slow lately; t_down failing
    for its last 4 sweeps."""
    _x(c, "INSERT INTO deployment_health_samples (id, project_id, deployment_target_id, "
          "checked_at, ok, status_code, latency_ms) "
          "SELECT 'hs_' || t || '_' || g, :p, t, now() - make_interval(secs => g * 120), "
          "  CASE WHEN t = 't_down' AND g < 4 THEN false ELSE true END, "
          "  CASE WHEN t = 't_down' AND g < 4 THEN 502 ELSE 200 END, "
          "  CASE WHEN t = 't_slow' AND g < 5 THEN 2600 ELSE 120 + (g % 7) END "
          "FROM unnest(ARRAY['t_live', 't_slow', 't_down']) AS t, generate_series(0, 719) AS g",
       p=project_id)


def clear(c, project_id: str) -> None:
    for sql in (
        "DELETE FROM deployment_health_samples WHERE project_id = :p",
        "DELETE FROM stack_deployments WHERE project_id = :p",
        "DELETE FROM deployment_runs WHERE project_id = :p",
        "DELETE FROM deployment_targets WHERE project_id = :p",
        "DELETE FROM deployment_hosts WHERE project_id = :p",
        "DELETE FROM secret_refs WHERE project_id = :p AND scope_type = 'deployment_host'",
        "DELETE FROM app_previews WHERE project_id = :p",
        "DELETE FROM code_task_runs WHERE id IN ('run_pv_live', 'run_pv_fail') AND project_id = :p",
        "DELETE FROM decisions WHERE id IN ('dec_sd_ok', 'dec_sd_failed', 'dec_sd_wait')",
        "DELETE FROM events WHERE project_id = :p AND event_type LIKE 'observability.stack%'",
    ):
        c.execute(text(sql), {"p": project_id})
