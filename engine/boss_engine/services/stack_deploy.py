"""Deploy an application STACK from its repository to a fleet host — governed, end to end.

Before this, the only thing BOSS could put on a VM was one container it had built itself, and
in Community even that was blocked: SSH deployment bypassed Resource Plane, so there was no
ChangeSet, no budget reservation and no receipt for the write. An operator who wanted BOSS on a
VM did it by hand, which is the opposite of what BOSS is for.

The application now says how it is deployed (`boss.deploy.yaml`, app_deploy_manifest.py) and
BOSS performs it through the same governance as every other provider effect:

    plan     resolve the ref to a commit, read the manifest AT that commit, validate it, ask
             the host read-only questions (reachable? dir writable? docker? sudo?), then record
             a ChangeSet (operation `ssh_host.stack.deploy`), a budget reservation (0 — a
             deploy to your own machine costs BOSS nothing, and the reservation still proves
             the path) and a blocking decision for a person to approve.
    approve  the existing decisions API. Approval approves the ChangeSet and queues a durable
             worker job; a ten-minute install never runs inside anyone's HTTP request.
    apply    the worker hands the leased job to the API, which applies the ChangeSet. INSIDE
             that apply — ChangeSet `applying`, reservation present, receipt written — the
             commit's tree is streamed to the host, the environment written, the manifest's
             steps run, the health checks probed ON the host, and `current` switched.

Host layout (one directory per stack, default /opt/boss-apps/<name>):

    releases/<commit>/     the extracted tree, kept for the last three releases
    current -> releases/<commit>
    .boss/deploy.env       mode 600; every step sources it
    .boss/installed        present after the first healthy deploy: later deploys run `update`

Credentials: the GitHub token is used inside the API container only, to download the tarball;
it never reaches the host or a log. Generated environment values are minted once per (target,
stack), stored through the secret store and reused; their values never appear in a response,
an event or the log tail (every output is redacted by value).

Failure is not rolled back automatically. A half-applied install is diagnosed, not destroyed:
the deployment fails with a headline and the next action, and the console offers a rollback,
which is simply a new governed deployment of the previous healthy commit.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets as _secrets
import shlex
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import quote

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .app_deploy_manifest import (
    DEFAULT_MANIFEST_PATH,
    DeployManifest,
    ManifestError,
    load_manifest,
    render,
)
from .core import create_blocking_decision, emit_event, make_id

log = logging.getLogger(__name__)

OPERATION_SKILL = "ssh_host.stack.deploy"
JOB_KIND = "deployment.stack_apply"
DECISION_ACTION = "deploy_stack"
TARGET_KIND = "stack"

#: A deployment in one of these is still going somewhere; a second plan for the same commit on
#: the same target returns it instead of starting another.
ACTIVE_STATES = ("planned", "awaiting_approval", "running")
TERMINAL_STATES = ("healthy", "failed", "rejected")
#: How much command output a deployment keeps. Enough to read the failing command's own error.
LOG_TAIL_CHARS = 12000
#: Releases kept on the host, the running one included.
KEEP_RELEASES = 3


class StackDeployError(Exception):
    """A plan BOSS refuses, said as {headline, action} — the console shows both."""

    def __init__(self, headline: str, action: str, *, status_code: int = 422):
        super().__init__(headline)
        self.headline = headline
        self.action = action
        self.status_code = status_code

    def to_dict(self) -> dict[str, str]:
        return {"headline": self.headline, "action": self.action}


class StackStepFailed(Exception):
    def __init__(self, step: str, headline: str, action: str):
        super().__init__(headline)
        self.step = step
        self.headline = headline
        self.action = action


# ── GitHub: the commit, the manifest at it, and its tree ─────────────────────────────────────

_SHA = re.compile(r"^[0-9a-f]{40}$")
_PR_REF = re.compile(r"^(?:#|pr[/:#-]?|pull/|refs/pull/)(\d+)(?:/head|/merge)?$", re.IGNORECASE)


def split_repository(repository: str) -> tuple[str, str]:
    value = str(repository or "").strip().removeprefix("https://github.com/")
    value = value.removesuffix(".git").strip("/")
    owner, _, repo = value.partition("/")
    if not owner or not repo or "/" in repo:
        raise StackDeployError(
            f"{repository!r} is not a GitHub repository.",
            "Give it as owner/name, e.g. seanventures/boss.")
    return owner, repo


class GitHubSource:
    """Read-only GitHub access with the repository's own connection token.

    The token lives in this object and in request headers, nowhere else: not in a URL, a
    command line, an exception message or a log line.
    """

    API = "https://api.github.com"

    def __init__(self, token: str = ""):
        self._token = token or ""

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "boss-stack-deploy"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _get(self, path: str, *, accept: str = "application/vnd.github+json"):
        import httpx

        try:
            return httpx.get(f"{self.API}{path}", headers=self._headers(accept), timeout=30,
                             follow_redirects=True)
        except httpx.HTTPError as exc:
            raise StackDeployError(
                f"BOSS could not reach GitHub ({type(exc).__name__}).",
                "Check the API container's network access to api.github.com, then try again.")

    def _refused(self, response, what: str) -> StackDeployError:
        code = response.status_code
        if code in (401, 403):
            return StackDeployError(
                f"GitHub refused access while reading {what} (HTTP {code}).",
                "Re-test the repository's GitHub connection in Setup; the token needs read "
                "access to its contents.")
        if code == 404:
            return StackDeployError(
                f"GitHub has no {what}.",
                "Check the repository name and ref; a private repository also answers 404 "
                "when the connection's token cannot see it.")
        return StackDeployError(f"GitHub answered HTTP {code} while reading {what}.",
                                "Try again in a minute; if it persists, check githubstatus.com.")

    def resolve_commit(self, owner: str, repo: str, ref: str) -> str:
        ref = str(ref or "").strip()
        if not ref:
            raise StackDeployError("No ref was given.",
                                   "Name a branch, a tag, a commit or a pull request (pull/N).")
        pr = _PR_REF.match(ref)
        if pr:
            number = pr.group(1)
            response = self._get(f"/repos/{owner}/{repo}/pulls/{number}")
            if response.status_code != 200:
                raise self._refused(response, f"pull request #{number} in {owner}/{repo}")
            sha = str(((response.json() or {}).get("head") or {}).get("sha") or "")
        else:
            response = self._get(f"/repos/{owner}/{repo}/commits/{quote(ref, safe='')}")
            if response.status_code != 200:
                raise self._refused(response, f"ref {ref!r} in {owner}/{repo}")
            sha = str((response.json() or {}).get("sha") or "")
        if not _SHA.match(sha):
            raise StackDeployError(f"GitHub did not name a commit for {ref!r}.",
                                   "Check the ref and try again.")
        return sha

    def read_file(self, owner: str, repo: str, path: str, commit: str) -> Optional[str]:
        response = self._get(
            f"/repos/{owner}/{repo}/contents/{quote(path.lstrip('/'))}?ref={commit}",
            accept="application/vnd.github.raw")
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise self._refused(response, f"{path} at {commit[:12]}")
        return response.text

    def download_tarball(self, owner: str, repo: str, commit: str, dest: str) -> None:
        import httpx

        try:
            with httpx.stream("GET", f"{self.API}/repos/{owner}/{repo}/tarball/{commit}",
                              headers=self._headers(), timeout=300,
                              follow_redirects=True) as response:
                if response.status_code != 200:
                    raise self._refused(response, f"the tree of {owner}/{repo}@{commit[:12]}")
                with open(dest, "wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
        except httpx.HTTPError as exc:
            raise StackDeployError(
                f"Downloading {owner}/{repo}@{commit[:12]} from GitHub failed "
                f"({type(exc).__name__}).",
                "Try again; if it persists, check the API container's network access.")


def github_source_for(connection: Connection, settings: Settings, project_id: str,
                      owner: str, repo: str) -> GitHubSource:
    """The token this project registered for owner/repo, falling back to its connection."""
    from .github_connections import GitHubConnectionService, github_token_for

    token = None
    try:
        token = GitHubConnectionService(settings).token_for_repository(
            connection, project_id=project_id, owner=owner, repo=repo)
    except Exception:  # noqa: BLE001 — no token is an answer; a public repo needs none
        log.debug("stack deploy: no registered token for %s/%s", owner, repo, exc_info=True)
    if not token:
        token = github_token_for(connection, settings, project_id)
    return GitHubSource(token or "")


# ── The host ─────────────────────────────────────────────────────────────────────────────────

@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return f"{self.stdout or ''}{self.stderr or ''}"


@dataclass
class HostAccess:
    """One host, its user and port, and the host's own key — resolved for this call only."""

    host: str
    user: str
    port: int
    key_material: str = field(repr=False)
    adapter: Any = field(repr=False)

    def run(self, command: str, *, timeout: int = 120, write: bool = False,
            changeset_id: Optional[str] = None, stdin_text: Optional[str] = None,
            stdin_path: Optional[str] = None) -> CommandResult:
        proc = self.adapter.stack_exec(
            host=self.host, user=self.user, port=self.port, key_material=self.key_material,
            command=command, timeout=timeout, write=write, changeset_id=changeset_id,
            stdin_text=stdin_text, stdin_path=stdin_path)
        return CommandResult(int(proc.returncode), str(proc.stdout or ""),
                             str(proc.stderr or ""))


