"""THE DISPATCH SEAM: a coding-agent turn runs in this process, or on the boss-runner sidecar.

BOSSAPP-828. Every coding CLI used to be a child of the API process (owned_process). An upgrade
SIGTERMed the API, the drain ended every tree, and the next API re-ran the stage from the last git
checkpoint: every in-flight agent turn was paid for twice.

`run()` is `owned_process.run()` for a harness turn:

* BOSS_RUNNER_URL unset (the default) — EXACTLY `owned_process.run(args, **kwargs)`, the call the
  harness made before this seam existed. Nothing else happens.
* BOSS_RUNNER_URL set, and the turn belongs to a code-task run with a live identity run — the
  turn is handed to the runner (apps/runner), which owns the process. This process only waits.

HAND-OFF. The runner's public DPoP key comes from `GET /v1/runner/identity`; the run's shim mints
a run token bound to it (identity/runs.runner_token — a sibling of the run's own token); the
start command carries that token, the argv/env/cwd the workload would have run with here
(owned_process.workload_spawn: the run's sandbox uid, its build services), the stdin, the
timeout, and a SESSION KEY: the owner path + a hash of the turn (prompt, harness, model, mode).

RE-ATTACH. While BOSS drains for an update, a waiting turn does NOT end its process: it raises
`RunnerDetached` (the stage's drain handler hands the job back without spending an attempt) and
keeps its sandbox directory from teardown. The next API runs the stage again; the stage reaches
the same turn with the same session key, and the runner — which still holds that turn, running or
finished-but-not-taken — answers `adopted` instead of spawning. The wait resumes; the result is
the original turn's. If the stage was re-provisioned into a new sandbox directory, the turn's
working tree is carried over from the old one (`carry_tree`) before the result is returned.

Any failure to REACH the runner before the hand-off falls back to the in-process spawn (logged):
a sidecar that is down must never stop a build.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from . import owned_process

_log = logging.getLogger("boss.runner_dispatch")

RUNNER_URL_ENV = "BOSS_RUNNER_URL"
POLL_SECONDS = 1.0
#: The runner-bound token lives 5 minutes; the waiter mints a fresh one before that.
TOKEN_REFRESH_SECONDS = 200
#: How long the waiter keeps trying a runner it cannot reach before the turn counts as lost.
UNREACHABLE_GRACE_SECONDS = 120


class RunnerUnavailable(RuntimeError):
    """The runner could not take the turn (before any hand-off): run it here instead."""


class RunnerDetached(RuntimeError):
    """BOSS is draining: the turn keeps running on boss-runner and is re-attached when the build
    resumes. Raised so the stage hands its job back (drain.hand_back_interrupted)."""


class RunnerLost(RuntimeError):
    """The runner no longer has the turn (it restarted, or became unreachable for good)."""


def runner_url() -> Optional[str]:
    return (os.environ.get(RUNNER_URL_ENV) or "").strip().rstrip("/") or None


# ------------------------------------------------------------------------------------------ #
# The seam
# ------------------------------------------------------------------------------------------ #

def run(args: Any, *, session_key: Optional[str] = None, collect_files: Optional[list[str]] = None,
        **kwargs: Any) -> subprocess.CompletedProcess:
    """`owned_process.run` for a coding-agent turn. See the module docstring."""
    url = runner_url()
    if not url or session_key is None:
        return owned_process.run(args, **kwargs)
    try:
        handoff = _handoff(url)
    except RunnerUnavailable as exc:
        _log.warning("boss-runner unavailable (%s); running this turn in the API process", exc)
        return owned_process.run(args, **kwargs)
    if handoff is None:
        return owned_process.run(args, **kwargs)
    return run_on_runner(handoff, args, session_key=session_key, collect_files=collect_files or [], **kwargs)


class Handoff:
    """Everything a turn needs to go to the runner: the client, the run, a way to mint tokens."""

    def __init__(self, client: "RunnerClient", *, owner: owned_process.Owner, task_run_id: str,
                 identity_run_id: str, mint: Callable[[], Optional[str]], api_base: str = "",
                 engine: Any = None):
        self.client = client
        self.owner = owner
        self.task_run_id = task_run_id
        self.identity_run_id = identity_run_id
        self.mint = mint
        self.api_base = api_base
        self.engine = engine


#: Tests replace this to hand off without a live identity stack (a fake issuer).
HANDOFF_FACTORY: Optional[Callable[[str], Optional[Handoff]]] = None


def _handoff(url: str) -> Optional[Handoff]:
    if HANDOFF_FACTORY is not None:
        return HANDOFF_FACTORY(url)
    owner = owned_process.current_owner()
    if owner is None:
        return None
    task_run_id = owner.ids()[0]
    from ..identity import runs

    shim = runs.shim_for_task_run(task_run_id)
    engine = getattr(runs, "_ENGINE", None)
    if shim is None or engine is None:
        # No identity run for this build (shadow mode, a legacy path): the runner could not report
        # as the run, so the turn stays here.
        _log.info("no live identity run for %s; its turn stays in the API process", task_run_id)
        return None
    client = RunnerClient(url)
    jkt = client.identity()["jkt"]

    def mint() -> Optional[str]:
        return runs.runner_token(shim, runner_jkt=jkt, engine=engine)

    api_base = ""
    with contextlib.suppress(Exception):
        from ..identity.authn_support import public_origin

        with engine.begin() as db:
            api_base = public_origin(db) or ""
    return Handoff(client, owner=owner, task_run_id=task_run_id, identity_run_id=shim.run_id, mint=mint,
                   api_base=api_base, engine=engine)


def run_on_runner(h: Handoff, args: Any, *, session_key: str, collect_files: list[str],
                  cwd: Any = None, input: Any = None, capture_output: bool = False,  # noqa: A002
                  text: Optional[bool] = None, timeout: Optional[float] = None,
                  env: Optional[dict[str, str]] = None, **rest: Any) -> subprocess.CompletedProcess:
    original_args = args
    spawn = {"env": env, "cwd": str(cwd) if cwd is not None else None}
    if rest.get("shell"):
        spawn["shell"] = True
    _sandbox, argv = owned_process.workload_spawn(list(args), h.owner, spawn)
    token = h.mint()
    if not token:
        _log.info("no runner token for %s; its turn stays in the API process", h.task_run_id)
        return owned_process.run(original_args, cwd=cwd, input=input, capture_output=capture_output,
                                 text=text, timeout=timeout, env=env, **rest)
    key = "/".join(h.owner.ids()) + "#" + session_key
    local_cwd = str(spawn.get("cwd") or cwd or "")
    body = {"kind": "start", "command_id": f"cmd_{uuid.uuid4().hex}", "run_id": h.identity_run_id,
            "task_run_id": h.task_run_id, "session_key": key, "argv": [str(a) for a in argv],
            "cwd": local_cwd, "env": spawn.get("env"), "timeout": timeout, "collect": list(collect_files),
            "api_base": h.api_base,
            "input": (input.decode("utf-8", errors="replace") if isinstance(input, bytes) else input)}
    try:
        started = h.client.command(body, token)
    except RunnerUnavailable as exc:
        _log.warning("boss-runner refused the turn (%s); running it in the API process", exc)
        return owned_process.run(original_args, cwd=cwd, input=input, capture_output=capture_output,
                                 text=text, timeout=timeout, env=env, **rest)
    sid = str(started["session_id"])
    if started.get("adopted"):
        _log.warning("re-attached to %s's turn %s on boss-runner (it kept running across the restart)",
                     h.task_run_id, sid)
    _record(h, "open", session_id=sid, session_key=key)
    result = _wait(h, sid, token, original_args, local_cwd=local_cwd)
    if result.get("timed_out"):
        _close(h, sid, token)
        raise subprocess.TimeoutExpired(original_args, timeout or 0, output=result.get("stdout"),
                                        stderr=result.get("stderr"))
    for path, content in zip(collect_files, result.get("collected") or []):
        if content is not None:
            with contextlib.suppress(OSError):
                Path(path).write_text(content, encoding="utf-8")
    remote_cwd = str(result.get("cwd") or "")
    if started.get("adopted") and remote_cwd and local_cwd and os.path.realpath(remote_cwd) != os.path.realpath(local_cwd):
        carried = carry_tree(remote_cwd, local_cwd)
        _log.warning("carried %s's turn output from %s into the resumed sandbox %s (%s)",
                     h.task_run_id, remote_cwd, local_cwd, carried)
    _close(h, sid, token)
    stdout, stderr = result.get("stdout"), result.get("stderr")
    if not text and not rest.get("universal_newlines") and not rest.get("encoding"):
        stdout = stdout.encode("utf-8") if isinstance(stdout, str) else stdout
        stderr = stderr.encode("utf-8") if isinstance(stderr, str) else stderr
    if not capture_output and rest.get("stdout") is None:
        stdout = None
    if not capture_output and rest.get("stderr") is None:
        stderr = None
    code = result.get("exit_code")
    return subprocess.CompletedProcess(original_args, int(code) if code is not None else -1, stdout, stderr)


def _wait(h: Handoff, sid: str, token: str, args: Any, *, local_cwd: str) -> dict[str, Any]:
    minted_at = time.monotonic()
    cancel_sent = False
    unreachable_since: Optional[float] = None
    while True:
        if owned_process.draining():
            # A DEPLOY IS NOT A FAILURE, AND NOW NOT A KILL EITHER: the turn keeps running on the
            # runner. The sandbox directory it works in must outlive this process's teardown.
            retain(local_cwd)
            raise RunnerDetached(f"BOSS is restarting; turn {sid} keeps running on boss-runner and is "
                                 "re-attached when the build resumes")
        if not cancel_sent and any(owned_process.was_cancelled(i) for i in h.owner.ids()):
            with contextlib.suppress(RunnerUnavailable):
                h.client.command({"kind": "cancel", "command_id": f"cmd_{uuid.uuid4().hex}", "session_id": sid},
                                 token)
                cancel_sent = True
        if time.monotonic() - minted_at > TOKEN_REFRESH_SECONDS:
            fresh = h.mint()
            if fresh:
                token, minted_at = fresh, time.monotonic()
        try:
            status = h.client.status(sid, token)
            unreachable_since = None
        except RunnerUnavailable as exc:
            unreachable_since = unreachable_since or time.monotonic()
            if time.monotonic() - unreachable_since > UNREACHABLE_GRACE_SECONDS:
                raise RunnerLost(f"boss-runner unreachable for {UNREACHABLE_GRACE_SECONDS}s: {exc}") from exc
            time.sleep(POLL_SECONDS)
            continue
        if status.get("state") == "lost":
            raise RunnerLost(f"boss-runner lost turn {sid} (the runner restarted mid-turn)")
        if status.get("state") not in ("starting", "running"):
            return h.client.result(sid, token)
        time.sleep(POLL_SECONDS)


def _close(h: Handoff, sid: str, token: str) -> None:
    with contextlib.suppress(RunnerUnavailable, RunnerLost):
        h.client.command({"kind": "close", "command_id": f"cmd_{uuid.uuid4().hex}", "session_id": sid}, token)
    _record(h, "close", session_id=sid)


def _record(h: Handoff, what: str, **kw: Any) -> None:
    """The registry row the reclaim path reads. Best effort: the runner's own pushes keep it."""
    if h.engine is None:
        return
    try:
        from . import runner_registry

        with h.engine.begin() as c:
            if what == "open":
                runner_registry.open_session(c, session_id=kw["session_id"], run_id=h.task_run_id,
                                             identity_run_id=h.identity_run_id, runner_id=h.client.runner_id or "",
                                             session_key=kw["session_key"])
            else:
                runner_registry.close_session(c, kw["session_id"])
    except Exception:  # noqa: BLE001 — the registry is advisory; the runner is the truth
        _log.debug("runner registry %s failed", what, exc_info=True)


