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

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from ..config import Settings
from ..db import begin
from .code_task_service import load_code_task_run
from .core import emit_event


def _app_name(repo: str, name: Optional[str]) -> str:
    base = (name or repo or "app").lower()
    base = re.sub(r"[^a-z0-9-]+", "-", base).strip("-") or "app"
    return base[:40]


# Where deployed apps live on a target host. One constant because the connector's preflight
# must check the SAME directory the deploy writes to — a precondition checked against a second
# copy of a path is a precondition that silently stops matching.
REMOTE_BASE = "/opt/boss-apps"

# Host/user land in a local `shell=True` pipeline (tar|ssh). Reject metacharacters so a
# deploy request cannot run arbitrary commands on the BOSS API host.
_SAFE_HOST_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
_SAFE_USER_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_ssh_target(host: str, user: str) -> None:
    host = (host or "").strip()
    user = (user or "").strip()
    if not host or host == "<HOST>" or not _SAFE_HOST_RE.fullmatch(host):
        raise ValueError(f"invalid deploy host: {host!r}")
    if not user or not _SAFE_USER_RE.fullmatch(user):
        raise ValueError(f"invalid deploy user: {user!r}")


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
    remote = f"{REMOTE_BASE}/{app}"

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


# --- Phase 2 (actuate) + Phase 3 (health-gate + rollback) --------------------------------------
# Key files BOSS itself materialised from BOSS_SSH_HOST_PRIVATE_KEY and therefore owns. Only
# these may be deleted after a deploy.
#
# This set exists because the cleanup path used to unlink whatever `_ssh_key_file` returned —
# which in the (preferred) file-reference mode is the OPERATOR'S OWN private key. Every deploy
# destroyed the credential it had just used, and the next one failed with "does not exist".
# That is what happened to ~/.ssh/kaappu-demo.pem, and it is why deploys from this laptop had
# been broken long before anyone looked: the failure appeared one run after its cause.
#
# The general shape: a function that returns "a path" from two sources, where ownership differs
# per source but the caller cannot tell them apart. Cleanup must follow ownership, never the
# value.
_EPHEMERAL_KEY_FILES: set[str] = set()


def _discard_key_file(path: str) -> None:
    """Remove a key file only if BOSS created it. A referenced key is never ours to delete."""
    if path in _EPHEMERAL_KEY_FILES:
        try:
            os.unlink(path)
        except OSError:
            pass
        _EPHEMERAL_KEY_FILES.discard(path)


def _ssh_key_file(settings: Settings) -> str:
    # Prefer a PATH to the key over the key material in an env var. A private key pasted into
    # configuration ends up in process listings, crash dumps and, sooner or later, a commit.
    # A file reference keeps the secret on disk at 0600 and lets the operator manage it with
    # normal key hygiene; the material form stays supported for containerised deployments
    # where mounting a file is awkward.
    key_path = (getattr(settings, "ssh_host_private_key_file", "") or "").strip()
    if key_path:
        expanded = os.path.expanduser(key_path)
        if not os.path.isfile(expanded):
            raise RuntimeError(
                f"BOSS_SSH_HOST_PRIVATE_KEY_FILE points at {key_path!r}, which does not exist")
        if (os.stat(expanded).st_mode & 0o077):
            raise RuntimeError(
                f"{key_path} is group/world readable; ssh will refuse it. chmod 600 it.")
        return expanded
    key = (getattr(settings, "ssh_host_private_key", "") or "").strip()
    if not key:
        raise RuntimeError(
            "No SSH key configured: set BOSS_SSH_HOST_PRIVATE_KEY_FILE (path, preferred) "
            "or BOSS_SSH_HOST_PRIVATE_KEY (material)")
    fd, path = tempfile.mkstemp(suffix=".pem")
    with os.fdopen(fd, "w") as h:
        h.write(key if key.endswith("\n") else key + "\n")
    os.chmod(path, 0o600)
    # BOSS made this one, so BOSS may delete it. The referenced-file branch above returns
    # without registering, which is what keeps the operator's key safe.
    _EPHEMERAL_KEY_FILES.add(path)
    return path


def _ssh(key_path: str, host: str, user: str, port: int, command: str, timeout: int = 180):
    return subprocess.run(
        ["ssh", "-i", key_path, "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
         "-p", str(port), f"{user}@{host}", command],
        capture_output=True, text=True, timeout=timeout)