def host_access_for(connection: Connection, settings: Settings, host: dict,
                    adapter: Any = None) -> HostAccess:
    from .secret_backends import SecretUnavailable, resolve_secret_ref
    from .ssh_host_adapter import SSHHostAdapter

    ref = str(host.get("key_secret_ref") or "")
    if not ref:
        raise StackDeployError(
            f"Host {host.get('name')!r} has no SSH key yet.",
            "In Infra, generate the host's key, add the public key to the VM's "
            "~/.ssh/authorized_keys, then Verify.")
    try:
        material = resolve_secret_ref(connection, ref, settings)
    except SecretUnavailable as exc:
        raise StackDeployError(f"Host {host.get('name')!r}'s SSH key cannot be read: {exc}",
                               "Generate a new key for the host in Infra and verify it again.")
    return HostAccess(host=str(host["address"]), user=str(host.get("ssh_user") or "root"),
                      port=int(host.get("ssh_port") or 22), key_material=material,
                      adapter=adapter or SSHHostAdapter(settings))


def preflight_host(access: HostAccess, manifest: DeployManifest) -> list[dict[str, str]]:
    """Read-only questions to the host. Each problem is {headline, action}; none writes."""
    d = shlex.quote(manifest.dir)
    parent = shlex.quote(os.path.dirname(manifest.dir) or "/")
    probe = (
        "echo BOSS_SSH_OK; "
        f"if [ -d {d} ]; then test -w {d} && echo DIR_OK || echo DIR_NOT_WRITABLE; "
        f"elif [ -w {parent} ]; then echo DIR_CREATABLE; "
        f"else p={parent}; while [ ! -d \"$p\" ]; do p=$(dirname \"$p\"); done; "
        "test -w \"$p\" && echo DIR_CREATABLE || echo DIR_NEEDS_SUDO; fi; "
        "sudo -n true >/dev/null 2>&1 && echo SUDO_OK || echo SUDO_NO; "
        "command -v docker >/dev/null 2>&1 && echo DOCKER_PRESENT || echo DOCKER_MISSING; "
        "docker info >/dev/null 2>&1 && echo DOCKER_USABLE || echo DOCKER_UNUSABLE; "
        "command -v tar >/dev/null 2>&1 && echo TAR_OK || echo TAR_MISSING; "
        "command -v curl >/dev/null 2>&1 && echo CURL_OK || echo CURL_MISSING"
    )
    result = access.run(probe, timeout=40)
    out = result.output
    where = f"{access.user}@{access.host}"
    if "BOSS_SSH_OK" not in out:
        return [explain_failure("preflight", result.returncode, out, host=where)]
    problems: list[dict[str, str]] = []
    sudo = "SUDO_OK" in out
    if "DIR_NOT_WRITABLE" in out and not sudo:
        problems.append({
            "headline": (f"{manifest.dir} exists on {access.host} but {access.user} cannot "
                         "write it."),
            "action": f"On the host, run once: sudo chown -R {access.user} {manifest.dir}"})
    if "DIR_NEEDS_SUDO" in out and not sudo:
        problems.append({
            "headline": f"{access.user} cannot create {manifest.dir} on {access.host} and has no "
                        "passwordless sudo.",
            "action": f"On the host, run once: sudo mkdir -p {manifest.dir} && "
                      f"sudo chown {access.user} {manifest.dir}"})
    if manifest.requires_sudo and not sudo:
        problems.append({
            "headline": f"The manifest needs sudo and {access.user} on {access.host} has no "
                        "passwordless sudo.",
            "action": f"Grant {access.user} passwordless sudo on the host, or connect the host "
                      "as a user that has it."})
    if manifest.requires_docker:
        if "DOCKER_MISSING" in out:
            problems.append({
                "headline": f"Docker is not installed on {access.host}, and the manifest needs it.",
                "action": "Install Docker on the host (for example `curl -fsSL "
                          "https://get.docker.com | sh`), then plan the deployment again."})
        elif "DOCKER_UNUSABLE" in out and not sudo:
            problems.append({
                "headline": f"{access.user} on {access.host} cannot talk to the Docker daemon.",
                "action": f"On the host: sudo usermod -aG docker {access.user}, then reconnect."})
    if "TAR_MISSING" in out:
        problems.append({"headline": f"tar is not installed on {access.host}.",
                         "action": "Install tar on the host (apt-get install -y tar)."})
    if manifest.health and "CURL_MISSING" in out:
        problems.append({
            "headline": f"curl is not installed on {access.host}; health checks run there.",
            "action": "Install curl on the host (apt-get install -y curl)."})
    return problems


# ── Failures, in words ───────────────────────────────────────────────────────────────────────

def explain_failure(step: str, returncode: int, output: str, *,
                    host: str = "the host", url: str = "", timeout: int = 0) -> dict[str, str]:
    """{headline, action} for a failed step — the model_failures shape, for a host."""
    lowered = (output or "").lower()
    if returncode == 127 and "ssh` client" in lowered:
        return {"headline": "BOSS cannot run ssh where it is installed.",
                "action": "Install openssh-client in the API image and restart BOSS."}
    if "boss_ssh_host_deploy_allowed" in lowered or "configured not to reach out" in lowered:
        return {"headline": "BOSS is configured not to reach out over SSH.",
                "action": "Set BOSS_SSH_HOST_DEPLOY_ALLOWED=true for the API and restart it."}
    if "permission denied (publickey" in lowered or "host key verification failed" in lowered:
        return {"headline": f"{host} refused BOSS's SSH key.",
                "action": "In Infra, open the host, add the public key it shows to "
                          "~/.ssh/authorized_keys on the VM, then Verify."}
    if any(m in lowered for m in ("could not resolve hostname", "connection refused",
                                  "connection timed out", "no route to host",
                                  "operation timed out", "network is unreachable")):
        return {"headline": f"BOSS could not connect to {host}.",
                "action": "Check the address and that port 22 is open to BOSS, then Verify the "
                          "host in Infra."}
    if "boss_dir_not_writable" in lowered:
        return {"headline": f"The deploy directory is not writable on {host}.",
                "action": "On the host, chown the directory to the SSH user (or grant "
                          "passwordless sudo), then deploy again."}
    if "no space left on device" in lowered:
        return {"headline": f"{host} ran out of disk space during {step}.",
                "action": "Free space on the host (docker system prune, old releases), then "
                          "deploy again."}
    if step == "health":
        return {"headline": f"The app did not answer as expected at {url} within {timeout}s.",
                "action": "Read the log tail for what the install printed, fix it and deploy "
                          "again — or roll back to the previous healthy commit."}
    if returncode == 124 or "did not finish within" in lowered:
        return {"headline": f"The step '{step}' timed out on {host}.",
                "action": "Raise timeout_seconds for that step in boss.deploy.yaml, or read the "
                          "log tail for where it stalled."}
    if re.search(r"docker: (command )?not found", lowered):
        return {"headline": f"Docker is not installed on {host}.",
                "action": "Install Docker on the host, then deploy again."}
    if "permission denied while trying to connect to the docker daemon" in lowered:
        return {"headline": f"The SSH user cannot use Docker on {host}.",
                "action": "Add the user to the docker group on the host, then deploy again."}
    if "sudo: a password is required" in lowered or "sudo: a terminal is required" in lowered:
        return {"headline": f"A step needed sudo and the SSH user has no passwordless sudo on "
                            f"{host}.",
                "action": "Grant passwordless sudo to that user, or remove sudo from the step."}
    return {"headline": f"The step '{step}' failed on {host} (exit {returncode}).",
            "action": "Read the log tail for the command's own error, fix it in the repository "
                      "and deploy again — or roll back to the previous healthy commit."}