def turn_key(*parts: Any) -> str:
    """The stable identity of a turn: what makes a re-run of the stage the SAME turn."""
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:32]


# ------------------------------------------------------------------------------------------ #
# Sandbox directories a detached turn still works in
# ------------------------------------------------------------------------------------------ #

_RETAINED: set[str] = set()
_RETAINED_LOCK = threading.Lock()


def retain(path: Optional[str]) -> None:
    if path:
        with _RETAINED_LOCK:
            _RETAINED.add(os.path.realpath(path))


def retained(path: Any) -> bool:
    """Is `path` a sandbox a runner-owned turn still works in? (EphemeralSandbox.teardown keeps it.)"""
    if not path or not _RETAINED:
        return False
    with _RETAINED_LOCK:
        return os.path.realpath(str(path)) in _RETAINED


def _git_files(root: str) -> Optional[set[str]]:
    try:
        proc = owned_process.boss_run(["git", "-c", "safe.directory=*", "-C", root, "ls-files", "-co",
                                       "--exclude-standard", "-z"], capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return {p for p in proc.stdout.decode("utf-8", errors="surrogateescape").split("\0") if p}


def carry_tree(src: str, dst: str) -> dict[str, int]:
    """Make `dst`'s working tree what the turn left in `src` (both checkouts of the same branch):
    every tracked or untracked-not-ignored file of `src` copied over, every such file of `dst`
    that `src` no longer has removed. `.git` is never touched. Then `src` goes."""
    out = {"copied": 0, "removed": 0}
    if not os.path.isdir(src) or not os.path.isdir(dst):
        return out
    have, want = _git_files(src), _git_files(dst)
    if have is None:
        return out
    # `ls-files -c` also names tracked files the turn deleted: those are removals, not copies.
    present = {rel for rel in have if os.path.lexists(os.path.join(src, rel)) and rel.split("/", 1)[0] != ".git"}
    for rel in sorted(present):
        s, d = os.path.join(src, rel), os.path.join(dst, rel)
        os.makedirs(os.path.dirname(d) or dst, exist_ok=True)
        with contextlib.suppress(OSError):
            if os.path.islink(s):
                if os.path.lexists(d):
                    os.unlink(d)
                os.symlink(os.readlink(s), d)
            else:
                shutil.copy2(s, d)
            out["copied"] += 1
    for rel in sorted(((want or set()) | have) - present):
        if rel.split("/", 1)[0] == ".git" or not os.path.lexists(os.path.join(dst, rel)):
            continue
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(dst, rel))
            out["removed"] += 1
    with _RETAINED_LOCK:
        _RETAINED.discard(os.path.realpath(src))
    shutil.rmtree(src, ignore_errors=True)
    with contextlib.suppress(Exception):
        from .resource_ledger import release_dir

        release_dir(Path(src), "runner_carried")
    return out


