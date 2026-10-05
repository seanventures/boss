"""EVERY PROCESS BOSS STARTS IS OWNED, AND ENDS WITH EVERYTHING IT STARTED.

The one spawn helper (stability spec §4a.2). Before this, a killed or timed-out code agent left
its descendants running — the dev server an agent started with `npm run dev &`, a watcher,
Chromium — because every site killed ONE pid (`child.kill()`), and `subprocess.run(timeout=)`
kills the direct child only. Measured in the api image: three agents killed the way
code_agent.py killed them left 9 processes alive holding 166 MB, and after those exited, 9
zombies nobody reaped. Each spawn site had its own partial fix (sandbox.py killed the group on
timeout but not on exit; preview_service killed by a pid read back from the database).

So a process is never "a pid" here. It is:

* a PROCESS GROUP — `start_new_session=True`, always, so the leader's pgid is a handle to the
  tree, usable after the leader itself has exited;
* plus an OWNERSHIP TOKEN carried in the environment (`BOSS_OWNER_TOKENS`), inherited by every
  descendant — including one that calls setsid() and leaves the group, which a group kill alone
  would miss;
* plus an OWNER — the build, preview, seat or deploy it belongs to (`owner_scope`), so a cancel
  can end everything that build started and nothing else.

Ending — success, failure, timeout, idle, cancel — always means: SIGTERM to the group and every
token holder, a grace period, SIGKILL, then VERIFY that nothing with that pgid or token remains.
Ending is in `finally`; the ledger reaper (resource_reaper) is the backstop for the one ending
`finally` cannot cover, the parent itself being SIGKILLed.

`subprocess.Popen`/`run` are not called directly anywhere in `boss_engine/services` outside an
allowlist (engine/tests/test_no_bare_subprocess_in_services.py); this module is where they live.
"""
from __future__ import annotations

import contextlib
import contextvars
import io
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

_log = logging.getLogger("boss.owned_process")

#: The environment variable every owned child (and so every descendant) carries. A list, so a
#: process BOSS started inside another owned process is found by either owner's sweep.
TOKEN_ENV = "BOSS_OWNER_TOKENS"

#: SIGTERM, then this long for the group to leave on its own, then SIGKILL (spec §4a.2).
DEFAULT_GRACE_SECONDS = 5.0
#: After SIGKILL, how long the verify step waits for the kernel to finish the job.
_VERIFY_SECONDS = 3.0


# ------------------------------------------------------------------------------------------ #
# Owners
# ------------------------------------------------------------------------------------------ #

@dataclass(frozen=True)
class Owner:
    """Who a process belongs to. `chain` is every enclosing owner id, outermost first, so ending
    a build ends its review seats too."""

    kind: str                      # code_run | preview | sandbox | review_seat | deploy | api ...
    id: str
    project_id: Optional[str] = None
    chain: tuple[str, ...] = ()

    def ids(self) -> tuple[str, ...]:
        return (*self.chain, self.id)


_CURRENT: contextvars.ContextVar[Optional[Owner]] = contextvars.ContextVar(
    "boss_owned_process_owner", default=None)


def current_owner() -> Optional[Owner]:
    return _CURRENT.get()


@contextlib.contextmanager
def owner_scope(kind: str, owner_id: str, *, project_id: Optional[str] = None,
                fresh: bool = False) -> Iterator[Owner]:
    """Everything spawned inside this block belongs to (kind, owner_id), nested in whatever
    owner encloses it. Thread pools do not inherit context: submit with
    `contextvars.copy_context().run` (see governed_code_task_service's review panel)."""
    outer = _CURRENT.get()
    if fresh:
        # A new execution of this owner (a redelivered build): an old stop no longer refuses
        # its spawns. If the build is still meant to be stopped, its own stage checks say so.
        _CANCELLED.pop(str(owner_id), None)
    owner = Owner(kind=kind, id=str(owner_id),
                  project_id=project_id or (outer.project_id if outer else None),
                  chain=(outer.ids() if outer else ()))
    token = _CURRENT.set(owner)
    try:
        yield owner
    finally:
        _CURRENT.reset(token)