def redact(output: str, values: list[str]) -> str:
    """Remove every secret VALUE from text before it is stored or shown."""
    cleaned = output or ""
    for value in sorted({v for v in values if v and len(v) >= 4}, key=len, reverse=True):
        cleaned = cleaned.replace(value, "[redacted]")
    return cleaned


def _env_file(values: dict[str, str]) -> str:
    lines = ["# Written by BOSS for this deployment. Mode 600. Do not edit by hand: the next",
             "# deploy rewrites it from boss.deploy.yaml and BOSS's secret store."]
    for key, value in values.items():
        lines.append(f"{key}='" + str(value).replace("'", "'\"'\"'") + "'")
    return "\n".join(lines) + "\n"


# ── The apply itself (no database in here: records go through `record`) ─────────────────────

class StackApplier:
    """Run one deployment's steps on the host, reporting each as it happens.

    Database-free on purpose: `record(steps, log_tail)` is how progress leaves, so the order
    of host writes, their redaction and their failure handling are testable with a fake host.
    """

    def __init__(self, *, access: HostAccess, rendered: dict[str, Any], commit: str, ref: str,
                 env_values: dict[str, str], redact_values: list[str], changeset_id: str,
                 fetch_tree: Callable[[str], None],
                 record: Callable[[list[dict[str, Any]], str], None],
                 now: Callable[[], str]):
        self.access = access
        self.rendered = rendered
        self.commit = commit
        self.ref = ref
        self.env_values = env_values
        self.redact_values = list(redact_values)
        self.changeset_id = changeset_id
        self.fetch_tree = fetch_tree
        self.record = record
        self.now = now
        self.dir = rendered["dir"]
        self.log = ""
        self.steps: list[dict[str, Any]] = []
        self.mode = "install"

    # -- bookkeeping ---------------------------------------------------------------------------
    def _plan_steps(self, commands: list[dict[str, Any]]) -> None:
        names = ["prepare", "upload", "environment", "switch"]
        names += [f"{self.mode}: {c['name']}" for c in commands]
        if self.rendered["health"]:
            names.append("health")
        names.append("finalize")
        done = {s["name"]: s for s in self.steps}
        self.steps = [done.get(n) or {"name": n, "status": "pending", "started_at": None,
                                      "ended_at": None} for n in names]

    def _step(self, name: str) -> dict[str, Any]:
        for step in self.steps:
            if step["name"] == name:
                return step
        step = {"name": name, "status": "pending", "started_at": None, "ended_at": None}
        self.steps.append(step)
        return step

    def _append(self, text_: str) -> None:
        self.log = redact((self.log + text_)[-LOG_TAIL_CHARS * 2:], self.redact_values)
        self.log = self.log[-LOG_TAIL_CHARS:]

    def _start(self, name: str) -> None:
        step = self._step(name)
        step.update(status="running", started_at=self.now())
        self._append(f"\n$ [{name}]\n")
        self.record(self.steps, self.log)

    def _finish(self, name: str, status: str) -> None:
        self._step(name).update(status=status, ended_at=self.now())
        self.record(self.steps, self.log)

    def _write(self, name: str, command: str, *, timeout: int = 300,
               stdin_text: Optional[str] = None, stdin_path: Optional[str] = None,
               ok_marker: Optional[str] = None) -> CommandResult:
        result = self.access.run(command, timeout=timeout, write=True,
                                 changeset_id=self.changeset_id, stdin_text=stdin_text,
                                 stdin_path=stdin_path)
        self._append(result.output[-6000:])
        failed = result.returncode != 0 or (ok_marker is not None and ok_marker not in
                                            result.output)
        if failed:
            self._finish(name, "failed")
            said = explain_failure(name, result.returncode, result.output,
                                   host=f"{self.access.user}@{self.access.host}",
                                   timeout=timeout)
            raise StackStepFailed(name, said["headline"], said["action"])
        return result

    # -- the steps ----------------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        d = shlex.quote(self.dir)
        release = f"{self.dir}/releases/{self.commit}"
        r = shlex.quote(release)
        self._plan_steps(self.rendered["install"])

        self._start("prepare")
        prepared = self._write("prepare", (
            "set -e; "
            f"if [ ! -d {d} ]; then mkdir -p {d} 2>/dev/null || sudo -n mkdir -p {d}; fi; "
            f"test -w {d} || sudo -n chown \"$(id -un)\" {d} 2>/dev/null || true; "
            f"test -w {d} || {{ echo BOSS_DIR_NOT_WRITABLE {d}; exit 3; }}; "
            f"mkdir -p {d}/releases {d}/.boss; chmod 700 {d}/.boss; "
            f"if [ -f {d}/.boss/installed ]; then echo BOSS_MODE=update; "
            "else echo BOSS_MODE=install; fi; "
            f"if [ -f {r}/.boss-release-complete ]; then echo BOSS_RELEASE_PRESENT; fi; "
            "echo BOSS_PREPARED"), timeout=60, ok_marker="BOSS_PREPARED")
        self.mode = "update" if "BOSS_MODE=update" in prepared.output else "install"
        commands = self.rendered[self.mode]
        self._finish("prepare", "succeeded")
        self._plan_steps(commands)
        self.record(self.steps, self.log)

        self._start("upload")
        if "BOSS_RELEASE_PRESENT" in prepared.output:
            self._append("release already on the host; not uploaded again\n")
            self._finish("upload", "skipped")
        else:
            fd, tarball = tempfile.mkstemp(suffix=".tar.gz")
            os.close(fd)
            try:
                try:
                    self.fetch_tree(tarball)
                except StackDeployError as exc:
                    self._finish("upload", "failed")
                    raise StackStepFailed("upload", exc.headline, exc.action)
                partial = shlex.quote(f"{release}.partial")
                self._write("upload", (
                    "set -e; "
                    f"rm -rf {partial}; mkdir -p {partial}; "
                    f"tar -xzf - -C {partial} --strip-components=1; "
                    f"rm -rf {r}; mv {partial} {r}; touch {r}/.boss-release-complete; "
                    "echo BOSS_UPLOADED"), timeout=900, stdin_path=tarball,
                    ok_marker="BOSS_UPLOADED")
            finally:
                try:
                    os.unlink(tarball)
                except OSError:
                    pass
            self._finish("upload", "succeeded")

        self._start("environment")
        env_path = shlex.quote(f"{self.dir}/.boss/deploy.env")
        self._write("environment", (
            "set -e; umask 077; "
            f"cat > {env_path}.tmp; mv {env_path}.tmp {env_path}; chmod 600 {env_path}; "
            "echo BOSS_ENV_WRITTEN"), timeout=60, stdin_text=_env_file(self.env_values),
            ok_marker="BOSS_ENV_WRITTEN")
        self._finish("environment", "succeeded")

        self._start("switch")
        self._write("switch", (
            "set -e; "
            f"if [ -L {d}/current ]; then readlink {d}/current > {d}/.boss/previous; fi; "
            f"ln -sfn releases/{shlex.quote(self.commit)} {d}/current.next; "
            f"mv -Tf {d}/current.next {d}/current; echo BOSS_SWITCHED"),
            timeout=60, ok_marker="BOSS_SWITCHED")
        self._finish("switch", "succeeded")

        for command in commands:
            name = f"{self.mode}: {command['name']}"
            self._start(name)
            limit = int(command["timeout_seconds"])
            self._write(name, (
                f"cd {d}/current && set -a && . {env_path} && set +a && "
                f"timeout {limit} bash -lc {shlex.quote(command['run'])}"),
                timeout=limit + 60)
            self._finish(name, "succeeded")

        for check in self.rendered["health"]:
            self._start("health")
            url = shlex.quote(check["url"])
            expect = int(check["expect"])
            limit = int(check["timeout_seconds"])
            probe = (
                f"deadline=$(( $(date +%s) + {limit} )); code=000; "
                "while :; do "
                f"code=$(curl -s -o /dev/null -w '%{{http_code}}' --max-time 10 {url} || true); "
                f"if [ \"$code\" = \"{expect}\" ]; then echo BOSS_HEALTHY $code; exit 0; fi; "
                "if [ $(date +%s) -ge $deadline ]; then echo BOSS_UNHEALTHY $code; exit 1; fi; "
                "sleep 5; done")
            result = self.access.run(probe, timeout=limit + 60, write=False)
            self._append(result.output[-2000:])
            if result.returncode != 0 or "BOSS_HEALTHY" not in result.output:
                self._finish("health", "failed")
                said = explain_failure("health", result.returncode, result.output,
                                       host=self.access.host, url=check["url"], timeout=limit)
                raise StackStepFailed("health", said["headline"], said["action"])
        if self.rendered["health"]:
            self._finish("health", "succeeded")

        self._start("finalize")
        keep = KEEP_RELEASES - 1
        self._write("finalize", (
            "set -e; "
            f"touch {d}/.boss/installed; echo {shlex.quote(self.commit)} > {d}/.boss/last_healthy; "
            f"cd {d}/releases && ls -1t | grep -v -x -F {shlex.quote(self.commit)} "
            f"| grep -v '[.]partial$' | tail -n +{keep + 1} | xargs -r rm -rf --; "
            "echo BOSS_FINALIZED"), timeout=300, ok_marker="BOSS_FINALIZED")
        self._finish("finalize", "succeeded")
        return {"mode": self.mode, "steps": self.steps, "log_tail": self.log,
                "open_url": self.rendered["open_url"]}


