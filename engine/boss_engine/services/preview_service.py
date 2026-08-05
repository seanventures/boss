"""Tier-1 "1-box" app previews.

After a code task produces a green PR, an operator wants to *see the working website*
before merging. This runs the built app from its branch as a managed, auto-expiring local
process on the dev box and hands back a live preview URL — zero extra compute.

Process-based (not containerized) for v1: simplest, works with loopback-binding apps on
macOS where container host-networking is awkward. Containerized isolation is the v2 hardening.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import text

from ..config import Settings
from ..db import begin
from .code_task_service import load_code_task_run
from .core import emit_event, make_id
from .execution_substrate import ExecutionSubstrate, normalize_build_substrate
from .sandbox_provider import MicroVMProvider

PREVIEW_ROOT = Path(os.environ.get("BOSS_PREVIEW_ROOT", "/tmp/boss-previews"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _low_priority(command: list[str]) -> list[str]:
    """Run a preview build at LOW CPU/IO priority. `npm install` + `vite build` run inside the api
    container; at normal priority they starve the api during the ~60s build and it returns 404s
    ("Reconnecting to the control plane"). nice (coreutils, always present) lets the api's own
    threads preempt the build; ionice de-prioritizes its disk IO when available."""
    prefix = ["nice", "-n", "19"]
    if shutil.which("ionice"):
        prefix += ["ionice", "-c3"]
    return prefix + command


# Single source of truth for "which python file starts this app". Kept as one constant because
# the host path (_detect_command) and the container path (_generate_dockerfile) MUST agree --
# they previously carried separate copies of this tuple, and an app whose entrypoint was named
# serve.py failed to launch in both while the test fallback reported a misleading 'verified'.
_PY_ENTRYPOINTS = ("server.py", "serve.py", "app.py", "wsgi.py", "main.py")
# Directories a static build drops its index.html into, in priority order ("." = repo root).
_STATIC_DIRS = (".", "web", "public", "dist", "site", "static")



class AmbiguousAppError(RuntimeError):
    """The repo holds several runnable apps and this task gave no signal which one is its own.

    Raised rather than guessed. The previous behaviour returned the first convention match,
    which showed an operator an application their task never touched — and in a repo shared
    across customers, another tenant's app entirely.
    """

    def __init__(self, candidates: list[str]) -> None:
        self.candidates = candidates
        super().__init__(
            "cannot tell which app this task built: the repository contains "
            f"{len(candidates)} runnable apps ({', '.join(candidates)}) and the run recorded "
            "no implementation files identifying one. Refusing to preview an arbitrary app. "
            "Retry the task so it records what it built, or add a boss.preview.command file."
        )


def lineage_impl_files(engine, run: dict) -> list[str]:
    """Every implementation file THIS TASK'S LINEAGE has written, newest attempt first.

    `state.impl_files` is per-ATTEMPT. On a repair it holds only the touched-up files — for
    crun_9b8e732b that was ["README.md", "nqueens_core/levels.py"], with no trace of the
    web-nqueens/ app the task exists to build. resolve_app_entry then finds no index.html
    among them and falls back to the repo-wide serve.py, so the preview served SUDOKU for an
    N-Queens task. Again.

    The branch holds the whole lineage's work, so the file list must too. (Deriving it with
    `git diff base...HEAD` is not an option: the preview clones --depth 1 --branch X, so the
    base ref does not exist in that workdir.)
    """
    fctx = run.get("finalize_ctx") or {}
    root = fctx.get("root_run_id")
    if not root and fctx.get("deferred_from_run"):
        # A deferred-findings FOLLOW-UP is spawned as its own root, so its file list is only the
        # findings it fixed — for crun_022620a9 that was ["nqueens_core/levels.py"], with no app
        # in it at all. It builds on the SAME branch as the run it came from, so its preview must
        # inherit that run's lineage. Resolved here rather than by writing root_run_id on the
        # follow-up, which would make the pipeline treat it as a retry and change its stages.
        with begin(engine) as c:
            parent = c.execute(
                text("SELECT COALESCE(finalize_ctx->>'root_run_id', id) AS r "
                     "FROM code_task_runs WHERE id=:i"),
                {"i": fctx["deferred_from_run"]},
            ).mappings().first()
        if parent:
            root = parent["r"]
    root = root or run.get("id")
    if not root:
        return list((run.get("state") or {}).get("impl_files") or [])
    with begin(engine) as c:
        rows = c.execute(
            text("""
                SELECT state->'impl_files' AS f
                FROM code_task_runs
                WHERE COALESCE(finalize_ctx->>'root_run_id', id) = :root
                   OR finalize_ctx->>'deferred_from_run' IN (
                        SELECT id FROM code_task_runs
                        WHERE COALESCE(finalize_ctx->>'root_run_id', id) = :root)
                ORDER BY created_at DESC
            """),
            {"root": root},
        ).mappings().all()
    out: list[str] = []
    for r in rows:
        for path in (r["f"] or []):
            if isinstance(path, str) and path not in out:
                out.append(path)
    return out


def _changed_dirs(changed: Optional[list[str]]) -> list[str]:
    """Directories this task touched, in the order they appear in the diff."""
    out: list[str] = []
    for c in changed or []:
        d = c.rsplit("/", 1)[0] if "/" in c else "."
        if d not in out:
            out.append(d)
    return out


def resolve_app_entry(workdir: Path, changed: Optional[list[str]] = None) -> tuple[str, str]:
    """THE single decision about which app a preview launches: ("py"|"static"|"node"|"", value).

    Both the host path (_detect_command) and the container path (_generate_dockerfile) call
    THIS. They previously each carried their own copy of the preference logic, and the copies
    drifted twice:

      PR #62  — the host path learned to prefer the entrypoint the task built; the container
                path did not, and previewed Sudoku for an N-Queens build.
      crun_9b8e732b — both paths then preferred a CHANGED .py entrypoint, but neither applied
                the same preference to STATIC dirs. _STATIC_DIRS tries "." first, and a task
                that built `web-nqueens/index.html` in a repo whose root already held the
                Sudoku app previewed Sudoku again — this time via the repo-wide serve.py.

    Hence one function. "What this task built" outranks "what the repo happens to contain",
    for every stack, and there is exactly one place to change that rule.
    """
    touched = _changed_dirs(changed)

    # 1. An entrypoint THIS task wrote.
    for cand in [c for c in (changed or []) if c.endswith(".py")]:
        base = cand.rsplit("/", 1)[-1]
        if base.startswith(("serve", "server", "app", "main", "wsgi")) and (workdir / cand).exists():
            return ("py", cand)
    # 2. A static app THIS task built. Ranked ABOVE the repo-wide python fallback: a task that
    #    ships web-nqueens/index.html means that app, not whatever serve.py predates it.
    for d in touched:
        if (workdir / d / "index.html").is_file():
            return ("static", d)
    # 3. Repo conventions — but ONLY when the repo is unambiguous.
    #
    # FAIL CLOSED ON AMBIGUITY. This used to walk the conventions and return the first hit, so
    # a task whose own output revealed no app previewed whatever the repo happened to contain.
    # A deferred-findings follow-up touching only nqueens_core/levels.py showed the operator
    # the SUDOKU app sitting at the repo root. In a repo holding one customer's work that is a
    # confusing preview; in a repo holding several it is showing someone another tenant's
    # application. Serving the wrong app is strictly worse than serving nothing, so when the
    # repo offers more than one candidate and the task gives no signal, we refuse and say why.
    candidates: list[tuple[str, str]] = []
    for entry in _PY_ENTRYPOINTS:
        if (workdir / entry).exists():
            candidates.append(("py", entry))
    for cand in sorted(workdir.glob("*.py")):
        if cand.name.startswith(("serve", "server", "app", "main", "wsgi")):
            if ("py", cand.name) not in candidates:
                candidates.append(("py", cand.name))
    for rel in _STATIC_DIRS:
        if (workdir / rel / "index.html").is_file():
            candidates.append(("static", rel))
    # Any OTHER top-level directory holding an index.html is a candidate too. Counting only the
    # convention names made a multi-app repo look unambiguous: boss-sandbox holds web/ (Sudoku),
    # web-stock/ and web-nqueens/, but only "web" is a convention, so a task with no signal got
    # a confident answer of SUDOKU rather than the refusal this function promises. Ambiguity has
    # to be measured against what the repo actually contains.
    for cand in sorted(workdir.glob("*/index.html")):
        rel = cand.parent.name
        if ("static", rel) not in candidates:
            candidates.append(("static", rel))
    if not candidates and (workdir / "package.json").exists():
        candidates.append(("node", "."))
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        raise AmbiguousAppError([f"{k}:{v}" for k, v in candidates])
    return ("", "")


def _detect_command(workdir: Path, changed: Optional[list[str]] = None) -> Optional[list[str]]:
    """Detect the repo's declared preview command before falling back to stack hints."""
    hint = workdir / "boss.preview.command"
    if hint.is_file() and hint.read_text(errors="ignore").strip():
        return ["sh", "-lc", hint.read_text(errors="ignore").strip()]
    package = workdir / "package.json"
    if package.is_file():
        try:
            payload = json.loads(package.read_text())
        except (OSError, json.JSONDecodeError):
            payload = {}
        configured = ((payload.get("boss") or {}).get("preview") or {}).get("command")
        if isinstance(configured, str) and configured.strip():
            return ["sh", "-lc", configured.strip()]
        scripts = payload.get("scripts") or {}
        deps = {**(payload.get("dependencies") or {}), **(payload.get("devDependencies") or {})}
        is_vite = ("vite" in deps or (workdir / "vite.config.ts").exists()
                   or (workdir / "vite.config.js").exists())
        # A Vite app: BUILD it and serve the STATIC output — do NOT run the dev server. The dev
        # server emits origin-absolute ES-module imports (/node_modules/.vite/deps, /src, /@vite)
        # that the browser resolves against the API origin root, NOT the /v1/previews/<id>/web
        # proxy prefix, so every module 404s and the page renders blank. A production build bundles
        # to RELATIVE-path assets (base=./) that resolve correctly under any proxy prefix via the
        # injected <base href>. Served by Python's stdlib http.server (guaranteed present).
        if is_vite and (scripts.get("build") or "vite" in deps):
            return [
                "sh", "-lc",
                "npm install --no-audit --no-fund --loglevel=error && "
                "npx vite build --base=./ && "
                'python3 -m http.server "$PORT" --bind 0.0.0.0 --directory dist',
            ]
        script = "dev" if scripts.get("dev") else "start" if scripts.get("start") else None
        if script:
            suffix = " -- --host 0.0.0.0" if script == "dev" else ""
            return [
                "sh", "-lc",
                "npm install --no-audit --no-fund --loglevel=error && "
                f"npm run {script}{suffix}",
            ]
    procfile = workdir / "Procfile"
    if procfile.is_file():
        for line in procfile.read_text(errors="ignore").splitlines():
            kind, sep, command = line.partition(":")
            if sep and kind.strip().lower() == "web" and command.strip():
                return ["sh", "-lc", command.strip()]
    makefile = workdir / "Makefile"
    if makefile.is_file():
        content = makefile.read_text(errors="ignore")
        for target in ("dev", "run"):
            if re.search(rf"(?m)^{target}\s*:", content):
                return ["make", target]
    # PREFER WHAT THIS TASK BUILT. Selection used to walk a fixed filename list over the whole
    # repo, so a task that added `serve_nqueens.py` to a repo already containing the Sudoku
    # `serve.py` previewed SUDOKU — serve.py sorts earlier and serve_nqueens.py was not on the
    # list at all. The preview faithfully launched the wrong application (PR #62).
    # ONE decision, shared with the container path (see resolve_app_entry).
    kind, value = resolve_app_entry(workdir, changed)
    if kind == "py":
        return ["python3", value]
    if kind == "static":
        # A static site still needs a SERVER — ES modules refuse to load over file:// — so a
        # bare index.html is a runnable app, not a library.
        return ["sh", "-lc",
                f'python3 -m http.server "$PORT" --bind 0.0.0.0 --directory {value}']
    return None


