"""THE SANDBOX BOUNDARY: a code task's agent runs as its own unprivileged uid (IDENTITY.md §7.6).

Before this, the "sandbox" was a temp dir inside the API/worker container, run by the same uid as
the API (`boss`). Same uid means the agent could read /proc/<api>/environ (DATABASE_URL, the
GitHub token, the KMS and identity keys), the identity key volume, every other run's directory,
shim socket and mcp.json — a deny-list of environment variables is not a boundary.

Now, per code-task run:

* a uid from the pool [61000, 62000) is leased (an flock held for the run's life, so two
  processes in one container never share one), and every process the run starts — the coding
  CLI, its tools, verify commands, review seats — is spawned through `boss-sandbox-exec run <uid>`
  (scripts/sandbox/boss-sandbox-exec.c): new session, that uid, no groups, no_new_privs;
* the run gets a directory `<RUN_ROOT>/<key>` owned <uid>:boss 2770: its workspaces, HOME, TMPDIR
  and the agent's mcp.json live there, so only that run and BOSS can enter it;
* its shim socket (identity.runs) is owned by <uid>, 0600, in a boss-owned 0711 directory, and
  the shim checks SO_PEERCRED: only that uid (or BOSS itself) gets the egress credential;
* the network (scripts/sandbox/entrypoint.sh) lets the pool reach loopback, the API and
  boss-egress only; model and package traffic goes through egress's allowlisted CONNECT proxy;
* at run end every process of the uid is killed and the directory removed.

`BOSS_SANDBOX_ISOLATION`:
  required  — a build with no helper or no free uid FAILS (the container default);
  auto      — isolate when the helper is installed, otherwise run unisolated and say so loudly
              (a developer's Mac: there is no setuid helper and no iptables);
  off       — never isolate (posture reports it).
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import shutil
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

log = logging.getLogger("boss.sandbox_identity")

MODE_ENV = "BOSS_SANDBOX_ISOLATION"
HELPER_ENV = "BOSS_SANDBOX_HELPER"
DEFAULT_HELPER = "/usr/local/libexec/boss/boss-sandbox-exec"
RUN_ROOT_ENV = "BOSS_SANDBOX_RUN_ROOT"
DEFAULT_RUN_ROOT = "/var/lib/boss/sandboxes"          # compiled into the helper
SHIM_ROOT = "/run/boss-shim"                          # compiled into the helper
UID_BASE = 61000
UID_COUNT = 1000
#: THE SEALED SUB-POOL (the Acceptance Agent's sandbox, BOSSAPP-1219): the top SEALED_COUNT uids of
#: the pool. scripts/sandbox/entrypoint.sh gives them their own chain, ahead of the pool's, that
#: allows LOOPBACK ONLY — no BOSS API on the compose network, no boss-egress, no CONNECT proxy, no
#: Docker DNS — and writes SEALED_MARKER once the rules are in. An ordinary run never leases one.
SEALED_COUNT = 100
SEALED_BASE = UID_BASE + UID_COUNT - SEALED_COUNT
SEALED_MARKER = ".network-sealed"
#: What a sandboxed process gets for its proxies: boss-egress's CONNECT proxy (the model gateway
#: and package route). Loopback and the API stay direct.
EGRESS_CONNECT_ENV = "BOSS_EGRESS_CONNECT_PROXY"


class SandboxUnavailable(RuntimeError):
    """Isolation is required and cannot be provided. The build fails; it never runs unisolated."""


def mode() -> str:
    raw = (os.environ.get(MODE_ENV) or "auto").strip().lower()
    return raw if raw in ("required", "auto", "off") else "required"


def helper_path() -> Optional[str]:
    path = os.environ.get(HELPER_ENV) or DEFAULT_HELPER
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else None


def run_root() -> Path:
    return Path(os.environ.get(RUN_ROOT_ENV) or DEFAULT_RUN_ROOT)


NETWORK_ENV = "BOSS_SANDBOX_NETWORK"
NETWORK_MARKER = ".network-fenced"            # written by the entrypoint after the iptables rules


def network_fenced() -> bool:
    """Did the entrypoint fence the sandbox uids' network? `BOSS_SANDBOX_NETWORK=off` is the
    operator saying they accept an unfenced network (posture reports it)."""
    if (os.environ.get(NETWORK_ENV) or "enforce").strip().lower() == "off":
        return True
    marker = os.path.join(SHIM_ROOT, NETWORK_MARKER)
    try:
        st = os.stat(marker)
    except OSError:
        return False
    return st.st_uid == 0                         # root wrote it, after the rules went in


def network_sealed() -> bool:
    """Did the entrypoint install the sealed (loopback-only) chain for SEALED_BASE.. uids? A
    root-owned marker, written after the rules were read back. Never True with the network
    fence switched off."""
    if (os.environ.get(NETWORK_ENV) or "enforce").strip().lower() == "off":
        return False
    try:
        st = os.stat(os.path.join(SHIM_ROOT, SEALED_MARKER))
    except OSError:
        return False
    return st.st_uid == 0


NETWORK_PROBLEM_MARKER = ".network-unfenced"   # the entrypoint could not fence: why (one line)


def network_problem() -> Optional[str]:
    """Why the entrypoint could not fence the network, when it started without it
    (BOSS_SANDBOX_NETWORK=enforce on a host without iptables owner match). None otherwise."""
    try:
        with open(os.path.join(SHIM_ROOT, NETWORK_PROBLEM_MARKER), encoding="utf-8") as fh:
            return fh.read(300).strip() or "the sandbox network rules could not be installed"
    except OSError:
        return None


def unavailable_reason() -> str:
    """What a held build says: the one thing to fix, in plain words."""
    problem = network_problem()
    if problem and not network_fenced():
        return ("this host cannot fence the build sandbox's network (" + problem + "). Builds are "
                "held until the host is fixed, or until you set BOSS_SANDBOX_NETWORK=off in .env "
                "and restart (builds then reach this container's networks). See "
                "docs/community/SANDBOX.md.")
    return (f"sandbox isolation is required but boss-sandbox-exec, {run_root()}, {SHIM_ROOT} or "
            "the network fence is missing")


def available() -> bool:
    return (helper_path() is not None and run_root().is_dir() and os.path.isdir(SHIM_ROOT)
            and network_fenced())


def status() -> dict[str, Any]:
    """For posture and the system doctor."""
    return {"mode": mode(), "helper": bool(helper_path()), "network_fenced": network_fenced(),
            "network_problem": network_problem(),
            "isolating": mode() != "off" and available()}


@dataclass
class SandboxIdentity:
    """One run's uid and directory. `repr` never shows anything sensitive (there is nothing)."""
    key: str
    uid: int
    run_dir: Path
    _lock_fd: Optional[int] = field(default=None, repr=False)
    #: A workload that must hold no model credential (an authored skill's command, BOSSAPP-1138):
    #: no CLI login was seeded into its HOME, and env() withholds every model-shaped name.
    credential_free: bool = False
    #: A uid of the SEALED sub-pool (loopback only): env() gives it no proxy, and git in it reads
    #: no system/global config and has no credential helper.
    sealed: bool = False
    #: An fd on `sessions_dir`, held for the run: session homes are made through it, so a rename
    #: of the name by the run's uid (it owns run_dir) changes nothing BOSS does.
    _sessions_fd: Optional[int] = field(default=None, repr=False)

    @property
    def sessions_dir(self) -> Path:
        """Where a session's CLI home is made (subscription_shield): BOSS-owned, 0711 — the run's
        uid can traverse it but never create, rename or replace anything in it."""
        return self.run_dir / ".boss-sessions"

    def sessions_fd(self) -> int:
        """Open (creating it, at acquire, before agent code) `sessions_dir`, refusing anything
        but a real directory BOSS owns that no one else can write."""
        if self._sessions_fd is not None:
            return self._sessions_fd
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        run_fd = os.open(str(self.run_dir), flags)
        try:
            with contextlib.suppress(FileExistsError):
                os.mkdir(self.sessions_dir.name, 0o711, dir_fd=run_fd)
            fd = os.open(self.sessions_dir.name, flags, dir_fd=run_fd)
        finally:
            os.close(run_fd)
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
            os.close(fd)
            raise SandboxUnavailable("the run's session directory is not BOSS's own")
        os.fchmod(fd, 0o711)
        self._sessions_fd = fd
        return fd

    @property
    def home(self) -> Path:
        return self.run_dir / "home"

    @property
    def tmp(self) -> Path:
        return self.run_dir / "tmp"

    def _helper(self, *args: str, check: bool = True) -> Any:
        helper = helper_path()
        if helper is None:
            raise SandboxUnavailable("boss-sandbox-exec is not installed")
        from . import owned_process

        proc = owned_process.run([helper, *args], capture_output=True, text=True, timeout=30,
                                 sandboxed=False)
        if check and proc.returncode != 0:
            raise SandboxUnavailable(f"boss-sandbox-exec {args[0]} failed: {(proc.stderr or '').strip()[:200]}")
        return proc

    # ---- spawning -------------------------------------------------------------------------------
    def wrap(self, args: Any, *, shell: bool = False) -> list[str]:
        helper = helper_path()
        if helper is None:
            raise SandboxUnavailable("boss-sandbox-exec is not installed")
        if shell or isinstance(args, str):
            argv = ["/bin/sh", "-c", args] if isinstance(args, str) else ["/bin/sh", "-c", *args]
        else:
            argv = [str(a) for a in args]
        return [helper, "run", str(self.uid), "--", *argv]

    def env(self, env: Optional[Mapping[str, str]]) -> dict[str, str]:
        """The sandboxed process's environment: its own HOME/TMPDIR/config dirs inside the run
        directory, the egress CONNECT proxy for everything that is not BOSS. A credential-shaped
        variable is never added here."""
        from .agent_env import NEVER, agent_environment

        # Deny by default (round 2): a caller that passes no env gets the agent allow-list, never
        # the control plane's own environment (DATABASE_URL, provider keys, the master-key path);
        # an env a caller computed still loses every NEVER name.
        never = {n.upper() for n in NEVER} | {"BOSS_MASTER_KEY", "BOSS_IDENTITY_SERVER_KEY",
                                              "BOSS_EGRESS_SERVICE_KEY", "BOSS_KMS_MASTER_KEY"}
        out = {k: v for k, v in (agent_environment(os.environ) if env is None else dict(env)).items()
               if k.upper() not in never}
        home, tmp = str(self.home), str(self.tmp)
        out.update({"HOME": home, "TMPDIR": tmp, "TMP": tmp, "TEMP": tmp, "USER": f"sbx{self.uid}",
                    "LOGNAME": f"sbx{self.uid}", "XDG_CONFIG_HOME": f"{home}/.config",
                    "XDG_CACHE_HOME": f"{home}/.cache", "CLAUDE_CONFIG_DIR": f"{home}/.claude",
                    "npm_config_cache": f"{home}/.npm", "BOSS_SANDBOX_UID": str(self.uid)})
        # Opt-in toolchains' own settings (Go: its module proxy and checksum database through
        # boss-egress, its caches under this HOME so they persist per project).
        try:
            from . import toolchains

            out.update(toolchains.sandbox_env(home))
        except Exception as exc:  # noqa: BLE001 — the image's go.env still pins proxy and sumdb
            log.warning("toolchain environment for sandbox %s not set: %s", self.key, exc)
        for name in ("CODEX_HOME", "GROK_HOME"):
            # A session home BOSS made inside this run (subscription_shield) is kept; any other
            # (the control plane's own, 0700 to boss) becomes the run's.
            if name in out and not self._inside(out[name]):
                out[name] = f"{home}/.{name.split('_')[0].lower()}"
        # NO RAW SUBSCRIPTION LOGIN CROSSES INTO THE RUN (BOSSAPP-1480): a session's login is a
        # model-proxy run token by now (subscription_shield); a raw value under a login's name —
        # inherited from the control plane's environment by a step that passed none — is dropped.
        from .model_proxy import holds_subscription_login

        for name in holds_subscription_login(out):
            out.pop(name, None)
        # The checkout is BOSS's (it cloned it); the run's uid works in it. Git refuses a repo
        # another uid owns unless told it is safe — and it is, inside this run's directory.
        if self.credential_free:
            from .local_fenced import strip_model_env

            out = strip_model_env(out)
        n = int(out.get("GIT_CONFIG_COUNT") or 0)
        out[f"GIT_CONFIG_KEY_{n}"] = "safe.directory"
        out[f"GIT_CONFIG_VALUE_{n}"] = "*"
        out["GIT_CONFIG_COUNT"] = str(n + 1)
        if self.sealed:
            # LOOPBACK ONLY: no route out, so no proxy to name; and git with no system or global
            # config, no credential helper and no prompt — nothing that could authenticate a push.
            for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY",
                         "all_proxy", EGRESS_CONNECT_ENV):
                out.pop(name, None)
            n = int(out.get("GIT_CONFIG_COUNT") or 0)
            out[f"GIT_CONFIG_KEY_{n}"] = "credential.helper"
            out[f"GIT_CONFIG_VALUE_{n}"] = ""
            out["GIT_CONFIG_COUNT"] = str(n + 1)
            out.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                        "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false",
                        "SSH_ASKPASS": "/bin/false", "NO_PROXY": "*", "no_proxy": "*"})
            out.pop("SSH_AUTH_SOCK", None)
            # Tool caches in the session's own TMPDIR, never its HOME: a write outside the clone
            # and TMPDIR taints an acceptance session (acceptance/session.py), and a package
            # cache filling HOME is not a reason to.
            for name in ("XDG_CACHE_HOME", "npm_config_cache", "GOCACHE", "GOMODCACHE",
                         "GOPATH", "GRADLE_USER_HOME", "CARGO_HOME", "PIP_CACHE_DIR"):
                value = out.get(name)
                if value and str(value).startswith(home):
                    out[name] = tmp + str(value)[len(home):]
            out.setdefault("PIP_CACHE_DIR", f"{tmp}/.cache/pip")
            out.setdefault("MAVEN_OPTS", f"-Dmaven.repo.local={tmp}/.m2")
            return out
        proxy = (os.environ.get(EGRESS_CONNECT_ENV) or "").strip()
        if proxy:
            for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
                out[name] = proxy
            no_proxy = "localhost,127.0.0.1,::1,api,egress"
            out["NO_PROXY"] = out["no_proxy"] = no_proxy
        return out

    def _inside(self, path: Any) -> bool:
        try:
            Path(str(path)).resolve().relative_to(self.run_dir.resolve())
            return True
        except (ValueError, OSError):
            return False

    def cwd(self, cwd: Any) -> Optional[str]:
        """A sandboxed process can only stand inside its own run directory; a cwd outside it (a
        scratch dir of the control plane) becomes the run's HOME."""
        if cwd is None:
            return str(self.home)
        try:
            Path(cwd).resolve().relative_to(self.run_dir.resolve())
            return str(cwd)
        except (ValueError, OSError):
            return str(self.home)

    def adopt_socket(self, path: str) -> None:
        self._helper("sock", str(self.uid), path)

    def killall(self) -> None:
        with contextlib.suppress(Exception):
            self._helper("kill", str(self.uid), check=False)

    def workspace(self, prefix: str) -> Path:
        """A fresh directory for one sandbox (a stage's clone) inside the run directory, 2777:
        only this run and BOSS can reach it (the run directory is 2770), and the run's uid must
        be able to edit what BOSS clones into it (the clone's own git runs with umask 0 —
        a per-spawn `umask` (Popen's), never the process-wide one)."""
        import tempfile

        path = Path(tempfile.mkdtemp(prefix=prefix, dir=str(self.run_dir)))
        os.chmod(path, 0o2777)          # setgid: what the run's uid creates stays group boss
        return path


