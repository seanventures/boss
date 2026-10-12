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
from . import owned_process
from ..db import begin
from .code_task_service import load_code_task_run
from .core import emit_event
from .github_connections import github_token_for


def _app_name(repo: str, name: Optional[str]) -> str:
    base = (name or repo or "app").lower()
    base = re.sub(r"[^a-z0-9-]+", "-", base).strip("-") or "app"
    return base[:40]


# Where deployed apps live on a target host. One constant because the connector's preflight
# must check the SAME directory the deploy writes to — a precondition checked against a second
# copy of a path is a precondition that silently stops matching.
REMOTE_BASE = "/opt/boss-apps"


def ensure_dir_sh(path: str, user: str, *, ok: str, created: str, need: str) -> str:
    """Shell that makes `path` a directory `user` can write, printing `ok`, `created` or `need`.

    THE ONE WAY BOSS prepares a directory on a host. Tried in order: it is already there and
    writable; plain `mkdir -p` (root, or a writable parent — a VM reached as root often has no
    sudo at all); passwordless sudo. Only when all three fail is it a person's step, and `need`
    says so. A missing directory is not an unwritable one: preflight refused a fresh VM reached
    as root with "/opt/boss-apps is not writable by root", which nothing on that host prevented.
    """
    import shlex as _shlex

    d, u = _shlex.quote(path), _shlex.quote(user)
    return (f"if [ -d {d} ] && [ -w {d} ]; then echo {ok}; "
            f"elif mkdir -p {d} 2>/dev/null && [ -w {d} ]; then echo {created}; "
            f"elif sudo -n mkdir -p {d} 2>/dev/null && sudo -n chown {u} {d} 2>/dev/null; "
            f"then echo {created}; else echo {need}; fi")

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
                    name: Optional[str] = None, port: int = 8080, user: str = "root",
                    source: Optional[dict] = None) -> dict:
    """Build the deploy plan. Pure/read-only — touches no host (dry run).

    Accepts the same `source` as `deploy_ssh` so a target with no code task run can be planned.
    Fixing only deploy_ssh left this raising "code task run not found:" — and since
    `execute_deployment` calls plan() BEFORE apply(), the deployment still died on the very
    error the apply-side fix removed. A capability added to one path of a two-path flow is not
    added at all.
    """
    if source:
        owner = source.get("owner") or settings.target_github_owner
        repo = source.get("repo") or settings.target_github_repo
        branch = source.get("ref") or "main"
    else:
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
# That is what happened to an operator's ~/.ssh/demo-host.pem, and it is why deploys from a laptop had
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


def _materialise_key(material: str) -> str:
    """Write resolved key material to a 0600 temp file that BOSS owns and will delete.

    Registered as ephemeral for the same reason the env-material path is: cleanup must follow
    OWNERSHIP. This file is ours, so it goes; a referenced key file never does.
    """
    fd, path = tempfile.mkstemp(suffix=".pem")
    with os.fdopen(fd, "w") as handle:
        handle.write(material if material.endswith("\n") else material + "\n")
    os.chmod(path, 0o600)
    _EPHEMERAL_KEY_FILES.add(path)
    return path


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


