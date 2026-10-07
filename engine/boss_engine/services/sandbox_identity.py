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
import logging
import os
import shutil
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
        for name in ("CODEX_HOME", "GROK_HOME"):
            if name in out:
                out[name] = f"{home}/.{name.split('_')[0].lower()}"
        # The checkout is BOSS's (it cloned it); the run's uid works in it. Git refuses a repo
        # another uid owns unless told it is safe — and it is, inside this run's directory.
        n = int(out.get("GIT_CONFIG_COUNT") or 0)
        out[f"GIT_CONFIG_KEY_{n}"] = "safe.directory"
        out[f"GIT_CONFIG_VALUE_{n}"] = "*"
        out["GIT_CONFIG_COUNT"] = str(n + 1)
        proxy = (os.environ.get(EGRESS_CONNECT_ENV) or "").strip()
        if proxy:
            for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
                out[name] = proxy
            no_proxy = "localhost,127.0.0.1,::1,api,egress"
            out["NO_PROXY"] = out["no_proxy"] = no_proxy
        return out

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
        """A fresh directory for one sandbox (a stage's clone) inside the run directory. BOSS
        creates it and its first contents with umask 0: only this run and BOSS can reach them
        (the run directory is 2770), and the run's uid must be able to edit what BOSS cloned."""
        import tempfile

        path = Path(tempfile.mkdtemp(prefix=prefix, dir=str(self.run_dir)))
        os.chmod(path, 0o2777)          # setgid: what the run's uid creates stays group boss
        return path


_ACTIVE: dict[str, SandboxIdentity] = {}
_ACTIVE_LOCK = threading.Lock()


def _lease_uid(key: str) -> tuple[int, int]:
    """An flock on SHIM_ROOT/uid-<n>.lock, held for the run's life: unique across every process
    in this container. Starts at a hash of the key so restarts of one run tend to the same uid."""
    start = int(hashlib.sha256(key.encode()).hexdigest(), 16) % UID_COUNT
    for i in range(UID_COUNT):
        uid = UID_BASE + (start + i) % UID_COUNT
        fd = os.open(os.path.join(SHIM_ROOT, f"uid-{uid}.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
def bind(owner_id: str) -> Iterator[Optional[SandboxIdentity]]:
    """Give the run `owner_id` its own uid and directory for the block. Everything spawned under
    that owner (owned_process) runs as it. Yields None when isolation is off, or `auto` with no
    helper (logged); raises SandboxUnavailable when it is `required` and impossible."""
    m = mode()
    if m == "off":
        yield None
        return
    if not available():
        if m == "required":
            raise SandboxUnavailable(unavailable_reason())
        log.warning("SANDBOX NOT ISOLATED: run %s runs as the control plane's uid (no boss-sandbox-exec; "
                    "BOSS_SANDBOX_ISOLATION=auto). Never run this way with untrusted code.", owner_id)
        yield None
        return
    with _ACTIVE_LOCK:
        existing = _ACTIVE.get(owner_id)
    if existing is not None:                 # nested bind of the same run (a redelivery in-process)
        yield existing
        return
    key = hashlib.sha256(owner_id.encode()).hexdigest()[:24]
    uid, lock_fd = _lease_uid(key)
    ident = SandboxIdentity(key=key, uid=uid, run_dir=run_root() / key, _lock_fd=lock_fd)
    try:
        ident.killall()                                  # nothing of a previous lease survives
        if ident.run_dir.exists():
            _purge(ident)
        old = os.umask(0)
        try:
            ident.run_dir.mkdir(mode=0o700)
            for sub in (ident.home, ident.tmp):
                sub.mkdir()
                os.chmod(sub, 0o2777)
        finally:
            os.umask(old)
        _seed_cli_logins(ident)
        ident._helper("adopt", str(uid), str(ident.run_dir))
        with _ACTIVE_LOCK:
            _ACTIVE[owner_id] = ident
        log.info("sandbox run=%s uid=%s", owner_id, uid)
        yield ident
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.pop(owner_id, None)
        ident.killall()
        _purge(ident)
        with contextlib.suppress(OSError):
            os.close(lock_fd)


def _seed_cli_logins(ident: SandboxIdentity) -> None:
    """The coding CLIs' own logins (Codex's auth.json, Grok's), copied into the run's HOME: the
    model credential is the one secret an agent is meant to hold (agent_env), and the control
    plane's own CLI homes stay 0700 to the boss uid. Readable inside the run directory only."""
    for env_name, sub in (("CODEX_HOME", ".codex"), ("GROK_HOME", ".grok")):
        src = (os.environ.get(env_name) or "").strip()
        if not src or not os.path.isdir(src):
            continue
        dst = ident.home / sub
        dst.mkdir(exist_ok=True)
        os.chmod(dst, 0o2777)
        for name in ("auth.json", "config.toml", "config.json", "credentials.json"):
            path = os.path.join(src, name)
            if os.path.isfile(path):
                shutil.copyfile(path, dst / name)
                os.chmod(dst / name, 0o644)


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