# ------------------------------------------------------------------------------------------ #
# The runner's HTTP API, as the API calls it
# ------------------------------------------------------------------------------------------ #

class RunnerClient:
    def __init__(self, url: str, *, timeout: float = 30.0):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.runner_id: Optional[str] = None

    def _call(self, method: str, path: str, *, token: Optional[str] = None,
              body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        data = None
        if token:
            headers["Authorization"] = f"DPoP {token}"
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 — BOSS_RUNNER_URL
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read() or b"{}").get("error")
            except ValueError:
                detail = None
            if exc.code == 404 and path.startswith("/v1/runner/sessions/"):
                raise RunnerLost(f"boss-runner does not have that turn ({detail})") from exc
            raise RunnerUnavailable(f"{exc.code} {detail or ''}".strip()) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise RunnerUnavailable(f"{type(exc).__name__}: {exc}") from exc

    def identity(self) -> dict[str, Any]:
        out = self._call("GET", "/v1/runner/identity")
        if not out.get("jkt"):
            raise RunnerUnavailable("runner identity has no key")
        self.runner_id = out.get("runner_id")
        return out

    def command(self, body: dict[str, Any], token: str) -> dict[str, Any]:
        return self._call("POST", "/v1/runner/commands", token=token, body=body)

    def status(self, session_id: str, token: str) -> dict[str, Any]:
        return self._call("GET", "/v1/runner/sessions/" + urllib.parse.quote(session_id, safe=""), token=token)

    def result(self, session_id: str, token: str) -> dict[str, Any]:
        return self._call("GET", "/v1/runner/sessions/" + urllib.parse.quote(session_id, safe="") + "/result",
                          token=token)