_ACTIVE: dict[str, SandboxIdentity] = {}
_ACTIVE_LOCK = threading.Lock()


def _lease_uid(key: str, *, sealed: bool = False) -> tuple[int, int]:
    """An flock on SHIM_ROOT/uid-<n>.lock, held for the run's life: unique across every process
    in this container. Starts at a hash of the key so restarts of one run tend to the same uid.
    An ordinary lease comes from below SEALED_BASE; a sealed one only from the sealed sub-pool."""
    base, count = ((SEALED_BASE, SEALED_COUNT) if sealed
                   else (UID_BASE, UID_COUNT - SEALED_COUNT))
    start = int(hashlib.sha256(key.encode()).hexdigest(), 16) % count
    for i in range(count):
        uid = base + (start + i) % count
        fd = os.open(os.path.join(SHIM_ROOT, f"uid-{uid}.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, key.encode("utf-8"))
            return uid, fd
        except OSError:
            os.close(fd)
    raise SandboxUnavailable("no free sandbox uid")


def for_owner(owner: Any) -> Optional[SandboxIdentity]:
    """The sandbox identity bound to this owner or any owner enclosing it."""
    if owner is None:
        return None
    ids = owner.ids() if hasattr(owner, "ids") else (str(owner),)
    with _ACTIVE_LOCK:
        for oid in reversed(ids):
            if oid in _ACTIVE:
                return _ACTIVE[oid]
    return None


def current() -> Optional[SandboxIdentity]:
    from . import owned_process

    return for_owner(owned_process.current_owner())


@contextlib.contextmanager
def bind(owner_id: str, *, project_id: Optional[str] = None) -> Iterator[Optional[SandboxIdentity]]:
    """Give the run `owner_id` its own uid and directory for the block. Everything spawned under
    that owner (owned_process) runs as it. Yields None when isolation is off, or `auto` with no
    helper (logged); raises SandboxUnavailable when it is `required` and impossible.

    With `project_id` and an opt-in toolchain (services.toolchains), the run's HOME starts with
    that project's dependency caches from its earlier runs, and they are kept when it ends —
    per project, never another project's."""
    with _ACTIVE_LOCK:
        existing = _ACTIVE.get(owner_id)
    if existing is not None:                 # nested bind of the same run (a redelivery in-process)
        yield existing
        return
    ident = acquire(owner_id, project_id=project_id)
    try:
        yield ident
    finally:
        if ident is not None:
            release(owner_id, project_id=project_id)


def acquire(owner_id: str, *, project_id: Optional[str] = None,
            what: str = "run", seed_logins: bool = True,
            sealed: bool = False) -> Optional[SandboxIdentity]:
    """`bind` for a workload that outlives the call that starts it (an app PREVIEW): the uid and
    directory are held until `release(owner_id)`. Same rules: None when isolation is off (or
    `auto` with no helper, logged loudly); SandboxUnavailable when it is `required` and
    impossible. Idempotent: a second acquire of a held owner returns the same identity."""
    m = mode()
    if sealed:
        # A SEALED workload never runs unfenced, whatever the mode says: no None, no `auto`.
        if m == "off" or not available() or not network_sealed():
            raise SandboxUnavailable(
                "the sealed (loopback-only) sandbox is unavailable: "
                + ("sandbox isolation is off" if m == "off"
                   else unavailable_reason() if not available()
                   else "the entrypoint did not install the sealed network rules"))
    if m == "off":
        return None
    if not available():
        if m == "required":
            raise SandboxUnavailable(unavailable_reason())
        log.warning("SANDBOX NOT ISOLATED: %s %s runs as the control plane's uid (no boss-sandbox-exec; "
                    "BOSS_SANDBOX_ISOLATION=auto). Never run this way with untrusted code.", what, owner_id)
        return None
    with _ACTIVE_LOCK:
        existing = _ACTIVE.get(owner_id)
    if existing is not None:
        return existing
    key = owner_key(owner_id)
    uid, lock_fd = _lease_uid(key, sealed=sealed)
    ident = SandboxIdentity(key=key, uid=uid, run_dir=run_root() / key, _lock_fd=lock_fd,
                            credential_free=not seed_logins or sealed, sealed=sealed)
    try:
        ident.killall()                                  # nothing of a previous lease survives
        if ident.run_dir.exists():
            _purge(ident)
        # Modes by chmod, never os.umask: the umask is process-wide, and every other thread's
        # files would be created world-writable meanwhile (process_umask guard test).
        ident.run_dir.mkdir(mode=0o700)
        os.chmod(ident.run_dir, 0o700)
        for sub in (ident.home, ident.tmp):
            sub.mkdir()
            os.chmod(sub, 0o2777)
        if seed_logins and not sealed:
            _seed_cli_logins(ident)
        _seed_toolchains(ident, project_id)
        ident._helper("adopt", str(uid), str(ident.run_dir))
        ident.sessions_fd()                # before any agent code: BOSS's own, 0711
    except BaseException:
        ident.killall()
        _purge(ident)
        with contextlib.suppress(OSError):
            os.close(lock_fd)
        raise
    with _ACTIVE_LOCK:
        _ACTIVE[owner_id] = ident
    log.info("sandbox %s=%s uid=%s", what, owner_id, uid)
    return ident


def held_identity(owner_id: str) -> Optional[SandboxIdentity]:
    """The uid and directory `owner_id` holds: this process's lease, or — after an API restart,
    or from another process — the one its run directory names (its owner is the uid that held
    it). None when it holds none (isolation off, or already released)."""
    with _ACTIVE_LOCK:
        ident = _ACTIVE.get(owner_id)
    if ident is not None:
        return ident
    try:
        run_dir = run_root() / owner_key(owner_id)
        st = os.lstat(run_dir)
    except OSError:
        return None
    if not (stat.S_ISDIR(st.st_mode) and UID_BASE <= st.st_uid < UID_BASE + UID_COUNT):
        return None
    with _ACTIVE_LOCK:
        for active in _ACTIVE.values():
            if active.uid == st.st_uid:
                return None
    lock_path = os.path.join(SHIM_ROOT, f"uid-{st.st_uid}.lock")
    if os.path.exists(lock_path):
        try:
            fd = os.open(lock_path, os.O_RDWR)
        except OSError:
            try:
                fd = os.open(lock_path, os.O_RDONLY)
            except OSError:
                return None
        try:
            holder = os.read(fd, 256).decode("utf-8", errors="ignore").strip()
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                if holder != run_dir.name:
                    return None
            else:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
                if holder and holder != run_dir.name:
                    return None
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)
    return SandboxIdentity(key=run_dir.name, uid=st.st_uid, run_dir=run_dir)


def stop_owner(owner_id: str) -> Optional[SandboxIdentity]:
    """Kill every process of the uid `owner_id` holds, and say which identity that was (None when
    it holds none). Raises SandboxUnavailable when the kill could not be done: a caller about to
    read the owner's tree as BOSS must not read it while its uid can still change it."""
    ident = held_identity(owner_id)
    if ident is not None:
        ident._helper("kill", str(ident.uid))
    return ident


def release(owner_id: str, *, project_id: Optional[str] = None) -> None:
    """End what `acquire` started: every process of the uid is killed, the directory removed and
    the uid freed. Idempotent; never raises. A lease this process does not hold (an API restart)
    is still cleaned up from its directory: its owner is the uid that held it."""
    with _ACTIVE_LOCK:
        ident = _ACTIVE.pop(owner_id, None)
    if ident is None:
        with contextlib.suppress(Exception):
            orphan = held_identity(owner_id)
            if orphan is not None:
                orphan.killall()
                _purge(orphan)
        return
    ident.killall()
    if project_id:
        with contextlib.suppress(Exception):
            from . import toolchains

            toolchains.save_cache(ident.home, project_id)
    _purge(ident)
    if ident._lock_fd is not None:
        with contextlib.suppress(OSError):
            os.ftruncate(ident._lock_fd, 0)
        with contextlib.suppress(OSError):
            os.close(ident._lock_fd)
    if ident._sessions_fd is not None:
        with contextlib.suppress(OSError):
            os.close(ident._sessions_fd)
        ident._sessions_fd = None


def owner_key(owner_id: str) -> str:
    """The run directory's name for an owner (also what a later process finds it by)."""
    return hashlib.sha256(owner_id.encode()).hexdigest()[:24]


def preview_port_for_uid(uid: int) -> Optional[int]:
    """The preview port a pool uid owns (scripts/sandbox/entrypoint.sh BOSS_PREVIEW_SELF: uid
    UID_BASE+i may reach the i-th port of BOSS_PREVIEW_PORTS on loopback, and no other preview
    port). None when the range has no port for it."""
    from ..app_links.service import preview_ports

    pool = preview_ports()
    port = pool.low + (int(uid) - UID_BASE)
    return port if UID_BASE <= int(uid) < UID_BASE + UID_COUNT and pool.low <= port <= pool.high else None


def held(owner_id: str) -> Optional[SandboxIdentity]:
    with _ACTIVE_LOCK:
        return _ACTIVE.get(owner_id)


def _seed_cli_logins(ident: SandboxIdentity) -> None:
    """The coding CLIs' own CONFIG (Grok's config.toml), copied into the run's HOME — and never
    a login (BOSSAPP-1480). A subscription login stays in the control plane: each session gets a
    run token at the model proxy and a session home with no login in it (subscription_shield).
    Before this, Codex's auth.json, Grok's auth.json and the Claude CLI's access token were
    copied here, readable by everything the run executed."""
    for env_name, sub in (("GROK_HOME", ".grok"),):
        src = (os.environ.get(env_name) or "").strip() or os.path.expanduser(f"~/{sub}")
        if not src or not os.path.isdir(src):
            continue
        dst = ident.home / sub
        dst.mkdir(exist_ok=True)
        os.chmod(dst, 0o2777)
        for name in ("config.toml", "config.json"):
            path = os.path.join(src, name)
            if os.path.isfile(path):
                shutil.copyfile(path, dst / name)
                os.chmod(dst / name, 0o644)
    # The CLIs' state homes (Codex's sessions, so `codex exec resume` finds a thread across
    # sessions), made HERE, before any agent code runs, and never chmod-ed or created again: a
    # session links to them by name from its own home (subscription_shield._session_home), after
    # checking they are still real directories BOSS owns — never following what a run planted.
    for path in (ident.home / ".codex", ident.home / ".codex" / "sessions", ident.home / ".grok"):
        path.mkdir(exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o2777)
    _refresh_logins_ahead(ident)


def _refresh_logins_ahead(ident: SandboxIdentity) -> None:
    """A login about to expire is refreshed BY THE CONTROL PLANE before the run starts (the
    proxy reads it on every request, so the run never needs a refresh of its own). Never fails
    the run: a login that cannot be refreshed fails the first session that needs it, in words."""
    from . import claude_login, codex_login

    for owner, need in ((codex_login, codex_login.SEED_MIN_VALID_SECONDS),
                        (claude_login, claude_login.SEED_MIN_VALID_SECONDS)):
        try:
            if owner is codex_login and codex_login.read_tokens() is None:
                continue
            if owner is claude_login and not _claude_cli_login_present():
                continue
            ok, why = owner.ensure_fresh(need)
            if not ok:
                log.warning("sandbox %s: %s", ident.key, why)
        except Exception as exc:  # noqa: BLE001
            log.warning("sandbox %s: login refresh skipped: %s", ident.key, type(exc).__name__)


def _claude_cli_login_present() -> bool:
    from .claude_credential import cli_login_present

    return cli_login_present() and not runs_on_long_lived_token()


#: What a session's Codex login carries in place of the real refresh token
#: (subscription_shield.codex_run_login): nothing that can rotate the operator's login.
RUN_REFRESH_TOKEN = "rt_boss_run_copy_cannot_refresh"


def runs_on_long_lived_token() -> bool:
    """Does a `claude` child resolve to the subscription's long-lived token (kind subscription)
    rather than the CLI's own login? The same resolution the child runs. Never raises: a store
    that did not answer is "no"."""
    try:
        from ..config import get_settings
        from .claude_credential import KIND_SUBSCRIPTION, resolve_claude_credential

        return resolve_claude_credential(get_settings()).kind == KIND_SUBSCRIPTION
    except Exception:  # noqa: BLE001
        return False


def _seed_toolchains(ident: SandboxIdentity, project_id: Optional[str]) -> None:
    """Opt-in toolchains (Java): the egress proxy in Maven's and Gradle's own config (they ignore
    HTTPS_PROXY), and this project's dependency caches. Never fails the run."""
    try:
        from . import toolchains

        toolchains.write_proxy_config(ident.home, os.environ.get(EGRESS_CONNECT_ENV))
        toolchains.restore_cache(ident.home, project_id)
    except Exception as exc:  # noqa: BLE001 — a cold cache is slower, not broken
        log.warning("toolchain setup for run %s failed: %s", ident.key, exc)


def _purge(ident: SandboxIdentity) -> None:
    """Remove the run directory: what the run's uid created is removed AS that uid (BOSS may not
    be able to enter a directory the agent chmod-ed), then BOSS removes the rest."""
    helper = helper_path()
    if helper and ident.run_dir.exists():
        with contextlib.suppress(Exception):
            from . import owned_process

            owned_process.run([helper, "run", str(ident.uid), "--", "/bin/sh", "-c",
                               'find "$1" -mindepth 1 -user "$(id -u)" -exec rm -rf {} + 2>/dev/null; true',
                               "sh", str(ident.run_dir)], capture_output=True, timeout=120,
                              sandboxed=False, cwd="/")
    shutil.rmtree(ident.run_dir, ignore_errors=True)
    with contextlib.suppress(OSError):
        if ident.run_dir.exists():
            os.rmdir(ident.run_dir)


def end_owner(owner_id: str) -> None:
    """A revoke / stop: kill every process of the run's uid NOW, wherever it is in its stage."""
    with _ACTIVE_LOCK:
        ident = _ACTIVE.get(owner_id)
    if ident is not None:
        ident.killall()