def _detect_verification(workdir: Path) -> Optional[str]:
    """A library/CLI/module build has no server to preview — the way you 'run' it is its test
    suite. Detect that so the preview can report an honest 'verified by tests' result instead of a
    misleading 'you forgot to add a preview command' error. Returns a human-readable test command."""
    package = workdir / "package.json"
    if package.is_file():
        try:
            scripts = (json.loads(package.read_text()).get("scripts") or {})
        except (OSError, json.JSONDecodeError):
            scripts = {}
        if scripts.get("test"):
            return "npm test"
    makefile = workdir / "Makefile"
    if makefile.is_file() and re.search(r"(?m)^test\s*:", makefile.read_text(errors="ignore")):
        return "make test"
    has_py_tests = (
        any(workdir.glob("test_*.py")) or any(workdir.glob("*_test.py"))
        or any(workdir.glob("**/test_*.py")) or (workdir / "tests").is_dir()
    )
    if has_py_tests:
        if (workdir / "pytest.ini").is_file() or (workdir / "pyproject.toml").is_file():
            return "python3 -m pytest -q"
        return "python3 -m unittest discover -v"
    return None


def _descendant_pids(pid: int) -> list[int]:
    pids = [pid]
    try:
        out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=5).stdout
        for child in out.split():
            pids.extend(_descendant_pids(int(child)))
    except Exception:
        pass
    return pids


def _url_ok(url: str, *, headers: Optional[dict[str, str]] = None, check_body: bool = False) -> bool:
    try:
        request = urllib.request.Request(url, headers=headers or {})
        response = urllib.request.urlopen(request, timeout=2)
        status = int(response.status)
        if not (200 <= status < 400):
            return False
        # APP-4 (#94): the primary-route smoke must see real served content, not just a socket. A
        # blank body or a rendered error overlay on a 2xx no longer counts as healthy. (An SPA shell
        # is non-blank and not an error page, so it still passes — full render rubric is the headless gate.)
        if check_body and 200 <= status < 300:
            body = response.read(65536).decode("utf-8", "ignore")
            if not body.strip():
                return False
            from .visual_gate import is_error_page
            if is_error_page(body):
                return False
        return True
    except urllib.error.HTTPError:
        # A framework error page is not a healthy preview.
        return False
    except Exception:
        return False


def _http_ok(port: int) -> bool:
    return _url_ok(f"http://127.0.0.1:{port}", check_body=True)


def _classify_start_failure(message: str) -> tuple[str, str]:
    lowered = message.lower()
    if "clone" in lowered:
        return "clone_failed", "Check repository access and the task branch, then retry."
    if "detect" in lowered or "command" in lowered:
        return "command_missing", "Add boss.preview.command or retry with an explicit command."
    if "did not serve" in lowered or "port" in lowered:
        return "port_timeout", "Inspect startup logs, ensure HOST/PORT are honored, or retry in Docker mode."
    return "boot_crash", "Inspect startup logs and retry in Docker mode."