# ── Records ──────────────────────────────────────────────────────────────────────────────────

_COLUMNS = (
    "id, project_id, target_id, host_id, repository, ref, commit_sha, manifest, status, "
    "decision_id, changeset_id, reservation_id, job_id, steps, log_tail, open_url, error, "
    "failure_reason, preflight, workflow_run_id, code_task_run_id, rollback_of, "
    "apply_started_at, created_by, created_at, updated_at, finished_at"
)


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def serialize(row: Any) -> dict[str, Any]:
    """The `deployment` the API returns. No secret, no manifest text, no token."""
    if row is None:
        return {}
    data = dict(row)
    manifest = _json(data.get("manifest")) or {}
    return {
        "id": data["id"],
        "project_id": data["project_id"],
        "target_id": data["target_id"],
        "host_id": data.get("host_id"),
        "repository": data["repository"],
        "ref": data["ref"],
        "commit": data["commit_sha"],
        "manifest": manifest,
        "status": data["status"],
        "decision_id": data.get("decision_id"),
        "changeset_id": data.get("changeset_id"),
        "steps": _json(data.get("steps")) or [],
        "log_tail": data.get("log_tail") or "",
        "open_url": data.get("open_url"),
        "error": _json(data.get("error")) or None,
        "preflight": _json(data.get("preflight")) or {},
        "workflow_run_id": data.get("workflow_run_id"),
        "code_task_run_id": data.get("code_task_run_id"),
        "rollback_of": data.get("rollback_of"),
        "created_by": data.get("created_by"),
        "created_at": _iso(data.get("created_at")),
        "updated_at": _iso(data.get("updated_at")),
        "finished_at": _iso(data.get("finished_at")),
    }


def _row(connection: Connection, deployment_id: str, *, lock: bool = False):
    return connection.execute(
        text(f"SELECT {_COLUMNS}, manifest_text FROM stack_deployments WHERE id = :i"
             + (" FOR UPDATE" if lock else "")),
        {"i": deployment_id}).mappings().first()


def get_stack_deployment(connection: Connection, deployment_id: str) -> Optional[dict]:
    row = _row(connection, deployment_id)
    return serialize(row) if row else None


def list_stack_deployments(connection: Connection, project_id: str, *,
                           target_id: Optional[str] = None, limit: int = 50) -> list[dict]:
    rows = connection.execute(
        text(f"SELECT {_COLUMNS} FROM stack_deployments WHERE project_id = :p "
             "AND (CAST(:t AS TEXT) IS NULL OR target_id = :t) "
             "ORDER BY created_at DESC LIMIT :n"),
        {"p": project_id, "t": target_id, "n": max(1, min(int(limit), 200))}).mappings().all()
    return [serialize(r) for r in rows]


def _set(connection: Connection, deployment_id: str, **fields: Any) -> None:
    sets, params = [], {"i": deployment_id}
    for key, value in fields.items():
        if key in ("steps", "error", "manifest", "preflight"):
            sets.append(f"{key} = CAST(:{key} AS JSONB)")
            params[key] = json.dumps(value) if value is not None else None
        elif value == "__now__":
            sets.append(f"{key} = now()")
        else:
            sets.append(f"{key} = :{key}")
            params[key] = value
    connection.execute(
        text(f"UPDATE stack_deployments SET {', '.join(sets)}, updated_at = now() "
             "WHERE id = :i"), params)


# ── Stack targets ────────────────────────────────────────────────────────────────────────────

def is_stack_target(target: Optional[dict]) -> bool:
    return bool(target) and str(target.get("target_kind") or "").lower() == TARGET_KIND


def _target(connection: Connection, target_id: str) -> Optional[dict]:
    row = connection.execute(text("SELECT * FROM deployment_targets WHERE id = :i"),
                             {"i": target_id}).mappings().first()
    return dict(row) if row else None


def create_stack_target(connection: Connection, *, project_id: str, host_id: str,
                        app_name: str, repository: str, environment: str = "production",
                        manifest_path: Optional[str] = None, public_url: Optional[str] = None,
                        actor: str) -> dict[str, Any]:
    """Where a stack is deployed: a repository, on a host, for an environment.

    `target_ref` is derived from the host, as every fleet target's is; a stack target never
    carries an address of its own.
    """
    from .deployment_fleet import get_host, target_ref_for

    owner, repo = split_repository(repository)
    host = get_host(connection, host_id)
    if not host or host.get("project_id") != project_id:
        raise StackDeployError("No such host in this project.",
                               "Add the VM in Infra first, then create the target on it.",
                               status_code=404)
    if host.get("status") == "retired":
        raise StackDeployError(f"Host {host['name']!r} is retired.",
                               "Pick an active host, or register the VM again.")
    name = str(app_name or "").strip() or repo
    environment = str(environment or "production").strip() or "production"
    path = str(manifest_path or "").strip().lstrip("/") or None
    if path and (".." in path.split("/") or not re.match(r"^[A-Za-z0-9._/-]+$", path)):
        raise StackDeployError(f"manifest_path {path!r} is not a path inside the repository.",
                               "Leave it blank for boss.deploy.yaml at the repository root.")
    url = str(public_url or "").strip() or None
    if url and not re.match(r"^https?://", url):
        raise StackDeployError(f"public_url {url!r} must start with http:// or https://.",
                               "Give the address people open the app at.")
    clash = connection.execute(
        text("SELECT id, status FROM deployment_targets "
             "WHERE project_id = :p AND app_name = :a AND environment = :e"),
        {"p": project_id, "a": name, "e": environment}).mappings().first()
    if clash:
        raise StackDeployError(
            f"{name} already has a {environment} target ({clash['id']}, {clash['status']}).",
            "Use that target, or choose another name or environment.", status_code=409)
    target_id = make_id("deptgt")
    connection.execute(
        text("INSERT INTO deployment_targets "
             "(id, project_id, app_name, repo, environment, target_kind, target_ref, domains, "
             " required_resources, secret_refs, health_checks, status, created_by, "
             " host_id, manifest_path, public_url, created_at, updated_at) "
             "VALUES (:i, :p, :a, :r, :e, :k, :ref, CAST(:dom AS JSONB), "
             "        CAST(:req AS JSONB), '[]'::jsonb, '[]'::jsonb, 'active', :by, "
             "        :h, :mp, :url, now(), now())"),
        {"i": target_id, "p": project_id, "a": name, "r": f"{owner}/{repo}", "e": environment,
         "k": TARGET_KIND, "ref": target_ref_for(host),
         "dom": json.dumps([url] if url else []),
         "req": json.dumps([{"kind": "repo_owner", "name": owner}]),
         "by": actor, "h": host_id, "mp": path, "url": url})
    emit_event(connection, project_id, "deployment.stack_target_created", actor,
               "deployment_target", target_id,
               {"app_name": name, "environment": environment, "repository": f"{owner}/{repo}",
                "host": host["name"]})
    return _target(connection, target_id) or {}


