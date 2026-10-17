#!/usr/bin/env python3
"""THE HOST SELF-INSTALLER: BOSS installs its own approved fixes, and only those.

BOSSAPP-571, docs/product/BOSS_FIXES_ITSELF.md §6.4, §6.5, decisions 1 and 2. Run on the HOST
(never in a container: nothing in a container can build images or restart services, by design,
and the Docker socket is never mounted into one). `boss self-fix …` runs it.

    seed                       clone the checkout into BOSS's self-source mirror (.boss/selfsrc)
    enroll                     mint this installer's own key (sys_self_installer), kept 0600
    watch [--once]             poll BOSS for tickets every 10 s and run them (opt-in service)
    install SFX                run that self-fix's install now (the "run this command" fallback)
    status                     what is installed on the local stack
    plan-upgrade --to REF      each installed fix against a release: dropped, carried, rework
    restack --to REF           after an upgrade to REF: re-apply the carried fixes on it

A TICKET IS THE ONLY ORDER IT TAKES. It is an EdDSA-signed JWS from BOSS (the install's own
signing key, verified here against BOSS's published JWKS with a dependency-free Ed25519), bound
to one approved decision, one base, one fix commit and its tree, single use (BOSS refuses a
second take), expiring in an hour. Before anything changes it re-reads that the decision is
approved, that the stack head is the ticket's base, and that the fix commit it fetched (a git
bundle) has exactly the tree BOSS's gates ran on and the base as its parent. It executes
nothing else: no command, path or image name comes from a ticket.

INSTALL PHASES (each reported to BOSS, shown live on the parked step):
    backup   scripts/install/backup.sh                                on failure: nothing changed
    apply    the fix on refs/boss/stack/<release> + the stack worktree on failure: ref restored
    build    images from the stack worktree; the current ones kept as :prev  failure: nothing restarted
    start    compose up with the new images                           on failure: roll back
    health   the API answers with the new stack head, the worker beats, no restart loop, the job
             error rate no worse than the hour before, for the health window   failure: roll back
    done     BOSS_STACK_REF / BOSS_STACK_HEAD recorded in .boss-release and .env

ROLLBACK re-tags :prev, starts it, checks it, and moves the stack ref back: never a rebuild,
never code from the fix (this file is `never`-protected for self-fixes). The checkout's HEAD is
never moved: it stays on the release, so upgrade.sh's fast-forward rule holds for the base.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

TICKET_TYP = "boss-self-fix-ticket+jwt"
SERVICES = ("api", "worker", "mcp-tools", "egress", "web")
STACK_WORKTREE = ".boss/stack-worktree"
MIRROR = ".boss/selfsrc/boss.git"
KEY_FILE = ".boss/self-installer.key"
POLL_SECONDS = 10


class Refused(RuntimeError):
    """The installer will not run this ticket (and changed nothing)."""


class PhaseFailed(RuntimeError):
    def __init__(self, phase: str, detail: str):
        super().__init__(f"{phase}: {detail}")
        self.phase = phase
        self.detail = detail


# ── Ed25519 verification (RFC 8032 §5.1.7), no dependencies ──────────────────────────────────

_P = 2 ** 255 - 19
_Q = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _add(a: tuple, b: tuple) -> tuple:
    x1, y1, z1, t1 = a
    x2, y2, z2, t2 = b
    aa = (y1 - x1) * (y2 - x2) % _P
    bb = (y1 + x1) * (y2 + x2) % _P
    cc = t1 * 2 * _D * t2 % _P
    dd = z1 * 2 * z2 % _P
    e, f, g, h = bb - aa, dd - cc, dd + cc, bb + aa
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(s: int, point: tuple) -> tuple:
    out = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            out = _add(out, point)
        point = _add(point, point)
        s >>= 1
    return out


def _recover_x(y: int, sign: int) -> Optional[int]:
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


def _decompress(raw: bytes) -> Optional[tuple]:
    if len(raw) != 32:
        return None
    y = int.from_bytes(raw, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


_GY = 4 * pow(5, _P - 2, _P) % _P
_G = (_recover_x(_GY, 0), _GY, 1, _recover_x(_GY, 0) * _GY % _P)


def _equal(a: tuple, b: tuple) -> bool:
    x1, y1, z1, _ = a
    x2, y2, z2, _ = b
    return (x1 * z2 - x2 * z1) % _P == 0 and (y1 * z2 - y2 * z1) % _P == 0


def ed25519_verify(public: bytes, message: bytes, signature: bytes) -> bool:
    if len(public) != 32 or len(signature) != 64:
        return False
    a = _decompress(public)
    r = _decompress(signature[:32])
    if a is None or r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _Q:
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), "little") % _Q
    return _equal(_mul(s, _G), _add(r, _mul(h, a)))


def _b64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def verify_ticket(token: str, jwks: dict[str, Any], *, now: Optional[float] = None
                  ) -> dict[str, Any]:
    """The ticket's claims, or Refused. Algorithm, type and kid are checked before the key is
    looked up; the key comes only from BOSS's published key set."""
    parts = str(token or "").split(".")
    if len(parts) != 3:
        raise Refused("the ticket is not a signed token")
    try:
        header = json.loads(_b64u(parts[0]))
        claims = json.loads(_b64u(parts[1]))
        signature = _b64u(parts[2])
    except (ValueError, TypeError) as error:
        raise Refused(f"the ticket does not parse ({error})") from error
    if header.get("alg") != "EdDSA" or header.get("typ") != TICKET_TYP or "crit" in header:
        raise Refused("the ticket is not a BOSS self-fix ticket (algorithm or type)")
    key = next((k for k in jwks.get("keys") or [] if k.get("kid") == header.get("kid")
                and k.get("kty") == "OKP" and k.get("crv") == "Ed25519"), None)
    if key is None:
        raise Refused("the ticket was signed by a key BOSS does not publish")
    if not ed25519_verify(_b64u(key["x"]), f"{parts[0]}.{parts[1]}".encode(), signature):
        raise Refused("the ticket's signature is not BOSS's")
    moment = time.time() if now is None else now
    if claims.get("typ") != "self_fix_ticket" or claims.get("kind") not in (
            "install", "rollback", "remove", "remove_all"):
        raise Refused("the ticket is not an install order")
    if int(claims.get("exp") or 0) < moment:
        raise Refused("the ticket expired")
    if int(claims.get("iat") or 0) > moment + 120:
        raise Refused("the ticket is from the future")
    for field in ("ticket_id", "sfx_id", "decision_id", "base_sha", "fix_commit_sha",
                  "fix_tree_sha", "stack_ref", "release"):
        if not claims.get(field):
            raise Refused(f"the ticket has no {field}")
    for field in ("base_sha", "fix_commit_sha", "fix_tree_sha"):
        if not re.fullmatch(r"[0-9a-f]{40}", str(claims[field])):
            raise Refused(f"the ticket's {field} is not a commit id")
    if not re.fullmatch(r"refs/boss/stack/[A-Za-z0-9._-]+", str(claims["stack_ref"])):
        raise Refused("the ticket names a ref that is not a BOSS stack")
    return claims