def _candidate_ports(log_path: str, port_hint: int) -> list[int]:
    """Ordered candidates that unambiguously belong to THIS app:
    1) the URL/port the framework printed in its own log, then
    2) the PORT we injected via env (apps that honour it).
    Deliberately NOT process-tree lsof — on a dev box that picks up unrelated local services
    (Docker, Neo4j, IDEs) that also answer HTTP."""
    ordered: list[int] = []
    seen: set[int] = set()

    def add(p: int):
        if p not in seen:
            seen.add(p)
            ordered.append(p)

    try:
        log = open(log_path, errors="ignore").read()
        for m in re.finditer(r"https?://[^\s/]*?:(\d{2,5})\b", log):
            add(int(m.group(1)))
        for m in re.finditer(r"(?:port|listening on)\D{0,8}(\d{3,5})\b", log, re.I):
            add(int(m.group(1)))
    except Exception:
        pass
    add(port_hint)
    return ordered


def _discover_working_port(log_path: str, port_hint: int, timeout: float = 20.0) -> Optional[int]:
    """Return the port the app actually serves HTTP on — its log-announced port first, then the
    injected PORT — each health-checked, so a neighboring local service is never mistaken for it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for port in _candidate_ports(log_path, port_hint):
            if _http_ok(port):
                return port
        time.sleep(0.8)
    return None


class PreviewNotReady(Exception):
    """A preview was requested for a run that has not produced a reviewable change yet.

    Deliberately NOT a RuntimeError: the API tries several preview substrates in turn and
    treats RuntimeError as "this substrate failed, try the next one". Retrying three ways
    cannot conjure a PR, so this must propagate on the first attempt instead of being
    swallowed into a generic "preview could not start".
    """

    def __init__(self, run_id: str, status: str, phase: str) -> None:
        self.run_id, self.status, self.phase = run_id, status, phase
        super().__init__(
            f"run {run_id} has not opened a pull request yet "
            f"(status={status}, phase={phase}), so there is no completed change to preview"
        )

    def as_detail(self) -> dict:
        return {
            "error": "preview_unavailable_no_pr",
            "message": ("This run has not opened a pull request yet, so there is no completed "
                        "change to preview."),
            "run_id": self.run_id, "run_status": self.status, "run_phase": self.phase,
            "next_action": ("Wait for the run to finish. If it is blocked on a plan-approval "
                            "decision, approve it; if it failed, retry the task."),
        }


def assert_previewable(run: dict, run_id: str) -> None:
    """A preview is only meaningful once the work exists as a PR.

    Enforced HERE, in the one place every caller already passes through, rather than in each
    entrypoint. The console gated its button and the API gated its endpoint, but the MCP tool
    called the service directly and bypassed both — the same "fix the host path, miss the
    container path" mistake that once launched Sudoku as the preview for an N-Queens build.
    Share the decision, not just the data.

    Gated on the PR, deliberately not on gates passing: an advisory run legitimately opens a
    PR with failing checks and IS previewable.
    """
    if not (run.get("pull_request_url") or ""):
        raise PreviewNotReady(run_id, str(run.get("status") or "?"), str(run.get("phase") or "?"))


def start_preview(engine, settings: Settings, run_id: str, *, ttl_minutes: int = 60) -> dict:
    """Clone the run's branch, launch the app, discover its port, register an expiring preview."""
    with begin(engine) as c:
        run = load_code_task_run(c, run_id)
    if run is None:
        raise ValueError(f"code task run not found: {run_id}")
    assert_previewable(run, run_id)
    spec = run.get("spec") or {}
    owner = spec.get("owner") or settings.target_github_owner
    repo = spec.get("repo") or settings.target_github_repo
    # Iterations reuse the ROOT run's branch (boss/code/{root_run_id}); a run's own
    # id may have no branch. Prefer an explicit work_branch, else the root-derived one.
    _fctx = run.get("finalize_ctx") or {}
    branch = _fctx.get("work_branch") or f"boss/code/{_fctx.get('root_run_id') or run_id}"
    build_substrate = _fctx.get("execution_substrate") or normalize_build_substrate(
        settings.sandbox_provider
    ).value
    project_id = run["project_id"]
    preview_id = make_id("prev")

    PREVIEW_ROOT.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix=f"{preview_id}-", dir=str(PREVIEW_ROOT)))
    log_path = str(workdir / "_preview.log")

    def _fail(msg: str) -> dict:
        failure_kind, suggested_action = _classify_start_failure(msg)
        with begin(engine) as c:
            c.execute(text(
                "INSERT INTO app_previews (id,project_id,run_id,repo,branch,substrate,build_substrate,"
                "status,error,failure_kind,log_path,expires_at) "
                "VALUES (:id,:p,:r,:repo,:b,:sub,:build,'failed',:e,:fk,:lg,now())"),
                {"id": preview_id, "p": project_id, "r": run_id, "repo": f"{owner}/{repo}",
                 "b": branch, "sub": ExecutionSubstrate.PREVIEW_HOST.value,
                 "build": build_substrate, "e": msg[:500], "fk": failure_kind,
                 "lg": log_path})
        return {"id": preview_id, "status": "failed", "error": msg,
                "failure_kind": failure_kind, "suggested_action": suggested_action}

    token = settings.github_token
    from .repo_host import clone_url_for  # BOSS-267: host-aware clone
    clone_url = clone_url_for(getattr(settings, "target_repo_host", "github") or "github",
                              owner, repo, token or "")
    clone = subprocess.run(["git", "clone", "--depth", "1", "--branch", branch, clone_url, str(workdir)],
                           capture_output=True, text=True, timeout=120)
    if clone.returncode != 0:
        return _fail(f"git clone failed: {(clone.stderr or '')[-300:]}")

    def _verify(cmd_str: str) -> dict:
        """Library/CLI/module build — there is no web server to serve. 'Run it' means run its
        tests: execute the suite one-shot, capture output, and return the result so the user sees
        it work, instead of a misleading 'add a preview command' error."""
        try:
            proc = subprocess.run(["sh", "-lc", cmd_str], cwd=str(workdir),
                                  capture_output=True, text=True, timeout=300)
            output = (proc.stdout or "") + (proc.stderr or "")
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            output, exit_code = "test run exceeded 300s and was stopped", 124
        try:
            Path(log_path).write_text(output, encoding="utf-8")
        except OSError:
            pass
        passed = exit_code == 0
        status = "verified" if passed else "failed"
        with begin(engine) as c:
            c.execute(text(
                "INSERT INTO app_previews (id,project_id,run_id,repo,branch,command,mode,substrate,"
                "build_substrate,status,failure_kind,error,log_path,expires_at) "
                "VALUES (:id,:p,:r,:repo,:b,:cmd,'test',:sub,:build,:st,:fk,:err,:lg,now())"),
                {"id": preview_id, "p": project_id, "r": run_id, "repo": f"{owner}/{repo}",
                 "b": branch, "cmd": cmd_str, "sub": ExecutionSubstrate.PREVIEW_HOST.value,
                 "build": build_substrate, "st": status,
                 "fk": None if passed else "tests_failed",
                 "err": None if passed else "tests failed — see output", "lg": log_path})
        return {
            "id": preview_id, "status": status, "mode": "test", "kind": "library",
            "command": cmd_str, "exit_code": exit_code, "passed": passed,
            "output": output[-6000:],
            "message": (
                f"Verified by tests — NOT a live app preview. No runnable entrypoint was detected "
                f"(looked for: boss.preview.command, package.json dev/start, Procfile web, Makefile "
                f"dev/run, {'/'.join(_PY_ENTRYPOINTS)}, index.html in {'/'.join(_STATIC_DIRS[1:])}), "
                f"so this is either a library/module or an app whose entrypoint is named something "
                f"unrecognised. Ran its test suite (`{cmd_str}`): "
                f"{'PASSED ✓' if passed else 'FAILED ✗'} (exit {exit_code}). See the output below "
                f"and the PR to review the code."),
        }

    # `impl_files` is what THIS task built — prefer its entrypoint over any
    # pre-existing app already in the repo.
    _changed = lineage_impl_files(engine, run)
    command = _detect_command(workdir, _changed)
    if not command:
        # No web server, but a library still has a way to 'run it' — its tests. Prefer the test
        # command the pipeline already recorded on the run, else detect one from the repo.
        test_cmd = (run.get("state") or {}).get("test_command") or _detect_verification(workdir)
        if test_cmd:
            return _verify(test_cmd)
        return _fail(
            "no runnable app or test suite found in this build (add boss.preview.command, a "
            "package.json dev/start/test script, a Procfile web command, a Makefile dev/run/test "
            "target, or a Python entrypoint/tests)"
        )

    port_hint = _free_port()
    # Point HOME + the npm cache at the (writable) workdir: the container's /home/boss/.npm is
    # owned by root (from the global claude-code install), so `npm install` in the preview would
    # fail with EACCES ("chown -R 100:101 /home/boss/.npm"). Keeps all npm writes inside workdir.
    env = {**os.environ, "PORT": str(port_hint), "HOST": "0.0.0.0", "PYTHONUNBUFFERED": "1",
           "HOME": str(workdir), "npm_config_cache": str(workdir / ".npm-cache"),
           "npm_config_fund": "false", "npm_config_audit": "false",
           "npm_config_update_notifier": "false"}
    logf = open(log_path, "w")
    proc = subprocess.Popen(_low_priority(command), cwd=str(workdir), env=env, stdout=logf,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    logf.close()

    # Web builds do `npm install` + `vite build` before the static server binds — well past the
    # default 20s window — so give discovery generous headroom (it returns as soon as the port
    # answers, so this never slows the fast case).
    port = _discover_working_port(log_path, port_hint, timeout=180.0)
    if port is None:
        _kill(proc.pid)
        tail = ""
        try:
            tail = Path(log_path).read_text(errors="ignore")[-300:]
        except Exception:
            pass
        return _fail(f"app did not serve HTTP within timeout. log tail: {tail}")

    url = f"http://127.0.0.1:{port}"
    expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    with begin(engine) as c:
        c.execute(text(
            "INSERT INTO app_previews (id,project_id,run_id,repo,branch,command,port,url,pid,"
            "substrate,build_substrate,status,log_path,expires_at) "
            "VALUES (:id,:p,:r,:repo,:b,:cmd,:port,:url,:pid,:sub,:build,'running',:lg,:exp)"),
            {"id": preview_id, "p": project_id, "r": run_id, "repo": f"{owner}/{repo}", "b": branch,
             "cmd": " ".join(command), "port": port, "url": url, "pid": proc.pid,
             "sub": ExecutionSubstrate.PREVIEW_HOST.value, "build": build_substrate,
             "lg": log_path, "exp": expires})
        emit_event(c, project_id, "app_preview.started", "operator", "app_preview", preview_id,
                   {"run_id": run_id, "url": url, "repo": f"{owner}/{repo}", "branch": branch})
    return {"id": preview_id, "status": "running", "url": url, "port": port,
            "pid": proc.pid, "expires_at": expires.isoformat(), "run_id": run_id,
            "repo": f"{owner}/{repo}", "branch": branch,
            "substrate": ExecutionSubstrate.PREVIEW_HOST.value,
            "build_substrate": build_substrate}


def _generate_dockerfile(workdir: Path, changed: Optional[list[str]] = None) -> int:
    """Ensure the app has a Dockerfile; return the container port the app listens on.
    Generated images set HOST=0.0.0.0 + PORT so deploy-ready apps bind correctly for -p mapping."""
    existing = workdir / "Dockerfile"
    if existing.exists():
        m = re.search(r"EXPOSE\s+(\d{2,5})", existing.read_text(errors="ignore"))
        return int(m.group(1)) if m else 8000
    # Prefer the entrypoint THIS task built. Sharing _PY_ENTRYPOINTS between the host and
    # container paths was not enough: the preference logic lived only in _detect_command, so a
    # docker-mode preview still picked the repo's pre-existing serve.py and launched Sudoku for
    # an N-Queens build (PR #62). Both paths must make the SAME choice, not merely consult the
    # same list.
    kind, value = resolve_app_entry(workdir, changed)
    if kind == "static":
        # Serve the static app THIS task built. Without this branch the container path fell
        # through to the repo-wide python entrypoint and launched the wrong application.
        port = 8000
        existing.write_text(
            "FROM python:3.11-slim\nWORKDIR /app\nCOPY . .\n"
            f"ENV HOST=0.0.0.0 PORT={port}\nEXPOSE {port}\n"
            f'CMD ["python3", "-m", "http.server", "{port}", "--bind", "0.0.0.0", '
            f'"--directory", "{value}"]\n')
        return port
    py_entry = value if kind == "py" else None
    if py_entry:
        port = 8000
        reqs = "COPY requirements.txt ./\nRUN pip install --no-cache-dir -r requirements.txt\n" \
            if (workdir / "requirements.txt").exists() else ""
        existing.write_text(
            "FROM python:3.11-slim\nWORKDIR /app\n" + reqs +
            f"COPY . .\nENV HOST=0.0.0.0 PORT={port} PYTHONUNBUFFERED=1\nEXPOSE {port}\n"
            f'CMD ["python3", "{py_entry}"]\n')
        return port
    if (workdir / "package.json").exists():
        port = 3000
        existing.write_text(
            "FROM node:20-slim\nWORKDIR /app\nCOPY package*.json ./\n"
            "RUN npm install --no-audit --no-fund --loglevel=error\nCOPY . .\n"
            "RUN npm run build --if-present || true\n"
            f"ENV HOST=0.0.0.0 PORT={port}\nEXPOSE {port}\nCMD [\"npm\", \"run\", \"start\"]\n")
        return port
    raise RuntimeError("no Dockerfile and could not infer a stack (server.py/app.py/package.json)")


def start_container_preview(engine, settings: Settings, run_id: str, *, ttl_minutes: int = 60) -> dict:
    """Tier-2 dev-container preview: build a Docker image from the run's branch and run it with a
    mapped port — isolated, reproducible, the same image that later ships to remote compute."""
    if shutil.which("docker") is None:
        raise RuntimeError("docker is not available on this host")
    with begin(engine) as c:
        run = load_code_task_run(c, run_id)
    if run is None:
        raise ValueError(f"code task run not found: {run_id}")
    assert_previewable(run, run_id)
    spec = run.get("spec") or {}
    owner = spec.get("owner") or settings.target_github_owner
    repo = spec.get("repo") or settings.target_github_repo
    # Iterations reuse the ROOT run's branch (boss/code/{root_run_id}); a run's own
    # id may have no branch. Prefer an explicit work_branch, else the root-derived one.
    _fctx = run.get("finalize_ctx") or {}
    branch = _fctx.get("work_branch") or f"boss/code/{_fctx.get('root_run_id') or run_id}"
    build_substrate = _fctx.get("execution_substrate") or normalize_build_substrate(
        settings.sandbox_provider
    ).value
    project_id = run["project_id"]
    preview_id = make_id("prev")
    tag = f"boss-prev-{preview_id.replace('_', '-').lower()}"
    PREVIEW_ROOT.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix=f"{preview_id}-", dir=str(PREVIEW_ROOT)))

    def _fail(msg: str) -> dict:
        failure_kind, suggested_action = _classify_start_failure(msg)
        with begin(engine) as c:
            c.execute(text(
                "INSERT INTO app_previews (id,project_id,run_id,repo,branch,mode,substrate,"
                "build_substrate,status,error,failure_kind,expires_at) "
                "VALUES (:id,:p,:r,:repo,:b,'docker',:sub,:build,'failed',:e,:fk,now())"),
                {"id": preview_id, "p": project_id, "r": run_id, "repo": f"{owner}/{repo}",
                 "b": branch, "sub": ExecutionSubstrate.PREVIEW_DOCKER.value,
                 "build": build_substrate, "e": msg[:500], "fk": failure_kind})
        return {"id": preview_id, "status": "failed", "error": msg, "mode": "docker",
                "failure_kind": failure_kind, "suggested_action": suggested_action}

    token = settings.github_token
    from .repo_host import clone_url_for  # BOSS-267: host-aware clone
    clone_url = clone_url_for(getattr(settings, "target_repo_host", "github") or "github",
                              owner, repo, token or "")
    clone = subprocess.run(["git", "clone", "--depth", "1", "--branch", branch, clone_url, str(workdir)],
                           capture_output=True, text=True, timeout=120)
    if clone.returncode != 0:
        return _fail(f"git clone failed: {(clone.stderr or '')[-300:]}")

    try:
        _changed_ep = lineage_impl_files(engine, run)
        container_port = _generate_dockerfile(workdir, _changed_ep)
    except RuntimeError as exc:
        return _fail(str(exc))

    build = subprocess.run(["docker", "build", "-t", tag, str(workdir)],
                           capture_output=True, text=True, timeout=600)
    if build.returncode != 0:
        return _fail(f"docker build failed: {(build.stderr or build.stdout or '')[-400:]}")

    host_port = _free_port()
    run_cmd = subprocess.run(
        ["docker", "run", "-d", "--name", tag, "-p", f"{host_port}:{container_port}",
         "-e", f"PORT={container_port}", "-e", "HOST=0.0.0.0", tag],
        capture_output=True, text=True, timeout=60)
    if run_cmd.returncode != 0:
        return _fail(f"docker run failed: {(run_cmd.stderr or '')[-300:]}")
    container_id = run_cmd.stdout.strip()[:12]

    url = f"http://127.0.0.1:{host_port}"
    ok = False
    for _ in range(20):
        if _http_ok(host_port):
            ok = True
            break
        time.sleep(1.0)
    if not ok:
        logs = subprocess.run(["docker", "logs", "--tail", "20", tag], capture_output=True, text=True).stdout
        subprocess.run(["docker", "rm", "-f", tag], capture_output=True)
        return _fail(f"container did not serve HTTP. logs: {logs[-300:]}")

    expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    with begin(engine) as c:
        c.execute(text(
            "INSERT INTO app_previews (id,project_id,run_id,repo,branch,mode,container_id,port,url,"
            "substrate,build_substrate,status,expires_at) "
            "VALUES (:id,:p,:r,:repo,:b,'docker',:cid,:port,:url,:sub,:build,'running',:exp)"),
            {"id": preview_id, "p": project_id, "r": run_id, "repo": f"{owner}/{repo}", "b": branch,
             "cid": container_id, "port": host_port, "url": url,
             "sub": ExecutionSubstrate.PREVIEW_DOCKER.value, "build": build_substrate,
             "exp": expires})
        emit_event(c, project_id, "app_preview.started", "operator", "app_preview", preview_id,
                   {"run_id": run_id, "url": url, "mode": "docker", "image": tag})
    return {"id": preview_id, "status": "running", "url": url, "port": host_port, "mode": "docker",
            "container_id": container_id, "image": tag, "expires_at": expires.isoformat(),
            "run_id": run_id, "repo": f"{owner}/{repo}", "branch": branch,
            "substrate": ExecutionSubstrate.PREVIEW_DOCKER.value,
            "build_substrate": build_substrate}