def _transfer_shell_cmd(*, work: Path, key_path: str, host: str, user: str, ssh_port: int,
                        remote: str) -> str:
    """Build the local tar|ssh pipeline. Host/user MUST be validated + shell-quoted."""
    dest = shlex.quote(f"{user}@{host}")
    # remote is derived from sanitized _app_name; still quote for defense in depth.
    remote_q = shlex.quote(remote)
    remote_cmd = f"rm -rf {remote_q}/dist && mkdir -p {remote_q} && tar xzf - -C {remote_q}"
    return (
        f"tar czf - -C {shlex.quote(str(work))} dist | ssh -i {shlex.quote(key_path)} "
        f"-o StrictHostKeyChecking=no -o BatchMode=yes -p {int(ssh_port)} {dest} "
        f"{shlex.quote(remote_cmd)}"
    )


def _docker_run_cmd(app: str, app_port: int, remote: str, mode: str = "static",
                    entry: str = "") -> str:
    """Start the app container, tolerating the port not being free *yet*.

    `docker rm -f` returns before the daemon has finished tearing the endpoint down, so a
    `run` issued immediately after can fail with "driver failed programming external
    connectivity" even though nothing actually holds the port — a retry a second later
    succeeds. That is a race, not a busy port, and it made a working deployment fail.

    Only the port-contention failures are retried; any other error exits immediately, because
    retrying a genuinely broken run just turns a clear error into a slow one.
    """
    app_q = shlex.quote(app)
    remote_q = shlex.quote(remote)
    if mode == "py":
        # Mounted read-WRITE: the app keeps a SQLite file beside its source, and a read-only
        # mount turns every write into a 500 that a "/" health check would never see.
        run = (f"docker run -d --name {app_q} --restart unless-stopped -p {int(app_port)}:80 "
               f"-v {remote_q}/dist:/app -w /app -e PORT=80 -e HOST=0.0.0.0 "
               f"python:3.12-slim python {shlex.quote(entry)}")
    else:
        run = (f"docker run -d --name {app_q} --restart unless-stopped -p {int(app_port)}:80 "
               f"-v {remote_q}/dist:/usr/share/nginx/html:ro nginx:alpine")
    return (
        f"for i in 1 2 3 4 5 6; do "
        f"  out=$({run} 2>&1) && {{ echo \"$out\"; exit 0; }}; "
        f"  case \"$out\" in "
        f"    *'external connectivity'*|*'already allocated'*|*'already in use'*) "
        f"      docker rm -f {app_q} >/dev/null 2>&1; sleep 2 ;; "
        f"    *) echo \"$out\" >&2; exit 1 ;; "
        f"  esac; "
        f"done; echo \"$out\" >&2; exit 1"
    )


def _rollback_remote(key_path: str, host: str, user: str, ssh_port: int, *, remote: str,
                     app: str, app_port: int, mode: str = "static", entry: str = ""):
    """Restore staged previous dist and ensure a container is serving it.

    Called after transfer wipe, docker-run failure (live container already removed), or a
    failed health gate. Prefer restart when the container still exists; otherwise re-run.
    """
    remote_q = shlex.quote(remote)
    app_q = shlex.quote(app)
    return _ssh(
        key_path, host, user, ssh_port,
        f"if [ -d {remote_q}/dist.prev ]; then "
        f"rm -rf {remote_q}/dist && mv {remote_q}/dist.prev {remote_q}/dist; fi; "
        f"if docker inspect {app_q} >/dev/null 2>&1; then docker restart {app_q}; "
        f"else {_docker_run_cmd(app, app_port, remote, mode, entry)}; fi || true",
        timeout=180,
    )


def _resolve_repo_branch(run: dict, settings: Settings) -> tuple[str, str, str]:
    spec = run.get("spec") or {}
    owner = spec.get("owner") or settings.target_github_owner
    repo = spec.get("repo") or settings.target_github_repo
    fctx = run.get("finalize_ctx") or {}
    branch = fctx.get("work_branch") or f"boss/code/{fctx.get('root_run_id') or run['id']}"
    return owner, repo, branch