# ── the host ─────────────────────────────────────────────────────────────────────────────────

class Host:
    """The install directory, its git, its Docker, its BOSS API. Every command it runs is fixed
    here; `docker` and `backup` may be replaced by a test (BOSS_SELF_FIX_DOCKER,
    BOSS_SELF_FIX_BACKUP) and nothing else can be."""

    def __init__(self, install_dir: Path, *, api: Optional[str] = None,
                 key: Optional[str] = None, out: Callable[[str], None] = print):
        self.dir = Path(install_dir).resolve()
        self.out = out
        self.env_file = self.dir / ".env"
        self.release_file = self.dir / ".boss-release"
        self._api = api
        self._key = key
        self.docker = shlex.split(os.environ.get("BOSS_SELF_FIX_DOCKER") or "docker")

    # -- small helpers --------------------------------------------------------------------
    def env_value(self, name: str) -> str:
        try:
            for line in self.env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip()
        except OSError:
            pass
        return ""

    def set_env(self, path: Path, values: dict[str, str]) -> None:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        seen = set()
        out = []
        for line in lines:
            name = line.split("=", 1)[0]
            if name in values:
                out.append(f"{name}={values[name]}")
                seen.add(name)
            else:
                out.append(line)
        out += [f"{k}={v}" for k, v in values.items() if k not in seen]
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600 if path.name == ".env" else 0o644)
        tmp.replace(path)

    def git(self, *args: str, cwd: Optional[Path] = None, check: bool = True) -> str:
        done = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args],
                              cwd=str(cwd or self.dir), capture_output=True, text=True)
        if check and done.returncode != 0:
            raise PhaseFailed("git", (done.stderr or done.stdout).strip()[-400:])
        return done.stdout.strip()

    def run(self, argv: list[str], *, env: Optional[dict[str, str]] = None, timeout: int = 3600,
            cwd: Optional[Path] = None) -> tuple[int, str]:
        merged = {**os.environ, **(env or {})}
        try:
            done = subprocess.run(argv, cwd=str(cwd or self.dir), env=merged, text=True,
                                  capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            return 124, str(error)
        return done.returncode, ((done.stdout or "") + (done.stderr or ""))[-4000:]

    def compose_env(self) -> dict[str, str]:
        project = os.environ.get("COMPOSE_PROJECT_NAME") or "boss-community"
        return {"COMPOSE_PROJECT_NAME": project,
                "BOSS_COMMUNITY_IMAGE_TAG": os.environ.get("BOSS_COMMUNITY_IMAGE_TAG") or project}

    def compose(self, *args: str, compose_file: Optional[Path] = None,
                project_dir: Optional[Path] = None, env: Optional[dict[str, str]] = None
                ) -> tuple[int, str]:
        profiles: list[str] = []
        if self.env_value("BOSS_COMMUNITY_WEB_ENABLED") != "0":
            profiles += ["--profile", "web"]
        if self.env_value("BOSS_COMMUNITY_SECRET_STORE") == "infisical":
            profiles += ["--profile", "secrets"]
        argv = [*self.docker, "compose", "-f",
                str(compose_file or self.dir / "scripts/install/community-compose.yml"),
                *(["--project-directory", str(project_dir)] if project_dir else []),
                "--env-file", str(self.env_file), *profiles, *args]
        return self.run(argv, env={**self.compose_env(), **(env or {})})

    # -- the API --------------------------------------------------------------------------
    def api_url(self) -> str:
        if self._api:
            return self._api.rstrip("/")
        bind, port = self.env_value("BOSS_API_BIND"), self.env_value("BOSS_API_PORT") or "8000"
        if bind in ("", "127.0.0.1", "0.0.0.0"):
            return f"http://127.0.0.1:{port}"
        return f"http://[{bind}]:{port}" if ":" in bind else f"http://{bind}:{port}"

    def key(self) -> str:
        if self._key:
            return self._key
        value = (os.environ.get("BOSS_SELF_INSTALLER_KEY") or "").strip()
        if value:
            return value
        path = self.dir / KEY_FILE
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise Refused("this host has no self-installer key yet: run `boss self-fix enroll`"
                          ) from error

    def call(self, method: str, path: str, body: Optional[dict] = None, *,
             auth: bool = True, timeout: int = 30) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.api_url() + path, data=data, method=method)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if auth:
            request.add_header("Authorization", f"Bearer {self.key()}")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            detail = error.read()[:500].decode("utf-8", "replace")
            raise Refused(f"BOSS answered {error.code}: {detail}") from error
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise Refused(f"BOSS did not answer at {self.api_url()} ({error})") from error

    def report(self, ticket_id: str, phase: str, ok: bool, detail: str = "",
               stack_head: Optional[str] = None) -> None:
        self.out(f"  {phase}: {'ok' if ok else 'FAILED'}{' — ' + detail[:200] if detail else ''}")
        try:
            self.call("POST", f"/v1/self-fixes/tickets/{ticket_id}/report",
                      {"phase": phase, "ok": ok, "detail": detail[:1500],
                       **({"stack_head": stack_head} if stack_head else {})})
        except Refused as error:
            self.out(f"  (BOSS did not take the report: {error})")

    # -- the stack ------------------------------------------------------------------------
    def release_sha(self) -> str:
        return self.git("rev-parse", "HEAD")

    def stack_head(self, ref: str) -> str:
        found = self.git("rev-parse", "-q", "--verify", f"{ref}^{{commit}}", check=False)
        return found or self.release_sha()

    def worktree(self) -> Path:
        return self.dir / STACK_WORKTREE

    def checkout_stack(self, commit: str) -> None:
        wt = self.worktree()
        if (wt / ".git").exists():
            self.git("checkout", "-q", "--detach", "--force", commit, cwd=wt)
        else:
            wt.parent.mkdir(parents=True, exist_ok=True)
            self.git("worktree", "add", "-q", "--detach", "--force", str(wt), commit)

    def images(self) -> list[str]:
        tag = self.compose_env()["BOSS_COMMUNITY_IMAGE_TAG"]
        project = self.compose_env()["COMPOSE_PROJECT_NAME"]
        out = []
        for service in SERVICES:
            name = f"{project}-{service}:{tag}"
            code, _ = self.run([*self.docker, "image", "inspect", name])
            if code == 0:
                out.append(name)
        return out

    def keep_previous(self) -> list[str]:
        kept = []
        for image in self.images():
            prev = image.rsplit(":", 1)[0] + ":prev"
            code, said = self.run([*self.docker, "tag", image, prev])
            if code != 0:
                raise PhaseFailed("build", f"could not keep {image} as :prev ({said[-200:]})")
            kept.append(prev)
        return kept

    def restore_previous(self) -> None:
        tag = self.compose_env()["BOSS_COMMUNITY_IMAGE_TAG"]
        project = self.compose_env()["COMPOSE_PROJECT_NAME"]
        restored = 0
        for service in SERVICES:
            prev = f"{project}-{service}:prev"
            code, _ = self.run([*self.docker, "image", "inspect", prev])
            if code != 0:
                continue
            code, said = self.run([*self.docker, "tag", prev, f"{project}-{service}:{tag}"])
            if code != 0:
                raise PhaseFailed("rollback", f"could not put {prev} back ({said[-200:]})")
            restored += 1
        if not restored:
            raise PhaseFailed("rollback", "there are no :prev images to put back")

    def restart_counts(self) -> dict[str, int]:
        out = {}
        for service in ("api", "worker"):
            code, said = self.compose("ps", "-q", service)
            ids = [i for i in said.split() if re.fullmatch(r"[0-9a-f]{6,}", i)]
            if code != 0 or not ids:
                continue
            code, count = self.run([*self.docker, "inspect", "-f", "{{.RestartCount}}", ids[0]])
            if code == 0 and count.strip().isdigit():
                out[service] = int(count.strip())
        return out

    def healthy(self, stack_head: str, *, minutes: float, settle: int = 90) -> None:
        """The health window, or PhaseFailed('health', why)."""
        window = float(os.environ.get("BOSS_SELF_FIX_HEALTH_SECONDS") or minutes * 60)
        poll = float(os.environ.get("BOSS_SELF_FIX_HEALTH_POLL") or 15)
        settle = int(os.environ.get("BOSS_SELF_FIX_SETTLE_SECONDS") or settle)
        deadline = time.time() + settle
        report: dict[str, Any] = {}
        while True:
            try:
                self.call("GET", "/health", auth=False, timeout=10)
                report = self.call("GET", "/v1/self-fixes/health", timeout=10)
                if report.get("stack_head") == stack_head:
                    break
                why = f"BOSS answers as {str(report.get('stack_head'))[:12]}, not {stack_head[:12]}"
            except Refused as error:
                why = str(error)
            if time.time() > deadline:
                raise PhaseFailed("health", f"the API did not come back as the new build in "
                                            f"{settle}s ({why})")
            time.sleep(min(poll, 5))
        restarts = self.restart_counts()
        end = time.time() + window
        while True:
            heartbeat = report.get("worker_heartbeat_age_seconds")
            if heartbeat is not None and float(heartbeat) > 120:
                raise PhaseFailed("health", f"the worker has not beaten for {int(heartbeat)}s")
            if report.get("regressed"):
                raise PhaseFailed("health", f"the job error rate went from "
                                            f"{report.get('error_rate_before')} to "
                                            f"{report.get('error_rate')}")
            now = self.restart_counts()
            looping = [s for s, n in now.items() if n > restarts.get(s, n)]
            if looping:
                raise PhaseFailed("health", f"{', '.join(looping)} restarted during the window")
            if time.time() >= end:
                return
            time.sleep(poll)
            try:
                self.call("GET", "/health", auth=False, timeout=10)
                report = self.call("GET", "/v1/self-fixes/health", timeout=10)
            except Refused as error:
                raise PhaseFailed("health", str(error)) from error
            if report.get("stack_head") != stack_head:
                raise PhaseFailed("health", "BOSS stopped answering as the new build")

    def record(self, ref: str, head: str) -> None:
        values = {"BOSS_STACK_REF": ref, "BOSS_STACK_HEAD": head}
        self.set_env(self.release_file, values)
        self.set_env(self.env_file, {"BOSS_STACK_HEAD": head})