def start_rootfs_preview(
    engine, settings: Settings, run_id: str, *, ttl_minutes: int = 60,
    command: str = "",
) -> dict:
    """Serve a micro-VM run from its retained build rootfs (no host-side re-clone)."""
    with begin(engine) as c:
        run = load_code_task_run(c, run_id)
    if run is None:
        raise ValueError(f"code task run not found: {run_id}")
    assert_previewable(run, run_id)
    fctx = run.get("finalize_ctx") or {}
    state = run.get("state") or {}
    build_substrate = fctx.get("execution_substrate") or state.get("execution_substrate")
    if build_substrate != ExecutionSubstrate.MICROVM.value:
        raise RuntimeError("rootfs preview is only available for a micro-VM build")
    sandbox_ids = fctx.get("sandbox_task_ids") or state.get("sandbox_task_ids") or []
    sandbox_id = fctx.get("sandbox_task_id") or (sandbox_ids[-1] if sandbox_ids else None)
    if not sandbox_id:
        raise RuntimeError("micro-VM build has no retained sandbox_task_id")

    provider = MicroVMProvider(settings)
    provider.hold(
        sandbox_id,
        ttl_seconds=max(60, int(ttl_minutes) * 60),
        reason=f"rootfs preview for {run_id}",
    )
    served = provider.serve_preview(
        sandbox_id,
        ttl_seconds=max(60, int(ttl_minutes) * 60),
        command=command,
    )
    remote_url = served.get("url") or served.get("proxy_url")
    if not remote_url:
        raise RuntimeError("sandbox service did not return a rootfs preview URL")
    preview_id = make_id("prev")
    spec = run.get("spec") or {}
    owner = spec.get("owner") or settings.target_github_owner
    repo = spec.get("repo") or settings.target_github_repo
    branch = fctx.get("work_branch") or f"boss/code/{fctx.get('root_run_id') or run_id}"
    expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    with begin(engine) as c:
        c.execute(
            text(
                "INSERT INTO app_previews (id,project_id,run_id,repo,branch,command,url,mode,"
                "substrate,build_substrate,sandbox_task_id,status,expires_at) "
                "VALUES (:id,:p,:r,:repo,:b,:cmd,:url,'rootfs',:sub,:build,:sid,'running',:exp)"
            ),
            {"id": preview_id, "p": run["project_id"], "r": run_id,
             "repo": f"{owner}/{repo}", "b": branch, "cmd": served.get("command") or command,
             "url": remote_url, "sub": ExecutionSubstrate.PREVIEW_ROOTFS.value,
             "build": build_substrate, "sid": sandbox_id, "exp": expires},
        )
        emit_event(
            c, run["project_id"], "app_preview.started", "operator", "app_preview", preview_id,
            {"run_id": run_id, "mode": "rootfs", "sandbox_task_id": sandbox_id,
             "substrate": ExecutionSubstrate.PREVIEW_ROOTFS.value},
        )
    return {
        "id": preview_id, "status": "running", "mode": "rootfs",
        "substrate": ExecutionSubstrate.PREVIEW_ROOTFS.value,
        "build_substrate": build_substrate, "sandbox_task_id": sandbox_id,
        "expires_at": expires.isoformat(), "run_id": run_id,
        "repo": f"{owner}/{repo}", "branch": branch,
    }