# ── Environment values ───────────────────────────────────────────────────────────────────────

_GENERATE = {
    "password": lambda: _secrets.token_urlsafe(24),
    "hex32": lambda: _secrets.token_hex(32),
    "token": lambda: _secrets.token_urlsafe(32),
}


def _generated_secret_name(stack: str, key: str) -> str:
    return f"stack/{stack}/{key}"


def _find_secret_ref(connection: Connection, project_id: str, keys: list[str]) -> Optional[str]:
    row = connection.execute(
        text("SELECT id FROM secret_refs WHERE project_id = :p AND status = 'active' "
             "AND ref_key = ANY(:k) ORDER BY updated_at DESC LIMIT 1"),
        {"p": project_id, "k": keys}).first()
    return str(row[0]) if row else None


def operator_secret_ref(connection: Connection, project_id: str, name: str) -> Optional[str]:
    """The secret an operator set in BOSS under `name` (project scope), or None."""
    from .secrets_service import ref_key_for

    return _find_secret_ref(connection, project_id, [ref_key_for(project_id, name), name])


def ensure_generated_secret(engine, settings: Settings, *, project_id: str, target_id: str,
                            stack: str, key: str, generator: str,
                            actor: str = "boss") -> str:
    """The value for a `{generate: ...}` env key: minted ONCE per (target, stack), then reused.

    Written in its own committed transaction BEFORE it is used, so a deploy that dies halfway
    cannot leave a host initialised with a password BOSS no longer has.
    """
    from .secret_backends import durable_secret_backend, resolve_secret_ref
    from .secrets_service import put_secret, ref_key_for

    name = _generated_secret_name(stack, key)
    with engine.begin() as connection:
        existing = _find_secret_ref(connection, project_id, [ref_key_for(target_id, name)])
        if existing:
            return resolve_secret_ref(connection, existing, settings)
        value = _GENERATE[generator]()
        stored = put_secret(connection, settings, project_id=project_id, name=name,
                            value=value, secret_kind="api_token",
                            backend=durable_secret_backend(settings),
                            scope_type="deployment_target",
                            scope_id=target_id, actor=actor)
        # Not even the last four characters of a generated credential are kept in metadata.
        connection.execute(
            text("UPDATE secret_refs SET metadata = metadata - 'last4' WHERE id = :i"),
            {"i": stored["secret_ref_id"]})
        return value


def _environment(connection: Connection, settings: Settings, *, deployment: dict,
                 manifest: DeployManifest, rendered: dict[str, Any]
                 ) -> tuple[dict[str, str], list[str]]:
    from .secret_backends import resolve_secret_ref

    values: dict[str, str] = {
        "BOSS_DEPLOY_COMMIT": deployment["commit_sha"],
        "BOSS_DEPLOY_REF": deployment["ref"],
        "BOSS_DEPLOY_DIR": manifest.dir,
    }
    hidden: list[str] = []
    for item in manifest.env:
        if item.source == "literal":
            values[item.key] = rendered["env_literals"][item.key]
        elif item.source == "generate":
            value = ensure_generated_secret(
                connection.engine, settings, project_id=deployment["project_id"],
                target_id=deployment["target_id"], stack=manifest.name, key=item.key,
                generator=item.value)
            values[item.key] = value
            hidden.append(value)
        else:
            ref = operator_secret_ref(connection, deployment["project_id"], item.value)
            if not ref:
                raise StackStepFailed(
                    "environment", f"The secret {item.value!r} ({item.key}) is not set in BOSS.",
                    f"Set a project secret named {item.value!r} in BOSS, then deploy again.")
            value = resolve_secret_ref(connection, ref, settings)
            values[item.key] = value
            hidden.append(value)
    return values, hidden


# ── Plan ─────────────────────────────────────────────────────────────────────────────────────

def _deployment_ctx(target: dict, host: dict, commit: str, ref: str) -> dict[str, str]:
    return {"commit": commit, "ref": ref, "target.host": str(host["address"]),
            "target.user": str(host.get("ssh_user") or "root"),
            "target.public_url": str(target.get("public_url") or
                                     f"http://{host['address']}")}