# ── one ticket ───────────────────────────────────────────────────────────────────────────────

def _fetch_fix(host: Host, claims: dict[str, Any], bundle_b64: Optional[str]) -> None:
    if not bundle_b64:
        raise Refused("BOSS sent no commit for this ticket")
    fd, name = tempfile.mkstemp(prefix="boss-self-fix-", suffix=".bundle")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(base64.b64decode(bundle_b64))
        host.git("bundle", "verify", "-q", name)
        branch = str(claims.get("branch") or "")
        if not re.fullmatch(r"boss/self-fix/[A-Za-z0-9._/-]+", branch):
            raise Refused("the ticket names a branch that is not a self-fix branch")
        host.git("fetch", "-q", name, f"+refs/heads/{branch}:refs/heads/{branch}")
    finally:
        Path(name).unlink(missing_ok=True)
    commit = host.git("rev-parse", f"refs/heads/{branch}^{{commit}}")
    if commit != claims["fix_commit_sha"]:
        raise Refused("the fix BOSS sent is not the commit the ticket names")
    tree = host.git("rev-parse", f"{commit}^{{tree}}")
    if tree != claims["fix_tree_sha"]:
        raise Refused("the fix's tree is not the one BOSS's gates ran on")
    parent = host.git("rev-parse", f"{commit}^")
    if parent != claims["base_sha"]:
        raise Refused("the fix is not built on the ticket's base")