def _process_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def _docker_alive(container_id: Optional[str]) -> bool:
    if not container_id or shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container_id],
        capture_output=True, text=True, timeout=5,
    )
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def _respawn_process_preview(engine, settings: Settings, preview_id: str) -> None:
    """Re-launch a dead process-mode preview IN PLACE — same preview id, so its
    /v1/previews/<id>/web URL survives an api restart/redeploy. Re-clones the branch, rebuilds,
    serves, then flips the SAME row back to 'running' with the new pid/port. Runs in a background
    thread (the clone+build takes ~60s) so it never blocks api boot."""
    try:
        with begin(engine) as c:
            row = c.execute(text(
                "SELECT run_id,repo,branch,build_substrate,expires_at FROM app_previews WHERE id=:id"
            ), {"id": preview_id}).mappings().first()
        if row is None:
            return
        repo_full = row["repo"] or ""
        owner, _, repo = repo_full.partition("/")
        branch = row["branch"]
        if not owner or not repo or not branch:
            return
        PREVIEW_ROOT.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix=f"{preview_id}-", dir=str(PREVIEW_ROOT)))
        log_path = str(workdir / "_preview.log")
        token = settings.github_token
        from .repo_host import clone_url_for  # BOSS-267: host-aware clone
        clone_url = clone_url_for(getattr(settings, "target_repo_host", "github") or "github",
                                  owner, repo, token or "")
        clone = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", branch, clone_url, str(workdir)],
            capture_output=True, text=True, timeout=120)
        if clone.returncode != 0:
            return
        _changed = lineage_impl_files(engine, run)
        command = _detect_command(workdir, _changed)
        if not command:
            return
        port_hint = _free_port()
        env = {**os.environ, "PORT": str(port_hint), "HOST": "0.0.0.0", "PYTHONUNBUFFERED": "1",
               "HOME": str(workdir), "npm_config_cache": str(workdir / ".npm-cache"),
               "npm_config_fund": "false", "npm_config_audit": "false",
               "npm_config_update_notifier": "false"}
        logf = open(log_path, "w")
        proc = subprocess.Popen(_low_priority(command), cwd=str(workdir), env=env, stdout=logf,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True)
        logf.close()
        port = _discover_working_port(log_path, port_hint, timeout=180.0)
        if port is None:
            _kill(proc.pid)
            return
        url = f"http://127.0.0.1:{port}"
        with begin(engine) as c:
            c.execute(text(
                "UPDATE app_previews SET status='running', pid=:pid, port=:port, url=:url, "
                "log_path=:lg WHERE id=:id"),
                {"pid": proc.pid, "port": port, "url": url, "lg": log_path, "id": preview_id})
    except Exception:  # noqa: BLE001 — best-effort; a failed respawn just leaves it 'stopped'
        return