def plan_stack_deploy(engine, settings: Settings, *, project_id: str, target_id: str,
                      repository: Optional[str], ref: str, actor: str,
                      run_id: Optional[str] = None, workflow_run_id: Optional[str] = None,
                      rollback_of: Optional[str] = None) -> dict[str, Any]:
    """Record a deployment of `repository@ref` to a stack target, held for approval.

    Touches the host only to ASK (one read-only ssh session). Raises StackDeployError with a
    {headline, action} for everything an operator can fix before anything is approved.
    """
    from .budget import BudgetGovernor
    from .deployment_fleet import get_host
    from .resource_plane import ResourcePlaneService

    with engine.begin() as connection:
        target = _target(connection, target_id)
        if not target or target.get("project_id") != project_id:
            raise StackDeployError("No such deployment target in this project.",
                                   "Pick a stack target in Infra.", status_code=404)
        if not is_stack_target(target):
            raise StackDeployError(
                f"Target {target.get('app_name')!r} is a {target.get('target_kind')!r} target, "
                "not a stack target.",
                "Create a stack target for the repository on the host (Infra), then deploy.")
        if target.get("status") != "active":
            raise StackDeployError(f"Target {target.get('app_name')!r} is {target.get('status')}.",
                                   "Restore the target, or deploy to an active one.")
        host = get_host(connection, str(target.get("host_id") or "")) if target.get("host_id") \
            else None
        if not host:
            raise StackDeployError("This target is not attached to a host.",
                                   "Attach it to a verified host in Infra.")
        if host.get("status") != "verified":
            raise StackDeployError(
                f"Host {host['name']!r} is {host.get('status')!r}, not verified.",
                "In Infra, add the host's public key to the VM and press Verify.")
        repository = str(repository or target.get("repo") or "").strip()
        owner, repo = split_repository(repository)
        target_repo = str(target.get("repo") or "")
        if "/" in target_repo and target_repo.lower() != f"{owner}/{repo}".lower():
            raise StackDeployError(
                f"This target deploys {target_repo}, not {owner}/{repo}.",
                "Deploy the target's own repository, or create a target for this one.")
        github = github_source_for(connection, settings, project_id, owner, repo)
        access = host_access_for(connection, settings, host)

    commit = github.resolve_commit(owner, repo, ref)

    with engine.begin() as connection:
        running = connection.execute(
            text(f"SELECT {_COLUMNS} FROM stack_deployments WHERE target_id = :t "
                 "AND commit_sha = :c AND status = ANY(:s) ORDER BY created_at DESC LIMIT 1"),
            {"t": target_id, "c": commit, "s": list(ACTIVE_STATES)}).mappings().first()
        if running:
            # The same commit to the same place is ONE deployment. A second click, a retried
            # workflow step or two operators at once must not queue a second install.
            return {**serialize(running), "reused": True}

    path = str(target.get("manifest_path") or DEFAULT_MANIFEST_PATH)
    manifest_text = github.read_file(owner, repo, path, commit)
    if manifest_text is None:
        raise StackDeployError(
            f"{owner}/{repo} has no {path} at {commit[:12]}.",
            f"Add {path} to the repository (version: 1, name, install, health — see "
            "engine/boss_engine/services/app_deploy_manifest.py), or set the target's "
            "manifest path.")
    try:
        manifest = load_manifest(manifest_text)
        rendered = render(manifest, _deployment_ctx(target, host, commit, ref))
    except ManifestError as exc:
        raise StackDeployError(f"{path} at {commit[:12]} is not valid: {exc}",
                               f"Fix {path} in the repository and deploy again.")

    with engine.begin() as connection:
        missing = [f"{key} → {name}" for key, name in rendered["env_secrets"].items()
                   if not operator_secret_ref(connection, project_id, name)]
    if missing:
        raise StackDeployError(
            f"The manifest reads secrets BOSS does not have: {', '.join(missing)}.",
            "Set each as a project secret in BOSS under that name, then deploy again.")

    problems = preflight_host(access, manifest)
    if problems:
        first = problems[0]
        more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
        raise StackDeployError(first["headline"] + more, first["action"])

    deployment_id = make_id("sdep")
    short = commit[:12]
    summary = manifest.summary()
    rp = ResourcePlaneService(settings)
    with engine.begin() as connection:
        reservation = BudgetGovernor(settings).reserve(
            connection, project_id, "provider", 0,
            f"Stack deployment {deployment_id}: {owner}/{repo}@{short} to {host['name']}",
            expires_in_minutes=7 * 24 * 60, risk_tier=2)
        if reservation.get("status") == "denied" or not reservation.get("reservation_id"):
            raise StackDeployError(
                "The budget refused to record this deployment's reservation.",
                str(reservation.get("message") or "Review the budget decision in Decisions."))
        desired = {
            "deployment_id": deployment_id, "deployment_target_id": target_id,
            "target_kind": TARGET_KIND, "environment": target.get("environment"),
            "host_id": host["id"], "host": host["address"],
            "user": host.get("ssh_user") or "root", "port": int(host.get("ssh_port") or 22),
            "repository": f"{owner}/{repo}", "ref": ref, "commit": commit,
            "manifest_path": path, "stack": manifest.name, "dir": manifest.dir,
        }
        changeset = rp.create_changeset(
            connection, project_id=project_id, provider_account_id=None,
            title=f"Deploy {owner}/{repo}@{short} to {host['name']}",
            summary=(f"Run {manifest.name}'s boss.deploy.yaml from {owner}/{repo}@{short} on "
                     f"{host.get('ssh_user') or 'root'}@{host['address']} in {manifest.dir}."),
            operations=[{"skill_id": OPERATION_SKILL, "action": "deploy",
                         "resource_type": "ssh_host.stack",
                         "resource_ref": f"{target_id}:{manifest.name}",
                         "desired_state": desired, "risk_tier": 1}],
            expected_cost_usd_micros=0, reservation_id=reservation["reservation_id"],
            rollback_plan=[f"Deploy the previous healthy commit to {target.get('app_name')} "
                           "through the same governed path (Rollback in the console)."],
            risk_tier=1, approval_required=False, actor=actor)
        decision_id = create_blocking_decision(
            connection, project_id, None,
            f"Deploy {owner}/{repo}@{short} to {host['name']} ({manifest.dir})?",
            {"action": "deploy_stack", "deployment_id": deployment_id,
             "changeset_id": changeset["id"], "target_id": target_id,
             "repository": f"{owner}/{repo}", "ref": ref, "commit": commit,
             "host": host["name"], "dir": manifest.dir, "steps": summary["steps"],
             "env_keys": summary["env_keys"]},
            {"expected_cost_usd_micros": 0, "reservation_id": reservation["reservation_id"],
             "cost_confidence": "no_provider_spend"},
            "stack_deploy_requires_approval", risk_tier=2, default_action="hold")
        # The deploy is a tier-2 effect on somebody's machine, so its ChangeSet says so and
        # waits on THIS decision — the one whose approval queues the apply.
        connection.execute(
            text("UPDATE changesets SET status = 'pending_approval', risk_tier = 2, "
                 "approval_decision_id = :d, updated_at = now() WHERE id = :c"),
            {"d": decision_id, "c": changeset["id"]})
        connection.execute(
            text("UPDATE changeset_operations SET risk_tier = 2 WHERE changeset_id = :c"),
            {"c": changeset["id"]})
        connection.execute(
            text("UPDATE decisions SET external_ref = :c WHERE id = :d"),
            {"c": changeset["id"], "d": decision_id})
        steps = [{"name": n, "status": "pending", "started_at": None, "ended_at": None}
                 for n in ["prepare", "upload", "environment", "switch"]
                 + [f"install or update: {s}" for s in summary["steps"]["install"]]
                 + (["health"] if manifest.health else []) + ["finalize"]]
        connection.execute(
            text("INSERT INTO stack_deployments "
                 "(id, project_id, target_id, host_id, repository, ref, commit_sha, manifest, "
                 " manifest_text, status, decision_id, changeset_id, reservation_id, steps, "
                 " log_tail, open_url, preflight, workflow_run_id, code_task_run_id, "
                 " rollback_of, created_by) "
                 "VALUES (:i, :p, :t, :h, :repo, :ref, :c, CAST(:m AS JSONB), :mt, "
                 "        'awaiting_approval', :d, :cs, :res, CAST(:steps AS JSONB), '', :open, "
                 "        CAST(:pf AS JSONB), :wr, :cr, :rb, :by)"),
            {"i": deployment_id, "p": project_id, "t": target_id, "h": host["id"],
             "repo": f"{owner}/{repo}", "ref": ref, "c": commit, "m": json.dumps(summary),
             "mt": manifest_text, "d": decision_id, "cs": changeset["id"],
             "res": reservation["reservation_id"], "steps": json.dumps(steps),
             "open": rendered["open_url"], "pf": json.dumps({"ok": True, "host": host["name"]}),
             "wr": workflow_run_id, "cr": run_id, "rb": rollback_of, "by": actor})
        emit_event(connection, project_id, "deployment.stack_planned", actor,
                   "stack_deployment", deployment_id,
                   {"target_id": target_id, "repository": f"{owner}/{repo}", "ref": ref,
                    "commit": commit, "changeset_id": changeset["id"],
                    "decision_id": decision_id, "workflow_run_id": workflow_run_id})
        return serialize(_row(connection, deployment_id))


def rollback_stack_deploy(engine, settings: Settings, *, deployment_id: str,
                          actor: str) -> dict[str, Any]:
    """Plan a NEW governed deployment of the commit that was healthy before this one."""
    with engine.begin() as connection:
        row = _row(connection, deployment_id)
        if not row:
            raise StackDeployError("No such deployment.", "Pick one from the target's list.",
                                   status_code=404)
        previous = connection.execute(
            text("SELECT commit_sha FROM stack_deployments WHERE target_id = :t "
                 "AND status = 'healthy' AND commit_sha <> :c AND created_at < :at "
                 "ORDER BY created_at DESC LIMIT 1"),
            {"t": row["target_id"], "c": row["commit_sha"], "at": row["created_at"]}).first()
    if not previous:
        raise StackDeployError(
            "There is no earlier healthy commit on this target to roll back to.",
            "Fix forward: deploy a commit that works.")
    return plan_stack_deploy(engine, settings, project_id=row["project_id"],
                             target_id=row["target_id"], repository=row["repository"],
                             ref=str(previous[0]), actor=actor, rollback_of=deployment_id)


# ── Approval (DecisionService follow-on) ─────────────────────────────────────────────────────

def on_decision_approved(connection: Connection, settings: Settings, decision: dict,
                         actor: str) -> Optional[dict[str, Any]]:
    """Approving the decision approves the ChangeSet and queues the apply as a worker job."""
    from .jobs import JOB_PRIORITY_INTERACTIVE, enqueue_job
    from .resource_plane import ResourcePlaneService

    rec = decision.get("recommendation") or {}
    deployment_id = str(rec.get("deployment_id") or "")
    row = _row(connection, deployment_id, lock=True) if deployment_id else None
    if not row or row["status"] != "awaiting_approval":
        return None
    changeset = ResourcePlaneService(settings).approve_changeset(
        connection, changeset_id=row["changeset_id"], actor=actor)
    if changeset.get("status") != "approved":
        # Dual control still waiting on another approver role: nothing runs yet.
        return None
    job = enqueue_job(
        connection, row["project_id"], JOB_KIND,
        {"deployment_id": deployment_id, "changeset_id": row["changeset_id"], "actor": actor},
        priority=JOB_PRIORITY_INTERACTIVE, max_attempts=5,
        dedupe_key=f"stack-deploy:{deployment_id}", data_boundary="internal")
    _set(connection, deployment_id, status="running", job_id=job.get("id"))
    emit_event(connection, row["project_id"], "deployment.stack_approved", actor,
               "stack_deployment", deployment_id,
               {"decision_id": decision.get("id"), "changeset_id": row["changeset_id"],
                "job_id": job.get("id")})
    return job