def take_and_check(host: Host, ticket: dict[str, Any], jwks: dict[str, Any]
                   ) -> tuple[dict[str, Any], Optional[str]]:
    """Verify the ticket, take it ONCE, re-read its decision. (claims, the bundle)."""
    claims = verify_ticket(ticket["token"], jwks)
    if claims["ticket_id"] != ticket.get("ticket_id"):
        raise Refused("the ticket's id does not match its listing")
    taken = host.call("POST", f"/v1/self-fixes/tickets/{claims['ticket_id']}/take", {})
    if taken.get("token") != ticket["token"]:
        raise Refused("BOSS handed back a different ticket than the one verified")
    if taken.get("decision_status") != "approved":
        raise Refused(f"the decision behind this ticket is {taken.get('decision_status')!r}, "
                      "not approved")
    return claims, taken.get("bundle_b64")


def install(host: Host, claims: dict[str, Any], bundle_b64: Optional[str]) -> str:
    """The install phases. Returns the new stack head; raises PhaseFailed (after rolling back
    whatever it had started) or Refused (nothing changed)."""
    ticket_id = claims["ticket_id"]
    ref = claims["stack_ref"]
    base = claims["base_sha"]
    fix = claims["fix_commit_sha"]
    current = host.stack_head(ref)
    if current != base:
        raise Refused(f"the stack moved: its head is {current[:12]}, the ticket's base is "
                      f"{base[:12]}; BOSS checks the fix again on the new stack")
    _fetch_fix(host, claims, bundle_b64)
    backup = os.environ.get("BOSS_SELF_FIX_BACKUP")
    argv = shlex.split(backup) if backup else ["bash", str(host.dir / "scripts/install/backup.sh"),
                                               "--dir", str(host.dir)]
    code, said = host.run(argv, timeout=1800)
    host.report(ticket_id, "backup", code == 0, said.strip().splitlines()[-1] if said.strip()
                else "")
    if code != 0:
        raise PhaseFailed("backup", said[-300:])
    # APPLY: the stack ref moves from the base to the fix (compare-and-swap) and the stack
    # worktree checks it out. The checkout's own HEAD never moves.
    previous_ref = host.git("rev-parse", "-q", "--verify", ref, check=False)
    host.git("update-ref", "-m", f"self-fix {claims['sfx_id']}", ref, fix,
             previous_ref or "0" * 40)
    try:
        host.checkout_stack(fix)
    except PhaseFailed as error:
        _restore_ref(host, ref, previous_ref)
        host.report(ticket_id, "apply", False, error.detail)
        raise PhaseFailed("apply", error.detail) from error
    host.report(ticket_id, "apply", True, f"{ref} → {fix[:12]}")
    # BUILD from the stack worktree; the running images are kept as :prev first.
    try:
        host.keep_previous()
        wt = host.worktree()
        code, said = host.compose("build", compose_file=wt / "scripts/install/community-compose.yml",
                                  project_dir=wt / "scripts/install",
                                  env={"BOSS_SOURCE_SHA": host.release_sha(),
                                       "BOSS_STACK_HEAD": fix})
        if code != 0:
            raise PhaseFailed("build", said[-600:])
    except PhaseFailed as error:
        _restore_ref(host, ref, previous_ref)
        host.checkout_stack(previous_ref or host.release_sha())
        host.report(ticket_id, "build", False, error.detail)
        raise
    host.report(ticket_id, "build", True)
    # START with the new images; from here a failure puts the previous build back.
    host.set_env(host.env_file, {"BOSS_STACK_HEAD": fix})
    code, said = host.compose("up", "-d", "--no-build", env={"BOSS_STACK_HEAD": fix})
    if code != 0:
        host.report(ticket_id, "start", False, said[-300:])
        rollback(host, claims, previous_head=base, previous_ref=previous_ref,
                 trigger=f"start failed: {said[-200:]}")
        raise PhaseFailed("start", said[-300:])
    host.report(ticket_id, "start", True, stack_head=fix)
    try:
        host.healthy(fix, minutes=float(claims.get("health_minutes") or 10))
    except PhaseFailed as error:
        host.report(ticket_id, "health", False, error.detail)
        rollback(host, claims, previous_head=base, previous_ref=previous_ref,
                 trigger=f"health: {error.detail}")
        raise
    host.report(ticket_id, "health", True, stack_head=fix)
    host.record(ref, fix)
    host.report(ticket_id, "done", True, stack_head=fix)
    return fix


