"""Governed SSH-host deployment — take a sandbox-built app to a customer's own host.

Phased build:
  • Phase 1 (this): plan_ssh_deploy() produces the exact, safe deploy PLAN for a run — the steps,
    the SSH/docker commands it WOULD run, the health check, and the rollback — WITHOUT touching any
    host. Lets the operator see the whole deploy before a real host or credentials exist.
  • Phase 2: actuate the plan through ssh_host_adapter (transfer dist/, run nginx, health-check).
  • Phase 3: governed rollout — keep the previous release, health-gate, auto-rollback.

Static frontend apps (Vite/React) need NO image build and NO registry: build dist/ (BOSS has
Node), rsync it to the host, and serve it with stock `nginx:alpine` bind-mounting the files. The
host only needs Docker (which ssh_host_adapter already assumes) — nothing else to install.
"""
from __future__ import annotations

import re
from typing import Optional

from ..config import Settings
from ..db import begin
from .code_task_service import load_code_task_run


def _app_name(repo: str, name: Optional[str]) -> str:
    base = (name or repo or "app").lower()
    base = re.sub(r"[^a-z0-9-]+", "-", base).strip("-") or "app"
    return base[:40]


def plan_ssh_deploy(engine, settings: Settings, run_id: str, *, host: str = "<HOST>",
                    name: Optional[str] = None, port: int = 8080, user: str = "root") -> dict:
    """Build the deploy plan for a run's built app. Pure/read-only — touches no host (dry run)."""
    with begin(engine) as c:
        run = load_code_task_run(c, run_id)
    if run is None:
        raise ValueError(f"code task run not found: {run_id}")
    spec = run.get("spec") or {}
    owner = spec.get("owner") or settings.target_github_owner
    repo = spec.get("repo") or settings.target_github_repo
    fctx = run.get("finalize_ctx") or {}
    branch = fctx.get("work_branch") or f"boss/code/{fctx.get('root_run_id') or run_id}"
    app = _app_name(repo, name)
    remote = f"/opt/boss-apps/{app}"

    # Static Vite/React build served by stock nginx with dist/ bind-mounted — no image, no registry.
    steps = [
        {"n": 1, "step": "build", "where": "BOSS (in-container, Node)",
         "cmd": "npm ci --no-audit --no-fund && npm run build   # produces ./dist"},
        {"n": 2, "step": "connection_test", "where": "SSH → host",
         "cmd": "docker version --format '{{.Server.Version}}'   # confirm host + docker reachable"},
        {"n": 3, "step": "stage previous (for rollback)", "where": "SSH → host",
         "cmd": f"[ -d {remote}/dist ] && rm -rf {remote}/dist.prev && cp -a {remote}/dist {remote}/dist.prev || true"},
        {"n": 4, "step": "transfer build", "where": "SSH → host",
         "cmd": f"rsync -az --delete ./dist/ {user}@{host}:{remote}/dist/"},
        {"n": 5, "step": "deploy (stock nginx, files bind-mounted)", "where": "SSH → host",
         "cmd": (f"docker rm -f {app} 2>/dev/null; docker run -d --name {app} --restart unless-stopped "
                 f"-p {port}:80 -v {remote}/dist:/usr/share/nginx/html:ro nginx:alpine")},
        {"n": 6, "step": "health check", "where": "SSH → host",
         "cmd": f"curl -fsS -o /dev/null -w '%{{http_code}}' http://localhost:{port}/   # expect 200"},
    ]
    rollback = {
        "when": "health check (step 6) is not 2xx within the timeout",
        "how": "swap the staged previous build back and restart the container",
        "cmd": (f"rm -rf {remote}/dist && mv {remote}/dist.prev {remote}/dist && docker restart {app}"),
    }
    return {
        "run_id": run_id,
        "app": app,
        "repo": f"{owner}/{repo}",
        "branch": branch,
        "strategy": "static-nginx — stock nginx:alpine serving a bind-mounted dist/ (no image build, no registry)",
        "target": {"host": host, "user": user, "ssh_port": 22, "app_port": port,
                   "url": f"http://{host}:{port}/"},
        "steps": steps,
        "rollback": rollback,
        "prerequisites": [
            "customer host reachable over SSH with Docker installed",
            "BOSS_SSH_HOST_PRIVATE_KEY set (SecretRef — never in chat/logs)",
            "BOSS_SSH_HOST_DEPLOY_ALLOWED=true (outward-action gate, off by default)",
        ],
        "actuation_gated": not bool(getattr(settings, "ssh_host_deploy_allowed", False)),
        "mode": "dry_run",
        "note": ("Phase 1 dry run — this is exactly what BOSS WOULD run; no host was contacted. "
                 "Provide host + SSH key and enable the gate to actuate (Phase 2)."),
    }


def render_plan_markdown(plan: dict) -> str:
    """Human-readable DEPLOY.md for the plan (committable to the repo / shown to the operator)."""
    t = plan["target"]
    lines = [
        f"# Deploy plan — {plan['app']}",
        "",
        f"- **Repo/branch:** `{plan['repo']}` @ `{plan['branch']}`",
        f"- **Strategy:** {plan['strategy']}",
        f"- **Target:** `{t['user']}@{t['host']}` → serves at **{t['url']}**",
        f"- **Actuation gated:** {plan['actuation_gated']} (Phase 2 turns this on with host creds)",
        "",
        "## Steps",
    ]
    for s in plan["steps"]:
        lines += [f"{s['n']}. **{s['step']}** _( {s['where']} )_", "   ```sh", f"   {s['cmd']}", "   ```"]
    lines += ["", "## Rollback", f"- **When:** {plan['rollback']['when']}",
              f"- **How:** {plan['rollback']['how']}", "   ```sh", f"   {plan['rollback']['cmd']}", "   ```",
              "", "## Prerequisites"]
    lines += [f"- {p}" for p in plan["prerequisites"]]
    lines += ["", f"> {plan['note']}"]
    return "\n".join(lines)