# How many dead process previews to auto-respawn on boot (newest first) — bounds the rebuild
# cost so a restart with many historical previews doesn't stampede npm builds.
_RESPAWN_ON_BOOT_LIMIT = 3


def reconcile_previews(engine, settings: Settings) -> dict[str, int]:
    """Idempotently reconcile durable preview rows against their live substrate."""
    with begin(engine) as c:
        rows = c.execute(text(
            "SELECT id,pid,mode,container_id,url,expires_at,sandbox_task_id FROM app_previews "
            "WHERE status='running'"
        )).mappings().all()
    now = datetime.now(timezone.utc)
    alive = stopped = expired = 0
    headers = ({"X-Sandbox-Token": settings.sandbox_token}
               if settings.sandbox_token else {})
    respawn_candidates: list[str] = []   # dead process-mode previews to relaunch in place
    for raw in rows:
        row = dict(raw)
        expires_at = row.get("expires_at")
        mode = row.get("mode") or "process"
        if expires_at and expires_at <= now:
            target_status = "expired"
        else:
            substrate_alive = (
                _docker_alive(row.get("container_id")) if mode == "docker"
                else _url_ok(row.get("url") or "", headers=headers) if mode == "rootfs"
                else _process_alive(row.get("pid"))
            )
            healthy = substrate_alive and _url_ok(
                row.get("url") or "", headers=headers if mode == "rootfs" else None
            )
            target_status = "running" if healthy else "stopped"
        if target_status == "running":
            alive += 1
            continue
        if target_status == "expired":
            _teardown(row, settings)
        with begin(engine) as c:
            c.execute(
                text("UPDATE app_previews SET status=:status WHERE id=:id AND status='running'"),
                {"status": target_status, "id": row["id"]},
            )
        if target_status == "expired":
            expired += 1
        else:
            stopped += 1
            # A process preview (web app) that died with the old container — respawn it IN PLACE so
            # its URL survives the restart. rootfs/docker/test modes reconcile on their own.
            if mode == "process":
                respawn_candidates.append(row["id"])

    # Bound the rebuild cost: only relaunch the newest few (they're the ones anyone is looking at).
    respawned = 0
    if respawn_candidates:
        with begin(engine) as c:
            ordered = [r[0] for r in c.execute(text(
                "SELECT id FROM app_previews WHERE id = ANY(:ids) ORDER BY created_at DESC LIMIT :lim"
            ), {"ids": respawn_candidates, "lim": _RESPAWN_ON_BOOT_LIMIT}).all()]
        for pid in ordered:
            threading.Thread(target=_respawn_process_preview, args=(engine, settings, pid),
                             daemon=True).start()
            respawned += 1
    return {"checked": len(rows), "running": alive, "stopped": stopped, "expired": expired,
            "respawning": respawned}


def preview_log_snapshot(engine, preview_id: str, *, tail_chars: int = 12000) -> dict:
    with begin(engine) as c:
        row = c.execute(text(
            "SELECT status,mode,log_path,container_id,failure_kind,error FROM app_previews WHERE id=:id"
        ), {"id": preview_id}).mappings().first()
    if row is None:
        raise ValueError("preview not found")
    content = ""
    if row["log_path"]:
        try:
            content = Path(row["log_path"]).read_text(errors="ignore")[-tail_chars:]
        except OSError:
            content = ""
    elif row["mode"] == "docker" and row["container_id"] and shutil.which("docker"):
        result = subprocess.run(
            ["docker", "logs", "--tail", "200", row["container_id"]],
            capture_output=True, text=True, timeout=10,
        )
        content = (result.stdout + result.stderr)[-tail_chars:]
    return {"preview_id": preview_id, "status": row["status"], "mode": row["mode"],
            "failure_kind": row["failure_kind"], "error": row["error"], "log": content}


def stream_preview_logs(engine, preview_id: str):
    """SSE generator that tails startup logs until the preview reaches a terminal state."""
    offset = 0
    for _ in range(600):  # bounded to ten minutes; clients may reconnect
        snapshot = preview_log_snapshot(engine, preview_id)
        content = snapshot["log"]
        if len(content) > offset:
            chunk = content[offset:]
            offset = len(content)
            yield f"event: log\ndata: {json.dumps(chunk)}\n\n"
        else:
            yield ": heartbeat\n\n"
        if snapshot["status"] in {"failed", "stopped", "expired"}:
            yield f"event: end\ndata: {json.dumps(snapshot['status'])}\n\n"
            return
        time.sleep(1)


def _docker_rm(container_id: Optional[str]) -> None:
    if container_id:
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)


def _kill(pid: Optional[int]) -> None:
    if not pid:
        return
    try:
        os.killpg(os.getpgid(pid), 9)
    except Exception:
        try:
            os.kill(pid, 9)
        except Exception:
            pass