def _restore_ref(host: Host, ref: str, previous: str) -> None:
    if previous:
        host.git("update-ref", ref, previous, check=False)
    else:
        host.git("update-ref", "-d", ref, check=False)


def rollback(host: Host, claims: dict[str, Any], *, previous_head: str,
             previous_ref: Optional[str], trigger: str) -> None:
    """R7: :prev back, started, checked, the stack ref moved back. Reported as `rollback`."""
    ticket_id = claims["ticket_id"]
    ref = claims["stack_ref"]
    try:
        host.restore_previous()
        host.set_env(host.env_file, {"BOSS_STACK_HEAD": previous_head})
        code, said = host.compose("up", "-d", "--no-build", env={"BOSS_STACK_HEAD": previous_head})
        if code != 0:
            raise PhaseFailed("rollback", f"the previous build did not start: {said[-300:]}")
        host.healthy(previous_head, minutes=0, settle=120)
        _restore_ref(host, ref, previous_ref or "")
        host.checkout_stack(previous_head)
        host.record(ref, previous_head)
    except PhaseFailed as error:
        host.report(ticket_id, "rollback", False, f"{error.detail} — restore the backup: "
                                                  "scripts/install/restore.sh")
        raise
    host.report(ticket_id, "rollback", True, f"{trigger}; {previous_head[:12]} is running again",
                stack_head=previous_head)