# ------------------------------------------------------------------------------------------ #
# Seeing processes: /proc on Linux (production), `ps` elsewhere (a developer's Mac)
# ------------------------------------------------------------------------------------------ #

@dataclass
class ProcInfo:
    pid: int
    ppid: int
    pgid: int
    state: str
    comm: str


def _proc_available() -> bool:
    return sys.platform.startswith("linux") and os.path.isdir("/proc/self")


def _read_proc_stat(pid: int) -> Optional[ProcInfo]:
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    # comm is parenthesised and may itself contain spaces or parentheses: split on the LAST ')'.
    lpar, rpar = raw.find("("), raw.rfind(")")
    if lpar < 0 or rpar < 0:
        return None
    rest = raw[rpar + 2:].split()
    try:
        return ProcInfo(pid=pid, ppid=int(rest[1]), pgid=int(rest[2]), state=rest[0],
                        comm=raw[lpar + 1:rpar])
    except (IndexError, ValueError):
        return None


def processes() -> list[ProcInfo]:
    """Every process this one can see."""
    out: list[ProcInfo] = []
    if _proc_available():
        for name in os.listdir("/proc"):
            if name.isdigit():
                info = _read_proc_stat(int(name))
                if info is not None:
                    out.append(info)
        return out
    try:
        listing = subprocess.run(  # noqa: S603,S607 — the census itself; nothing to own
            ["ps", "-axo", "pid=,ppid=,pgid=,stat=,comm="], capture_output=True, text=True,
            timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return out
    for line in listing.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        try:
            out.append(ProcInfo(pid=int(parts[0]), ppid=int(parts[1]), pgid=int(parts[2]),
                                state=parts[3][:1], comm=(parts[4] if len(parts) > 4 else "")))
        except ValueError:
            continue
    return out


def _environ_has_token(pid: int, token: str) -> bool:
    try:
        with open(f"/proc/{pid}/environ", "rb") as fh:
            env = fh.read()
    except OSError:
        return False
    needle = token.encode()
    for entry in env.split(b"\0"):
        if entry.startswith(TOKEN_ENV.encode() + b"=") and needle in entry:
            return True
    return False


def pids_with_token(token: str) -> set[int]:
    """Every live process whose environment carries `token` — the descendants that left the
    group (setsid, a daemonising dev server) as well as the ones that did not."""
    if not token:
        return set()
    found: set[int] = set()
    if _proc_available():
        for name in os.listdir("/proc"):
            if name.isdigit() and _environ_has_token(int(name), token):
                found.add(int(name))
        return found
    try:
        listing = subprocess.run(  # noqa: S603,S607
            ["ps", "-Eww", "-axo", "pid=,command="], capture_output=True, text=True,
            timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return found
    for line in listing.splitlines():
        if token in line:
            head = line.strip().split(None, 1)[0]
            if head.isdigit():
                found.add(int(head))
    return found


def _protected_pids() -> set[int]:
    """Never signal ourselves or our ancestors, whatever a stale handle says."""
    mine = {os.getpid(), 0, 1}
    with contextlib.suppress(OSError):
        mine.add(os.getppid())
    return mine


def members(pgid: Optional[int], token: Optional[str] = None,
            *, census: Optional[list[ProcInfo]] = None) -> dict[str, list[int]]:
    """What is left of an owned tree: live processes and zombies, by pgid or token."""
    if pgid is not None and pgid <= 1:
        pgid = None                                  # never "the init's group"
    census = census if census is not None else processes()
    tokened = pids_with_token(token) if token else set()
    protected = _protected_pids()
    hit = {p.pid for p in census if (pgid and p.pgid == pgid) or p.pid in tokened}
    # Descendants by parentage too: a child that called setsid() has left the group, and where
    # environments are unreadable (macOS) its parent link is what still ties it to the tree.
    children: dict[int, list[int]] = {}
    for p in census:
        children.setdefault(p.ppid, []).append(p.pid)
    frontier = list(hit)
    while frontier:
        for kid in children.get(frontier.pop(), ()):
            if kid not in hit:
                hit.add(kid)
                frontier.append(kid)
    alive: list[int] = []
    zombies: list[int] = []
    for p in census:
        if p.pid in protected or p.pid not in hit:
            continue
        (zombies if p.state.startswith("Z") else alive).append(p.pid)
    return {"alive": sorted(alive), "zombies": sorted(zombies)}


def zombie_count(census: Optional[list[ProcInfo]] = None) -> int:
    census = census if census is not None else processes()
    return sum(1 for p in census if p.state.startswith("Z"))


def _signal_tree(pgid: Optional[int], token: Optional[str], sig: int) -> None:
    protected = _protected_pids()
    # Everything found BEFORE the group signal: once the leader dies its children are
    # reparented and the parent link that finds a setsid() child is gone.
    found = members(pgid, token)["alive"]
    if pgid and pgid > 1 and pgid != os.getpgid(0):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pgid, sig)
    for pid in found:
        if pid in protected:
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, sig)