def _inject_shim(html: bytes, prefix: str) -> bytes:
    """Make a path-prefixed preview behave like an app mounted at `/`.

    The server-side rewrite handles initial markup/CSS; the client shim covers
    dynamic DOM, fetch/XHR, history routing, WebSocket/EventSource, and captures
    preview-anchored operator feedback.
    """
    prefix = prefix.rstrip("/")
    p_json = json.dumps(prefix)
    feedback_url = json.dumps(prefix.rsplit("/web", 1)[0] + "/feedback")
    shim = (
        "<base href=\"" + prefix + "/\">"
        "<script data-boss-preview-shim>(function(){var P=" + p_json + ",F=" + feedback_url + ";"
        "function path(u){if(typeof u!==\"string\")return u;"
        "if(u.charAt(0)===\"/\"&&u.slice(0,2)!==\"//\"&&u.indexOf(P)!==0)return P+u;return u;}"
        "function srcset(v){return String(v||\"\").split(\",\").map(function(x){var p=x.trim().split(/\\s+/);"
        "p[0]=path(p[0]);return p.join(\" \").trim();}).join(\", \");}"
        "function ws(u){u=path(u);if(typeof u===\"string\"&&u.charAt(0)===\"/\")"
        "return(location.protocol===\"https:\"?\"wss:\":\"ws:\")+\"//\"+location.host+u;return u;}"
        "var of=window.fetch;window.fetch=function(u,o){return of.call(this,path(u),o);};"
        "var xo=XMLHttpRequest.prototype.open;XMLHttpRequest.prototype.open=function(m,u){"
        "arguments[1]=path(u);return xo.apply(this,arguments);};"
        "[\"pushState\",\"replaceState\"].forEach(function(k){var old=history[k];"
        "history[k]=function(s,t,u){return old.call(this,s,t,path(u));};});"
        "var OW=window.WebSocket;if(OW){window.WebSocket=function(u,p){"
        "return p===undefined?new OW(ws(u)):new OW(ws(u),p);};window.WebSocket.prototype=OW.prototype;}"
        "var OE=window.EventSource;if(OE){window.EventSource=function(u,o){return new OE(path(u),o);};"
        "window.EventSource.prototype=OE.prototype;}"
        "function fix(root){if(!root||!root.querySelectorAll)return;"
        "root.querySelectorAll(\"[href],[src],[action],[srcset],[style]\").forEach(function(el){"
        "[\"href\",\"src\",\"action\"].forEach(function(a){var v=el.getAttribute(a);"
        "if(v)el.setAttribute(a,path(v));});var ss=el.getAttribute(\"srcset\");if(ss)el.setAttribute(\"srcset\",srcset(ss));"
        "var st=el.getAttribute(\"style\");if(st)el.setAttribute(\"style\",st.replace(/url\\((['\"]?)\\/(?!\\/)/g,\"url($1\"+P+\"/\"));});}"
        "new MutationObserver(function(ms){ms.forEach(function(m){m.addedNodes.forEach(fix);});})"
        ".observe(document.documentElement,{childList:true,subtree:true});"
        "document.addEventListener(\"DOMContentLoaded\",function(){fix(document);"
        "if(document.querySelector(\"[data-boss-feedback]\"))return;var b=document.createElement(\"button\");"
        "b.textContent=\"Send preview feedback\";b.setAttribute(\"data-boss-feedback\",\"1\");"
        "b.style.cssText=\"position:fixed;right:16px;bottom:16px;z-index:2147483647;padding:9px 13px;"
        "border:0;border-radius:999px;background:#111827;color:white;font:600 12px system-ui;cursor:pointer\";"
        "b.onclick=async function(){var message=prompt(\"What should BOSS change here?\");if(!message)return;"
        "var route=location.pathname.indexOf(P)===0?(location.pathname.slice(P.length)||\"/\"):location.pathname;"
        "var a=document.activeElement,sel=a?String(a.tagName||\"\").toLowerCase()+"
        "(a.id?\"#\"+a.id:\"\")+(a.className&&typeof a.className===\"string\"?\".\"+a.className.trim().replace(/\\s+/g,\".\"):\"\"):\"\";"
        "var r=await of.call(window,F,{method:\"POST\",headers:{\"Content-Type\":\"application/json\"},"
        "body:JSON.stringify({message:message,anchor:route,selected_element:sel})});"
        "var d=await r.json();alert(d.question||\"Feedback captured. Confirm the proposed iteration in BOSS.\");};"
        "document.body.appendChild(b);});})();</script>"
    )
    try:
        s = html.decode("utf-8", "ignore")
        # Initial static resources and CSS URLs must be prefixed before the browser
        # starts fetching them. Protocol-relative/external/fragment URLs are untouched.
        def attr_rewrite(match: re.Match) -> str:
            value = match.group("value")
            if value.startswith("/") and not value.startswith("//") and not value.startswith(prefix):
                value = prefix + value
            return f"{match.group('attr')}={match.group('quote')}{value}{match.group('quote')}"

        s = re.sub(
            r"(?P<attr>href|src|action)=(?P<quote>['\"])(?P<value>[^'\"]*)(?P=quote)",
            attr_rewrite, s, flags=re.I,
        )
        def srcset_rewrite(match: re.Match) -> str:
            candidates = []
            for candidate in match.group("value").split(","):
                pieces = candidate.strip().split()
                if pieces and pieces[0].startswith("/") and not pieces[0].startswith("//"):
                    pieces[0] = prefix + pieces[0]
                candidates.append(" ".join(pieces))
            return f"srcset={match.group('quote')}{', '.join(candidates)}{match.group('quote')}"

        s = re.sub(
            r"srcset=(?P<quote>['\"])(?P<value>[^'\"]*)(?P=quote)",
            srcset_rewrite, s, flags=re.I,
        )
        s = re.sub(r"url\((['\"]?)/(?!/)", rf"url(\1{prefix}/", s, flags=re.I)
        if re.search(r"<head(?:\s[^>]*)?>", s, flags=re.I):
            s = re.sub(r"(<head(?:\s[^>]*)?>)", lambda m: m.group(1) + shim, s, count=1, flags=re.I)
        elif "<html" in s.lower():
            s = re.sub(r"(<html[^>]*>)", lambda m: m.group(1) + "<head>" + shim + "</head>", s,
                       count=1, flags=re.I)
        else:
            s = shim + s
        return s.encode("utf-8")
    except Exception:  # noqa: BLE001
        return html


