"""RUN a multi-service app's verify plan (services.stacks.StackPlan) in a build sandbox.

1. Each service's own install, build and unit tests, in its own directory and toolchain — through
   the sandbox runner, like every verify command (argv, allowlisted, the run's uid).
2. Then the backend is STARTED with its real command — launched with preview_service.launch_app,
   the launcher the serve proof and the preview use — and BOSS waits for it to answer HTTP.
3. With it up, the front end's e2e commands (and an e2e harness's) run with BACKEND_URL,
   BOSS_BACKEND_URL and API_BASE_URL pointing at it. A front end whose Playwright config starts no
   web server is served by BOSS too (its built `npm run preview`), and BASE_URL names it.

The first failure ends the plan, as run_until_failure does for a single-package app, and what ran
is returned so the gate's output, the repair prompt and the evidence read the same as before.
`backend_running` is also what an applied template's tests run inside (the template gate), so
"the template's tests ran against the started backend" is literally true.
"""
from __future__ import annotations

import contextlib
import logging
import os
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from .stacks import BACKEND_ENV, StackPlan

log = logging.getLogger("boss.stack_verify")

BACKEND_START_SECONDS = 180.0


class BackendDidNotStart(RuntimeError):
    pass


@dataclass
class Session:
    url: str
    port: int
    command: str
    frontend_url: Optional[str] = None
    record: dict[str, Any] = field(default_factory=dict)

    def env(self) -> dict[str, str]:
        out = {name: self.url for name in BACKEND_ENV}
        if self.frontend_url:
            out.update({"BASE_URL": self.frontend_url, "PLAYWRIGHT_BASE_URL": self.frontend_url})
        return out


def answers(port: int, path: str = "/", timeout: float = 2.0) -> bool:
    """Anything below 500 is a server answering — an API may well 404 at `/`."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
            return int(r.status) < 500
    except urllib.error.HTTPError as error:
        return int(error.code) < 500
    except Exception:  # noqa: BLE001 — not up yet
        return False


def _tail(path: str, limit: int = 1500) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def _launch(workdir: Path, command: list[str], label: str, *, port: Optional[int] = None,
            extra_env: Optional[dict[str, str]] = None, health: str = "/",
            timeout: float = BACKEND_START_SECONDS):
    """Start one process the preview's way and wait for it to answer. (proc, port, log path)."""
    from .preview_service import _free_port, _kill, launch_app

    fd, log_path = tempfile.mkstemp(prefix=f"boss-stack-{label}-", suffix=".log")
    os.close(fd)
    os.unlink(log_path)  # launch_app creates it O_NOFOLLOW
    env = {"HOME": os.environ.get("HOME", str(workdir))}
    env.update(extra_env or {})
    proc, port = launch_app(workdir, command, log_path, port=port or _free_port(), extra_env=env)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if answers(port, health):
            return proc, port, log_path
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    code = proc.poll()
    _kill(proc.pid)
    why = (f"exited (code {code}) before it answered HTTP" if code is not None
           else f"did not answer HTTP on port {port} within {timeout:g}s")
    tail = _tail(log_path)
    with contextlib.suppress(OSError):
        os.unlink(log_path)
    raise BackendDidNotStart(f"The {label} ({' '.join(command[-1:])}) {why}."
                             + (f" Its output ended:\n{tail}" if tail.strip() else ""))


@contextlib.contextmanager
def backend_running(plan: StackPlan, *, timeout: Optional[float] = None) -> Iterator[Session]:
    """The plan's backend (and, when its Playwright starts none, its front end), up for the body.
    Raises BackendDidNotStart with the process's own output."""
    from .preview_service import _kill, preview_start_timeout

    if plan.backend is None or not plan.backend_command:
        raise BackendDidNotStart(plan.error or "this app has no backend to start")
    back_dir = plan.dir_of(plan.backend)
    limit = timeout or preview_start_timeout(back_dir, BACKEND_START_SECONDS)
    procs: list[Any] = []
    logs: list[str] = []
    try:
        proc, port, log_path = _launch(back_dir, plan.backend_command, "backend",
                                       health=plan.backend_health, timeout=limit)
        procs.append(proc)
        logs.append(log_path)
        session = Session(f"http://127.0.0.1:{port}", port, plan.backend_command[-1])
        session.record = {"backend": {"dir": plan.backend.rel, "command": session.command,
                                      "url": session.url, "ok": True}}
        if plan.serve_frontend and plan.frontend is not None:
            from .stacks import VITE_SERVE

            front_cmd = plan.frontend_command or ["sh", "-lc", VITE_SERVE]
            fproc, fport, flog = _launch(plan.dir_of(plan.frontend), front_cmd, "frontend",
                                         extra_env=session.env(), timeout=120.0)
            procs.append(fproc)
            logs.append(flog)
            session.frontend_url = f"http://127.0.0.1:{fport}"
            session.record["frontend"] = {"dir": plan.frontend.rel, "url": session.frontend_url,
                                          "command": front_cmd[-1]}
        yield session
    finally:
        for p in procs:
            _kill(p.pid)
        for path in logs:
            with contextlib.suppress(OSError):
                os.unlink(path)