def terminate_tree(pgid: Optional[int], token: Optional[str] = None, *,
                   grace: float = DEFAULT_GRACE_SECONDS,
                   reap: Optional[Callable[[], Any]] = None) -> dict[str, Any]:
    """SIGTERM the group and every token holder; after `grace`, SIGKILL; then verify.

    `reap` is called while waiting, so a leader that is our own child is collected (otherwise it
    sits as OUR zombie and the verify step would count it). Returns what remained:
    `{"alive": [...], "zombies": [...], "killed": bool}`; `alive` empty is the guarantee."""
    if not pgid and not token:
        return {"alive": [], "zombies": [], "killed": False}
    left = members(pgid, token)
    if not left["alive"]:
        return {**left, "killed": False}
    if grace > 0:
        _signal_tree(pgid, token, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if reap:
                with contextlib.suppress(Exception):
                    reap()
            left = members(pgid, token)
            if not left["alive"]:
                return {**left, "killed": True}
            time.sleep(0.1)
    _signal_tree(pgid, token, signal.SIGKILL)
    deadline = time.monotonic() + _VERIFY_SECONDS
    while True:
        if reap:
            with contextlib.suppress(Exception):
                reap()
        left = members(pgid, token)
        if not left["alive"] or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    if left["alive"]:
        _log.error("owned tree pgid=%s still has %d process(es) after SIGKILL: %s",
                   pgid, len(left["alive"]), left["alive"][:10])
    return {**left, "killed": True}


# ------------------------------------------------------------------------------------------ #
# The ledger hook: owned_resources rows, when the API has turned it on
# ------------------------------------------------------------------------------------------ #

class _NullLedger:
    def open(self, **_kw: Any) -> Optional[str]:
        return None

    def release(self, _row_id: Optional[str], _reason: str) -> None:
        return None

    def set_handle(self, _row_id: Optional[str], **_kw: Any) -> None:
        return None


_LEDGER: Any = _NullLedger()


def set_ledger(ledger: Any) -> None:
    """resource_ledger.DbLedger in the API; the null ledger everywhere else (tests, CLIs)."""
    global _LEDGER
    _LEDGER = ledger or _NullLedger()


def ledger() -> Any:
    return _LEDGER


# ------------------------------------------------------------------------------------------ #
# The live registry: what this process started that is still running
# ------------------------------------------------------------------------------------------ #

_LIVE: dict[int, "OwnedPopen"] = {}
_LIVE_LOCK = threading.Lock()
#: Owner ids a cancel has ended, so a stage that dies of the kill is read as cancelled, not
#: failed (governed_code_task_service). Bounded: only ids, only recent.
_CANCELLED: dict[str, float] = {}


def live() -> list["OwnedPopen"]:
    with _LIVE_LOCK:
        return list(_LIVE.values())


def live_owner_ids(kind: Optional[str] = None) -> set[str]:
    """Every owner id (at any depth) that has a live process here, optionally of one kind."""
    out: set[str] = set()
    for proc in live():
        o = proc.owner
        if o is None:
            continue
        if kind is None:
            out.update(o.ids())
        elif o.kind == kind:
            out.add(o.id)
    return out


def terminate_owner(owner_id: str, *, reason: str = "cancel",
                    grace: float = DEFAULT_GRACE_SECONDS) -> dict[str, Any]:
    """End every live process that `owner_id` owns, at any depth. Returns the count ended and
    anything that survived (which the verify inside terminate_tree already logged)."""
    owner_id = str(owner_id)
    if reason == "cancel":
        _CANCELLED[owner_id] = time.time()
        for stale in [k for k, at in _CANCELLED.items() if time.time() - at > 6 * 3600]:
            _CANCELLED.pop(stale, None)
    ended, survivors = 0, []
    for proc in live():
        if proc.owner is not None and owner_id in proc.owner.ids():
            result = proc.end(reason=reason, grace=grace)
            ended += 1
            survivors.extend(result.get("alive") or [])
    return {"owner_id": owner_id, "ended": ended, "survivors": survivors}


def was_cancelled(owner_id: Optional[str]) -> bool:
    return bool(owner_id) and str(owner_id) in _CANCELLED


class OwnerCancelled(RuntimeError):
    """A spawn refused because its owner was stopped. Without this a stage's retry loop would
    start a fresh agent a second after Stop killed the last one."""


def terminate_all(*, reason: str = "shutdown", grace: float = DEFAULT_GRACE_SECONDS) -> int:
    """Graceful stop (spec §4a.5): end every owned tree this process holds."""
    procs = live()
    for proc in procs:
        with contextlib.suppress(Exception):
            _signal_tree(proc.pgid, proc.token, signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, grace)
    while time.monotonic() < deadline and any(p.poll() is None for p in procs):
        time.sleep(0.1)
    for proc in procs:
        with contextlib.suppress(Exception):
            proc.end(reason=reason, grace=0)
    return len(procs)


# ------------------------------------------------------------------------------------------ #
# Orphans: owned trees whose creator is gone (the parent was SIGKILLed)
# ------------------------------------------------------------------------------------------ #

def _tokens_of_env(raw: str) -> list[str]:
    return [t for t in raw.split(",") if t.startswith("own_")]


def token_census() -> dict[str, list[int]]:
    """{token: [pids carrying it]} for every process this one can see."""
    out: dict[str, list[int]] = {}
    if _proc_available():
        prefix = TOKEN_ENV.encode() + b"="
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/environ", "rb") as fh:
                    env = fh.read()
            except OSError:
                continue
            for entry in env.split(b"\0"):
                if entry.startswith(prefix):
                    for tok in _tokens_of_env(entry[len(prefix):].decode("ascii", "replace")):
                        out.setdefault(tok, []).append(int(name))
        return out
    try:
        listing = subprocess.run(  # noqa: S603,S607
            ["ps", "-Eww", "-axo", "pid=,command="], capture_output=True, text=True,
            timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return out
    marker = TOKEN_ENV + "="
    for line in listing.splitlines():
        at = line.find(marker)
        if at < 0:
            continue
        head = line.strip().split(None, 1)[0]
        if not head.isdigit():
            continue
        value = line[at + len(marker):].split(" ", 1)[0]
        for tok in _tokens_of_env(value):
            out.setdefault(tok, []).append(int(head))
    return out


def _creator_of(token: str) -> Optional[int]:
    parts = token.split("_")
    if len(parts) >= 3 and parts[1].isdigit():
        return int(parts[1])
    return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def orphan_trees(*, only: Optional[set[str]] = None) -> dict[str, list[int]]:
    """Owned trees nobody owns any more: their creator process is gone, or it is THIS process
    and the tree is no longer in its registry (ended without being collected). A tree whose
    creator is another live process (a CLI run in the same container) is not ours to judge."""
    mine = {p.token for p in live()}
    me = os.getpid()
    protected = _protected_pids()
    out: dict[str, list[int]] = {}
    for token, pids in token_census().items():
        if only is not None and token not in only:
            continue
        creator = _creator_of(token)
        if creator is None:
            continue
        if creator == me:
            if token in mine:
                continue
        elif _pid_alive(creator):
            continue
        pids = [p for p in pids if p not in protected]
        if pids:
            out[token] = sorted(set(pids))
    return out


def sweep_orphans(*, grace: float = 2.0, only: Optional[set[str]] = None) -> dict[str, Any]:
    """End every orphaned owned tree (the reaper's backstop for a parent SIGKILL). Scoped to
    BOSS's own tokens by construction: nothing without one is ever signalled."""
    orphans = orphan_trees(only=only)
    ended, survivors = 0, []
    for token in orphans:
        result = terminate_tree(None, token, grace=grace)
        ended += 1
        survivors.extend(result.get("alive") or [])
    return {"trees": ended, "processes": sum(len(v) for v in orphans.values()),
            "survivors": survivors}


def poll_all() -> int:
    """Collect every owned leader that has exited (and so end what it left behind). The
    reaper calls this: a preview's Popen nobody waits on would otherwise sit as a zombie."""
    n = 0
    for proc in live():
        with contextlib.suppress(Exception):
            if proc.poll() is not None:
                n += 1
    return n


# ------------------------------------------------------------------------------------------ #
# The spawn helper
# ------------------------------------------------------------------------------------------ #

def _child_env(env: Optional[dict[str, str]], token: str) -> dict[str, str]:
    base = dict(os.environ if env is None else env)
    inherited = [t for t in (base.get(TOKEN_ENV) or os.environ.get(TOKEN_ENV) or "").split(",")
                 if t]
    base[TOKEN_ENV] = ",".join([*inherited, token])
    return base


class OwnedPopen(subprocess.Popen):
    """`subprocess.Popen`, owned. Drop-in: `kill()`, `terminate()`, `wait()` and
    `communicate()` keep their meaning, applied to the whole tree.

    * always its own session (`start_new_session=True`), so `pgid == pid`;
    * `kill()` ends the tree now; `terminate()` with the grace period;
    * when the leader exits (any `wait`/`poll` that sees it), whatever it left behind in its
      group or carrying its token is ended too — unless `keep_tree_on_exit` (a daemonising
      launcher whose children are the point; previews never are);
    * `owner` defaults to the enclosing `owner_scope`; `resource` names the ledger kind.
    """

    def __init__(self, args: Any, *, owner: Optional[Owner] = None,
                 resource: str = "process_group", record: bool = True,
                 keep_tree_on_exit: bool = False, expires_at: Any = None,
                 **kwargs: Any) -> None:
        self.owner = owner or current_owner()
        if self.owner is not None:
            stopped = next((i for i in self.owner.ids() if i in _CANCELLED), None)
            if stopped:
                raise OwnerCancelled(f"{stopped} was stopped; not starting {_argv0(args)}")
        # The creator's pid rides in the token: the orphan sweep can tell a tree whose creator
        # is gone (an orphan) from one another live process in this container still owns.
        self.token = f"own_{os.getpid()}_{uuid.uuid4().hex}"
        self.resource = resource
        self.keep_tree_on_exit = keep_tree_on_exit
        self.end_reason: Optional[str] = None
        self.end_result: Optional[dict[str, Any]] = None
        self._ended = False
        self._end_lock = threading.Lock()
        kwargs["start_new_session"] = True
        kwargs["env"] = _child_env(kwargs.get("env"), self.token)
        # THE ROW BEFORE THE SPAWN (spec §4a.1): a parent killed between spawn and record would
        # otherwise leave a tree no ledger knows about.
        self.ledger_id = None
        if record:
            try:
                self.ledger_id = _LEDGER.open(
                    owner=self.owner, resource_kind=resource, handle=f"token:{self.token}",
                    expires_at=expires_at, meta={"argv0": str(_argv0(args))[:200]})
            except Exception:  # noqa: BLE001 — the ledger is a backstop, never a reason to fail
                _log.debug("ledger open failed", exc_info=True)
        try:
            super().__init__(args, **kwargs)
        except BaseException:
            with contextlib.suppress(Exception):
                _LEDGER.release(self.ledger_id, "spawn_failed")
            raise
        self.pgid = self.pid
        with contextlib.suppress(Exception):
            if self.ledger_id:
                _LEDGER.set_handle(self.ledger_id, pgid=self.pgid, token=self.token)
        with _LIVE_LOCK:
            _LIVE[self.pid] = self

    # -- ending --------------------------------------------------------------------------

    def end(self, *, reason: str = "exit", grace: float = DEFAULT_GRACE_SECONDS) -> dict[str, Any]:
        """End the tree (idempotent) and release the ledger row. Safe from any thread."""
        with self._end_lock:
            if self._ended and self.end_result is not None and not self.end_result.get("alive"):
                return self.end_result
            result = terminate_tree(self.pgid, self.token, grace=grace, reap=self._reap_leader)
            self._reap_leader()
            self.end_reason = self.end_reason or reason
            self.end_result = result
            self._ended = True
        with _LIVE_LOCK:
            _LIVE.pop(self.pid, None)
        with contextlib.suppress(Exception):
            _LEDGER.release(self.ledger_id, self.end_reason or reason)
        return result

    def _reap_leader(self) -> None:
        if self.returncode is None:
            with contextlib.suppress(Exception):
                super().poll()

    def kill(self) -> None:  # noqa: D401 — Popen's name
        self.end(reason="killed", grace=0)

    def terminate(self) -> None:
        self.end(reason="terminated", grace=DEFAULT_GRACE_SECONDS)

    def send_signal(self, sig: int) -> None:
        if sig in (signal.SIGKILL,):
            self.kill()
            return
        super().send_signal(sig)

    def _leader_exited(self) -> None:
        if self._ended:
            return
        if self.keep_tree_on_exit:
            with _LIVE_LOCK:
                _LIVE.pop(self.pid, None)
            return
        self.end(reason=f"exit:{self.returncode}", grace=2.0)

    def wait(self, timeout: Optional[float] = None) -> int:  # type: ignore[override]
        rc = super().wait(timeout=timeout)
        self._leader_exited()
        return rc

    def poll(self) -> Optional[int]:  # type: ignore[override]
        rc = super().poll()
        if rc is not None:
            self._leader_exited()
        return rc


def _argv0(args: Any) -> str:
    if isinstance(args, (list, tuple)) and args:
        # Skip the scheduling prefix (nice -n 10 ionice -c 3) to name the real command.
        items = [str(a) for a in args]
        while items and items[0] in ("nice", "ionice"):
            items = items[3:] if len(items) > 2 and items[1] in ("-n", "-c") else items[1:]
        return items[0] if items else str(args[0])
    return str(args).split(" ", 1)[0]


#: Bounded drain after a kill: a pipe held open by something that escaped costs this, not the
#: rest of the run (sandbox.py learned this the hard way; now everyone gets it).
_DRAIN_SECONDS = 10.0


def communicate(proc: OwnedPopen, input: Any = None,  # noqa: A002
                timeout: Optional[float] = None) -> tuple[Any, Any]:
    """`Popen.communicate`, except that the LEADER's exit ends the read.

    The stock communicate() reads until EOF, and EOF never comes while anything holds the
    pipe: `npm run dev &` inside a command, Playwright's webServer, a daemonised watcher. The
    command has finished and the caller hangs on its grandchild. Here the pipes are drained on
    threads while the leader is watched; when it exits, the tree is ended (which closes every
    write end) and the readers finish. On timeout the tree is killed and the drain is bounded.
    """
    chunks: dict[str, list[Any]] = {"out": [], "err": []}

    def _pump(stream: Any, key: str) -> None:
        try:
            while True:
                data = stream.read(65536) if hasattr(stream, "read") else None
                if not data:
                    break
                chunks[key].append(data)
        except (OSError, ValueError):
            pass
        finally:
            with contextlib.suppress(Exception):
                stream.close()

    readers = []
    for stream, key in ((proc.stdout, "out"), (proc.stderr, "err")):
        if stream is not None:
            t = threading.Thread(target=_pump, args=(stream, key), daemon=True,
                                 name=f"owned-{key}-{proc.pid}")
            t.start()
            readers.append(t)
    if proc.stdin is not None:
        def _feed() -> None:
            try:
                if input is not None:
                    proc.stdin.write(input)
            except (BrokenPipeError, OSError, ValueError):
                pass
            finally:
                with contextlib.suppress(Exception):
                    proc.stdin.close()
        threading.Thread(target=_feed, daemon=True, name=f"owned-in-{proc.pid}").start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)          # the leader's exit ends the tree (OwnedPopen.wait)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.end(reason="timeout", grace=0)
    for t in readers:
        t.join(timeout=_DRAIN_SECONDS)

    def _joined(key: str) -> Any:
        parts = chunks[key]
        if not parts:
            stream = proc.stdout if key == "out" else proc.stderr
            if stream is None:
                return None
            return "" if isinstance(stream, io.TextIOBase) else b""
        return parts[0][:0].join(parts)

    out, err = _joined("out"), _joined("err")
    if timed_out:
        raise subprocess.TimeoutExpired(proc.args, timeout or 0, output=out, stderr=err)
    return out, err


def popen(args: Any, **kwargs: Any) -> OwnedPopen:
    return OwnedPopen(args, **kwargs)


def run(args: Any, *, timeout: Optional[float] = None, input: Any = None,  # noqa: A002
        check: bool = False, capture_output: bool = False, owner: Optional[Owner] = None,
        resource: str = "process_group", record: bool = False,
        **kwargs: Any) -> subprocess.CompletedProcess:
    """`subprocess.run`, owned: on timeout the WHOLE tree is killed and the drain is bounded,
    and on any exit whatever the command left running is ended. Raises TimeoutExpired like
    run(). `record=False` by default: a run-to-completion call cannot outlive this frame, so
    the in-process registry is its owner; long-lived spawns use `popen`."""
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout and stderr arguments may not be used with capture_output.")
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    if input is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used.")
        kwargs["stdin"] = subprocess.PIPE
    proc = OwnedPopen(args, owner=owner, resource=resource, record=record, **kwargs)
    try:
        out, err = communicate(proc, input=input, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise subprocess.TimeoutExpired(args, timeout or 0, output=exc.output,
                                        stderr=exc.stderr) from None
    except BaseException:
        proc.end(reason="interrupted", grace=0)
        raise
    finally:
        if not proc._ended:
            proc.end(reason=f"exit:{proc.returncode}", grace=2.0)
    completed = subprocess.CompletedProcess(args, proc.returncode, out, err)
    if check:
        completed.check_returncode()
    return completed


__all__ = [
    "DEFAULT_GRACE_SECONDS", "Owner", "OwnedPopen", "ProcInfo", "TOKEN_ENV", "current_owner",
    "ledger", "live", "live_owner_ids", "members", "owner_scope", "pids_with_token", "popen",
    "processes", "run", "set_ledger", "terminate_all", "terminate_owner", "terminate_tree",
    "was_cancelled", "zombie_count", "OwnerCancelled", "orphan_trees", "poll_all",
    "sweep_orphans", "token_census",
]


# Re-exported so callers catch the same exceptions they always did.
TimeoutExpired = subprocess.TimeoutExpired
CompletedProcess = subprocess.CompletedProcess
PIPE = subprocess.PIPE
DEVNULL = subprocess.DEVNULL
STDOUT = subprocess.STDOUT