def on_decision_not_approved(connection: Connection, settings: Settings, decision: dict,
                             actor: str, outcome: str) -> None:
    """A refused deployment closes its ChangeSet and hands back its reservation."""
    from .budget import BudgetGovernor

    rec = decision.get("recommendation") or {}
    deployment_id = str(rec.get("deployment_id") or "")
    row = _row(connection, deployment_id, lock=True) if deployment_id else None
    if not row or row["status"] != "awaiting_approval":
        return
    _set(connection, deployment_id, status="rejected", finished_at="__now__",
         failure_reason=f"Not approved ({outcome}).",
         error={"headline": f"The deployment was not approved ({outcome}).",
                "action": "Plan it again when it should go ahead."})
    connection.execute(
        text("UPDATE changesets SET status = 'rejected', failure_reason = :r, updated_at = now() "
             "WHERE id = :c AND status IN ('pending_approval', 'policy_checked', "
             "'budget_reserved')"),
        {"c": row["changeset_id"], "r": f"decision {outcome} by {actor}"})
    if row["reservation_id"]:
        BudgetGovernor(settings).release(connection, row["project_id"], row["reservation_id"],
                                         "Stack deployment was not approved.")
    emit_event(connection, row["project_id"], "deployment.stack_rejected", actor,
               "stack_deployment", deployment_id, {"outcome": outcome})


# ── Execute (the worker's leased job, handed to the API) ─────────────────────────────────────

def _fail(connection: Connection, deployment_id: str, project_id: str, *, headline: str,
          action: str, actor: str, steps: Optional[list] = None,
          log_tail: Optional[str] = None) -> None:
    fields: dict[str, Any] = {"status": "failed", "finished_at": "__now__",
                              "failure_reason": headline,
                              "error": {"headline": headline, "action": action}}
    if steps is not None:
        fields["steps"] = steps
    if log_tail is not None:
        fields["log_tail"] = log_tail
    _set(connection, deployment_id, **fields)
    emit_event(connection, project_id, "deployment.stack_failed", actor, "stack_deployment",
               deployment_id, {"headline": headline})


def execute_stack_job(engine, settings: Settings, *, deployment_id: str, job_id: str,
                      worker_id: str, lease_token: str) -> dict[str, Any]:
    """Apply an approved deployment under the worker's lease. Idempotent across replays."""
    from .jobs import JobLeaseService, LeaseNotFoundError
    from .resource_plane import ResourcePlaneService

    with engine.begin() as connection:
        try:
            JobLeaseService(settings).require_active_lease(connection, worker_id, job_id,
                                                           lease_token)
        except LeaseNotFoundError as exc:
            return {"status": "failed", "retry": False, "error": f"lease: {exc}"}
        row = _row(connection, deployment_id, lock=True)
        if not row:
            return {"status": "failed", "retry": False, "error": "no such deployment"}
        if row["status"] != "running":
            return {"status": row["status"], "retry": False,
                    "deployment": serialize(row)}
        if row["apply_started_at"] is not None:
            # A REPLAY AFTER A CRASH. The host may be half way through an install, and running
            # it again blind is how a database gets initialised twice. Stop, say so, and let a
            # person decide: deploying again runs `update` once `installed` exists.
            _fail(connection, deployment_id, row["project_id"],
                  headline="BOSS was interrupted while this deployment was running, so the "
                           "host may be part-way through it.",
                  action="Open the host's log to see where it stopped, then deploy again or "
                         "roll back.", actor=worker_id)
            connection.execute(
                text("UPDATE changesets SET status = 'deadlettered', failure_reason = :r, "
                     "updated_at = now() WHERE id = :c AND status IN ('approved', 'applying')"),
                {"c": row["changeset_id"], "r": "apply interrupted; not replayed"})
            return {"status": "failed", "retry": False,
                    "deployment": serialize(_row(connection, deployment_id))}
        busy = connection.execute(
            text("SELECT id FROM stack_deployments WHERE target_id = :t AND id <> :i "
                 "AND status = 'running' AND apply_started_at IS NOT NULL LIMIT 1"),
            {"t": row["target_id"], "i": deployment_id}).first()
        if busy:
            return {"status": "waiting", "retry": True,
                    "error": f"another deployment ({busy[0]}) is running on this target"}
        _set(connection, deployment_id, apply_started_at="__now__", job_id=job_id)

    with engine.begin() as connection:
        try:
            result = ResourcePlaneService(settings).apply_changeset(
                connection, changeset_id=row["changeset_id"],
                actor=str(row["created_by"] or worker_id), created_by_job_id=job_id)
        except ValueError as exc:
            _fail(connection, deployment_id, row["project_id"],
                  headline=f"The ChangeSet could not be applied: {exc}",
                  action="Plan the deployment again.", actor=worker_id)
            result = {"status": "blocked"}
        if result.get("status") == "blocked":
            current = _row(connection, deployment_id)
            if current and current["status"] == "running":
                _fail(connection, deployment_id, row["project_id"],
                      headline="Resource Plane blocked the apply.",
                      action="Open the decision it raised in Decisions.", actor=worker_id)
        final = _row(connection, deployment_id)
        if final["status"] == "running":
            # The operation never reached its own verdict — refused at the governance check,
            # or the provider layer failed before it. The receipt holds why.
            reason = connection.execute(
                text("SELECT error FROM resource_receipts WHERE changeset_id = :c "
                     "ORDER BY created_at DESC LIMIT 1"), {"c": row["changeset_id"]}).scalar()
            message = str((_json(reason) or {}).get("message") or "it did not run")
            _fail(connection, deployment_id, row["project_id"],
                  headline=f"The deployment did not run: {message}"[:300],
                  action="Plan the deployment again.", actor=worker_id)
            final = _row(connection, deployment_id)
    return {"status": final["status"], "retry": False, "deployment": serialize(final)}


def _require_governed(connection: Connection, operation: dict, dedupe_key: str,
                      deployment: Any) -> None:
    """The ONLY door to a host write in Community: an applying ChangeSet that was approved,
    with its reservation and this operation's receipt already recorded."""
    changeset_id = str(operation.get("changeset_id") or "")
    cs = connection.execute(
        text("SELECT status, approved_by, reservation_id, project_id FROM changesets "
             "WHERE id = :c"), {"c": changeset_id}).mappings().first()
    if not cs or cs["status"] != "applying" or not cs["approved_by"]:
        raise PermissionError("Stack deployment refused: its ChangeSet is not an approved "
                              "ChangeSet being applied.")
    if deployment is None or deployment["changeset_id"] != changeset_id:
        raise PermissionError("Stack deployment refused: the ChangeSet does not belong to it.")
    if not cs["reservation_id"] or not connection.execute(
            text("SELECT 1 FROM budget_reservations WHERE id = :r"),
            {"r": cs["reservation_id"]}).first():
        raise PermissionError("Stack deployment refused: it has no budget reservation.")
    if not connection.execute(
            text("SELECT 1 FROM receipts WHERE project_id = :p AND dedupe_key = :k"),
            {"p": cs["project_id"], "k": dedupe_key}).first():
        raise PermissionError("Stack deployment refused: its receipt was not recorded first.")