def proxy_preview(engine, preview_id: str, subpath: str, method: str,
                  query_string: str = "", body: Optional[bytes] = None,
                  content_type: Optional[str] = None,
                  sandbox_token: str = ""):
    """Reverse-proxy a request to a running preview's loopback port → (status, content_type, bytes).
    Only the api can reach the preview's 127.0.0.1 port, so this exposes it via the api's own
    (tailnet-routed) URL. HTML responses get the root-relative path-rewrite shim."""
    import urllib.error
    with begin(engine) as c:
        row = c.execute(text("SELECT url,status,mode FROM app_previews WHERE id=:id"),
                        {"id": preview_id}).mappings().first()
    if row is None:
        return 404, "text/plain", b"preview not found", {}
    if row["status"] != "running" or not row["url"]:
        return 409, "text/plain", f"preview not running (status={row['status']})".encode(), {}
    target = row["url"].rstrip("/") + "/" + (subpath or "")
    if query_string:
        target += "?" + query_string
    req = urllib.request.Request(target, data=body or None, method=method)
    if content_type:
        req.add_header("Content-Type", content_type)
    if row["mode"] == "rootfs" and sandbox_token:
        req.add_header("X-Sandbox-Token", sandbox_token)
    response_headers: dict[str, str] = {}
    try:
        resp = urllib.request.build_opener(_NoRedirect()).open(req, timeout=25)
        status = resp.status
        ctype = resp.headers.get("Content-Type", "application/octet-stream")
        data = resp.read()
        if resp.headers.get("Location"):
            response_headers["Location"] = resp.headers["Location"]
    except urllib.error.HTTPError as e:
        status, ctype, data = e.code, e.headers.get("Content-Type", "text/plain"), e.read()
        if e.headers.get("Location"):
            response_headers["Location"] = e.headers["Location"]
    except Exception as e:  # noqa: BLE001 — process gone (e.g. after an api restart): self-heal
        try:
            with begin(engine) as c2:
                c2.execute(text("UPDATE app_previews SET status='stopped' WHERE id=:id AND status='running'"),
                           {"id": preview_id})
        except Exception:
            pass
        return 502, "text/html", (
            "<div style=\"font-family:system-ui;padding:24px;max-width:520px\">"
            "<h3>Preview is no longer running</h3>"
            "<p>Its process ended (this happens after a redeploy). Go back to the code task and click "
            "<b>Run preview</b> to regenerate it.</p>"
            f"<p style=\"color:#888;font-size:12px\">{e}</p></div>"
        ).encode(), {}
    location = response_headers.get("Location")
    if location and location.startswith("/") and not location.startswith("//"):
        response_headers["Location"] = f"/v1/previews/{preview_id}/web{location}"
    if "text/html" in (ctype or ""):
        data = _inject_shim(data, f"/v1/previews/{preview_id}/web")
    return status, ctype, data, response_headers


def smoke_test(engine, preview_id: str, checks: Optional[list[dict]] = None) -> dict:
    """Tier-3 smoke canary: hit the live preview URL with a set of checks and assert each — proves
    the deployed app actually works, not just that it serves a page. Records a receipt event.
    Each check: {method, path, body?, expect_status?, expect_contains?}. Default: GET / -> 2xx."""
    import urllib.request
    with begin(engine) as c:
        row = c.execute(text("SELECT project_id,url,status,run_id FROM app_previews WHERE id=:id"),
                        {"id": preview_id}).mappings().first()
    if row is None:
        raise ValueError("preview not found")
    if row["status"] != "running" or not row["url"]:
        raise ValueError(f"preview not running (status={row['status']})")
    base = row["url"].rstrip("/")
    checks = checks or [{"method": "GET", "path": "/", "expect_status": 200}]
    results, passed = [], 0
    for chk in checks:
        method = (chk.get("method") or "GET").upper()
        path = chk.get("path") or "/"
        want_status = chk.get("expect_status")
        want_contains = chk.get("expect_contains")
        data = json.dumps(chk["body"]).encode() if chk.get("body") is not None else None
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        ok, status, detail = False, None, ""
        try:
            resp = urllib.request.urlopen(req, timeout=8)
            status = resp.status
            text_body = resp.read(8000).decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            status = e.code
            text_body = (e.read(2000).decode("utf-8", "ignore") if hasattr(e, "read") else "")
        except Exception as e:  # noqa: BLE001
            detail = str(e)[:160]
            text_body = ""
        if status is not None:
            ok = ((want_status is None or status == want_status)
                  and (want_contains is None or want_contains in text_body))
            if not ok and not detail:
                detail = (f"status {status}" + (f" != {want_status}" if want_status and status != want_status else "")
                          + ("; missing expected text" if want_contains and want_contains not in text_body else ""))
        results.append({"method": method, "path": path, "status": status, "ok": ok, "detail": detail})
        if ok:
            passed += 1
    all_ok = passed == len(checks)
    with begin(engine) as c:
        emit_event(c, row["project_id"], "app_preview.smoke", "operator", "app_preview", preview_id,
                   {"run_id": row["run_id"], "passed": passed, "total": len(checks),
                    "ok": all_ok, "results": results})
    return {"preview_id": preview_id, "url": base, "ok": all_ok,
            "passed": passed, "total": len(checks), "results": results}


def _teardown(row, settings: Optional[Settings] = None) -> None:
    if row.get("mode") == "docker":
        _docker_rm(row.get("container_id"))
    elif row.get("mode") == "rootfs":
        sandbox_id = row.get("sandbox_task_id")
        if settings and sandbox_id:
            # Explicitly stop/release the retained rootfs when the preview is stopped
            # or expires. Both operations are best-effort and bounded by service TTL.
            base = (settings.sandbox_agent_url or "").rstrip("/")
            headers = ({"X-Sandbox-Token": settings.sandbox_token}
                       if settings.sandbox_token else {})
            try:
                import httpx
                with httpx.Client(timeout=15.0) as client:
                    client.delete(
                        f"{base}/v1/sandbox/tasks/{sandbox_id}/preview", headers=headers
                    )
            except Exception:  # noqa: BLE001
                pass
            MicroVMProvider(settings).release(sandbox_id)
    else:
        _kill(row.get("pid"))


def stop_preview(engine, preview_id: str, settings: Optional[Settings] = None) -> dict:
    with begin(engine) as c:
        row = c.execute(text(
            "SELECT pid,mode,container_id,sandbox_task_id FROM app_previews WHERE id=:id"
        ),
                        {"id": preview_id}).mappings().first()
        if row is None:
            raise ValueError("preview not found")
        _teardown(dict(row), settings)
        c.execute(text("UPDATE app_previews SET status='stopped' WHERE id=:id"), {"id": preview_id})
    return {"id": preview_id, "status": "stopped"}


def list_previews(
    engine, project_id: str, limit: int = 50, settings: Optional[Settings] = None
) -> list[dict]:
    """Reap expired previews (tear down + mark) and return recent ones."""
    with begin(engine) as c:
        expired = c.execute(text(
            "SELECT id,pid,mode,container_id,sandbox_task_id FROM app_previews "
            "WHERE status='running' AND expires_at < now()")).mappings().all()
        for r in expired:
            _teardown(dict(r), settings)
            c.execute(text("UPDATE app_previews SET status='expired' WHERE id=:id"), {"id": r["id"]})
        rows = c.execute(text(
            "SELECT id,run_id,repo,branch,url,port,mode,substrate,build_substrate,sandbox_task_id,"
            "status,error,failure_kind,expires_at,created_at "
            "FROM app_previews WHERE project_id=:p ORDER BY created_at DESC LIMIT :l"),
            {"p": project_id, "l": int(limit)}).mappings().all()
    return [dict(r) for r in rows]


def start_preview_for_run(engine, settings: Settings, run_id: str, *, ttl_minutes: int = 60) -> dict:
    """Start a preview the same way the console's button does: try the substrates in order.

    Exists so the pipeline's auto-preview and the API endpoint share ONE launch policy. The
    endpoint used to own the ordering inline, and anything else calling a single start_* would
    have silently used a different strategy — the same drift that made the container path pick
    a different app from the host path.
    """
    attempts = (start_rootfs_preview, start_container_preview, start_preview)
    last: dict = {"status": "failed", "error": "preview could not start"}
    for fn in attempts:
        try:
            res = fn(engine, settings, run_id, ttl_minutes=ttl_minutes)
        except (PreviewNotReady, AmbiguousAppError):
            raise
        except (ValueError, RuntimeError) as exc:
            last = {"status": "failed", "error": str(exc)}
            continue
        if res.get("status") == "running":
            return res
        last = res
    return last