def run_rollback_ticket(host: Host, claims: dict[str, Any]) -> None:
    """A rollback ticket: BOSS saw the fix not hold. Only the top of the stack can be put back
    this way (its :prev images are the build before it)."""
    ref = claims["stack_ref"]
    fix = claims["fix_commit_sha"]
    if host.stack_head(ref) != fix:
        raise Refused("this fix is not the top of the stack, so :prev is not the build before "
                      "it; Remove it instead (a rebuild without it)")
    rollback(host, claims, previous_head=claims["base_sha"],
             previous_ref=claims["base_sha"] if claims["base_sha"] != host.release_sha() else "",
             trigger=str(claims.get("trigger") or "BOSS asked for a rollback"))


def run_ticket(host: Host, ticket: dict[str, Any], jwks: dict[str, Any]) -> str:
    """One ticket, end to end. Returns what happened, in a word."""
    try:
        claims, bundle = take_and_check(host, ticket, jwks)
    except Refused as error:
        host.out(f"Refused ticket {ticket.get('ticket_id')}: {error}")
        return "refused"
    host.out(f"Ticket {claims['ticket_id']} ({claims['kind']}) for {claims['sfx_id']}")
    try:
        if claims["kind"] == "install":
            install(host, claims, bundle)
            return "installed"
        if claims["kind"] == "rollback":
            run_rollback_ticket(host, claims)
            return "rolled_back"
        raise Refused(f"{claims['kind']} tickets are not supported by this installer yet")
    except Refused as error:
        host.report(claims["ticket_id"], "apply", False, str(error))
        return "refused"
    except PhaseFailed as error:
        host.out(f"  stopped at {error.phase}: {error.detail[:300]}")
        return "failed"


def poll_once(host: Host) -> list[str]:
    tickets = host.call("GET", "/v1/self-fixes/tickets").get("tickets") or []
    if not tickets:
        return []
    jwks = host.call("GET", "/.well-known/boss-jwks.json", auth=False)
    return [run_ticket(host, t, jwks) for t in tickets]


# ── upgrades (R8) ────────────────────────────────────────────────────────────────────────────

def _git(source: str, *args: str, check: bool = True) -> str:
    done = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=source,
                          capture_output=True, text=True)
    if check and done.returncode != 0:
        raise PhaseFailed("git", (done.stderr or done.stdout).strip()[-400:])
    return done.stdout.strip()


def plan_stack(source: str, to_ref: str, fixes: list[dict[str, Any]], *,
               behaviour: Optional[Callable[[str, str], bool]] = None) -> list[dict[str, Any]]:
    """Each fix of the stack (in order) against the release `to_ref` (§6.4):
    dropped_upstream — `git cherry` sees an equivalent patch upstream, an upstream commit carries
                       this fix's `BOSS-Self-Fix:` trailer, or (`behaviour`) its regression test
                       passes on the release without it;
    carried          — it applies cleanly on the rebuilt stack;
    needs_rework     — it conflicts (a re-port self-fix goes through the normal loop)."""
    to_sha = _git(source, "rev-parse", f"{to_ref}^{{commit}}")
    head = to_sha
    out = []
    scratch = tempfile.mkdtemp(prefix="boss-stack-plan-")
    try:
        _git(source, "worktree", "add", "-q", "--detach", scratch, to_sha)
        for fix in fixes:
            commit = str(fix.get("commit") or "")
            sfx = str(fix.get("self_fix_id") or "")
            item = {"self_fix_id": sfx, "commit": commit}
            merged = subprocess.run(["git", "merge-base", "--is-ancestor", commit, to_sha],
                                    cwd=source, capture_output=True).returncode == 0
            cherry = _git(source, "cherry", to_sha, commit, f"{commit}^", check=False)
            trailer = _git(source, "log", "--format=%H", f"--grep=BOSS-Self-Fix: {sfx}",
                           to_sha, check=False) if sfx else ""
            if merged or cherry.startswith("-"):
                # The release holds this very commit (merged as it is), or an equivalent patch.
                item.update(outcome="dropped_upstream", how="commit")
            elif trailer:
                item.update(outcome="dropped_upstream", how="trailer")
            elif behaviour is not None and behaviour(commit, to_sha):
                item.update(outcome="dropped_upstream", how="behaviour")
            else:
                done = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "cherry-pick",
                                       "--allow-empty", "-x", commit], cwd=scratch,
                                      capture_output=True, text=True,
                                      env={**os.environ, "GIT_COMMITTER_NAME": "BOSS",
                                           "GIT_COMMITTER_EMAIL": "boss@boss.invalid"})
                if done.returncode == 0:
                    head = _git(scratch, "rev-parse", "HEAD")
                    item.update(outcome="carried", new_commit=head)
                else:
                    subprocess.run(["git", "cherry-pick", "--abort"], cwd=scratch,
                                   capture_output=True)
                    item.update(outcome="needs_rework",
                                why=(done.stderr or done.stdout).strip()[-300:])
            out.append(item)
    finally:
        _git(source, "worktree", "remove", "--force", scratch, check=False)
    return out