def apply_stack_operation(connection: Connection, settings: Settings, operation: dict, *,
                          receipt_dedupe_key: str, adapter: Any = None) -> dict[str, Any]:
    """The `ssh_host.stack.deploy` operation, called by ResourcePlaneService inside apply.

    Progress is written on a SEPARATE connection so the console can watch it; the verdict is
    written on THIS one, so `healthy` and the receipt commit together.
    """
    from .app_deploy_manifest import load_manifest as _load
    from .core import now_utc
    from .deployment_fleet import get_host
    from .resource_plane import ProviderAdapterError

    desired = operation.get("desired_state") or {}
    deployment_id = str(desired.get("deployment_id") or "")
    row = _row(connection, deployment_id) if deployment_id else None
    _require_governed(connection, operation, receipt_dedupe_key, row)
    deployment = dict(row)
    project_id = deployment["project_id"]
    actor = str(deployment.get("created_by") or "boss")
    progress_engine = connection.engine

    def record(steps: list[dict[str, Any]], log_tail: str) -> None:
        try:
            with progress_engine.begin() as c:
                _set(c, deployment_id, steps=steps, log_tail=log_tail)
        except Exception:  # noqa: BLE001 — progress is for watching; the verdict is below
            log.warning("stack deploy %s: progress not recorded", deployment_id, exc_info=True)

    applier: Optional[StackApplier] = None
    try:
        target = _target(connection, deployment["target_id"]) or {}
        host = get_host(connection, deployment["host_id"]) if deployment["host_id"] else None
        if not host:
            raise StackStepFailed("prepare", "The target's host no longer exists.",
                                  "Attach the target to a host in Infra and deploy again.")
        manifest = _load(deployment["manifest_text"])
        rendered = render(manifest, _deployment_ctx(target, host, deployment["commit_sha"],
                                                     deployment["ref"]))
        owner, repo = split_repository(deployment["repository"])
        access = host_access_for(connection, settings, host, adapter)
        github = github_source_for(connection, settings, project_id, owner, repo)
        env_values, hidden = _environment(connection, settings, deployment=deployment,
                                          manifest=manifest, rendered=rendered)
        applier = StackApplier(
            access=access, rendered=rendered, commit=deployment["commit_sha"],
            ref=deployment["ref"], env_values=env_values,
            redact_values=hidden + [access.key_material],
            changeset_id=str(operation.get("changeset_id")),
            fetch_tree=lambda dest: github.download_tarball(owner, repo,
                                                            deployment["commit_sha"], dest),
            record=record, now=lambda: now_utc().isoformat())
        outcome = applier.run()
    except (StackStepFailed, StackDeployError, ManifestError) as exc:
        headline = getattr(exc, "headline", None) or f"The manifest is not valid: {exc}"
        action = getattr(exc, "action", None) or "Fix boss.deploy.yaml and deploy again."
        _fail(connection, deployment_id, project_id, headline=headline, action=action,
              actor=actor, steps=applier.steps if applier else None,
              log_tail=applier.log if applier else None)
        raise ProviderAdapterError(headline)
    except Exception as exc:  # noqa: BLE001 — recorded with a sentence, then re-raised
        said = explain_failure("deploy", 1, str(exc))
        _fail(connection, deployment_id, project_id, headline=said["headline"],
              action=said["action"], actor=actor,
              steps=applier.steps if applier else None,
              log_tail=applier.log if applier else None)
        raise ProviderAdapterError(said["headline"])

    _set(connection, deployment_id, status="healthy", finished_at="__now__",
         steps=outcome["steps"], log_tail=outcome["log_tail"], open_url=outcome["open_url"],
         error=None, failure_reason=None)
    emit_event(connection, project_id, "deployment.stack_healthy", actor, "stack_deployment",
               deployment_id, {"commit": deployment["commit_sha"], "mode": outcome["mode"],
                               "open_url": outcome["open_url"]})
    return {
        "provider_ref": f"ssh://{access.user}@{access.host}{manifest.dir}@"
                        f"{deployment['commit_sha'][:12]}",
        "resource_external_id": f"stack:{deployment['target_id']}:{manifest.name}",
        "resource_name": manifest.name,
        "resource_type": "ssh_host.stack",
        "state": "active",
        "health_status": "healthy",
        "operation": outcome["mode"],
        "commit": deployment["commit_sha"],
        "open_url": outcome["open_url"],
    }


def fail_for_deadlettered_job(connection: Connection, job: dict, error: dict) -> None:
    """A deadlettered apply job fails its deployment, with the reason in words."""
    if job.get("kind") != JOB_KIND:
        return
    payload = _json(job.get("payload")) or {}
    deployment_id = str(payload.get("deployment_id") or "")
    row = _row(connection, deployment_id) if deployment_id else None
    if not row or row["status"] != "running":
        return
    _fail(connection, deployment_id, row["project_id"],
          headline="The worker job running this deployment stopped before it finished.",
          action=f"Deploy again. The worker said: {str(error.get('message') or '')[:200]}",
          actor="worker")


# ── Workflows: the deploy step, for a stack target ───────────────────────────────────────────

def stack_target_for_deploy(connection: Connection, project_id: str,
                            inputs: dict[str, Any]) -> Optional[dict]:
    """The stack target a workflow's deploy step is bound to, or None for the legacy path."""
    target_id = str(inputs.get("target_id") or "").strip()
    if target_id:
        target = _target(connection, target_id)
        return target if is_stack_target(target) and target.get("project_id") == project_id \
            else None
    # Without a named target, only a build whose repository HAS a stack target goes this way:
    # a project's other apps keep the deploy path they have always had.
    run_id = str(inputs.get("run_id") or "").strip()
    if not run_id:
        return None
    spec = connection.execute(text("SELECT spec FROM code_task_runs WHERE id = :i"),
                              {"i": run_id}).scalar()
    repo = str((_json(spec) or {}).get("repo") or "").lower()
    if not repo:
        return None
    environment = str(inputs.get("environment") or "").strip()
    rows = [dict(r) for r in connection.execute(
        text("SELECT * FROM deployment_targets WHERE project_id = :p AND target_kind = :k "
             "AND status = 'active' AND (:e = '' OR environment = :e) ORDER BY created_at"),
        {"p": project_id, "k": TARGET_KIND, "e": environment}).mappings().all()]
    rows = [r for r in rows
            if str(r.get("repo") or "").lower().rsplit("/", 1)[-1] == repo.rsplit("/", 1)[-1]]
    return rows[0] if len(rows) == 1 else None


def plan_for_workflow_step(engine, settings: Settings, *, project_id: str, actor: str,
                           target: dict, inputs: dict[str, Any],
                           workflow_run_id: Optional[str]) -> dict[str, Any]:
    """Plan the build's pull-request head onto a stack target; the step waits on it.

    No preview is required: the manifest declares how the stack runs and the health checks
    prove it, and a platform like BOSS cannot be previewed as one container anyway.
    """
    run_id = str(inputs.get("run_id") or "").strip()
    with engine.begin() as connection:
        run = connection.execute(
            text("SELECT spec, pull_request_number, status FROM code_task_runs WHERE id = :i"),
            {"i": run_id}).mappings().first() if run_id else None
    if run is None:
        return {"capability": "deploy", "tool": None, "status": "failed",
                "error": "this deploy step names no build, so there is no commit to deploy."}
    if not run["pull_request_number"]:
        return {"capability": "deploy", "tool": None, "status": "failed",
                "error": "the build opened no pull request, so there is no commit to deploy."}
    spec = _json(run["spec"]) or {}
    repository = str(target.get("repo") or "")
    if "/" not in repository and spec.get("owner") and spec.get("repo"):
        repository = f"{spec['owner']}/{spec['repo']}"
    try:
        deployment = plan_stack_deploy(
            engine, settings, project_id=project_id, target_id=target["id"],
            repository=repository, ref=f"pull/{int(run['pull_request_number'])}", actor=actor,
            run_id=run_id, workflow_run_id=workflow_run_id)
    except StackDeployError as exc:
        return {"capability": "deploy", "tool": None, "status": "failed",
                "error": f"{exc.headline} {exc.action}"}
    return {
        "capability": "deploy", "tool": None, "status": "awaiting",
        "await": {"kind": "stack_deployment", "id": deployment["id"]},
        "stack_deployment_id": deployment["id"],
        "decision_id": deployment.get("decision_id"),
        "commit": deployment.get("commit"),
        "note": (f"Planned {deployment['repository']}@{str(deployment.get('commit'))[:12]} "
                 f"onto {target.get('app_name')}; it runs once the deployment decision is "
                 "approved."),
    }