def _build_dist(settings: Settings, owner: str, repo: str, branch: str,
                changed: Optional[list[str]] = None) -> tuple[Path, str, str]:
    """Prepare the deployable payload for a run's branch.

    Returns (root, mode, entry) where `root` always contains a `dist/` directory to transfer:

      mode "static" — dist/ is a servable file tree, run under nginx.
      mode "py"     — dist/ is the app source, run under python; `entry` is the server script.

    One payload name for both modes keeps staging, rollback and cleanup single-implementation;
    only the container command varies.
    """
    token = settings.github_token
    from .repo_host import clone_url_for  # BOSS-267: host-aware clone
    url = clone_url_for(getattr(settings, "target_repo_host", "github") or "github",
                        owner, repo, token or "")
    work = Path(tempfile.mkdtemp(prefix="boss-deploy-"))
    subprocess.run(["git", "clone", "--depth", "1", "--branch", branch, url, str(work)],
                   capture_output=True, text=True, timeout=180, check=True)
    # NOT EVERY APP HAS A BUILD. This ran `npm ci && npm run build` unconditionally and failed
    # on the first real deployment: the stock app is deliberately no-build vanilla JS, with a
    # package.json but no build script. Assuming a Node pipeline turns "nothing to build" into
    # a deploy failure for exactly the simplest apps.
    import json as _json

    pkg = work / "package.json"
    has_build = False
    if pkg.is_file():
        try:
            has_build = bool((_json.loads(pkg.read_text()).get("scripts") or {}).get("build"))
        except Exception:  # noqa: BLE001 — an unreadable package.json is "no build"
            has_build = False

    if has_build:
        env = {**os.environ, "HOME": str(work), "npm_config_cache": str(work / ".npm"),
               "npm_config_fund": "false", "npm_config_audit": "false"}
        subprocess.run(["sh", "-lc", "npm ci --no-audit --no-fund && npm run build"],
                       cwd=str(work), env=env, capture_output=True, text=True,
                       timeout=900, check=True)
        if not (work / "dist").is_dir():
            raise RuntimeError("build script ran but did not produce ./dist")
        return (work, "static", "")

    # No build step: the servable tree IS the source, and WHICH tree is the question the preview
    # resolver already answers. The previous code said it used that answer and then implemented
    # its own — first `*/index.html` in alphabetical order, with no idea what this task built.
    # In a repo holding several apps that deploys someone else's: a stock-app target went live
    # serving SUDOKU, and the health check passed, because a 200 from the wrong app is still a
    # 200. Health cannot catch identity; only picking correctly can.
    from .preview_service import AmbiguousAppError, resolve_app_entry

    static_dir = None
    try:
        kind, value = resolve_app_entry(work, changed)
    except AmbiguousAppError as exc:
        # Fail closed, exactly as the preview does. Deploying the wrong app to a real host is
        # worse than deploying nothing, and worse here than in preview: it is publicly served.
        raise RuntimeError(
            f"cannot tell which app to deploy — candidates: {exc}. "
            "Have the task build a single app, or set boss.preview.command.") from exc

    if kind == "py":
        # A server, not a file tree. The stock app serves its own UI *and* its /api/* routes
        # from one process, so shipping only web-stock/ to nginx would render a UI in which
        # every action fails — a deployment that looks healthy and does nothing.
        #
        # The payload is laid out under the SAME `dist` name the static path uses, so staging,
        # rollback and cleanup stay one implementation. Only the run command differs by mode.
        payload = Path(tempfile.mkdtemp(prefix="boss-deploy-payload-"))
        shutil.copytree(work, payload / "dist",
                        ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__"))
        shutil.rmtree(work, ignore_errors=True)
        return (payload, "py", value)

    if kind == "static":
        static_dir = work / value
    elif kind == "node":
        raise RuntimeError(
            f"the app resolved to node:{value} but declares no build script; "
            "add one producing ./dist, or deploy it to a runtime target.")
    elif (work / "dist").is_dir():
        static_dir = work / "dist"

    if static_dir is None or not (static_dir / "index.html").is_file():
        raise RuntimeError(
            "no build script and no index.html found — nothing servable to deploy")
    if static_dir == work / "dist":
        return (work, "static", "")
    dist = work / "dist"
    shutil.copytree(static_dir, dist)
    return (work, "static", "")


def deploy_ssh(engine, settings: Settings, run_id: str, *, host: str, user: str = "root",
               ssh_port: int = 22, app_port: int = 8080, name: Optional[str] = None) -> dict:
    """Actuate the deploy plan on a real host: build dist -> transfer -> nginx -> health-gate.
    On transfer/docker/health failure after staging, ROLL BACK to the previous build (Phase 3).
    Gated behind the outward-action flag + an SSH key; NEVER touches a host otherwise."""
    if not bool(getattr(settings, "ssh_host_deploy_allowed", False)):
        raise PermissionError("BOSS_SSH_HOST_DEPLOY_ALLOWED is false — the outward deploy gate is off")
    _validate_ssh_target(host, user)
    with begin(engine) as c:
        run = load_code_task_run(c, run_id)
    if run is None:
        raise ValueError(f"code task run not found: {run_id}")
    owner, repo, branch = _resolve_repo_branch(run, settings)
    project_id = run["project_id"]
    app = _app_name(repo, name)
    remote = f"{REMOTE_BASE}/{app}"
    key_path = _ssh_key_file(settings)
    work: Optional[Path] = None
    # Bound before the try: the rollback paths below read them, and a build that raises would
    # otherwise turn a deploy failure into a NameError that hides it.
    mode, entry = "static", ""
    needs_rollback = False
    rolled_back = False
    try:
        # The lineage's file list, not this attempt's: a repair touches only what it fixed, so
        # `state.impl_files` alone loses the app the task exists to build. Preview learned this
        # the expensive way; the deploy path must use the same source or it will diverge again.
        from .preview_service import lineage_impl_files

        work, mode, entry = _build_dist(settings, owner, repo, branch,          # 1 build
                                        changed=lineage_impl_files(engine, run))
        ct = _ssh(key_path, host, user, ssh_port, "docker version --format '{{.Server.Version}}'")
        if ct.returncode != 0:                                                  # 2 connection test
            raise RuntimeError(f"host unreachable or docker missing: {(ct.stderr or '')[:900]}")
        _ssh(key_path, host, user, ssh_port,                                    # 3 stage previous
             f"mkdir -p {shlex.quote(remote)} && "
             f"([ -d {shlex.quote(remote)}/dist ] && rm -rf {shlex.quote(remote)}/dist.prev && "
             f"cp -a {shlex.quote(remote)}/dist {shlex.quote(remote)}/dist.prev || true)")
        needs_rollback = True  # host may have dist wiped / container replaced from here on
        tar = subprocess.run(                                                   # 4 transfer (tar|ssh)
            _transfer_shell_cmd(work=work, key_path=key_path, host=host, user=user,
                                ssh_port=ssh_port, remote=remote),
            shell=True, capture_output=True, text=True, timeout=600)
        if tar.returncode != 0:
            _rollback_remote(key_path, host, user, ssh_port, remote=remote, app=app,
                             app_port=app_port, mode=mode, entry=entry)
            rolled_back = True
            raise RuntimeError(f"transfer failed: {(tar.stderr or '')[:900]}")
        dep = _ssh(key_path, host, user, ssh_port,                              # 5 deploy nginx
                   f"docker rm -f {shlex.quote(app)} 2>/dev/null; "
                   f"{_docker_run_cmd(app, app_port, remote, mode, entry)}")
        if dep.returncode != 0:
            # Live container was removed above — restore previous dist and re-run nginx.
            _rollback_remote(key_path, host, user, ssh_port, remote=remote, app=app,
                             app_port=app_port, mode=mode, entry=entry)
            rolled_back = True
            with begin(engine) as c:
                emit_event(c, project_id, "deploy.rolled_back", "boss", "code_task_run", run_id,
                           {"app": app, "host": host, "reason": "docker_run_failed",
                            "detail": (dep.stderr or "")[:900]})
            raise RuntimeError(f"docker run failed: {(dep.stderr or '')[:900]}")
        time.sleep(3)
        hc = _ssh(key_path, host, user, ssh_port,                              # 6 health gate
                  f"curl -fsS -o /dev/null -w '%{{http_code}}' http://localhost:{int(app_port)}/")
        code = (hc.stdout or "").strip()
        if not code.startswith("2"):                                           # Phase 3 rollback
            _rollback_remote(key_path, host, user, ssh_port, remote=remote, app=app,
                             app_port=app_port, mode=mode, entry=entry)
            rolled_back = True
            with begin(engine) as c:
                emit_event(c, project_id, "deploy.rolled_back", "boss", "code_task_run", run_id,
                           {"app": app, "host": host, "health": code})
            return {"status": "rolled_back", "app": app, "host": host, "health": code,
                    "detail": f"health check returned {code or 'no response'}; rolled back to previous build"}
        needs_rollback = False
        url = f"http://{host}:{app_port}/"
        with begin(engine) as c:
            emit_event(c, project_id, "deploy.succeeded", "boss", "code_task_run", run_id,
                       {"app": app, "host": host, "url": url, "health": code})
        return {"status": "deployed", "app": app, "host": host, "url": url, "health": code}
    except Exception:
        # Best-effort restore if we mutated the host and then crashed before an explicit rollback.
        if needs_rollback and not rolled_back:
            try:
                _rollback_remote(key_path, host, user, ssh_port, remote=remote, app=app,
                                 app_port=app_port, mode=mode, entry=entry)
            except Exception:  # noqa: BLE001 — never mask the original failure
                pass
        raise
    finally:
        _discard_key_file(key_path)
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