def stack_fixes(host: Host, ref: str) -> list[dict[str, Any]]:
    """The fixes on a stack ref, oldest first, from their BOSS-Self-Fix trailers."""
    release = host.release_sha()
    head = host.stack_head(ref)
    if head == release:
        return []
    out = []
    log = host.git("log", "--reverse", "--format=%H%x09%(trailers:key=BOSS-Self-Fix,valueonly)",
                   f"{release}..{head}")
    for line in log.splitlines():
        commit, _, sfx = line.partition("\t")
        out.append({"commit": commit.strip(), "self_fix_id": sfx.strip()})
    return out


# ── commands ─────────────────────────────────────────────────────────────────────────────────

def cmd_seed(host: Host) -> int:
    mirror = host.dir / MIRROR
    if (mirror / "HEAD").exists():
        host.git("--git-dir", str(mirror), "fetch", "-q", "--prune", "--tags", str(host.dir),
                 "+refs/*:refs/*")
    else:
        mirror.parent.mkdir(parents=True, exist_ok=True)
        host.git("clone", "-q", "--mirror", str(host.dir), str(mirror))
    # Read-only for BOSS's containers (mounted :ro); readable by their user.
    for path in [mirror, *mirror.rglob("*")]:
        try:
            path.chmod(path.stat().st_mode | 0o044 | (0o011 if path.is_dir() else 0))
        except OSError:
            pass
    host.out(f"BOSS's own source is mirrored at {mirror} ({host.release_sha()[:12]}).")
    return 0


def cmd_watch(host: Host, once: bool) -> int:
    host.out(f"Watching {host.api_url()} for approved BOSS self-fixes (every {POLL_SECONDS}s).")
    while True:
        try:
            for outcome in poll_once(host):
                host.out(f"  → {outcome}")
        except Refused as error:
            host.out(f"BOSS did not answer: {error}")
        if once:
            return 0
        time.sleep(POLL_SECONDS)


def cmd_install(host: Host, sfx: str) -> int:
    if not re.fullmatch(r"sfx_[0-9a-f]{6,40}", sfx):
        host.out("Name a self-fix (sfx_…)." )
        return 2
    try:
        host.call("POST", "/v1/self-fixes/tickets/reissue", {"self_fix_id": sfx})
    except Refused as error:
        host.out(f"BOSS did not issue a ticket for {sfx}: {error}")
    tickets = [t for t in host.call("GET", "/v1/self-fixes/tickets").get("tickets") or []
               if t.get("self_fix_id") == sfx]
    if not tickets:
        host.out(f"No approved install is waiting for {sfx}.")
        return 1
    jwks = host.call("GET", "/.well-known/boss-jwks.json", auth=False)
    outcomes = [run_ticket(host, t, jwks) for t in tickets]
    return 0 if all(o in ("installed", "rolled_back") for o in outcomes) else 1


def cmd_status(host: Host) -> int:
    ref = (host.env_value("BOSS_STACK_REF") or "")
    try:
        for line in host.release_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("BOSS_STACK_REF="):
                ref = line.split("=", 1)[1]
    except OSError:
        pass
    if not ref:
        host.out("No local BOSS fixes are installed: BOSS runs its release as it is.")
        return 0
    fixes = stack_fixes(host, ref)
    host.out(f"{len(fixes)} local fix(es) on {ref}:")
    for f in fixes:
        host.out(f"  {f['commit'][:12]}  {f['self_fix_id']}")
    return 0


def cmd_plan_upgrade(host: Host, to_ref: str, as_json: bool, ref: str) -> int:
    fixes = stack_fixes(host, ref) if ref else []
    plan = plan_stack(str(host.dir), to_ref, fixes) if fixes else []
    if as_json:
        print(json.dumps({"to": to_ref, "fixes": plan}))
    else:
        for item in plan:
            host.out(f"  {item['self_fix_id'] or item['commit'][:12]}: {item['outcome']}")
    return 0