def _kill_process_group(argv: list[str]) -> None:
    """Reap anything the timed-out command left behind.

    Best-effort by design: the caller has already decided this attempt failed, and a cleanup
    that raises would turn a handled timeout back into the unhandled exception this exists to
    prevent.
    """
    import signal

    try:
        target = " ".join(argv[:6])
        for line in owned_process.run(["ps", "-Ao", "pid=,pgid=,command="],
                                   capture_output=True, text=True, timeout=10).stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 3 or not parts[0].isdigit():
                continue
            if parts[2].startswith(target[:40]):
                try:
                    os.killpg(int(parts[1]), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
    except Exception:  # noqa: BLE001 — cleanup must never raise
        pass


def _ssh(key_path: str, host: str, user: str, port: int, command: str, timeout: int = 180,
         stdin_text: Optional[str] = None):
    """Run a command on the host. A host that does not answer is a RESULT, not an exception.

    Every caller here checks `returncode` — bootstrap, destroy, the deploy itself. Only one
    thing broke that contract: `subprocess.run` raises `TimeoutExpired` when the host hangs,
    and nothing caught it. So tearing down a target whose box had gone away returned HTTP 500
    with a subprocess traceback, when the honest answer is "the host did not respond in 180s".

    `ConnectTimeout` bounds the TCP connect separately, so an unroutable address fails in
    seconds instead of occupying the whole budget.
    """
    from .host_key_pin import ssh_options

    # Under a governed deploy the host's pinned key is enforced (host_key_pin); elsewhere the
    # long-standing option stands.
    argv = ["ssh", "-i", key_path, *ssh_options(), "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={max(5, min(timeout, 20))}",
            # ConnectTimeout bounds the TCP HANDSHAKE only. A host that accepts the connection
            # and then stops answering — a stopped VM, a dropped route, a firewall that
            # blackholes established flows — hangs indefinitely, which is exactly what a
            # decommissioned box does. ServerAlive* makes ssh itself give up after ~30s.
            "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
            # Never read the caller's stdin: an ssh that inherits a terminal can block waiting
            # on it forever, and BOSS runs these from API threads with no terminal at all.
            # (`stdin_text` is the one exception: a secret travels on stdin, never in argv.)
            *([] if stdin_text is not None else ["-n"]),
            "-p", str(port), f"{user}@{host}", command]
    try:
        # start_new_session puts ssh in its OWN process group, so the timeout path can kill the
        # whole tree rather than just the process Python is holding.
        result = owned_process.run(argv, capture_output=True, text=True, timeout=timeout,
                                   start_new_session=True,
                                   **({"input": stdin_text} if stdin_text is not None else {}))
        from .host_key_pin import note_result

        changed = note_result(result.returncode, result.stderr or "")
        if changed:
            return subprocess.CompletedProcess(argv, returncode=result.returncode,
                                               stdout=result.stdout or "", stderr=changed)
        return result
    except FileNotFoundError:
        # No ssh binary at all. Returned as a RESULT because every caller here checks
        # returncode — raising would surface as a 500 several layers up, naming nothing.
        return subprocess.CompletedProcess(
            argv, returncode=127,   # conventional "command not found"
            stdout="",
            stderr="the `ssh` client is not installed in this environment, so BOSS cannot "
                   "reach any host. Install openssh-client in the image that runs deployments.")
    except subprocess.TimeoutExpired:
        # `subprocess.run` kills only the direct child, and an ssh that has forked or is stuck
        # in an uninterruptible wait survives it. Orphans then accumulate: polling a dead host
        # every few seconds left dozens of live ssh processes, each holding an API worker
        # thread, until the console could not reach its own API. One unreachable host must not
        # be able to take down the control plane.
        _kill_process_group(argv)
        return subprocess.CompletedProcess(
            argv, returncode=124,   # the conventional timeout code, as `timeout(1)` uses
            stdout="",
            stderr=f"ssh to {user}@{host} did not respond within {timeout}s — the host may be "
                   f"stopped, unreachable, or blocking port {port}.")


def _transfer_shell_cmd(*, work: Path, key_path: str, host: str, user: str, ssh_port: int,
                        remote: str) -> str:
    """Build the local tar|ssh pipeline. Host/user MUST be validated + shell-quoted."""
    from .host_key_pin import ssh_options

    dest = shlex.quote(f"{user}@{host}")
    # remote is derived from sanitized _app_name; still quote for defense in depth.
    remote_q = shlex.quote(remote)
    # The tree being replaced may hold root-owned files written by the previous container.
    # `data` is created by the deploy user so the container inherits a directory it can write
    # without docker creating a root-owned one; it is never removed, which is the point of it.
    # Keep the remote program free of semicolon separators. Besides making the local command
    # shape auditable, this ensures there is exactly one metacharacter occurrence in an
    # adversarial host fixture — inside the one quoted destination token. The fallback removes
    # root-owned artifacts only when plain rm left the directory behind.
    dist_q = shlex.quote(f"{remote}/dist")
    parent_q = shlex.quote(remote)
    fallback = (
        f"([ ! -e {dist_q} ] || docker run --rm -v {parent_q}:/_x alpine:3 "
        f"rm -rf {shlex.quote('/_x/dist')} >/dev/null 2>&1)"
    )
    remote_cmd = (
        f"(rm -rf {dist_q} 2>/dev/null || true) && {fallback} && "
        f"mkdir -p {remote_q} {remote_q}/data && tar xzf - -C {remote_q}"
    )
    return (
        f"tar czf - -C {shlex.quote(str(work))} dist | ssh -i {shlex.quote(key_path)} "
        f"{' '.join(shlex.quote(o) for o in ssh_options())} -o BatchMode=yes "
        f"-p {int(ssh_port)} {dest} "
        f"{shlex.quote(remote_cmd)}"
    )


def _head_sha(work: Path) -> Optional[str]:
    """The commit actually checked out in the build tree, short form."""
    try:
        out = owned_process.run(["git", "-C", str(work), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=30)
        return (out.stdout or "").strip() or None
    except Exception:  # noqa: BLE001 — an unknown SHA must not fail a deploy
        return None


# Stamped on every container BOSS starts so a later teardown can PROVE what it is about to
# remove is ours. Without it, destroy would have to infer ownership from a name — and a name
# collision on a shared host (this one also runs unrelated production services) would mean
# deleting somebody else's container.
_OWNERSHIP_LABELS = "--label boss.managed=true --label boss.app={app}"


def _rm_rf_cmd(path: str) -> str:
    """Remove a path that MAY contain root-owned files, from an unprivileged shell.

    A container writes into its bind mount as root — `__pycache__`, a SQLite file, logs. The
    deploy user then cannot delete them, so the NEXT deploy dies on `rm: Permission denied` and
    the app is stuck on its first release forever. First deploy works, second fails: the same
    shape as the key-deletion bug, and just as invisible until someone deploys twice.

    Plain rm first because it is instant and covers the common case; a root container only when
    something survived. Docker is already a hard requirement of this connector, so this borrows
    a capability we know is present rather than adding sudo to the deploy user.
    """
    q = shlex.quote(path)
    return (f"rm -rf {q} 2>/dev/null; "
            f"[ -e {q} ] && docker run --rm -v {shlex.quote(os.path.dirname(path))}:/_x "
            f"alpine:3 rm -rf {shlex.quote('/_x/' + os.path.basename(path))} >/dev/null 2>&1; "
            f"true")


def app_env_file(remote: str) -> str:
    """Where a deploy writes the app's secrets for `docker run --env-file` (mode 600, outside dist
    so a release or a rollback never moves it)."""
    return f"{remote}/.boss/app.env"


def _app_env_text(values: dict) -> str:
    """docker's --env-file format: KEY=VALUE, literal, one per line (no quoting, no newlines)."""
    for key, value in values.items():
        if "\n" in str(value) or "\r" in str(value):
            raise ValueError(f"app secret {key} holds a line break, which a container environment "
                             "cannot carry; set it again as a single line")
    return "".join(f"{k}={v}\n" for k, v in values.items())


def _docker_run_cmd(app: str, app_port: int, remote: str, mode: str = "static",
                    entry: str = "", env_file: str = "") -> str:
    """Start the app container, tolerating the port not being free *yet*.

    `entry` (mode "py") is the shell command that starts the app from /app, as
    _python_deploy_command built it — never a bare file handed to `python`.

    `docker rm -f` returns before the daemon has finished tearing the endpoint down, so a
    `run` issued immediately after can fail with "driver failed programming external
    connectivity" even though nothing actually holds the port — a retry a second later
    succeeds. That is a race, not a busy port, and it made a working deployment fail.

    Only the port-contention failures are retried; any other error exits immediately, because
    retrying a genuinely broken run just turns a clear error into a slow one.
    """
    app_q = shlex.quote(app)
    remote_q = shlex.quote(remote)
    env_opt = f"--env-file {shlex.quote(env_file)} " if env_file else ""
    if mode == "node":
        # A Node server, run the way its own package.json says to.
        #
        # There was no such mode. `_docker_run_cmd` offered exactly two: a Python entrypoint, or
        # nginx serving files. Everything else -- including a complete Express + SQLite app --
        # took the nginx branch, so the backend never started, every /api call 404'd, and the
        # health gate passed because a static shell still answers 200 on `/`. That is a dead app,
        # publicly served, reported green.
        #
        # Same contract as the py mode: writable mount for code, a SEPARATE /data mount that
        # deploys never replace, HOST/PORT from the environment.
        run = (f"docker run -d --name {app_q} --restart unless-stopped {env_opt}-p {int(app_port)}:80 "
               f"{_OWNERSHIP_LABELS.format(app=app_q)} "
               f"-v {remote_q}/dist:/app -v {remote_q}/data:/data -w /app "
               f"-e PORT=80 -e HOST=0.0.0.0 -e DATA_DIR=/data -e NODE_ENV=production "
               f"-e NPM_CONFIG_UPDATE_NOTIFIER=false "
               f"node:22-bookworm-slim sh -lc "
               f"{shlex.quote('npm ci --omit=dev --no-audit --no-fund 2>/dev/null || npm install --omit=dev --no-audit --no-fund; npm start')}")
    elif mode == "py":
        # Mounted read-WRITE: the app keeps a SQLite file beside its source, and a read-only
        # mount turns every write into a 500 that a "/" health check would never see.
        #
        # PYTHONDONTWRITEBYTECODE stops the container littering root-owned __pycache__ into that
        # mount. Without it the next deploy cannot delete the tree it must replace.
        #
        # DATA_DIR is a SEPARATE mount that deploys never touch, so an app can keep state that
        # survives a release. Anything written under /app is replaced by the next deployment —
        # which is correct for code and destructive for data.
        run = (f"docker run -d --name {app_q} --restart unless-stopped {env_opt}-p {int(app_port)}:80 "
               f"{_OWNERSHIP_LABELS.format(app=app_q)} "
               f"-v {remote_q}/dist:/app -v {remote_q}/data:/data -w /app "
               f"-e PORT=80 -e HOST=0.0.0.0 -e DATA_DIR=/data -e PYTHONDONTWRITEBYTECODE=1 "
               f"python:3.12-slim sh -c {shlex.quote(entry)}")
    else:
        run = (f"docker run -d --name {app_q} --restart unless-stopped {env_opt}-p {int(app_port)}:80 "
               f"{_OWNERSHIP_LABELS.format(app=app_q)} "
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



#: How long a freshly started container gets to bind its port before the deploy gives up.
#:
#: This was `time.sleep(3)` followed by ONE curl. A Node service that runs migrations and seeds
#: a database before `app.listen` cannot be up in three seconds, so the probe hit a closed port,
#: curl reported `000` — "nothing answered" — and BOSS rolled back a deployment that was about
#: to work. Deterministically: the same app failed twice, ~50 seconds apart, for the same reason.
#:
#: The preview path already understood this and waits up to 180s for the port
#: (`_discover_working_port(timeout=180.0)`). Same requirement, two implementations, and only
#: one of them correct.
DEPLOY_HEALTH_TIMEOUT_SECONDS = 120

#: Gap between probes. Short enough that a fast app is not made to wait, long enough that a slow
#: one is not hammered with ssh connections while it is trying to start.
DEPLOY_HEALTH_INTERVAL_SECONDS = 3


def _await_healthy(key_path, host: str, user: str, ssh_port: int, app_port: int,
                   *, timeout: int = DEPLOY_HEALTH_TIMEOUT_SECONDS) -> tuple[str, int]:
    """Poll the app until it answers 2xx, or the deadline passes.

    Returns (last_http_code, seconds_waited). `000` means nothing ever listened — a different
    fault from a 500, and the caller says so.

    Succeeds the moment the app is ready, so a fast container is not delayed; only a slow or
    broken one uses the full budget.
    """
    deadline = time.monotonic() + timeout
    code = ""
    started = time.monotonic()
    while True:
        hc = _ssh(key_path, host, user, ssh_port,
                  f"curl -fsS -o /dev/null -w '%{{http_code}}' http://localhost:{app_port}/")
        code = (hc.stdout or "").strip()
        if code.startswith("2"):
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(DEPLOY_HEALTH_INTERVAL_SECONDS)
    return code, int(time.monotonic() - started)

def _rollback_remote(key_path: str, host: str, user: str, ssh_port: int, *, remote: str,
                     app: str, app_port: int, mode: str = "static", entry: str = "",
                     env_file: str = ""):
    """Restore staged previous dist and ensure a container is serving it.

    Called after transfer wipe, docker-run failure (live container already removed), or a
    failed health gate. Prefer restart when the container still exists; otherwise re-run.
    """
    remote_q = shlex.quote(remote)
    app_q = shlex.quote(app)
    return _ssh(
        key_path, host, user, ssh_port,
        f"if [ -d {remote_q}/dist.prev ]; then "
        f"{_rm_rf_cmd(remote + '/dist')} && mv {remote_q}/dist.prev {remote_q}/dist; fi; "
        f"if docker inspect {app_q} >/dev/null 2>&1; then docker restart {app_q}; "
        f"else {_docker_run_cmd(app, app_port, remote, mode, entry, env_file)}; fi || true",
        timeout=180,
    )


def _resolve_repo_branch(run: dict, settings: Settings) -> tuple[str, str, str]:
    spec = run.get("spec") or {}
    owner = spec.get("owner") or settings.target_github_owner
    repo = spec.get("repo") or settings.target_github_repo
    fctx = run.get("finalize_ctx") or {}
    branch = fctx.get("work_branch") or f"boss/code/{fctx.get('root_run_id') or run['id']}"
    return owner, repo, branch


def _build_dist(settings: Settings, owner: str, repo: str, branch: str, *, token: str = "",
                changed: Optional[list[str]] = None,
                ref: Optional[str] = None) -> tuple[Path, str, str, Optional[str]]:
    """Prepare the deployable payload for a run's branch.

    Returns (root, mode, entry, sha) where `root` always contains a `dist/` directory to
    transfer, and `sha` is the commit ACTUALLY checked out:

      mode "static" — dist/ is a servable file tree, run under nginx.
      mode "py"     — dist/ is the app source, run under python; `entry` is the shell command
                      that starts it from /app (see _python_deploy_command).

    One payload name for both modes keeps staging, rollback and cleanup single-implementation;
    only the container command varies.
    """
    # THE TOKEN IS PASSED IN, resolved by the caller where a database connection exists.
    # Reading settings.github_token here made this an anonymous clone on every install whose
    # repository was connected through the wizard — see github_connections.github_token_for.
    # The app's own build (npm ci, its scripts) then runs in this checkout, so the token rides
    # in git's environment only — never in the URL or `.git/config` (IDENTITY.md §7.10).
    from .repo_host import git_access  # BOSS-267: host-aware clone
    access = git_access(getattr(settings, "target_repo_host", "github") or "github",
                        owner, repo, token or "")
    url, genv = access.url, access.environ()
    work = Path(tempfile.mkdtemp(prefix="boss-deploy-"))
    if ref:
        # An explicit ref WINS over the code task's branch. Those disagree the moment the PR
        # merges: the branch is frozen at what the task built, while the deployment is asking
        # for a merged commit. Cloning the branch while RECORDING commit_ref meant the console
        # reported a build that was not the one running — a lie in exactly the field an
        # operator uses to answer "which version is live".
        #
        # init+fetch rather than clone --branch because a ref may be a SHA, which
        # `clone --branch` cannot take.
        owned_process.run(["git", "init", "-q", str(work)], capture_output=True, text=True,
                       timeout=60, check=True)
        owned_process.run(["git", "-C", str(work), "remote", "add", "origin", url],
                       capture_output=True, text=True, timeout=60, check=True)

        # Fetching the ref directly is the cheap path and works for a branch, a tag or a FULL
        # sha. It does NOT work for an abbreviated sha — the server cannot resolve one — and
        # commit_ref is routinely stored short (`f07021e`), so this failed on the first real
        # use. Falling back to a shallow history of the default branch lets git resolve the
        # short sha LOCALLY, where abbreviations are meaningful.
        direct = owned_process.run(["git", "-C", str(work), "fetch", "--depth", "1", "origin", ref],
                                capture_output=True, text=True, timeout=180, env=genv)
        if direct.returncode == 0:
            owned_process.run(["git", "-C", str(work), "checkout", "-q", "FETCH_HEAD"],
                           capture_output=True, text=True, timeout=60, check=True)
        else:
            owned_process.run(["git", "-C", str(work), "fetch", "--depth", "200", "origin"],
                           capture_output=True, text=True, timeout=300, check=True, env=genv)
            checked = owned_process.run(["git", "-C", str(work), "checkout", "-q", ref],
                                     capture_output=True, text=True, timeout=60)
            if checked.returncode != 0:
                raise RuntimeError(
                    f"cannot deploy ref {ref!r}: not fetchable directly and not found in the "
                    f"last 200 commits of {owner}/{repo}. "
                    f"{(checked.stderr or direct.stderr or '').strip()[:200]}")
    else:
        owned_process.run(["git", "clone", "--depth", "1", "--branch", branch, url, str(work)],
                       capture_output=True, text=True, timeout=180, check=True, env=genv)
    # Read here, where the clone definitely still has its .git directory. Every return path
    # below carries this value out, so no caller has to know which ones discard the clone.
    sha = _head_sha(work)

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

    # A BUILD SCRIPT IS NOT A STATEMENT THAT THE APP IS STATIC.
    #
    # This returned "static" the moment package.json had a `build`, BEFORE the resolver below
    # ever ran — so a full-stack app took the nginx branch on the strength of having a
    # frontend. BugBox is exactly that: `build` (vite) AND `start` (node server/index.js).
    # Its UI deployed, its API did not, and every request failed with "Could not reach the
    # API" on a deployment the health gate had passed — because a static shell answers 200
    # on `/` whether or not anything behind it works.
    #
    # `start` is the app saying it is a SERVER. When both are present the app is built and
    # then RUN, which is what `npm start` expects anyway: vite writes dist/, the server serves
    # it plus its own routes from one process. Same rule the node branch below already uses.
    has_start = False
    if pkg.is_file():
        try:
            has_start = bool((_json.loads(pkg.read_text()).get("scripts") or {}).get("start"))
        except Exception:  # noqa: BLE001
            has_start = False

    if has_build:
        env = {**os.environ, "HOME": str(work), "npm_config_cache": str(work / ".npm"),
               "npm_config_fund": "false", "npm_config_audit": "false"}
        owned_process.run(["sh", "-lc", "npm ci --no-audit --no-fund && npm run build"],
                       cwd=str(work), env=env, capture_output=True, text=True,
                       timeout=900, check=True)
        if not (work / "dist").is_dir():
            raise RuntimeError("build script ran but did not produce ./dist")
        if not has_start:
            return (work, "static", "", sha)
        # Built, and it has a server to run. Ship the whole app directory — its package.json is
        # the run contract — laid out under the same `dist` name every mode uses, so staging,
        # rollback and cleanup stay one implementation.
        payload = Path(tempfile.mkdtemp(prefix="boss-deploy-payload-"))
        shutil.copytree(work, payload / "dist",
                        ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__",
                                                      ".npm", "data", ".e2e-data"))
        shutil.rmtree(work, ignore_errors=True)
        return (payload, "node", "", sha)

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
        #
        # BUT SAY WHICH FAILURE THIS IS. "cannot tell which app this task built" describes one
        # cause — several apps, no signal — and was printed for a completely different one:
        # the app IS NOT IN THE TREE BEING DEPLOYED. HookWatch lives on its open PR branch,
        # the deploy checked out `main`, and the "candidates" were eight unrelated sample apps
        # that happen to exist on main. Four separate investigations went into binding, keys,
        # surface ownership and app_entry before anybody read `commit_ref` and saw `main`.
        #
        # The tree is right here, so the question is answerable: if the files this app is
        # known by are absent from the checkout, that is the fault, and no amount of declaring
        # `app_entry` will help.
        # "." IS NOT EVIDENCE. The repository root always exists, so including it made the
        # probe pass for every root-level app and print the misleading message anyway —
        # verified against the live failure, which is the only reason this was caught.
        wanted = [c for c in (changed or [])
                  if c and c not in (".", "./") and not c.startswith("-")]
        present = [c for c in wanted if (work / c).exists()]
        where = ref or branch or "the default branch"
        if wanted and not present:
            raise RuntimeError(
                f"this app is not in {where}. None of the files it is known by "
                f"({', '.join(wanted[:4])}{'…' if len(wanted) > 4 else ''}) exist in that "
                f"checkout, so there is nothing here to deploy — what the resolver found "
                f"instead belongs to other apps in the repository ({exc}). "
                f"Deploy the branch the app was built on, or merge it into {where} first."
            ) from exc
        raise RuntimeError(
            f"cannot tell which app to deploy from {where} — candidates: {exc}. "
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
        # `sha` was read BEFORE the clone was discarded. Reading it afterwards found no .git and
        # fell back to the branch NAME, so a py-mode deployment recorded
        # "boss/code/crun_..." as its build — the same dishonesty in this field that the
        # commit-ref fix removed, reintroduced by copying the payload out of the clone.
        return (payload, "py", _python_deploy_command(payload / "dist", value), sha)

    if kind == "node":
        # Ship the app's own directory (its package.json is the run contract), built if it has
        # a build step, so `npm start` finds `dist/` where the server expects it.
        app_root = work if value in (".", "") else work / value
        if (app_root / "package.json").is_file():
            owned_process.run(["sh", "-lc",
                            "npm ci --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund"],
                           cwd=app_root, check=False, timeout=900)
            owned_process.run(["sh", "-lc", "npm run build --if-present"],
                           cwd=app_root, check=False, timeout=900)
        payload = Path(tempfile.mkdtemp(prefix="boss-deploy-payload-"))
        shutil.copytree(app_root, payload / "dist",
                        ignore=shutil.ignore_patterns(".git", "__pycache__", "data", ".e2e-data"))
        shutil.rmtree(work, ignore_errors=True)
        return (payload, "node", "", sha)
    if kind == "static":
        static_dir = work / value
    elif (work / "dist").is_dir():
        static_dir = work / "dist"

    if static_dir is None or not (static_dir / "index.html").is_file():
        raise RuntimeError(
            "no build script and no index.html found — nothing servable to deploy")
    if static_dir == work / "dist":
        return (work, "static", "", sha)
    dist = work / "dist"
    shutil.copytree(static_dir, dist)
    return (work, "static", "", sha)


def _python_deploy_command(app_dir: Path, entry: str) -> str:
    """The shell command that runs a resolved Python entry from the app directory.

    This was `python {entry}`. A module inside a package (lead_triage_core/server.py, with
    relative imports) dies that way on "attempted relative import with no known parent package"
    — the error the preview was fixed for (crun_3e370c11) — the health gate sees nothing and
    the deploy rolls back. preview_service.python_entry_invocation is the one place that decides
    how an entry file runs: `python3 -m pkg.module` from above its package, `python3 file.py`
    for a plain script.

    ONLY the entry's invocation. A build's boss.preview.command is a preview / build-proof
    contract, not a production command, and deploy never reads it: what kind of deploy this is
    and how a node app starts (`npm start`) stay exactly as 00907e9 decided them (fourth review
    of dc3aeb7 — a declared `vite preview` cannot run after `npm ci --omit=dev`).
    """
    from .preview_service import python_entry_invocation

    run_dir, argv = python_entry_invocation(app_dir, entry)
    command = " ".join(shlex.quote(a) for a in argv)
    if run_dir not in ("", "."):
        command = f"cd {shlex.quote(run_dir)} && {command}"
    return command


def deploy_ssh(engine, settings: Settings, run_id: str, *, host: str, user: str = "root",
               ssh_port: int = 22, app_port: int = 8080, name: Optional[str] = None,
               ref: Optional[str] = None, key_material: Optional[str] = None,
               source: Optional[dict] = None,
               app_entry: Optional[str] = None,
               governance: Optional[dict] = None,
               app_env: Optional[dict] = None) -> dict:
    """Actuate the deploy plan on a real host: build dist -> transfer -> nginx -> health-gate.
    On transfer/docker/health failure after staging, ROLL BACK to the previous build (Phase 3).
    Gated behind the outward-action flag + an SSH key; NEVER touches a host otherwise.

    `governance` proves this is the apply of an approved Resource Plane ChangeSet
    (services/governed_host_deploy.py). With it, the write carries its own approval, reservation
    and receipt, so it needs only outward SSH; without it Community refuses outright."""
    from .governed_host_deploy import check_governed, ungoverned_refusal

    if governance is not None:
        check_governed(engine, governance)
        from .deployment_fleet import outward_ssh_allowed

        if not outward_ssh_allowed(settings):
            raise PermissionError("outward SSH is disabled (BOSS_SSH_OUTWARD_ALLOWED is false)")
    else:
        if str(getattr(settings, "boss_edition", "community")).strip().lower() in {"community", "oss", "local"}:
            raise PermissionError(ungoverned_refusal("Community SSH deployment"))
        if not bool(getattr(settings, "ssh_host_deploy_allowed", False)):
            raise PermissionError("BOSS_SSH_HOST_DEPLOY_ALLOWED is false — the outward deploy gate is off")
    _validate_ssh_target(host, user)

    # A deployment does NOT require a code task run. A run supplies three things — owner/repo,
    # a branch, and the file lineage that tells the resolver which app this task built — and an
    # app onboarded through the console has none of them. Requiring one made the wizard a dead
    # end: you could register a target and never deploy to it.
    #
    # `source` provides the same three directly: {owner, repo, ref, project_id, app_entry}. The
    # lineage is the only real loss, and `app_entry` replaces it — the TARGET knows which app it
    # is, which is better information than inferring it from what some task happened to touch.
    run = None
    if source:
        owner = source.get("owner") or settings.target_github_owner
        repo = source.get("repo") or settings.target_github_repo
        branch = source.get("ref") or "main"
        project_id = source.get("project_id") or ""
    else:
        with begin(engine) as c:
            run = load_code_task_run(c, run_id)
        if run is None:
            raise ValueError(f"code task run not found: {run_id}")
        owner, repo, branch = _resolve_repo_branch(run, settings)
        project_id = run["project_id"]
    app = _app_name(repo, name)
    remote = f"{REMOTE_BASE}/{app}"
    # Material resolved for THIS target wins over the global setting. Passing it in rather
    # than re-resolving here keeps "which credential" a single decision made by the connector.
    key_path = _materialise_key(key_material) if key_material else _ssh_key_file(settings)
    work: Optional[Path] = None
    # Bound before the try: the rollback paths below read them, and a build that raises would
    # otherwise turn a deploy failure into a NameError that hides it.
    mode, entry = "static", ""
    env_file = ""
    needs_rollback = False
    rolled_back = False
    try:
        # The lineage's file list, not this attempt's: a repair touches only what it fixed, so
        # `state.impl_files` alone loses the app the task exists to build. Preview learned this
        # the expensive way; the deploy path must use the same source or it will diverge again.
        from .preview_service import lineage_impl_files

        # With a run, "which app" comes from its file lineage. Without one, the target says so
        # explicitly via app_entry — a repo holding several apps cannot be guessed at, and
        # guessing is what once deployed Sudoku to a stock-app target.
        # `app_entry` names either a FILE (serve_nqueens.py) or a DIRECTORY (web-nqueens), but
        # `changed` is a list of FILE paths — the resolver derives touched directories with
        # dirname(). A bare directory therefore collapsed to "." and matched nothing, so a
        # target that HAD declared its app still failed with "cannot tell which app to deploy".
        #
        # Both forms are supplied: the literal entry (matches the .py rule) and an index.html
        # under it (matches the static-directory rule). Whichever is real, one matches.
        # AN OPERATOR WHO NAMES THE APP OUTRANKS ANYTHING WE INFER.
        #
        # `app_entry` was read from `source`, and `source` is only built when there is NO code
        # task run — so for the ordinary case, a run deploying its own build, a target that had
        # explicitly declared its app was ignored and the deploy failed with "cannot tell which
        # app to deploy" anyway. The operator answered the question and was not listened to.
        #
        # It also has to win over the lineage, not merely fill in for it. The lineage can be
        # incomplete in ways nobody can fix after the fact: this repo's HookWatch run recorded
        # its files under a root_run_id whose row no longer exists, so the file signal resolved
        # to nothing while eight unrelated sample apps sat in the same repository.
        entry_hint = app_entry or (source or {}).get("app_entry")
        changed = lineage_impl_files(engine, run) if run is not None else None
        if entry_hint:
            e = str(entry_hint).strip().rstrip("/") or "."
            # Both forms, as before: the literal entry matches the .py rule, the index.html
            # under it matches the static-directory rule. "." means the repository root.
            hint = [e, f"{e}/index.html" if e != "." else "index.html"]
            changed = hint + [c for c in (changed or []) if c not in hint]
        with begin(engine) as _c:
            _tok = github_token_for(_c, settings, project_id)
        work, mode, entry, sha = _build_dist(settings, owner, repo, branch,     # 1 build
                                             token=_tok, changed=changed, ref=ref)
        # The SHA that was actually checked out, captured inside _build_dist while the clone
        # still existed. Reporting the request rather than the result is how commit_ref came to
        # name a build that was never running.
        deployed_sha = sha or ref or branch
        ct = _ssh(key_path, host, user, ssh_port, "docker version --format '{{.Server.Version}}'")
        if ct.returncode != 0:                                                  # 2 connection test
            raise RuntimeError(f"host unreachable or docker missing: {(ct.stderr or '')[:900]}")
        _ssh(key_path, host, user, ssh_port,                                    # 3 stage previous
             f"mkdir -p {shlex.quote(remote)} && "
             f"([ -d {shlex.quote(remote)}/dist ] && {_rm_rf_cmd(remote + '/dist.prev')} && "
             f"cp -a {shlex.quote(remote)}/dist {shlex.quote(remote)}/dist.prev || true)")
        needs_rollback = True  # host may have dist wiped / container replaced from here on
        tar = owned_process.run(                                                   # 4 transfer (tar|ssh)
            _transfer_shell_cmd(work=work, key_path=key_path, host=host, user=user,
                                ssh_port=ssh_port, remote=remote),
            shell=True, capture_output=True, text=True, timeout=600)
        if tar.returncode != 0:
            _rollback_remote(key_path, host, user, ssh_port, remote=remote, app=app,
                             app_port=app_port, mode=mode, entry=entry, env_file=env_file)
            rolled_back = True
            raise RuntimeError(f"transfer failed: {(tar.stderr or '')[:900]}")
        if app_env:                                                             # 5a app secrets
            # The app's deploy secrets (templates.secrets.values_for_env, read by the governed
            # apply) go to the host on ssh's STDIN into a mode-600 file — never into argv, a
            # log line, the run record or the ChangeSet.
            env_file = app_env_file(remote)
            q = shlex.quote(env_file)
            wrote = _ssh(key_path, host, user, ssh_port,
                         f"set -e; umask 077; mkdir -p {shlex.quote(remote + '/.boss')}; "
                         f"cat > {q}.tmp; mv {q}.tmp {q}; chmod 600 {q}; echo BOSS_ENV_WRITTEN",
                         timeout=60, stdin_text=_app_env_text(app_env))
            if wrote.returncode != 0 or "BOSS_ENV_WRITTEN" not in (wrote.stdout or ""):
                _rollback_remote(key_path, host, user, ssh_port, remote=remote, app=app,
                                 app_port=app_port, mode=mode, entry=entry)
                rolled_back = True
                raise RuntimeError("could not write the app's environment on the host: "
                                   f"{(wrote.stderr or '')[:300]}")
        dep = _ssh(key_path, host, user, ssh_port,                              # 5 deploy nginx
                   f"docker rm -f {shlex.quote(app)} 2>/dev/null; "
                   f"{_docker_run_cmd(app, app_port, remote, mode, entry, env_file)}")
        if dep.returncode != 0:
            # Live container was removed above — restore previous dist and re-run nginx.
            _rollback_remote(key_path, host, user, ssh_port, remote=remote, app=app,
                             app_port=app_port, mode=mode, entry=entry, env_file=env_file)
            rolled_back = True
            with begin(engine) as c:
                emit_event(c, project_id, "deploy.rolled_back", "boss", "code_task_run", run_id,
                           {"app": app, "host": host, "reason": "docker_run_failed",
                            "detail": (dep.stderr or "")[:900]})
            raise RuntimeError(f"docker run failed: {(dep.stderr or '')[:900]}")
        code, waited = _await_healthy(key_path, host, user, ssh_port, int(app_port))  # 6 health gate
        if not code.startswith("2"):                                           # Phase 3 rollback
            _rollback_remote(key_path, host, user, ssh_port, remote=remote, app=app,
                             app_port=app_port, mode=mode, entry=entry, env_file=env_file)
            rolled_back = True
            with begin(engine) as c:
                emit_event(c, project_id, "deploy.rolled_back", "boss", "code_task_run", run_id,
                           {"app": app, "host": host, "health": code})
            never_answered = code in ("", "000")
            return {"status": "rolled_back", "app": app, "host": host, "health": code,
                    "detail": (
                        f"health check returned {code or 'no response'} after waiting "
                        f"{waited}s; rolled back to previous build. "
                        + ("Nothing was listening on port "
                           f"{int(app_port)} — the container never bound it (start crash, a "
                           "long migration/seed, or the app ignoring $PORT)."
                           if never_answered else
                           "The app answered but not with a 2xx, so it started and is "
                           "unhealthy rather than absent.")
                    )}
        needs_rollback = False
        url = f"http://{host}:{app_port}/"
        with begin(engine) as c:
            emit_event(c, project_id, "deploy.succeeded", "boss", "code_task_run", run_id,
                       {"app": app, "host": host, "url": url, "health": code})
        return {"status": "deployed", "app": app, "host": host, "url": url, "health": code,
                "deployed_sha": deployed_sha,
                # What the connector's rollback() restores if a later health check fails: the
                # previous build staged above as dist.prev, served the same way.
                "rollback_point": {"remote": remote, "mode": mode, "entry": entry}}
    except Exception:
        # Best-effort restore if we mutated the host and then crashed before an explicit rollback.
        if needs_rollback and not rolled_back:
            try:
                _rollback_remote(key_path, host, user, ssh_port, remote=remote, app=app,
                                 app_port=app_port, mode=mode, entry=entry, env_file=env_file)
            except Exception:  # noqa: BLE001 — never mask the original failure
                pass
        raise
    finally:
        _discard_key_file(key_path)
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