def run_in(sandbox, command: str, cwd: Path, *, timeout: Optional[int] = None,
           env: Optional[dict[str, str]] = None):
    """sandbox.run with an environment when the sandbox takes one."""
    if env:
        try:
            return sandbox.run(command, cwd=str(cwd), timeout=timeout, env=env)
        except TypeError:
            pass
    return sandbox.run(command, cwd=str(cwd), timeout=timeout)


def _failed(message: str):
    from .sandbox import CommandResult

    return CommandResult(command="", argv=[], exit_code=1, duration_ms=0, timed_out=False,
                         stdout="", stderr=message)


def _ensure_browsers(plan: Any, record: dict) -> set:
    """The e2e steps that install Playwright's browsers and are now covered by the shared cache.
    Never raises; an uncovered step runs in the sandbox as before."""
    covered: set = set()
    try:
        from . import playwright_cache
        from .verification_target import PLAYWRIGHT_INSTALL_CMD

        for command, cwd in plan.e2e_steps:
            if command != PLAYWRIGHT_INSTALL_CMD:
                continue
            got = playwright_cache.ensure(playwright_cache.project_version(cwd))
            record["browsers"] = {k: got.get(k) for k in ("ok", "cached", "seconds", "detail")}
            if got.get("ok"):
                covered.add((command, str(cwd)))
    except Exception as exc:  # noqa: BLE001
        record["browsers"] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"[:300]}
    return covered


def run_stack_plan(sandbox, plan: StackPlan, *, timeout: Optional[int] = None,
                   start_timeout: Optional[float] = None) -> tuple[list[Any], dict[str, Any]]:
    """Run the plan, stopping at the first failure. (results, record)."""
    from .verification_target import harden_web_package

    record: dict[str, Any] = {"services": [s.as_dict() for s in plan.services]}
    if plan.error:
        return [_failed(plan.error)], {**record, "error": plan.error}
    if plan.frontend is not None and plan.frontend.framework != "next":
        # The scaffold's promise for a web package (real vite and Playwright, no echo stubs),
        # kept for the front end exactly as for a single-package app.
        try:
            record["frontend_hardened"] = harden_web_package(plan.dir_of(plan.frontend)).get("changed")
        except Exception as exc:  # noqa: BLE001 — never fail a run on bookkeeping
            record["frontend_hardened_error"] = f"{type(exc).__name__}: {exc}"
    results: list[Any] = []
    for command, cwd in plan.steps:
        result = run_in(sandbox, command, cwd, timeout=timeout)
        results.append(result)
        if not getattr(result, "ok", False):
            return results, record
    if plan.backend is None:
        return results, record
    # The browsers come from BOSS's install-wide cache (playwright_cache), fetched BEFORE the
    # backend starts — a first download is minutes, and must not run against a live backend's
    # start timeout. A step it covered is not run in the sandbox at all.
    cached_steps = _ensure_browsers(plan, record)
    try:
        with backend_running(plan, timeout=start_timeout) as session:
            record.update(session.record)
            for command, cwd in plan.e2e_steps:
                if (command, str(cwd)) in cached_steps:
                    continue
                result = run_in(sandbox, command, cwd, timeout=timeout, env=session.env())
                results.append(result)
                if not getattr(result, "ok", False):
                    break
    except BackendDidNotStart as exc:
        record["backend"] = {"dir": plan.backend.rel, "command": (plan.backend_command or [""])[-1],
                             "ok": False, "error": str(exc)[:2000]}
        results.append(_failed(str(exc)))
    return results, record