def cmd_restack(host: Host, to_ref: str, without: list[str]) -> int:
    """After upgrade.sh moved the checkout to `to_ref`: rebuild the stack on it with the carried
    fixes, as a new stack ref, and record each fix's fate in `.boss/stack-plan.json` (BOSS reads
    it at the next poll). The images are rebuilt only when a fix is carried."""
    old_ref = ""
    try:
        for line in host.release_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("BOSS_STACK_REF="):
                old_ref = line.split("=", 1)[1]
    except OSError:
        pass
    if not old_ref:
        return 0
    old_fixes = _stack_on(host, old_ref)
    plan = plan_stack(str(host.dir), "HEAD", old_fixes)
    for item in plan:
        if item["self_fix_id"] in without and item["outcome"] != "dropped_upstream":
            item["outcome"] = "removed"
    new_ref = f"refs/boss/stack/{to_ref}"
    head = host.release_sha()
    if any(i["outcome"] == "carried" for i in plan):
        host.checkout_stack(head)
        wt = host.worktree()
        for item in plan:
            if item["outcome"] != "carried":
                continue
            done = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "cherry-pick", "-x",
                                   item["commit"]], cwd=str(wt), capture_output=True, text=True,
                                  env={**os.environ, "GIT_COMMITTER_NAME": "BOSS",
                                       "GIT_COMMITTER_EMAIL": "boss@boss.invalid"})
            if done.returncode != 0:
                subprocess.run(["git", "cherry-pick", "--abort"], cwd=str(wt), capture_output=True)
                item["outcome"] = "needs_rework"
                continue
            head = host.git("rev-parse", "HEAD", cwd=wt)
            item["new_commit"] = head
    (host.dir / ".boss").mkdir(exist_ok=True)
    (host.dir / ".boss" / "stack-plan.json").write_text(json.dumps(
        {"release": to_ref, "fixes": plan}, indent=1))
    try:
        # BOSS records each fix's fate (dropped, carried, rework, removed); when it cannot be
        # reached now, .boss/stack-plan.json keeps it for `boss self-fix status`.
        host.call("POST", "/v1/self-fixes/stack-report", {"release": to_ref, "fixes": plan})
    except Refused as error:
        host.out(f"(BOSS did not take the stack plan yet: {error})")
    if head == host.release_sha():
        host.record(new_ref, head)
        host.out("No local fixes carried over; BOSS runs the new release as it is.")
        return 0
    host.git("update-ref", new_ref, head)
    host.keep_previous()
    wt = host.worktree()
    code, said = host.compose("build", compose_file=wt / "scripts/install/community-compose.yml",
                              project_dir=wt / "scripts/install",
                              env={"BOSS_SOURCE_SHA": host.release_sha(), "BOSS_STACK_HEAD": head})
    if code == 0:
        code, said = host.compose("up", "-d", "--no-build", env={"BOSS_STACK_HEAD": head})
    if code != 0:
        host.restore_previous()
        host.compose("up", "-d", "--no-build", env={"BOSS_STACK_HEAD": host.release_sha()})
        host.record(new_ref, host.release_sha())
        host.out(f"The carried fixes did not build or start; the new release runs without them "
                 f"({said[-200:]}).")
        return 1
    host.record(new_ref, head)
    host.out(f"Carried {sum(1 for i in plan if i['outcome'] == 'carried')} local fix(es) onto "
             f"{to_ref}.")
    return 0


def _stack_on(host: Host, ref: str) -> list[dict[str, Any]]:
    head = host.git("rev-parse", "-q", "--verify", f"{ref}^{{commit}}", check=False)
    if not head:
        return []
    base = host.git("merge-base", head, "HEAD", check=False) or head
    log = host.git("log", "--reverse", "--format=%H%x09%(trailers:key=BOSS-Self-Fix,valueonly)",
                   f"{base}..{head}")
    out = []
    for line in log.splitlines():
        commit, _, sfx = line.partition("\t")
        if commit.strip():
            out.append({"commit": commit.strip(), "self_fix_id": sfx.strip()})
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="boss self-fix", description=__doc__.split("\n")[0])
    parser.add_argument("--dir", default=os.environ.get("BOSS_INSTALL_DIR") or
                        str(Path.home() / "boss"))
    parser.add_argument("--api", default=os.environ.get("BOSS_SELF_FIX_API") or None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed")
    watch = sub.add_parser("watch")
    watch.add_argument("--once", action="store_true")
    inst = sub.add_parser("install")
    inst.add_argument("sfx")
    sub.add_parser("status")
    plan = sub.add_parser("plan-upgrade")
    plan.add_argument("--to", required=True)
    plan.add_argument("--ref", default="")
    plan.add_argument("--json", action="store_true")
    restack = sub.add_parser("restack")
    restack.add_argument("--to", required=True)
    restack.add_argument("--without", action="append", default=[])
    args = parser.parse_args(argv)
    host = Host(Path(args.dir), api=args.api)
    if args.cmd == "seed":
        return cmd_seed(host)
    if args.cmd == "watch":
        return cmd_watch(host, args.once)
    if args.cmd == "install":
        return cmd_install(host, args.sfx)
    if args.cmd == "status":
        return cmd_status(host)
    if args.cmd == "plan-upgrade":
        return cmd_plan_upgrade(host, args.to, args.json, args.ref)
    if args.cmd == "restack":
        return cmd_restack(host, args.to, args.without)
    return 2


if __name__ == "__main__":
    sys.exit(main())
