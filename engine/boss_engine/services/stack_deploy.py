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
    apply    the worker hands the leased job to the API, which applies the ChangeSet in three
             phases (see "Execute" below): the ChangeSet `applying` and its receipts are
             COMMITTED first; then, with no transaction open, the commit's tree is streamed to
             the host, the environment written, the manifest's steps run, the health checks
             probed ON the host and `current` switched; then the verdict is recorded. The
             decision and this job are the only way in: the generic ChangeSet approve/apply
             routes refuse a stack deployment (resource_plane.PHASED_APPLY_SKILLS).

Host layout (one directory per stack, default /opt/boss-apps/<name>):

    releases/<commit>/     the extracted tree, kept for the last three releases
    current -> releases/<commit>
    .boss/deploy.env       mode 600; every step sources it
    .boss/installed        present after the first healthy deploy: later deploys run `update`

Credentials: the GitHub token is used inside the API container only, to download the tarball;
it never reaches the host or a log. A manifest's `{secret: NAME}` reads only an app secret the
operator set on that target — never a project secret, a host key or a GitHub credential. Generated environment values are minted once per (target,
stack), stored through the secret store and reused; their values never appear in a response,
an event or the log tail (every output is redacted by value).

Failure is not rolled back automatically. A half-applied install is diagnosed, not destroyed:
the deployment fails with a headline and the next action, and the console offers a rollback,
which is simply a new governed deployment of the previous healthy commit.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets as _secrets
import shlex
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings
from .app_deploy_manifest import (
    DEFAULT_MANIFEST_PATH,
    DeployManifest,
    ManifestError,
    app_manifest,
    declared_manifest,
    load_manifest,
    manifest_workdir,
    render,
)
from .core import create_blocking_decision, emit_event, make_id
from .people_names import who as _who

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
#: The step that removes a host target's earlier one-container release before its stack runs.
RETIRE_STEP = "retire previous single-container release"


#: WHOSE FIX A FAILURE IS (deploy_repair). `app`: the app's own manifest or code is wrong for
#: this machine — a missing runtime it assumed, an invalid manifest, an install, start or health
#: check failing with the app's own error — and a repair build of the app can fix it. `host`:
#: the machine, its access or BOSS's own plumbing — a person's to fix; never auto-repaired.
CAUSE_APP = "app"
CAUSE_HOST = "host"


class StackDeployError(Exception):
    """A plan BOSS refuses, said as {headline, action} — the console shows both.

    `cause` (CAUSE_APP / CAUSE_HOST) is whose fix it is, when the refusal knows; `facts` are
    what the host was found to have, when it was asked."""

    def __init__(self, headline: str, action: str, *, status_code: int = 422,
                 cause: Optional[str] = None, facts: Optional[dict] = None):
        super().__init__(headline)
        self.headline = headline
        self.action = action
        self.status_code = status_code
        self.cause = cause
        self.facts = dict(facts or {})

    def to_dict(self) -> dict[str, str]:
        return {"headline": self.headline, "action": self.action}


class StackStepFailed(Exception):
    def __init__(self, step: str, headline: str, action: str, cause: Optional[str] = None):
        super().__init__(headline)
        self.step = step
        self.headline = headline
        self.action = action
        self.cause = cause


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

    def compare(self, owner: str, repo: str, base: str, head: str) -> dict[str, Any]:
        """The commits `head` has beyond `base`, oldest first (release_scope's ship list)."""
        response = self._get(f"/repos/{owner}/{repo}/compare/"
                             f"{quote(base, safe='')}...{quote(head, safe='')}")
        if response.status_code != 200:
            raise self._refused(response, f"{base[:12]}...{head[:12]} in {owner}/{repo}")
        payload = response.json() or {}
        return {"behind_by": int(payload.get("behind_by") or 0),
                "commits": [{"sha": str(c.get("sha") or ""),
                             "message": str((c.get("commit") or {}).get("message") or "")
                             .splitlines()[0][:200] if (c.get("commit") or {}).get("message")
                             else "",
                             "full_message": str((c.get("commit") or {}).get("message")
                                                 or "")[:4000]}
                            for c in (payload.get("commits") or [])[-250:]]}

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
        token = github_token_for(connection, settings, project_id, owner, repo)
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


#: The runtimes a host is asked about on every preflight, whatever the manifest runs: what the
#: machine HAS is what a build is told to write its manifest for (deploy_repair.host_brief).
RUNTIME_TOOLS: tuple[str, ...] = ("docker", "node", "npm", "python3", "pip3", "java", "go",
                                  "ruby", "php", "git", "make")
#: How each runtime says its version, when it is there.
_VERSION_PROBES: dict[str, str] = {
    "docker": "docker --version", "compose": "docker compose version --short",
    "node": "node --version", "python3": "python3 --version", "java": "java -version 2>&1",
    "go": "go version",
}
#: Words a shell understands itself: a command segment starting with one runs no program the
#: machine might lack.
_SHELL_WORDS = frozenset({
    "cd", "export", "source", ".", "set", "unset", "echo", "printf", "test", "[", "[[", "true",
    "false", "exit", "return", ":", "if", "elif", "fi", "for", "while", "until", "command",
    "done", "case", "esac", "in", "function", "}", "wait", "trap", "read", "type", "hash",
    "shift", "eval", "exec", "local", "declare", "alias", "umask", "ulimit", "sleep", "mkdir",
    "cp", "mv", "rm", "ln", "cat", "chmod", "chown", "touch", "ls", "pwd", "kill",
})
#: Wrappers whose NEXT word is the program.
_WRAPPERS = frozenset({"sudo", "env", "nohup", "time", "nice", "setsid", "stdbuf",
                       # a keyword that opens a body: the body's first word is the program
                       "then", "else", "do", "{", "!"})
#: Wrapper flags that take no value (`sudo -E npm ci`).
_BARE_FLAGS = frozenset({"-E", "-n", "-H", "-S", "-i", "-k", "-b", "-P", "--preserve-env"})
#: A command that may install software for the commands after it: a program missing now may
#: exist by the time its own step runs, so nothing after one of these is refused for it.
_INSTALLS = re.compile(
    r"\b(apt-get|apt|yum|dnf|apk|zypper|brew|nvm|asdf|volta|fnm|pyenv|sdk|snap)\b\s+"
    r"(install|add|use)\b|\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z)?sh\b", re.I)
_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def command_executables(command: str) -> list[str]:
    """The programs one manifest command runs, in order: the first word of each segment, after
    its `VAR=x` assignments and wrappers (`sudo`, `env`, `nohup`, `timeout 30`). A path
    (`./start.sh`, `bin/x`) is the app's own file and is never asked about; a shell word is
    skipped; a command that cannot be read as shell yields nothing (never a guess)."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []
    found: list[str] = []
    segment: list[str] = []
    for token in tokens + [";"]:
        if token and set(token) <= set("&|;()"):
            words, segment = segment, []
            while words and (_ASSIGNMENT.match(words[0]) or words[0] in _WRAPPERS):
                words.pop(0)
                # A wrapper's flag that takes a value (`sudo -u bob`) hides which word is the
                # program: such a segment is not judged at all, never guessed at.
                while words and words[0].startswith("-"):
                    if words.pop(0) not in _BARE_FLAGS:
                        words = []
            if words and words[0] == "timeout":
                words = [w for w in words[1:] if not w.startswith("-")][1:]
            if not words:
                continue
            first = words[0]
            if first in _SHELL_WORDS or "/" in first or "$" in first \
                    or not _TOOL_NAME.match(first):
                continue
            if first not in found:
                found.append(first)
            continue
        segment.append(token)
    return found


def manifest_executables(manifest: DeployManifest) -> list[tuple[str, str]]:
    """(program, step name) for every program a `kind: commands` manifest runs on the machine
    directly — none after a step that may install software. A compose manifest runs Docker,
    which `requires.docker` already asks about."""
    if manifest.kind != "commands":
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for section in (manifest.install, manifest.update):
        for step in section:
            for tool in command_executables(step.run):
                if tool not in seen:
                    seen.add(tool)
                    out.append((tool, step.name))
            if _INSTALLS.search(step.run):
                break
    return out


class Preflight(list):
    """The problems a preflight found (each {headline, action, cause}) and `facts`: what the
    host was found to have — {"tools": {name: bool}, "versions": {name: str}, "docker": bool,
    "docker_usable": bool, "sudo": bool}. Names and versions only, never a secret."""

    def __init__(self, problems: Optional[list] = None, facts: Optional[dict] = None):
        super().__init__(problems or [])
        self.facts: dict[str, Any] = dict(facts or {})


def host_facts_from(out: str) -> dict[str, Any]:
    """The facts a preflight probe printed (BOSS_HAS / BOSS_LACKS / BOSS_VERSION lines). A
    program the probe did not mention is unknown, never missing."""
    tools: dict[str, bool] = {}
    versions: dict[str, str] = {}
    for line in (out or "").splitlines():
        parts = line.strip().split(" ", 2)
        if len(parts) >= 2 and parts[0] in ("BOSS_HAS", "BOSS_LACKS") \
                and _TOOL_NAME.match(parts[1]):
            tools[parts[1]] = parts[0] == "BOSS_HAS"
        elif len(parts) == 3 and parts[0] == "BOSS_VERSION" and _TOOL_NAME.match(parts[1]) \
                and parts[2].strip():
            versions[parts[1]] = parts[2].strip()[:80]
    facts: dict[str, Any] = {"tools": tools, "versions": versions}
    if "DOCKER_PRESENT" in out or "DOCKER_MISSING" in out:
        facts["docker"] = "DOCKER_PRESENT" in out
        facts["docker_usable"] = "DOCKER_USABLE" in out
        tools.setdefault("docker", bool(facts["docker"]))
    if "SUDO_OK" in out or "SUDO_NO" in out:
        facts["sudo"] = "SUDO_OK" in out
    return facts


def host_has(facts: Optional[Mapping[str, Any]]) -> list[str]:
    """The runtimes the facts say the host has, in a stable order."""
    tools = (facts or {}).get("tools") or {}
    return [t for t in RUNTIME_TOOLS if tools.get(t)] + sorted(
        t for t, present in tools.items() if present and t not in RUNTIME_TOOLS)


def missing_tool_problem(tool: str, step: str, host: str,
                         facts: Mapping[str, Any]) -> dict[str, str]:
    """The refusal for a manifest that runs a program the machine does not have — the app's
    to fix (its manifest assumed a runtime), so `cause` is CAUSE_APP."""
    has = host_has(facts)
    docker = bool((facts.get("tools") or {}).get("docker"))
    return {
        "headline": (f"The target machine {host} has no `{tool}` (it has: "
                     f"{', '.join(has) or 'none of the runtimes BOSS asks about'}); this "
                     f"manifest runs commands on the machine directly ('{step}' runs `{tool}`)."),
        "action": (f"Use kind: compose with a Dockerfile and docker-compose.yml (the machine has "
                   f"Docker), or install {tool} on it." if docker else
                   f"Install {tool} on the machine, or deploy to one that has it."),
        "cause": CAUSE_APP, "missing_tool": tool, "step": step,
    }


def _tools_probe(needed: list[str]) -> str:
    """`command -v` for every runtime and every program the manifest runs, in a LOGIN shell
    (the steps run under `bash -lc`, so a profile's nvm/asdf paths count), then the versions."""
    names = [t for t in dict.fromkeys(list(RUNTIME_TOOLS) + list(needed)) if _TOOL_NAME.match(t)]
    versions = "; ".join(
        f"command -v {cmd.split()[0]} >/dev/null 2>&1 && "
        f"echo BOSS_VERSION {name} $({cmd} 2>/dev/null | head -n 1)"
        for name, cmd in _VERSION_PROBES.items())
    script = (f"for t in {' '.join(shlex.quote(n) for n in names)}; do "
              'if command -v "$t" >/dev/null 2>&1; then echo "BOSS_HAS $t"; '
              'else echo "BOSS_LACKS $t"; fi; done; ' + versions)
    return f"bash -lc {shlex.quote(script)} 2>/dev/null || true"


def preflight_host(access: HostAccess, manifest: DeployManifest) -> Preflight:
    """Read-only questions to the host. Each problem is {headline, action, cause}; none writes.

    ONE SSH SESSION asks everything: the directory, sudo, Docker, tar and curl the deploy itself
    needs, every program a `kind: commands` manifest runs (`command -v`, in a login shell as the
    steps run), and which runtimes the machine has, with versions (`Preflight.facts`). A
    manifest that runs `npm` on a machine with no Node is refused HERE, before anything is
    approved or written — not at its install step after prepare, upload and switch had already
    changed the host (sdep_f145247613894c0bbff9: `bash: line 1: npm: command not found`)."""
    needed = manifest_executables(manifest)
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
        "command -v curl >/dev/null 2>&1 && echo CURL_OK || echo CURL_MISSING; "
        + _tools_probe([t for t, _ in needed])
    )
    result = access.run(probe, timeout=40)
    out = result.output
    where = f"{access.user}@{access.host}"
    if "BOSS_SSH_OK" not in out:
        said = explain_failure("preflight", result.returncode, out, host=where)
        return Preflight([{**said, "cause": CAUSE_HOST}])
    facts = host_facts_from(out)
    problems = Preflight(facts=facts)
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
    for found in problems:
        found.setdefault("cause", CAUSE_HOST)
    tools = facts.get("tools") or {}
    for tool, step in needed:
        if tools.get(tool) is False:
            problems.append(missing_tool_problem(tool, step, access.host, facts))
    return problems


# ── Failures, in words ───────────────────────────────────────────────────────────────────────

def _app_step(step: str) -> bool:
    """A step the app's manifest wrote (its install/update commands), not BOSS's plumbing."""
    return str(step or "").startswith(("install: ", "update: "))


def explain_failure(step: str, returncode: int, output: str, *,
                    host: str = "the host", url: str = "", timeout: int = 0) -> dict[str, str]:
    """{headline, action, cause} for a failed step — the model_failures shape, for a host.

    `cause` says whose fix it is (CAUSE_APP / CAUSE_HOST): the machine's access, disk, Docker
    or sudo are a person's; the app's own command or health check failing is the app's."""
    said = _explained(step, returncode, output, host=host, url=url, timeout=timeout)
    said.setdefault("cause", CAUSE_APP if (step == "health" or _app_step(step)) else CAUSE_HOST)
    return said


#: "Bind for 0.0.0.0:8000 failed: port is already allocated" (Docker), "listen tcp
#: 0.0.0.0:8000: bind: address already in use" — the port, from either.
PORT_HELD = re.compile(
    r"Bind for [^\s]*?:(\d{1,5}) failed: port is already allocated"
    r"|(?:listen|bind)[^\n]*?:(\d{1,5}):? ?(?:bind: )?address already in use", re.I)


def _explained(step: str, returncode: int, output: str, *,
               host: str, url: str, timeout: int) -> dict[str, str]:
    lowered = (output or "").lower()
    if returncode == 127 and "ssh` client" in lowered:
        return {"headline": "BOSS cannot run ssh where it is installed.",
                "action": "Install openssh-client in the API image and restart BOSS.",
                "cause": CAUSE_HOST}
    if "boss_ssh_outward_allowed" in lowered or "configured not to reach out" in lowered:
        return {"headline": "BOSS is configured not to reach out over SSH.",
                "action": "Remove BOSS_SSH_OUTWARD_ALLOWED=false from the API's environment "
                          "and restart it.", "cause": CAUSE_HOST}
    if "permission denied (publickey" in lowered or "host key verification failed" in lowered:
        return {"headline": f"{host} refused BOSS's SSH key.",
                "action": "In Infra, open the host, add the public key it shows to "
                          "~/.ssh/authorized_keys on the VM, then Verify.", "cause": CAUSE_HOST}
    if any(m in lowered for m in ("could not resolve hostname", "connection refused",
                                  "connection timed out", "no route to host",
                                  "operation timed out", "network is unreachable")) \
            and (returncode == 255 or not (step == "health" or _app_step(step))):
        # ssh's own exit (255) is the connection; an app's command refused by ITS database is
        # the app's error, said below with the log tail.
        return {"headline": f"BOSS could not connect to {host}.",
                "action": "Check the address and that port 22 is open to BOSS, then Verify the "
                          "host in Infra.", "cause": CAUSE_HOST}
    if "boss_dir_not_writable" in lowered:
        return {"headline": f"The deploy directory is not writable on {host}.",
                "action": "On the host, chown the directory to the SSH user (or grant "
                          "passwordless sudo), then deploy again.", "cause": CAUSE_HOST}
    if "no space left on device" in lowered:
        return {"headline": f"{host} ran out of disk space during {step}.",
                "action": "Inspect Docker disk use on the host with `docker system df`. "
                          "Remove only confirmed BOSS-owned, unused resources, then deploy again.",
                "cause": CAUSE_HOST}
    held = PORT_HELD.search(output or "")
    if held:
        # Not the app's code: something on the machine already holds the port — often this
        # app's own earlier release (step_recovery's clean_and_retry frees exactly that).
        port = next(g for g in held.groups() if g)
        return {"headline": f"Port {port} on {host} is already held by another container, so "
                            "the app could not start.",
                "action": "Stop what holds the port (BOSS retires this app's own earlier "
                          "release by itself), or publish the app on another port.",
                "cause": CAUSE_HOST}
    if "boss_not_this_apps" in lowered:
        return {"headline": f"A compose project BOSS was to retire on {host} has containers "
                            "that are not this app's, so nothing was removed.",
                "action": "Free the resource by hand, or deploy the app on another port or "
                          "host.", "cause": CAUSE_HOST}
    unwired = re.search(r"(?:getaddrinfo|could not translate host name)\W+(?:\w+\W+)?\"?(res-[a-z0-9-]+)",
                        output or "", re.I)
    if step == "health" and unwired:
        # THE APP CANNOT REACH A SERVICE BOSS PROVISIONED (res-db): BOSS's wiring, never the
        # app's code — a repair build would "fix" a working app (sdep_3521765ccd404540ba1d).
        return {"headline": f"The app could not reach {unwired.group(1)}, the service BOSS "
                            f"provisioned for it on {host}, so it exited at start.",
                "action": "This is BOSS's network wiring, not the app: the app's compose project "
                          "must share the resources' project. Update BOSS and deploy again.",
                "cause": CAUSE_HOST}
    if step == "health":
        return {"headline": f"The app did not answer as expected at {url} within {timeout}s.",
                "action": "Read the log tail for what the install printed, fix it and deploy "
                          "again — or roll back to the previous healthy commit.",
                "cause": CAUSE_APP}
    if returncode == 124 or "did not finish within" in lowered:
        return {"headline": f"The step '{step}' timed out on {host}.",
                "action": "Raise timeout_seconds for that step in boss.deploy.yaml, or read the "
                          "log tail for where it stalled."}
    if re.search(r"docker: (command )?not found", lowered):
        return {"headline": f"Docker is not installed on {host}.",
                "action": "Install Docker on the host, then deploy again.", "cause": CAUSE_HOST}
    if "permission denied while trying to connect to the docker daemon" in lowered:
        return {"headline": f"The SSH user cannot use Docker on {host}.",
                "action": "Add the user to the docker group on the host, then deploy again.",
                "cause": CAUSE_HOST}
    if "sudo: a password is required" in lowered or "sudo: a terminal is required" in lowered:
        return {"headline": f"A step needed sudo and the SSH user has no passwordless sudo on "
                            f"{host}.",
                "action": "Grant passwordless sudo to that user, or remove sudo from the step.",
                "cause": CAUSE_HOST}
    missing = re.search(r"(?:^|\n)(?:bash: )?(?:line \d+: )?([A-Za-z0-9._+-]+): (?:command )?"
                        r"not found", output or "")
    if returncode == 127 and missing and _app_step(step):
        tool = missing.group(1)
        return {"headline": f"The step '{step}' failed on {host}: the machine has no "
                            f"`{tool}` (exit 127).",
                "action": f"This manifest runs `{tool}` on the machine directly. Use kind: "
                          "compose with a Dockerfile when the machine has Docker, or install "
                          f"{tool} on it.", "cause": CAUSE_APP}
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
        if self.rendered.get("retire_container"):
            names.append(RETIRE_STEP)
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

    def _compose_state(self) -> str:
        """The compose project's containers and the last lines of each one not running, from
        the install command's `-p <project>`; "" for a manifest that runs no compose. Read-only."""
        project = ""
        # The step that BRINGS THE APP UP names its project; the step before it may retire an
        # older project of the app (join_resource_project), whose containers are not the answer.
        for step in reversed(list(self.rendered.get("install") or [])):
            run = str(step.get("run") or "")
            m = re.search(r"\s-p\s+([A-Za-z0-9._-]+)", run)
            if m and " up " in run:
                project = m.group(1)
                break
        if not project:
            return ""
        q = shlex.quote(f"label=com.docker.compose.project={project}")
        probe = (f"docker ps -a --filter {q} --format '{{{{.Names}}}} {{{{.Status}}}}'; "
                 f"for c in $(docker ps -a --filter {q} --filter status=exited --format '{{{{.Names}}}}'); do "
                 "echo \"--- $c (last lines) ---\"; docker logs --tail 30 \"$c\" 2>&1; done")
        try:
            result = self.access.run(probe, timeout=60, write=False)
        except Exception:  # noqa: BLE001 — diagnosis only; the health verdict stands
            return ""
        return redact(result.output[-3000:], self.redact_values) if result.output.strip() else ""

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
            raise StackStepFailed(name, said["headline"], said["action"], said.get("cause"))
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
                    raise StackStepFailed("upload", exc.headline, exc.action, CAUSE_HOST)
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

        # Steps run in the release's REAL directory, never through the `current` symlink.
        # Through the symlink every path a tool resolved was the same string as last release's,
        # so `docker compose up` saw an unchanged configuration and recreated nothing: stack-demo
        # version 2 reported healthy on boss-1 (2026-10-03) while nginx still served version 1
        # from the old release it had mounted.
        release = f"{d}/releases/{shlex.quote(self.commit)}"
        # ...and inside the MANIFEST'S directory: a manifest in helpdesk-ai/ names
        # docker-compose.yml meaning helpdesk-ai's, not the repository root's.
        workdir = str(self.rendered.get("workdir") or "").strip("/")
        if workdir:
            release = f"{release}/{shlex.quote(workdir)}"

        retire = str(self.rendered.get("retire_container") or "")
        if retire:
            # THE TARGET'S EARLIER ONE-CONTAINER RELEASE holds the port this stack is about to
            # publish. Removed only when it is BOSS's own release for this target (it mounts the
            # target's directory under /opt/boss-apps), never a container somebody else runs.
            name = RETIRE_STEP
            self._start(name)
            marker = shlex.quote(str(self.rendered.get("retire_mount") or ""))
            q = shlex.quote(retire)
            self._write(name, (
                f"if docker inspect -f '{{{{range .Mounts}}}}{{{{.Source}}}} {{{{end}}}}' {q} "
                f"2>/dev/null | grep -q -F {marker}; then docker rm -f {q} >/dev/null && "
                f"echo BOSS_RETIRED {q}; else echo BOSS_NOTHING_TO_RETIRE; fi"), timeout=120)
            self._finish(name, "succeeded")
        for command in commands:
            name = f"{self.mode}: {command['name']}"
            self._start(name)
            limit = int(command["timeout_seconds"])
            self._write(name, (
                f"cd {release} && set -a && . {env_path} && set +a && "
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
                # WHAT THE APP ITSELF SAID. "Did not answer within 600s" sent a repair build
                # to fix an app whose container had exited on `getaddrinfo EAI_AGAIN res-db`
                # (sdep_3521765ccd404540ba1d) — a BOSS wiring fault nobody could see. The
                # compose project's containers and the last lines of each one that is not
                # running go into the log, read-only, so a person and the repair both see it.
                tail = self._compose_state()
                if tail:
                    self._append(tail)
                self._finish("health", "failed")
                said = explain_failure("health", result.returncode,
                                       result.output + ("\n" + tail if tail else ""),
                                       host=self.access.host, url=check["url"], timeout=limit)
                raise StackStepFailed("health", said["headline"], said["action"],
                                      said.get("cause"))
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
    "apply_started_at, created_by, created_at, updated_at, finished_at, deploy_dir, "
    "waiting_on, apply_token"
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
        "dir": data.get("deploy_dir"),
        # Approved, but queued behind another deployment writing the same host directory.
        "waiting": ({"headline": "Waiting for deployment "
                                 f"{data['waiting_on']} to finish on this host directory.",
                     "deployment_id": data["waiting_on"]}
                    if data.get("waiting_on") and data.get("status") == "running" else None),
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


# ── What an operator may do next: decided here, rendered by the console ─────────────────────
#
# The console once derived its buttons from a record's status alone, and offered Roll back on
# every finished record — an OLDER healthy one planned a commit older than the release before
# the live one, and one that failed at plan or preflight never touched the host, so "rolling it
# back" redeployed whatever was already running. Each deployment now carries `actions`, and
# the console renders exactly those; the rollback route refuses what `actions` does not offer.

ACTIONS = ("approve", "reject", "rollback", "open")


def rollback_option(row: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    """Whether THIS deployment offers Roll back, and the commit that would deploy.

    `row` and `history` are `stack_deployments` rows for one target, carrying `id`, `status`,
    `commit_sha`, `apply_started_at` and `created_at`. Available only when `row` is the
    target's LATEST deployment that applied a ChangeSet (apply_started_at set), has finished
    healthy or failed, and an earlier healthy commit different from its own exists.
    """
    none = {"rollback_available": False, "rollback_commit": None}
    if row.get("status") not in ("healthy", "failed") or row.get("apply_started_at") is None:
        return none
    applied = [h for h in history if h.get("apply_started_at") is not None]
    latest = max(applied, key=lambda h: h["created_at"], default=None)
    if latest is None or latest.get("id") != row.get("id"):
        return none
    earlier = [h for h in history
               if h.get("status") == "healthy" and h.get("commit_sha")
               and h.get("commit_sha") != row.get("commit_sha")
               and h["created_at"] < row["created_at"]]
    if not earlier:
        return none
    return {"rollback_available": True,
            "rollback_commit": max(earlier, key=lambda h: h["created_at"])["commit_sha"]}


def deployment_actions(row: dict[str, Any], history: list[dict[str, Any]],
                       decision_status: Optional[str]) -> list[str]:
    """The operator actions valid on this deployment right now, in display order."""
    actions: list[str] = []
    if (row.get("status") == "awaiting_approval" and row.get("decision_id")
            and decision_status == "pending"):
        actions += ["approve", "reject"]
    if rollback_option(row, history)["rollback_available"]:
        actions.append("rollback")
    if row.get("status") == "healthy" and row.get("open_url"):
        actions.append("open")
    return actions


def _describe(connection: Connection, rows: list[Any]) -> list[dict[str, Any]]:
    """serialize() plus the engine-decided `actions` and the rollback commit."""
    rows = [dict(r) for r in rows if r]
    if not rows:
        return []
    target_ids = sorted({r["target_id"] for r in rows})
    history: dict[str, list[dict[str, Any]]] = {t: [] for t in target_ids}
    for h in connection.execute(
            text("SELECT id, target_id, status, commit_sha, apply_started_at, created_at "
                 "FROM stack_deployments WHERE target_id = ANY(:t)"),
            {"t": target_ids}).mappings().all():
        history[h["target_id"]].append(dict(h))
    decision_ids = sorted({r["decision_id"] for r in rows if r.get("decision_id")})
    decisions = {d["id"]: d["status"] for d in connection.execute(
        text("SELECT id, status FROM decisions WHERE id = ANY(:d)"),
        {"d": decision_ids}).mappings().all()} if decision_ids else {}
    out = []
    for r in rows:
        option = rollback_option(r, history[r["target_id"]])
        out.append({**serialize(r),
                    "actions": deployment_actions(r, history[r["target_id"]],
                                                  decisions.get(r.get("decision_id"))),
                    "rollback_commit": option["rollback_commit"]})
    return out


def get_stack_deployment(connection: Connection, deployment_id: str) -> Optional[dict]:
    row = _row(connection, deployment_id)
    return _describe(connection, [row])[0] if row else None


def list_stack_deployments(connection: Connection, project_id: str, *,
                           target_id: Optional[str] = None, limit: int = 50) -> list[dict]:
    rows = connection.execute(
        text(f"SELECT {_COLUMNS} FROM stack_deployments WHERE project_id = :p "
             "AND (CAST(:t AS TEXT) IS NULL OR target_id = :t) "
             "ORDER BY created_at DESC LIMIT :n"),
        {"p": project_id, "t": target_id, "n": max(1, min(int(limit), 200))}).mappings().all()
    return _describe(connection, list(rows))


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

def shipped_from_manifest(connection: Connection, target_id: str) -> Optional[dict[str, str]]:
    """{manifest_path, repository} of the target's newest stack deployment, or None."""
    row = connection.execute(text(
        "SELECT manifest->>'manifest_path' AS path, repository FROM stack_deployments "
        "WHERE target_id = :t AND COALESCE(manifest->>'manifest_path', '') <> '' "
        "ORDER BY created_at DESC LIMIT 1"), {"t": target_id}).mappings().first()
    if not row or not row["path"]:
        return None
    return {"manifest_path": str(row["path"]), "repository": str(row["repository"] or "")}


def declared_manifest_of_target(connection: Connection, target_id: str) -> Optional[str]:
    """The boss.deploy.yaml a target is shipped from, when BOSS has stack-deployed it: the
    manifest path recorded on its newest stack deployment. None for a target that never was.

    A target made by an app's first deploy keeps `target_kind = 'ssh'` and no manifest_path, yet
    every deploy of it is a stack deploy from the app's manifest. Asking only `target_kind`, the
    Infra Deploy button sent the Order API's next release down the one-container path (D2)."""
    row = connection.execute(text(
        "SELECT manifest->>'manifest_path' AS path FROM stack_deployments WHERE target_id = :t "
        "AND COALESCE(manifest->>'manifest_path', '') <> '' ORDER BY created_at DESC LIMIT 1"),
        {"t": target_id}).first()
    return str(row[0]) if row and row[0] else None


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


#: What a manifest's `{secret: NAME}` may never read, whatever it is named: a machine's SSH key or
#: a GitHub credential. Neither is an application's configuration, and both would leave BOSS
#: for a host whose install commands come from a pull request an agent wrote.
_FORBIDDEN_SECRET_KINDS = frozenset({"private_key", "ssh_private_key"})
_FORBIDDEN_SECRET_PROVIDERS = frozenset({"github", "private_key", "ssh_private_key"})


def operator_secret_ref(connection: Connection, project_id: str, target_id: str,
                        name: str) -> Optional[str]:
    """The APP secret an operator set on THIS target under `name`, or None.

    A manifest is repository content, so what it can name must be bounded by what the operator
    deliberately handed this target — not by everything the project's secret store holds.
    Only a secret scoped to this deployment target, at the key BOSS derives for (target,
    name), is found: never a raw backend key, never another target's or a host's secret, and
    never an SSH key or GitHub credential even if someone scoped one here.
    """
    from .secrets_service import ref_key_for

    row = connection.execute(
        text("SELECT id, secret_kind, provider FROM secret_refs WHERE project_id = :p "
             "AND status = 'active' AND scope_type = 'deployment_target' AND scope_id = :t "
             "AND ref_key = :k ORDER BY updated_at DESC LIMIT 1"),
        {"p": project_id, "t": target_id, "k": ref_key_for(target_id, name)}).mappings().first()
    if not row:
        return None
    if (str(row["secret_kind"] or "").lower() in _FORBIDDEN_SECRET_KINDS
            or str(row["provider"] or "").lower() in _FORBIDDEN_SECRET_PROVIDERS):
        return None
    return str(row["id"])


def _secret_action(target_id: str, names: list[str]) -> str:
    return (f"In BOSS, set each as a secret ON THIS TARGET (POST /v1/secrets with scope_type "
            f"deployment_target, scope_id {target_id}, name {', '.join(names)}), then plan "
            "again. Project-wide secrets, host keys and GitHub credentials are never readable "
            "from a manifest.")


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
    if manifest.signals:
        # THE APP'S OWN CHANNEL TO BOSS (incident_signals): one token per target, minted once,
        # the same one BOSS authenticates with when it reads the app's signals and signs what
        # it tells the app back.
        from .incident_signals import ENV_KEY, channel_token

        token = channel_token(connection.engine, settings, project_id=deployment["project_id"],
                              target_id=deployment["target_id"])
        values[ENV_KEY] = token
        hidden.append(token)
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
            ref = operator_secret_ref(connection, deployment["project_id"],
                                      deployment["target_id"], item.value)
            if not ref:
                raise StackStepFailed(
                    "environment",
                    f"The secret {item.value!r} ({item.key}) is not set on this target.",
                    _secret_action(deployment["target_id"], [item.value]))
            value = resolve_secret_ref(connection, ref, settings)
            values[item.key] = value
            hidden.append(value)
    return values, hidden


def _lock_key(*parts: str) -> int:
    """A Postgres advisory-lock key (signed 64-bit) for a tuple of names."""
    digest = hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _live_deployment(connection: Connection, target_id: str, commit: str):
    return connection.execute(
        text(f"SELECT {_COLUMNS} FROM stack_deployments WHERE target_id = :t "
             "AND commit_sha = :c AND status = ANY(:s) ORDER BY created_at DESC LIMIT 1"),
        {"t": target_id, "c": commit, "s": list(ACTIVE_STATES)}).mappings().first()


# ── Plan ─────────────────────────────────────────────────────────────────────────────────────

def app_deploy_env(connection: Connection, project_id: str, target: dict,
                   code_task_run_id: Optional[str],
                   manifest: Optional[DeployManifest] = None) -> dict[str, str]:
    """The app's deploy environment: THE ONE READER both deploy paths use
    (templates.base_url.deploy_environment) — its `deploy` secret values, generated, set or
    approved live, plus APP_BASE_URL where a template declares it. {} for an app with no key.

    `manifest`: the release being deployed. When it declares NO `resources:`, the values BOSS's
    resources bound for an EARLIER release of the app (`resource_binding_names`: a
    DATABASE_URL pointing at res-db) are not this release's: HelpDesk AI sdep_121762db692549afa39b
    brought its own Postgres in its compose file (`${DATABASE_URL:-…@db…}`), was handed the old
    resource's DATABASE_URL anyway, and exited on `getaddrinfo EAI_AGAIN res-db`."""
    from ..app_links.service import project_org
    from ..templates.base_url import deploy_environment
    from .governed_host_deploy import _app_key_for

    key = _app_key_for(connection, project_id, target,
                       {"code_task_run_id": code_task_run_id} if code_task_run_id else None)
    if not key:
        return {}
    env, _withheld, _address = deploy_environment(
        connection, org_id=project_org(connection, project_id), project_id=project_id,
        app_key=key, target=target)
    if manifest is not None and not manifest.resources:
        for name in resource_binding_names(connection, project_id, key):
            env.pop(name, None)
    return env


def resource_binding_names(connection: Connection, project_id: str, app_key: str) -> set[str]:
    """The env names BOSS's platform resources bind for this app (infra_platform_resources
    .bindings, e.g. DATABASE_URL) — values that belong to a release which asks for those
    resources, never to one that brings its own."""
    try:
        with connection.begin_nested():
            rows = connection.execute(
                text("SELECT bindings FROM infra_platform_resources WHERE project_id = :p "
                     "AND app_key = :a AND destroyed_at IS NULL"),
                {"p": project_id, "a": app_key}).all()
    except Exception:  # noqa: BLE001 — unread: keep the environment as it was
        log.warning("stack deploy: resource bindings of %s not read", app_key, exc_info=True)
        return set()
    names: set[str] = set()
    for (bindings,) in rows:
        data = bindings if isinstance(bindings, dict) else json.loads(bindings or "{}")
        names.update(str(k) for k in (data or {}) if k)
    return names


def merge_app_env(manifest: DeployManifest, values: dict[str, str], hidden: list[str],
                  app_env: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """The manifest's environment with the app's deploy values added.

    What a person set for the app (its Setup) beats a literal written in the repository, and
    fills every key the manifest does not name. A key the manifest GENERATES or reads from a
    target secret keeps the manifest's value: that is the one the host was initialised with
    (its database password), and swapping it would lock the app out of its own data. Every app
    value is redacted, whatever its name.
    """
    literal = {e.key for e in manifest.env if e.source == "literal"}
    out = dict(values)
    for key, value in app_env.items():
        if key not in out or key in literal:
            out[key] = value
    from ..templates.base_url import BASE_URL_ENV

    return out, list(hidden) + [v for k, v in app_env.items() if v and k != BASE_URL_ENV]


def target_address(target: dict, host: dict) -> str:
    """Where people open the app: the target's public URL, else its domain (a host target
    serves on a port of its own — HelpDesk at 192.0.2.202:8000), else the bare host."""
    if target.get("public_url"):
        return str(target["public_url"]).rstrip("/")
    domains = _json(target.get("domains")) or []
    if domains and str(domains[0]).strip():
        domain = str(domains[0]).strip().rstrip("/")
        if "://" in domain:
            return domain
        checks = _json(target.get("health_checks")) or []
        scheme = str((checks[0] if checks and isinstance(checks[0], dict) else {})
                     .get("scheme") or "http")
        return f"{scheme}://{domain}"
    return f"http://{host['address']}"


def _deployment_ctx(target: dict, host: dict, commit: str, ref: str) -> dict[str, str]:
    return {"commit": commit, "ref": ref, "target.host": str(host["address"]),
            "target.user": str(host.get("ssh_user") or "root"),
            "target.public_url": target_address(target, host)}


def address_env(target: dict, host: dict) -> dict[str, str]:
    """What a deployed app is told about its own address, unless its manifest says otherwise:
    APP_BASE_URL, and PORT when the target serves on a port of its own. The one-container
    deploy always told the app these (templates.base_url); a stack must not forget them, or a
    sign-in app builds its callback URLs for localhost."""
    from urllib.parse import urlsplit

    url = target_address(target, host)
    out = {"APP_BASE_URL": url}
    port = urlsplit(url).port
    if port and (target.get("domains") or not is_stack_target(target)):
        out["PORT"] = str(port)
    return out


def _plan_infrastructure(engine, *, project_id: str, target: dict, run_id: Optional[str],
                         manifest: DeployManifest, access: Any
                         ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """`resources:` resolved and planned (platform/planner.py), or (None, None) without any.
    Raises StackDeployError, in words, for anything to fix before an approval is asked for."""
    if not manifest.resources:
        return None, None
    from ..platform.contract import ContractError
    from ..platform.deploy import StackHostRunner, plan_for_deploy
    from .governed_host_deploy import _app_key_for

    with engine.begin() as connection:
        app = _app_key_for(connection, project_id, target,
                           {"code_task_run_id": run_id} if run_id else None)
    if not app:
        raise StackDeployError("This target names no app, so its resources have no owner.",
                               "Give the target an app name in Infra, then deploy again.")
    try:
        doc = plan_for_deploy(engine, project_id=project_id, app_key=app,
                              environment=str(target.get("environment") or "deploy"),
                              resources=list(manifest.resources), host=StackHostRunner(access))
    except ContractError as exc:
        raise StackDeployError("resources in boss.deploy.yaml cannot be planned: "
                               + "; ".join(exc.reasons) + ".",
                               "Enable a plugin that provides it in Infra › Plugins, or fix "
                               "resources: in boss.deploy.yaml.")
    if doc["refusals"]:
        raise StackDeployError("Policy refuses this infrastructure plan: "
                               + "; ".join(r["message"] for r in doc["refusals"]) + ".",
                               "Fix resources: in boss.deploy.yaml and deploy again.")
    clash = sorted(set(doc["bindings"]) & set(manifest.env_keys()))
    if clash:
        raise StackDeployError(f"{', '.join(clash)} is set both by env: and by a resource binding.",
                               "Remove it from env: (the resource provides it), or rename the "
                               "binding with bindings: under the resource.")
    return doc, app


def join_resource_project(rendered: dict[str, Any], app_project: str,
                          shared: Optional[str]) -> dict[str, Any]:
    """A `kind: compose` app with `resources:` runs its compose commands IN THE RESOURCES'
    PROJECT, so its services share one default network with `res-<name>`. Run as a project of
    its own, HelpDesk AI's app could not resolve its database — `getaddrinfo EAI_AGAIN res-db`
    (sdep_3521765ccd404540ba1d) — while res-db sat healthy in bossapp-helpdesk-ai-deploy.
    Resource services are named `res-…` precisely so the two never collide in one project."""
    if not shared or not app_project or shared == app_project:
        return rendered
    old, new = f" -p {app_project} ", f" -p {shared} "

    def steps(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**s, "run": str(s.get("run") or "").replace(old, new)} for s in items or []]

    def retire_first(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # THE APP'S OWN OLD PROJECT GOES FIRST: an earlier deploy under `-p <app>` kept the port
        # (helpdesk-ai-app-1 held 0.0.0.0:8000, sdep_ca4042c2 'port is already allocated').
        # Only that project's containers — never the shared one's resources.
        up = next((str(s.get("run") or "") for s in items or [] if old in str(s.get("run") or "")), "")
        if " up " not in up:
            return steps(items)
        down = up.split(" up ", 1)[0] + " down --remove-orphans || true"
        return ([{"name": "retire the app's own compose project", "run": down,
                  "timeout_seconds": 300}] + steps(items))

    return {**rendered, "install": retire_first(rendered.get("install")),
            "update": retire_first(rendered.get("update")),
            "teardown": steps(rendered.get("teardown"))}


#: A compose project name BOSS will ever retire (compose's own rule, lower-case).
_PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
RETIRE_OWN_STEP = "retire this app's earlier release"


def retire_own_projects(rendered: dict[str, Any], projects: list[str]) -> dict[str, Any]:
    """THIS APP'S OWN EARLIER RELEASES, retired before it comes up (step_recovery's
    `clean_and_retry`): `docker compose -p <project> down --remove-orphans` — containers and
    networks only, never `-v`: a volume (a database's data) is kept.

    Guarded ON THE HOST as well as in BOSS: a project is taken down only when EVERY one of its
    containers was started from under this target's own directory (compose's
    `project.working_dir` label), so another app's project — same name or not — is never
    touched; the step then fails with BOSS_NOT_THIS_APPS and nothing is removed. The project the
    app is about to run in (the resources' shared project, with its data services) is never in
    `projects` (step_recovery refuses it before planning)."""
    wanted = [p for p in dict.fromkeys(str(p or "").strip() for p in projects or [])
              if _PROJECT_NAME.match(p)]
    if not wanted:
        return rendered
    marker = shlex.quote(str(rendered.get("dir") or "").rstrip("/") + "/")
    steps = []
    for project in wanted:
        label = shlex.quote(f"label=com.docker.compose.project={project}")
        q = shlex.quote(project)
        steps.append({"name": f"{RETIRE_OWN_STEP} ({project})", "timeout_seconds": 300, "run": (
            f"ids=$(docker ps -aq --filter {label}); "
            "if [ -z \"$ids\" ]; then echo BOSS_NOTHING_TO_RETIRE " + q + "; exit 0; fi; "
            "if docker inspect -f '{{index .Config.Labels \"com.docker.compose.project.working_dir\"}}' "
            f"$ids | grep -v -F {marker} | grep -q .; then "
            f"echo BOSS_NOT_THIS_APPS {q}; exit 3; fi; "
            f"docker compose -p {q} down --remove-orphans && echo BOSS_RETIRED {q}")})
    return {**rendered, "install": steps + list(rendered.get("install") or []),
            "update": steps + list(rendered.get("update") or [])}


#: The step that retires this app's own containers from a project it shared with its resources.
RETIRE_SHARED_STEP = "retire the app's earlier release from its resources' project"


def earlier_shared_projects(connection: Any, target_id: str, keep: Optional[str]) -> list[str]:
    """The resources' shared compose projects an EARLIER release of this target's app ran in
    (`shared_compose_project` on its earlier stack deployments), other than the one this release
    runs in (`keep`).

    THE CASE (HelpDesk AI, sdep_f08aa1eb78af45d984d6, 2026-10-17). The previous build's manifest
    asked BOSS for its database (`resources:`), so its app ran inside the resources' project,
    bossapp-helpdesk-ai-deploy, publishing port 8000. The next build's manifest brought its own
    Postgres instead, so its release ran as the app's own project (helpdesk-ai) — and
    `compose up` failed on `Bind for 0.0.0.0:8000 failed: port is already allocated`, held by
    the old app container nobody retired: `retire_own_projects` only takes down projects EVERY
    container of which is the app's (that project also holds res-db, with its data), and the
    one-container retire step only knows the single-container release."""
    try:
        rows = connection.execute(
            text("SELECT DISTINCT manifest->>'shared_compose_project' AS p FROM stack_deployments "
                 "WHERE target_id = :t AND COALESCE(manifest->>'shared_compose_project', '') <> ''"),
            {"t": target_id}).all()
    except Exception:  # noqa: BLE001 — nothing to retire is the safe answer
        log.warning("stack deploy: earlier shared projects of %s not read", target_id,
                    exc_info=True)
        return []
    return sorted({str(r[0]) for r in rows
                   if r[0] and _PROJECT_NAME.match(str(r[0])) and str(r[0]) != (keep or "")})


def retire_app_in_shared(rendered: dict[str, Any], projects: list[str]) -> dict[str, Any]:
    """THIS APP'S OWN CONTAINERS in a project it shared with its resources, retired before the
    new release comes up (`earlier_shared_projects`): `docker rm -f` of exactly the containers
    started from under this target's directory (compose's `project.working_dir` label) — the
    resources' services (res-db and its volume, its data) are another working directory and are
    never touched, and nothing is ever removed with `-v`. Another app's containers have another
    directory, so the same guard keeps them. Nothing of the app there: nothing happens."""
    wanted = [p for p in dict.fromkeys(str(p or "").strip() for p in projects or [])
              if _PROJECT_NAME.match(p)]
    if not wanted:
        return rendered
    marker = shlex.quote(str(rendered.get("dir") or "").rstrip("/") + "/")
    steps = []
    for project in wanted:
        label = shlex.quote(f"label=com.docker.compose.project={project}")
        steps.append({"name": f"{RETIRE_SHARED_STEP} ({project})", "timeout_seconds": 300, "run": (
            f"for id in $(docker ps -aq --filter {label}); do "
            "wd=$(docker inspect -f '{{index .Config.Labels \"com.docker.compose.project.working_dir\"}}' "
            "\"$id\"); "
            f"case \"$wd/\" in {marker}*) n=$(docker inspect -f '{{{{.Name}}}}' \"$id\"); "
            "docker rm -f \"$id\" >/dev/null && echo \"BOSS_RETIRED ${n#/}\";; esac; done; "
            f"echo BOSS_SHARED_CHECKED {shlex.quote(project)}")})
    return {**rendered, "install": steps + list(rendered.get("install") or []),
            "update": steps + list(rendered.get("update") or [])}


def record_host_facts(engine, host_id: str, facts: Mapping[str, Any]) -> None:
    """What a preflight found the host to have, on the host's record (last_preflight.facts),
    so a build can be told before it writes its manifest and an agent can read it later.
    Its own short transaction; never fails a plan."""
    from .core import now_utc

    try:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE deployment_hosts SET last_preflight = "
                     "COALESCE(last_preflight, '{}'::jsonb) || jsonb_build_object("
                     "'facts', CAST(:f AS JSONB), 'facts_at', CAST(:at AS TEXT)), "
                     "updated_at = now() WHERE id = :h"),
                {"f": json.dumps(dict(facts)), "at": now_utc().isoformat(), "h": host_id})
    except Exception:  # noqa: BLE001 — facts are an aid; the plan's verdict stands without them
        log.warning("stack deploy: host facts for %s not recorded", host_id, exc_info=True)


def plan_stack_deploy(engine, settings: Settings, *, project_id: str, target_id: str,
                      repository: Optional[str], ref: str, actor: str,
                      run_id: Optional[str] = None, workflow_run_id: Optional[str] = None,
                      rollback_of: Optional[str] = None,
                      approver_role: Optional[str] = None,
                      workflow_step_id: Optional[str] = None,
                      manifest_path: Optional[str] = None,
                      approval_decision_id: Optional[str] = None,
                      retire_projects: Optional[list[str]] = None,
                      person_required: bool = False) -> dict[str, Any]:
    """Record a deployment of `repository@ref` to a stack target, held for approval.

    `person_required`: a workflow step that promised a person started it — only a person's
    answer approves it, never the environment's policy (deploy_policy.pre_approval).

    `retire_projects`: this app's own earlier compose projects to take down before it comes up
    (step_recovery's clean_and_retry; `retire_own_projects`, guarded on the host). Part of what
    is approved: the plan's steps and the decision say so.

    `manifest_path` is the boss.deploy.yaml the build's app DECLARED (`_declared_stack_target`):
    with it, the app's host target ships from that manifest like any stack target.

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
        declared = str(manifest_path or "").strip().strip("/")
        if not is_stack_target(target) and not declared:
            raise StackDeployError(
                f"Target {target.get('app_name')!r} is a {target.get('target_kind')!r} target, "
                "not a stack target.",
                "Create a stack target for the repository on the host (Infra), then deploy.")
        if target.get("status") != "active":
            raise StackDeployError(f"Target {target.get('app_name')!r} is {target.get('status')}.",
                                   "Restore the target, or deploy to an active one.")
        from ..templates.deploy_guard import deploy_refusal

        refused = deploy_refusal(connection, project_id,
                                 target={**target, "repository": repository or target.get("repo")},
                                 commit_ref=ref, source_run_id=run_id)
        if refused:
            raise StackDeployError(refused, refused.action, status_code=409)
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
        running = _live_deployment(connection, target_id, commit)
        if running:
            # The same commit to the same place is ONE deployment. A second click, a retried
            # workflow step or two operators at once must not queue a second install.
            return {**get_stack_deployment(connection, running["id"]), "reused": True}

    # WHAT THIS RELEASE SHIPS BEYOND WHAT IS SERVING (release_scope): every commit, the run it
    # came from, that run's approval and whether it was reviewed — on the card, bound to it.
    release_card = None
    if run_id and callable(getattr(github, "compare", None)):
        from . import release_scope

        with engine.begin() as connection:
            serving = release_scope.serving_of_target(connection, target_id)
            if serving:
                release_card = release_scope.ship_list(
                    connection, lambda base, head: github.compare(owner, repo, base, head),
                    serving_sha=serving, head_sha=commit, own_workflow_run_id=workflow_run_id,
                    own_run_ids=(run_id,), repository=f"{owner}/{repo}",
                    project_id=project_id)

    path = str(target.get("manifest_path") or declared or DEFAULT_MANIFEST_PATH)
    manifest_text = github.read_file(owner, repo, path, commit)
    if manifest_text is None:
        raise StackDeployError(
            f"{owner}/{repo} has no {path} at {commit[:12]}.",
            f"Add {path} to the repository (version: 1, name, install, health — see "
            "engine/boss_engine/services/app_deploy_manifest.py), or set the target's "
            "manifest path.", cause=CAUSE_APP)
    try:
        try:
            manifest = load_manifest(manifest_text)
            manifest_text, manifest = own_place_on_host(engine, manifest_text, manifest,
                                                        target, host)
            rendered = render(manifest, _deployment_ctx(target, host, commit, ref))
        except ManifestError as exc:
            raise StackDeployError(f"{path} at {commit[:12]} is not valid: {exc}",
                                   f"Fix {path} in the repository and deploy again.",
                                   cause=CAUSE_APP)

        with engine.begin() as connection:
            # THE APP'S DEPLOY ENVIRONMENT, by name — the same set the one-container deploy
            # passes (governed_host_deploy.app_secret_problem): generated secrets made now, a
            # required one with no deploy value refuses the plan in words, an optional one goes
            # only when set.
            from .governed_host_deploy import APP_SECRET_ACTION, app_secret_problem

            _app_key, app_secrets, secret_refusal = app_secret_problem(
                connection, project_id, target, {"code_task_run_id": run_id} if run_id else None)
            if secret_refusal:
                raise StackDeployError(secret_refusal, APP_SECRET_ACTION)
            app_secret_names = sorted({str(a["name"]) for a in app_secrets})
            unfit = manifest_misfit(connection, manifest=manifest, rendered=rendered, path=path,
                                    target=target, host=host, run_id=run_id,
                                    read=lambda p: github.read_file(owner, repo, p, commit),
                                    provided=app_secret_names)
        if unfit:
            # BEFORE ANYONE IS ASKED TO APPROVE: a plan that cannot work is refused in words.
            raise StackDeployError(unfit[0], unfit[1], cause=CAUSE_APP)
        with engine.begin() as connection:
            missing = {key: name for key, name in rendered["env_secrets"].items()
                       if not operator_secret_ref(connection, project_id, target_id, name)}
        if missing:
            raise StackDeployError(
                "The manifest reads secrets this target does not have: "
                + ", ".join(f"{key} → {name}" for key, name in missing.items()) + ".",
                _secret_action(target_id, sorted(set(missing.values()))))

        problems = preflight_host(access, manifest)
        facts = dict(getattr(problems, "facts", None) or {})
        if facts:
            # WHAT THE MACHINE HAS, kept where the next build and the deploy agent read it
            # (deploy_repair.host_brief, the step's Ask) — refused plan or not.
            record_host_facts(engine, str(host["id"]), facts)
        if problems:
            # A person's problem first: when the host needs something AND the manifest asks
            # for a program it lacks, fixing the app alone would not deploy it.
            ordered = sorted(problems, key=lambda p: p.get("cause") == CAUSE_APP)
            first = ordered[0]
            more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
            raise StackDeployError(first["headline"] + more, first["action"],
                                   cause=first.get("cause") or CAUSE_HOST, facts=facts)
    except StackDeployError as exc:
        # WHAT WAS REFUSED, with the refusal: the manifest as read at the commit, so a
        # repair of the app (deploy_repair) is told exactly what it wrote.
        exc.manifest_path, exc.manifest_text, exc.commit = path, manifest_text, commit
        raise

    infra_doc, infra_app = _plan_infrastructure(engine, project_id=project_id, target=target,
                                                run_id=run_id, manifest=manifest, access=access)
    shared_project = None
    if infra_doc is not None and manifest.kind == "compose" and manifest.compose_project:
        from ..infra_plugins._runtime import compose_project_for

        # THE RESOURCES' environment, not the target's name: resources default to "deploy"
        # (ResourceRequest.environment) while HelpDesk AI's target is called "staging", and the
        # app joined bossapp-helpdesk-ai-staging while res-db lived in …-deploy (sdep_ca4042c2).
        shared_project = compose_project_for(
            str(infra_app), str(manifest.resources[0].environment or "deploy"))
        rendered = join_resource_project(rendered, manifest.compose_project, shared_project)
    retire = [p for p in dict.fromkeys(str(x or "").strip() for x in retire_projects or [])
              if _PROJECT_NAME.match(p) and p != shared_project]
    with engine.connect() as connection:
        moved_from = earlier_shared_projects(connection, target_id, keep=shared_project)

    deployment_id = make_id("sdep")
    short = commit[:12]
    # A manifest in a directory speaks for that directory: its compose file and its commands
    # are relative to it (helpdesk-ai/boss.deploy.yaml's docker-compose.yml is helpdesk-ai's).
    summary = {**manifest.summary(), "manifest_path": path,
               "workdir": manifest_workdir(path), "app_secrets": app_secret_names,
               # Read back at apply, which renders the manifest again (join_resource_project).
               **({"shared_compose_project": shared_project} if shared_project else {}),
               **({"retire_projects": retire} if retire else {}),
               **({"retire_app_in": moved_from} if moved_from else {})}
    rp = ResourcePlaneService(settings)
    with engine.begin() as connection:
        # Check-then-insert under ONE lock per target. Everything above took seconds (GitHub,
        # an ssh preflight), so the early `running` lookup cannot be the guarantee: a double
        # click, a retried step or two operators would each get past it. The unique index on
        # live (target, commit) rows is the backstop for anything that skips this function.
        connection.execute(text("SELECT pg_advisory_xact_lock(:k)"),
                           {"k": _lock_key("stack-plan", target_id)})
        running = _live_deployment(connection, target_id, commit)
        if running:
            return {**get_stack_deployment(connection, running["id"]), "reused": True}
        clash = connection.execute(
            text("SELECT d.id, d.target_id, t.app_name, t.environment FROM stack_deployments d "
                 "JOIN deployment_targets t ON t.id = d.target_id "
                 "WHERE d.host_id = :h AND d.deploy_dir = :dir AND d.target_id <> :t "
                 "AND t.status = 'active' AND d.status <> 'rejected' "
                 "ORDER BY d.created_at DESC LIMIT 1"),
            {"h": host["id"], "dir": manifest.dir, "t": target_id}).mappings().first()
        if clash:
            raise StackDeployError(
                f"{manifest.dir} on {host['name']} already belongs to {clash['app_name']} "
                f"({clash['environment']}, target {clash['target_id']}).",
                "Two targets on one host must not share a directory: each has its own "
                "generated secrets and would overwrite the other's deploy.env. Set a different "
                "`dir` in this manifest (for example per environment), or deploy to another "
                "host.", status_code=409)
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
        # ONE GATE, ONE RUN. From a workflow's deploy step this decision IS that step's gate,
        # so it is opened as that run's step's own (run_decisions.open_step_decision): two runs
        # shipping the same commit to the same host get a card each, and approving one queues
        # one apply. A deploy started from the console has no run and merges as an alert does.
        from .run_decisions import open_step_decision

        question = f"Deploy {owner}/{repo}@{short} to {host['name']} ({manifest.dir})?"
        if infra_doc:
            from ..platform.deploy import preview as _infra_preview
            from ..platform.planner import summary_words

            question += f" Infrastructure: {summary_words(infra_doc)}."
        recommendation = {"action": "deploy_stack", "deployment_id": deployment_id,
            "changeset_id": changeset["id"], "target_id": target_id,
            "repository": f"{owner}/{repo}", "ref": ref, "commit": commit,
            "host": host["name"], "dir": manifest.dir, "steps": summary["steps"],
            "env_keys": summary["env_keys"],
            # THIS APP'S OWN EARLIER RELEASES to take down first (step_recovery), by name.
            **({"retire_projects": retire} if retire else {}),
            # Which BOSS secret each key carries to the host: the approver sees the names.
            "env_secrets": dict(rendered["env_secrets"]),
            "generated_env_keys": summary["generated_env_keys"],
            # The app's own deploy secrets (its Setup), by name only — never a value.
            "app_secrets": app_secret_names,
            # A workflow's deploy step has no gate of its own when it ships to a stack: THIS
            # decision is that gate. So it carries the step's approver and where it came
            # from, and approving it here is approving the release (one approval, not two).
            **({"approver_role": approver_role} if approver_role else {}),
            # THE MONITORING PACK THIS RELEASE CARRIES (monitoring_pack): approved with it.
            **({"monitoring_pack": pack} if (pack := _monitoring_pack_card(
                connection, workflow_run_id, workflow_step_id)) else {}),
            # The infrastructure plan (resources, bindings by name, ports, public access,
            # destructive actions, cost) IS part of what is approved: the decision preview shows
            # it and its content hash is pinned like any approved content (action_preview).
            **({"infra_plan": infra_doc, "action_preview": _infra_preview(infra_doc)}
               if infra_doc else {})}
        if release_card:
            from . import release_scope

            # In place: the card keeps its `deploy_stack` action, only gaining the list.
            recommendation.update(release_scope.attach(recommendation, release_card,
                                                       step_label="Deploy"))
            if release_card.get("needs_reason"):
                question += " It carries commits from rejected or unreviewed runs."
        cost = {"expected_cost_usd_micros": 0, "reservation_id": reservation["reservation_id"],
                "cost_confidence": "no_provider_spend"}
        # ALREADY APPROVED? The workflow gate's decision (passed explicitly) or the
        # environment's policy (deploy_policy) — then no card of its own; dual control never.
        from .deploy_policy import pre_approval

        approval = pre_approval(connection, project_id, target.get("environment"),
                                gate_decision_id=approval_decision_id,
                                person_required=person_required)
        if approval and rp.required_approvers_for_changeset(connection, changeset["id"]):
            approval = None
        if approval and release_card and release_card.get("needs_reason"):
            # NO POLICY SHIPS REJECTED OR UNREVIEWED COMMITS: only a person's approval of a
            # release card that listed exactly this head (and, needing one, gave a reason).
            from . import release_scope

            gate = None
            if approval.get("decision_id"):
                gate = release_scope.of_decision(connection.execute(
                    text("SELECT recommendation FROM decisions WHERE id = :d"),
                    {"d": approval["decision_id"]}).scalar())
            if not (gate and gate.get("head_sha") == commit):
                approval = None
        if approval:
            decision_id = approval["decision_id"]
            recommendation["approval"] = {k: approval[k]
                                          for k in ("source", "decision_id", "approved_by")}
        elif workflow_run_id:
            decision_id = open_step_decision(
                connection, project_id, None, question, recommendation, cost,
                "stack_deploy_requires_approval", run_id=workflow_run_id,
                step_id=str(workflow_step_id or "deploy"), risk_tier=2, default_action="hold")
        else:
            decision_id = create_blocking_decision(
                connection, project_id, None, question, recommendation, cost,
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
        if not approval:
            connection.execute(
                text("UPDATE decisions SET external_ref = :c WHERE id = :d"),
                {"c": changeset["id"], "d": decision_id})
        steps = [{"name": n, "status": "pending", "started_at": None, "ended_at": None}
                 for n in ["prepare", "upload", "environment", "switch"]
                 + ([] if is_stack_target(target) else [RETIRE_STEP])
                 + [f"{RETIRE_OWN_STEP} ({p})" for p in retire]
                 + [f"install or update: {s}" for s in summary["steps"]["install"]]
                 + (["health"] if manifest.health else []) + ["finalize"]]
        connection.execute(
            text("INSERT INTO stack_deployments "
                 "(id, project_id, target_id, host_id, repository, ref, commit_sha, manifest, "
                 " manifest_text, status, decision_id, changeset_id, reservation_id, steps, "
                 " log_tail, open_url, preflight, workflow_run_id, code_task_run_id, "
                 " rollback_of, created_by, deploy_dir) "
                 "VALUES (:i, :p, :t, :h, :repo, :ref, :c, CAST(:m AS JSONB), :mt, "
                 "        'awaiting_approval', :d, :cs, :res, CAST(:steps AS JSONB), '', :open, "
                 "        CAST(:pf AS JSONB), :wr, :cr, :rb, :by, :dir)"),
            {"i": deployment_id, "p": project_id, "t": target_id, "h": host["id"],
             "repo": f"{owner}/{repo}", "ref": ref, "c": commit, "m": json.dumps(summary),
             "mt": manifest_text, "d": decision_id, "cs": changeset["id"],
             "res": reservation["reservation_id"], "steps": json.dumps(steps),
             "open": rendered["open_url"],
             "pf": json.dumps({"ok": True, "host": host["name"],
                               "address": host.get("address"),
                               **({"facts": facts} if facts else {})}),
             "wr": workflow_run_id, "cr": run_id, "rb": rollback_of, "by": actor,
             "dir": manifest.dir})
        if infra_doc:
            from ..platform.deploy import link_decision, store_plan

            plan_id = store_plan(connection, project_id=project_id, app_key=str(infra_app),
                                 environment=str(target.get("environment") or "deploy"),
                                 doc=infra_doc, deployment_id=deployment_id, actor=actor)
            link_decision(connection, plan_id, decision_id)
        emit_event(connection, project_id, "deployment.stack_planned", actor,
                   "stack_deployment", deployment_id,
                   {"target_id": target_id, "repository": f"{owner}/{repo}", "ref": ref,
                    "commit": commit, "changeset_id": changeset["id"],
                    "decision_id": decision_id, "workflow_run_id": workflow_run_id})
        if approval:
            from .deploy_policy import record_pre_approval

            record_pre_approval(connection, project_id, approval,
                                entity_type="stack_deployment", entity_id=deployment_id,
                                actor=actor)
            on_decision_approved(connection, settings,
                                 {"id": decision_id,
                                  "recommendation": {"deployment_id": deployment_id}},
                                 approval["approved_by"])
        return get_stack_deployment(connection, deployment_id)


def rollback_stack_deploy(engine, settings: Settings, *, deployment_id: str,
                          actor: str) -> dict[str, Any]:
    """Plan a NEW governed deployment of the commit that was healthy before this one."""
    with engine.begin() as connection:
        row = _row(connection, deployment_id)
        if not row:
            raise StackDeployError("No such deployment.", "Pick one from the target's list.",
                                   status_code=404)
        current = get_stack_deployment(connection, deployment_id) or {}
    # The same rule that decides whether the console shows the button: one source of truth.
    if "rollback" not in (current.get("actions") or []):
        raise StackDeployError(
            "This deployment cannot be rolled back.",
            "Roll back is offered only on a target's latest applied deployment, once it has "
            "finished and an earlier healthy commit exists; otherwise deploy a commit that "
            "works.", status_code=409)
    # A HOST TARGET SHIPPING ITS APP'S DECLARED MANIFEST (`_declared_stack_target`) is planned
    # with that manifest's path, like the workflow's deploy that made this release: without it
    # plan_stack_deploy refused the rollback of every such release ("Target 'HelpDesk AI' is a
    # 'ssh' target, not a stack target", HelpDesk AI 2026-10-17).
    return plan_stack_deploy(engine, settings, project_id=row["project_id"],
                             target_id=row["target_id"], repository=row["repository"],
                             ref=str(current["rollback_commit"]), actor=actor,
                             rollback_of=deployment_id,
                             manifest_path=release_manifest_path(row))


def release_manifest_path(row: Mapping[str, Any]) -> Optional[str]:
    """The boss.deploy.yaml a recorded stack deployment shipped from (its manifest summary's
    `manifest_path`), or None — what any later plan of the same app on the same target reuses."""
    manifest = row.get("manifest")
    if isinstance(manifest, str):
        try:
            manifest = json.loads(manifest or "{}")
        except ValueError:
            manifest = {}
    path = str((manifest or {}).get("manifest_path") or "").strip().strip("/")
    return path or None


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
    rp = ResourcePlaneService(settings)
    changeset = rp.get_changeset(connection, row["project_id"], row["changeset_id"])
    if changeset.get("status") != "approved":
        # The decision is the ChangeSet's only door (the generic ChangeSet routes refuse a
        # stack deployment), and one already approved still gets its job queued below.
        changeset = rp.approve_changeset(connection, changeset_id=row["changeset_id"],
                                         actor=actor, phased_owner=True)
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
        {"c": row["changeset_id"], "r": f"decision {outcome} by {_who(actor, connection)}"})
    if row["reservation_id"]:
        BudgetGovernor(settings).release(connection, row["project_id"], row["reservation_id"],
                                         "Stack deployment was not approved.")
    emit_event(connection, row["project_id"], "deployment.stack_rejected", actor,
               "stack_deployment", deployment_id, {"outcome": outcome})


# ── Execute (the worker's leased job, handed to the API) ─────────────────────────────────────
#
# An apply is three phases, and only the middle one is long:
#
#   1. one short transaction, COMMITTED before any host write: the ChangeSet `applying`, its
#      receipt and an `in_progress` resource receipt, and the row's apply_started_at plus an
#      owner token. A host is never written without a durable record that BOSS was writing it.
#   2. the host work, with NO transaction open. Progress goes out in its own tiny writes,
#      guarded by the owner token.
#   3. one short transaction: the verdict on the receipts, the ChangeSet and the row.
#
# Two session-level advisory locks, held by the applying request for all three phases, are how
# everyone else tells "an apply is running" from "an apply died": one per deployment, one per
# (host, directory). Postgres drops them when the process holding them dies, so a replay that
# CAN take the deployment's lock knows nobody is applying it any more — and one that cannot is
# looking at a live apply and waits instead of failing it.

#: How long a job waits before asking again whether the target is free.
WAIT_SECONDS = 30


class _ApplyLocks:
    """Session advisory locks on a dedicated AUTOCOMMIT connection: no transaction stays open."""

    def __init__(self, engine):
        self._engine = engine
        self._conn = None
        self._held: list[int] = []

    def __enter__(self) -> "_ApplyLocks":
        self._conn = self._engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        return self

    def try_lock(self, key: int) -> bool:
        got = bool(self._conn.execute(text("SELECT pg_try_advisory_lock(:k)"),
                                      {"k": key}).scalar())
        if got:
            self._held.append(key)
        return got

    def __exit__(self, *exc) -> None:
        try:
            for key in reversed(self._held):
                self._conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
        except Exception:  # noqa: BLE001 — closing the session releases them regardless
            log.warning("stack deploy: advisory unlock failed", exc_info=True)
        finally:
            self._conn.close()


def _short(connection: Connection) -> None:
    """Nothing in an apply's bookkeeping waits long on a row lock: it fails and says so."""
    connection.execute(text("SET LOCAL lock_timeout = '10s'"))


def _fail(connection: Connection, deployment_id: str, project_id: str, *, headline: str,
          action: str, actor: str, steps: Optional[list] = None,
          log_tail: Optional[str] = None, cause: Optional[str] = None) -> None:
    """Fail a deployment with its words. `cause` (CAUSE_APP / CAUSE_HOST) is whose fix it is;
    unsaid, it is a person's — BOSS never auto-repairs a failure it cannot place."""
    failed_step = next((str(s.get("name")) for s in steps or []
                        if isinstance(s, dict) and s.get("status") == "failed"), None)
    fields: dict[str, Any] = {"status": "failed", "finished_at": "__now__",
                              "failure_reason": headline, "waiting_on": None,
                              "error": {"headline": headline, "action": action,
                                        "cause": cause or CAUSE_HOST,
                                        **({"step": failed_step} if failed_step else {})}}
    if steps is not None:
        fields["steps"] = steps
    if log_tail is not None:
        fields["log_tail"] = log_tail
    _set(connection, deployment_id, **fields)
    emit_event(connection, project_id, "deployment.stack_failed", actor, "stack_deployment",
               deployment_id, {"headline": headline, "cause": cause or CAUSE_HOST,
                               **({"step": failed_step} if failed_step else {})})


def _close_governance(connection: Connection, settings: Settings, row: Any, *, actor: str,
                      reason: str) -> None:
    """A deployment that ends without a verdict from its apply still closes its ChangeSet:
    an `applying` one gets its receipts failed, an unapplied one is deadlettered and hands
    back its reservation."""
    from .budget import BudgetGovernor
    from .resource_plane import ResourcePlaneService

    status = connection.execute(text("SELECT status FROM changesets WHERE id = :c"),
                                {"c": row["changeset_id"]}).scalar()
    if status == "applying":
        ResourcePlaneService(settings).complete_phased_apply(
            connection, changeset_id=row["changeset_id"], actor=actor, outcomes={},
            missing_message=reason)
    elif status in ("approved", "pending_approval", "policy_checked", "budget_reserved"):
        connection.execute(
            text("UPDATE changesets SET status = 'deadlettered', failure_reason = :r, "
                 "updated_at = now() WHERE id = :c"), {"c": row["changeset_id"], "r": reason})
        if row["reservation_id"]:
            BudgetGovernor(settings).release(connection, row["project_id"],
                                             row["reservation_id"], reason)


def execute_stack_job(engine, settings: Settings, *, deployment_id: str, job_id: str,
                      worker_id: str, lease_token: str, adapter: Any = None) -> dict[str, Any]:
    """Apply an approved deployment under the worker's lease. Idempotent across replays.

    Returns `job_deferred: True` when the job was handed back to the queue without spending an
    attempt (the worker then neither completes nor fails it): this deployment is being applied
    by another request right now, or another deployment is writing the same host directory.
    """
    from .jobs import JobLeaseService, LeaseNotFoundError
    from .resource_plane import ResourcePlaneService

    jobs = JobLeaseService(settings)
    with _ApplyLocks(engine) as locks:
        with engine.begin() as connection:
            _short(connection)
            try:
                jobs.require_active_lease(connection, worker_id, job_id, lease_token)
            except LeaseNotFoundError as exc:
                return {"status": "failed", "retry": False, "error": f"lease: {exc}"}
            row = _row(connection, deployment_id, lock=True)
            if not row:
                return {"status": "failed", "retry": False, "error": "no such deployment"}
            if row["status"] != "running":
                return {"status": row["status"], "retry": False, "deployment": serialize(row)}
            if not locks.try_lock(_lock_key("stack-deploy", deployment_id)):
                # Another request is applying THIS deployment right now — typically a worker
                # restarted while the API kept going. Its verdict is coming; replaying would be
                # wrong and failing it would be a lie.
                note = "This deployment is still being applied; waiting for its result."
                jobs.defer_leased(connection, worker_id, job_id, lease_token,
                                  delay_seconds=WAIT_SECONDS, reason="stack_deploy_in_flight",
                                  message=note)
                return {"status": "running", "retry": False, "job_deferred": True,
                        "error": note, "deployment": serialize(row)}
            if row["apply_started_at"] is not None:
                # A REPLAY AFTER A CRASH: an apply started, and nobody holds its lock, so the
                # process running it is gone. The host may be half way through an install, and
                # running it again blind is how a database gets initialised twice. Stop, say
                # so, and let a person decide: deploying again runs `update` once `installed`
                # exists.
                _fail(connection, deployment_id, row["project_id"],
                      headline="BOSS was interrupted while this deployment was running, so the "
                               "host may be part-way through it.",
                      action="Open the host's log to see where it stopped, then deploy again or "
                             "roll back.", actor=worker_id)
                _close_governance(connection, settings, row, actor=worker_id,
                                  reason="apply interrupted; not replayed")
                return {"status": "failed", "retry": False,
                        "deployment": serialize(_row(connection, deployment_id))}
            directory = str(row["deploy_dir"] or "")
            if not directory:
                from .app_deploy_manifest import load_manifest as _load

                directory = _load(row["manifest_text"]).dir
            if not locks.try_lock(_lock_key("stack-dir", str(row["host_id"] or ""), directory)):
                other = connection.execute(
                    text("SELECT id FROM stack_deployments WHERE host_id = :h "
                         "AND deploy_dir = :d AND id <> :i AND status = 'running' "
                         "AND apply_started_at IS NOT NULL ORDER BY apply_started_at LIMIT 1"),
                    {"h": row["host_id"], "d": directory, "i": deployment_id}).scalar()
                note = (f"Waiting for deployment {other or '(another)'} to finish writing "
                        f"{directory} on this host.")
                _set(connection, deployment_id, waiting_on=other or "another deployment")
                jobs.defer_leased(connection, worker_id, job_id, lease_token,
                                  delay_seconds=WAIT_SECONDS, reason="stack_deploy_target_busy",
                                  message=note)
                return {"status": "waiting", "retry": False, "job_deferred": True,
                        "error": note, "deployment": serialize(_row(connection, deployment_id))}
            actor = str(row["created_by"] or worker_id)
            try:
                started = ResourcePlaneService(settings).begin_phased_apply(
                    connection, changeset_id=row["changeset_id"], actor=actor,
                    created_by_job_id=job_id)
            except ValueError as exc:
                _fail(connection, deployment_id, row["project_id"],
                      headline=f"The ChangeSet could not be applied: {exc}",
                      action="Plan the deployment again.", actor=worker_id)
                _close_governance(connection, settings, row, actor=worker_id, reason=str(exc))
                return {"status": "failed", "retry": False,
                        "deployment": serialize(_row(connection, deployment_id))}
            token = _secrets.token_hex(16)
            _set(connection, deployment_id, apply_started_at="__now__", job_id=job_id,
                 apply_token=token, waiting_on=None)
        # Phase 1 is committed. From here to the verdict no transaction is open.
        operation_id = str(started[0]["operation"]["id"])
        try:
            outcome = apply_on_host(engine, settings, deployment_id=deployment_id,
                                    apply_token=token, dedupe_key=started[0]["dedupe_key"],
                                    adapter=adapter)
            error = None
        except _ApplyFailed as exc:
            outcome, error = None, exc
        except PermissionError as exc:
            # Governance changed under us between phases: nothing was written; say so.
            outcome, error = None, _ApplyFailed(str(exc), "Plan the deployment again.")
        with engine.begin() as connection:
            _short(connection)
            return _finalize(connection, settings, deployment_id=deployment_id, token=token,
                             operation_id=operation_id, outcome=outcome, error=error,
                             actor=actor)


class _ApplyFailed(Exception):
    """The host work ended without a healthy stack: headline, action and what had run."""

    def __init__(self, headline: str, action: str, steps: Optional[list] = None,
                 log_tail: Optional[str] = None, cause: Optional[str] = None):
        super().__init__(headline)
        self.headline = headline
        self.action = action
        self.steps = steps
        self.log_tail = log_tail
        self.cause = cause


def _finalize(connection: Connection, settings: Settings, *, deployment_id: str, token: str,
              operation_id: str, outcome: Optional[dict], error: Optional[_ApplyFailed],
              actor: str) -> dict[str, Any]:
    from .resource_plane import ResourcePlaneService

    row = _row(connection, deployment_id, lock=True)
    if error is None:
        outcomes = {operation_id: {"result": outcome["provider_result"]}}
    else:
        outcomes = {operation_id: {"error": {"message": error.headline,
                                             "error_type": "StackDeployFailed"}}}
    ResourcePlaneService(settings).complete_phased_apply(
        connection, changeset_id=row["changeset_id"], actor=actor, outcomes=outcomes)
    if row["status"] == "running" and row["apply_token"] == token:
        if error is None:
            _set(connection, deployment_id, status="healthy", finished_at="__now__",
                 steps=outcome["steps"], log_tail=outcome["log_tail"],
                 open_url=outcome["open_url"], error=None, failure_reason=None)
            emit_event(connection, row["project_id"], "deployment.stack_healthy", actor,
                       "stack_deployment", deployment_id,
                       {"commit": row["commit_sha"], "mode": outcome["mode"],
                        "open_url": outcome["open_url"]})
            checks = target_health_checks(outcome.get("health") or [])
            if checks:
                # ONE DEFINITION OF HEALTHY. The target's health_checks are what the monitor
                # samples and what a later deploy gate reads; they were "/" while the manifest
                # said /readyz, so a blank page kept passing everything but the deploy itself.
                connection.execute(
                    text("UPDATE deployment_targets SET health_checks = CAST(:h AS JSONB), "
                         "updated_at = now() WHERE id = :t"),
                    {"h": json.dumps(checks), "t": row["target_id"]})
        else:
            _fail(connection, deployment_id, row["project_id"], headline=error.headline,
                  action=error.action, actor=actor, steps=error.steps, log_tail=error.log_tail,
                  cause=error.cause)
    final = _row(connection, deployment_id)
    return {"status": final["status"], "retry": False, "deployment": serialize(final)}


def target_health_checks(rendered_health: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The manifest's health checks in the target's shape ({kind, scheme, port, path,
    expect_status}) — the ones that can be probed from off the host. A check against the host's
    own loopback (127.0.0.1, localhost) only means something on the host, so it is left to
    the deploy, which runs it there."""
    from urllib.parse import urlsplit

    out: list[dict[str, Any]] = []
    for check in rendered_health or []:
        parts = urlsplit(str(check.get("url") or ""))
        if parts.scheme not in ("http", "https") or not parts.hostname:
            continue
        if parts.hostname in ("127.0.0.1", "localhost", "::1"):
            continue
        out.append({"kind": "http", "scheme": parts.scheme,
                    "port": parts.port or (443 if parts.scheme == "https" else 80),
                    "path": (parts.path or "/") + (f"?{parts.query}" if parts.query else ""),
                    "expect_status": int(check.get("expect") or 200)})
    return out


def _require_governed(connection: Connection, changeset_id: str, dedupe_key: str,
                      deployment: Any) -> None:
    """The ONLY door to a host write in Community: an applying ChangeSet that was approved,
    with its reservation and this operation's receipt already COMMITTED (phase one)."""
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


def _require_owner(deployment: Any, apply_token: str) -> None:
    """Only the apply that started this deployment (phase one, under its job) writes the host.

    Every other caller — the generic ChangeSet apply, a stale request, a replay — is refused
    here, so nothing can skip the busy-target lock, the replay fence or the approval.
    """
    if deployment is None:
        raise PermissionError("Stack deployment refused: no such deployment.")
    if deployment["status"] != "running" or deployment["apply_started_at"] is None:
        raise PermissionError(
            f"Stack deployment refused: it is {deployment['status']}, not running under its "
            "worker job.")
    if not deployment["job_id"] or not apply_token or deployment["apply_token"] != apply_token:
        raise PermissionError("Stack deployment refused: this request does not own its apply.")


def apply_on_host(engine, settings: Settings, *, deployment_id: str, apply_token: str,
                  dedupe_key: str, adapter: Any = None) -> dict[str, Any]:
    """Phase two: the host work, with no database transaction open while it runs.

    Raises PermissionError before touching anything when the caller does not own a governed
    apply of this deployment, and _ApplyFailed (with the steps and log so far) when the stack
    did not come up healthy.
    """
    from .app_deploy_manifest import load_manifest as _load
    from .core import now_utc
    from .deployment_fleet import get_host

    applier: Optional[StackApplier] = None
    with engine.begin() as connection:
        deployment = _row(connection, deployment_id)
        _require_owner(deployment, apply_token)
        _require_governed(connection, str(deployment["changeset_id"]), dedupe_key, deployment)
        deployment = dict(deployment)
        project_id = deployment["project_id"]
        try:
            target = _target(connection, deployment["target_id"]) or {}
            host = get_host(connection, deployment["host_id"]) if deployment["host_id"] else None
            if not host:
                raise StackStepFailed("prepare", "The target's host no longer exists.",
                                      "Attach the target to a host in Infra and deploy again.")
            manifest = _load(deployment["manifest_text"])
            rendered = render(manifest, _deployment_ctx(target, host, deployment["commit_sha"],
                                                         deployment["ref"]))
            recorded = _json(deployment.get("manifest")) or {}
            rendered = join_resource_project(rendered, manifest.compose_project or "",
                                             recorded.get("shared_compose_project"))
            rendered = retire_own_projects(rendered, [
                p for p in recorded.get("retire_projects") or []
                if p != recorded.get("shared_compose_project")])
            rendered = retire_app_in_shared(rendered, [
                p for p in recorded.get("retire_app_in") or []
                if p != recorded.get("shared_compose_project")])
            rendered["workdir"] = str(recorded.get("workdir") or "")
            if not is_stack_target(target):
                # A host target shipping its app's declared manifest: its one-container release
                # (deploy_service, /opt/boss-apps/<app>) is replaced, not left holding the port.
                from .deploy_service import REMOTE_BASE, _app_name

                legacy = _app_name(str(target.get("repo") or ""), target.get("app_name"))
                rendered["retire_container"] = legacy
                rendered["retire_mount"] = f"{REMOTE_BASE}/{legacy}/"
            owner, repo = split_repository(deployment["repository"])
            access = host_access_for(connection, settings, host, adapter)
            github = github_source_for(connection, settings, project_id, owner, repo)
            env_values, hidden = _environment(connection, settings, deployment=deployment,
                                              manifest=manifest, rendered=rendered)
            app_env = app_deploy_env(connection, project_id, target,
                                     deployment.get("code_task_run_id"), manifest=manifest)
            env_values, hidden = merge_app_env(manifest, env_values, hidden, app_env)
            for key, value in address_env(target, host).items():
                env_values.setdefault(key, value)
        except (StackStepFailed, StackDeployError, ManifestError) as exc:
            raise _ApplyFailed(
                getattr(exc, "headline", None) or f"The manifest is not valid: {exc}",
                getattr(exc, "action", None) or "Fix boss.deploy.yaml and deploy again.",
                cause=(CAUSE_APP if isinstance(exc, ManifestError)
                       else getattr(exc, "cause", None)))

    def record(steps: list[dict[str, Any]], log_tail: str) -> None:
        try:
            with engine.begin() as c:
                _short(c)
                c.execute(
                    text("UPDATE stack_deployments SET steps = CAST(:s AS JSONB), "
                         "log_tail = :l, updated_at = now() WHERE id = :i "
                         "AND apply_token = :tok AND status = 'running'"),
                    {"s": json.dumps(steps), "l": log_tail, "i": deployment_id,
                     "tok": apply_token})
        except Exception:  # noqa: BLE001 — progress is for watching; the verdict is phase 3
            log.warning("stack deploy %s: progress not recorded", deployment_id, exc_info=True)

    infra: list[dict[str, Any]] = []
    if manifest.resources:
        # Infrastructure first (platform/deploy.py): the approved, hash-pinned plan runs through
        # receipts under THIS leased apply; the app's env file then gets its bindings, resolved
        # from the vault now and redacted like every other value.
        from ..platform.contract import ContractError
        from ..platform.deploy import StackHostRunner, apply_and_env
        from ..platform.executor import ApplyRefused
        from .host_key_pin import pinned as _pinned

        def lost() -> bool:
            with engine.begin() as c:
                return c.execute(text("SELECT 1 FROM stack_deployments WHERE id = :i AND "
                                      "apply_token = :t AND status = 'running'"),
                                 {"i": deployment_id, "t": apply_token}).first() is None
        try:
            with _pinned(engine, deployment.get("host_id")):
                infra, bound = apply_and_env(
                    engine, deployment_id=deployment_id, project_id=project_id,
                    resources=list(manifest.resources), actor_id=deployment.get("created_by"),
                    is_cancelled=lost,
                    host=StackHostRunner(access, changeset_id=str(deployment["changeset_id"])))
        except (ApplyRefused, ContractError) as exc:
            raise _ApplyFailed(f"Infrastructure: {redact(str(exc), hidden)}",
                               "Fix the resource or its plugin, then deploy again (a new plan "
                               "needs a new approval).")
        env_values.update(bound)
        hidden = hidden + list(bound.values())

    try:
        applier = StackApplier(
            access=access, rendered=rendered, commit=deployment["commit_sha"],
            ref=deployment["ref"], env_values=env_values,
            redact_values=hidden + [access.key_material],
            changeset_id=str(deployment["changeset_id"]),
            fetch_tree=lambda dest: github.download_tarball(owner, repo,
                                                            deployment["commit_sha"], dest),
            record=record, now=lambda: now_utc().isoformat())
        from .host_key_pin import pinned

        with pinned(engine, deployment.get("host_id")):
            outcome = applier.run()
    except (StackStepFailed, StackDeployError) as exc:
        raise _ApplyFailed(exc.headline, exc.action, applier.steps if applier else None,
                           applier.log if applier else None, cause=exc.cause)
    except Exception as exc:  # noqa: BLE001 — recorded with a sentence, never a stack trace
        log.warning("stack deploy %s failed unexpectedly", deployment_id, exc_info=True)
        said = explain_failure("deploy", 1, redact(str(exc), hidden + [access.key_material]))
        raise _ApplyFailed(said["headline"], said["action"],
                           applier.steps if applier else None, applier.log if applier else None)
    return {
        **outcome,
        "health": list(rendered["health"]),
        **({"infrastructure": infra} if infra else {}),
        "provider_result": {
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
        },
    }


def fail_for_deadlettered_job(connection: Connection, job: dict, error: dict,
                              settings: Optional[Settings] = None) -> None:
    """A deadlettered apply job fails its deployment, with the reason in words — unless an
    apply of it is still running somewhere, which owns the verdict."""
    if job.get("kind") != JOB_KIND:
        return
    payload = _json(job.get("payload")) or {}
    deployment_id = str(payload.get("deployment_id") or "")
    row = _row(connection, deployment_id, lock=True) if deployment_id else None
    if not row or row["status"] != "running":
        return
    if not connection.execute(text("SELECT pg_try_advisory_xact_lock(:k)"),
                              {"k": _lock_key("stack-deploy", deployment_id)}).scalar():
        return
    _fail(connection, deployment_id, row["project_id"],
          headline="The worker job running this deployment stopped before it finished.",
          action=f"Deploy again. The worker said: {str(error.get('message') or '')[:200]}",
          actor="worker")
    if settings is not None:
        _close_governance(connection, settings, row, actor="worker",
                          reason="the apply job was deadlettered")


# ── Workflows: the deploy step, for a stack target ───────────────────────────────────────────

def build_repository(spec: Any, default_owner: str = "") -> Optional[str]:
    """owner/repo of the repository a build worked in, or None when it cannot be told.

    The owner is part of the identity: `someone/shop` is not `acme/shop`, and a pull request
    number means nothing outside its own repository.
    """
    spec = _json(spec) or {}
    repo = str(spec.get("repo") or "").strip().removesuffix(".git").strip("/")
    if "/" in repo:
        owner, _, repo = repo.rpartition("/")
        owner = owner.rsplit("/", 1)[-1]
    else:
        owner = str(spec.get("owner") or default_owner or "").strip()
    return f"{owner}/{repo}" if owner and repo else None


def _same_repository(a: Optional[str], b: Optional[str]) -> bool:
    return bool(a) and bool(b) and str(a).lower() == str(b).lower()


def declared_manifest_for_run(connection: Connection, run_id: str) -> Optional[str]:
    """The boss.deploy.yaml this build's app declares itself with (app_deploy_manifest.
    app_manifest), read from every file the build's LINEAGE wrote and the manifests its
    repository already had (`repo_manifests`) — a repair touches only
    what it fixed, and the manifest was written by the first attempt."""
    run_id = str(run_id or "").strip()
    if not run_id:
        return None
    rows = connection.execute(
        text("""
            SELECT state->'impl_files' AS f, state->'repo_manifests' AS m FROM code_task_runs
            WHERE COALESCE(finalize_ctx->>'root_run_id', id) = (
                SELECT COALESCE(finalize_ctx->>'root_run_id', id) FROM code_task_runs
                WHERE id = :i)
        """), {"i": run_id}).mappings().all()
    files: list[str] = []
    existing: list[str] = []
    for r in rows:
        raw = _json(r["f"]) or []
        files.extend(str(f) for f in raw if isinstance(f, str))
        had = _json(r["m"]) or []
        existing.extend(str(m) for m in had if isinstance(m, str))
    # What the repository already declared counts too (app_deploy_manifest.app_manifest): a
    # feature added to an app that says how it runs is deployed the way it says.
    return app_manifest(files, existing)


def _declared_stack_target(connection: Connection, project_id: str,
                           inputs: dict[str, Any], target: Optional[dict],
                           default_owner: str, create: bool = False) -> Optional[dict]:
    """The app's own host target, deployed from the manifest its build declared — or None.

    AN APP THAT SAYS HOW IT RUNS IS DEPLOYED THE WAY IT SAYS. Its target was made for the
    one-container deploy (an SSH target on a host), and that deploy guesses: it served
    HelpDesk's unbuilt index.html from nginx. The same target, on the same host, ships from the
    manifest instead — through the same governed plan, approval and receipt as any stack.
    """
    run_id = str(inputs.get("run_id") or "").strip()
    path = declared_manifest_for_run(connection, run_id) if run_id else None
    if not path:
        return None
    if target is None:
        from .delivery_pipeline import app_name_of_run, pick_deploy_target

        run = connection.execute(
            text("SELECT id, spec, title, finalize_ctx FROM code_task_runs WHERE id = :i"),
            {"i": run_id}).mappings().first()
        if not run:
            return None
        repository = build_repository(run["spec"], default_owner) or ""
        repo_name = repository.rsplit("/", 1)[-1].lower()
        environment = str(inputs.get("environment") or inputs.get("create_environment")
                          or "staging").strip() or "staging"
        candidates = [dict(r) for r in connection.execute(
            text("SELECT * FROM deployment_targets WHERE project_id = :p AND status = 'active' "
                 "AND environment = :e AND host_id IS NOT NULL ORDER BY created_at"),
            {"p": project_id, "e": environment}).mappings().all()]
        # A target recorded without its repo (one made from Infra or Ask BOSS) is still this
        # app's when its NAME says so; pick_deploy_target matches by identity, never by repo alone.
        candidates = [t for t in candidates
                      if str(t.get("repo") or "").rsplit("/", 1)[-1].lower() in (repo_name, "")]
        host_id = str(inputs.get("host_id") or "").strip()
        if host_id:
            candidates = [t for t in candidates if t.get("host_id") == host_id]
        app_name = _imported_app_name(connection, project_id, run, repository, path) \
            or app_name_of_run(run)
        target = pick_deploy_target(candidates, app_name)
        if target is None and app_name and not [
                t for t in candidates if _same_app(t, app_name)]:
            # THE FIRST DEPLOY OF AN APP THAT DECLARES HOW IT RUNS takes the manifest path too.
            # With no target of its own this returned None, the deploy fell to the one-container
            # deploy_app, which made a target and then had its apply refused by declared_refusal
            # (deprun_22f9650e, wfr_9575c86a) — or, with no app-level manifest, shipped one
            # container and no Postgres. The target is made here, on the step's host, by the
            # same governed create_deployment_target deploy_app uses; asked before the step runs
            # (the gate, the console) it is only named, never made.
            target = _first_target(connection, project_id, run, app_name, repository,
                                   environment, host_id, create=create)
    if target is not None and target.get("pending_create"):
        return {**target, "manifest_path": path}
    if not target or target.get("project_id") != project_id or not target.get("host_id") \
            or target.get("status") != "active":
        return None
    return {**target, "manifest_path": target.get("manifest_path") or path}


def stack_target_for_deploy(connection: Connection, project_id: str,
                            inputs: dict[str, Any], *,
                            default_owner: str = "", create: bool = False) -> Optional[dict]:
    """The stack target a workflow's deploy step is bound to, or None for the legacy path.

    A target is a stack when it was made as one, OR when the build's app declares a
    boss.deploy.yaml (`_declared_stack_target`): then its host target ships from that manifest.
    An app that declares one and has no target yet gets it made on `inputs.host_id` when
    `create` (the deploy itself); asked before it runs, the answer is a target to be made
    (`pending_create`, no id), so the gate and the console say "a stack deploy" too.
    """
    target_id = str(inputs.get("target_id") or "").strip()
    if target_id:
        target = _target(connection, target_id)
        if is_stack_target(target):
            return target if target.get("project_id") == project_id else None
        return _declared_stack_target(connection, project_id, inputs, target, default_owner,
                                      create=create)
    # Without a named target, only a build whose repository HAS a stack target goes this way:
    # a project's other apps keep the deploy path they have always had.
    run_id = str(inputs.get("run_id") or "").strip()
    if not run_id:
        return None
    spec = connection.execute(text("SELECT spec FROM code_task_runs WHERE id = :i"),
                              {"i": run_id}).scalar()
    repository = build_repository(spec, default_owner)
    if not repository:
        return None
    environment = str(inputs.get("environment") or "").strip()
    rows = [dict(r) for r in connection.execute(
        text("SELECT * FROM deployment_targets WHERE project_id = :p AND target_kind = :k "
             "AND status = 'active' AND (:e = '' OR environment = :e) ORDER BY created_at"),
        {"p": project_id, "k": TARGET_KIND, "e": environment}).mappings().all()]
    rows = [r for r in rows if _same_repository(r.get("repo"), repository)]
    # THE APP'S OWN TARGET, NEVER THE REPOSITORY'S. A shared repository holds several apps:
    # paramg/boss-sandbox has HelpDesk AI's stack target, and a second app built there (Order
    # API) matched it as "the one stack target for this repository" — its first deploy would
    # have been planned onto HelpDesk's target, with HelpDesk's manifest path, at the new app's
    # commit. A target is this build's when it ships this build's manifest, was shipped by this
    # build's lineage, or carries this app's name.
    rows = _targets_of_this_app(connection, rows, run_id)
    if len(rows) == 1:
        return rows[0]
    if rows:
        return None
    return _declared_stack_target(connection, project_id, inputs, None, default_owner,
                                  create=create)


def _targets_of_this_app(connection: Connection, rows: list[dict],
                         run_id: str) -> list[dict]:
    if not rows:
        return rows
    from .delivery_pipeline import app_name_of_run

    manifest = declared_manifest_for_run(connection, run_id)
    lineage = {a["id"] for a in lineage_of(connection, run_id)} or {run_id}
    shipped = {str(r[0]) for r in connection.execute(
        text("SELECT DISTINCT target_id FROM stack_deployments "
             "WHERE code_task_run_id = ANY(:ids)"), {"ids": list(lineage)}).all()}
    run = connection.execute(
        text("SELECT id, title, finalize_ctx FROM code_task_runs WHERE id = :i"),
        {"i": run_id}).mappings().first()
    name = app_name_of_run(run) if run else ""
    mine = [r for r in rows
            if r["id"] in shipped
            or (manifest and str(r.get("manifest_path") or "") == manifest)
            or (name and _same_app(r, name))]
    if mine:
        return mine
    # Nothing ties a target to this build. When the build DECLARED its own manifest, it is an
    # app of its own (its first deploy makes its target: _declared_stack_target). When it
    # declared nothing — a feature whose files were never recorded — the repository's stack
    # target is the app, as it always was in a repository that holds one.
    return [] if manifest else rows


_POSTGRES_IMAGE = re.compile(r"""image:\s*["']?[\w./-]*(postgres|postgis)""", re.I)


def lineage_of(connection: Connection, run_id: str) -> list[dict[str, Any]]:
    """Every attempt of a build's lineage: (id, state), oldest first."""
    rows = connection.execute(
        text("""
            SELECT id, state FROM code_task_runs
            WHERE COALESCE(finalize_ctx->>'root_run_id', id) = (
                SELECT COALESCE(finalize_ctx->>'root_run_id', id) FROM code_task_runs
                WHERE id = :i)
            ORDER BY created_at
        """), {"i": str(run_id or "")}).mappings().all()
    return [{"id": r["id"], "state": _json(r["state"]) or {}} for r in rows]


def app_needs_postgres(connection: Connection, run_id: Optional[str]) -> bool:
    """The build decided its app uses Postgres (state.build_database.postgres, on any attempt)."""
    if not run_id:
        return False
    return any(bool(((a["state"] or {}).get("build_database") or {}).get("postgres"))
               for a in lineage_of(connection, run_id))


def manifest_declares_database(manifest: Any, read: Callable[[str], Optional[str]]) -> bool:
    """The manifest brings the app's Postgres: a database.relational resource, a postgres
    service in its compose file, or a DATABASE_URL it is given."""
    for r in manifest.resources or ():
        if str(getattr(r, "capability", "")) == "database.relational":
            return True
    if any(e.key == "DATABASE_URL" for e in manifest.env or ()):
        return True
    if manifest.kind == "compose" and manifest.compose_file:
        compose = read(manifest.compose_file)
        if compose and _POSTGRES_IMAGE.search(compose):
            return True
    return False


def manifest_misfit(connection: Connection, *, manifest: Any, rendered: Mapping[str, Any],
                    path: str, target: Mapping[str, Any], host: Mapping[str, Any],
                    run_id: Optional[str],
                    read: Callable[[str], Optional[str]],
                    provided: Any = ()) -> Optional[tuple[str, str]]:
    """(headline, action) when a manifest cannot run THIS app on THIS target, else None.

    Asked at plan time, before an approval is raised, for what failed after it on real runs:
      * the build's app uses Postgres and the manifest brings none (the one-container deploy
        shipped HelpDesk with no database; the plumbing gate wired DATABASE_URL by hand);
      * the manifest pins PORT, or health-checks a local port, other than the target's (a
        target on :8001 behind OAuth callbacks registered for :8000);
      * a health path no file the build wrote serves (the old target's /readyz);
      * a production deploy that gives the app a demo password its code refuses in production
        (HelpDesk's config.ts threw on DEMO_PASSWORD under NODE_ENV=production)."""
    from urllib.parse import urlsplit

    workdir = manifest_workdir(path)
    if not workdir and run_id:
        foreign = root_manifest_misfit(connection, manifest, run_id, read)
        if foreign:
            return foreign

    def in_dir(p: str) -> str:
        return f"{workdir}/{p}" if workdir and not p.startswith(workdir + "/") else p

    if app_needs_postgres(connection, run_id) and not manifest_declares_database(
            manifest, lambda p: read(in_dir(p))):
        return (f"This app uses Postgres, and {path} declares no database for it.",
                f"Declare it in {path}: `resources: {{db: {{capability: database.relational, "
                "engine: postgres}}}}`, or a postgres service in its compose file, then deploy "
                "again.")
    port = ("" if is_stack_target(dict(target))
            else (address_env(dict(target), dict(host)).get("PORT") or "").strip())
    if port:
        pinned = [e for e in manifest.env or () if e.key == "PORT" and e.source == "literal"]
        said = str((rendered.get("env_literals") or {}).get("PORT") or "").strip()
        if pinned and said and said != port:
            return (f"{path} sets PORT={said}, but this target serves on port {port}.",
                    f"Drop PORT from {path}'s env (BOSS passes the target's), or move the "
                    "target to that port in Infra.")
        for check in rendered.get("health") or []:
            parts = urlsplit(str(check.get("url") or ""))
            local = (parts.hostname or "") in ("127.0.0.1", "localhost", str(host.get("address")))
            if local and parts.port and str(parts.port) != port:
                return (f"{path} checks health on port {parts.port}, but this target serves "
                        f"on port {port}.",
                        "Health-check {target.public_url} (or $PORT) in the manifest.")
    served = _served_text(connection, run_id, workdir, read)
    given = {e.key for e in manifest.env or ()} | {str(n) for n in provided or ()}
    if (served is not None and "DEMO_PASSWORD" in given
            and str(target.get("environment") or "").lower() == "production"
            and refuses_in_production(served, "DEMO_PASSWORD")):
        return ("The app refuses DEMO_PASSWORD in production, and this production deploy "
                "gives it one: it would not start.",
                "Allow the demo/seed password BOSS provides in production, and make demo seeding "
                "an explicit opt-in (e.g. ALLOW_DEMO_SEED) instead of refusing the password; "
                "then deploy again.")
    # ONLY A MANIFEST THIS BUILD WROTE is judged against the files this build wrote. An imported
    # app's own boss.deploy.yaml health-checks a route its existing code serves (helpdesk-ai's
    # /auth/providers in server/auth/routes.mjs, untouched by the SLA build), and judging it
    # against the build's few changed files dead-lettered a correct deploy (2026-11-06).
    if served is not None and _build_wrote(connection, run_id, path):
        for check in rendered.get("health") or []:
            route = urlsplit(str(check.get("url") or "")).path.rstrip("/")
            # Mounted routes (a router at /api serving /health) are judged by their last part.
            if route and route not in served and "/" + route.rsplit("/", 1)[-1] not in served:
                return (f"{path} health-checks {route}, and no file this build wrote serves "
                        "that route.",
                        f"Health-check a route the app serves (its / or a /health it "
                        f"defines), or add {route} to the app, then deploy again.")
    return None


def root_manifest_misfit(connection: Connection, manifest: Any, run_id: str,
                         read: Callable[[str], Optional[str]]) -> Optional[tuple[str, str]]:
    """(headline, action) when the repository ROOT's manifest belongs to another app than the
    one this build made, else None.

    A repository holds several apps. boss-sandbox's root boss.deploy.yaml is stack-demo's; the
    Onboarding build (crun_2d097414) wrote its app under onboarding-path-builder/, edited that
    root manifest instead of writing its own, and the deploy took stack-demo's for it. A root
    manifest is the app's when the app lives at the root, or when it names the app's folder (its
    `name`, or steps / a compose file that run in that folder: `cd helpdesk && …`)."""
    from .app_deploy_manifest import app_folder
    from .naming import identity_key

    files = [str(f) for a in lineage_of(connection, run_id)
             for f in ((a["state"] or {}).get("impl_files") or [])]
    folder = app_folder(files)
    if not folder:
        return None
    if identity_key(manifest.name) == identity_key(folder):
        return None
    said = "\n".join(s.run for s in (*manifest.install, *manifest.update, *manifest.teardown))
    if manifest.compose_file:
        said += "\n" + manifest.compose_file + "\n" + (read(manifest.compose_file) or "")
    if re.search(rf"(^|[\s/'\"=]){re.escape(folder)}(/|\s|$|['\"])", said):
        return None
    return (f"The repository root's {DEFAULT_MANIFEST_PATH} is {manifest.name!r}'s, not this "
            f"app's: this build's app lives in {folder}/, and that manifest neither names it "
            "nor runs anything there.",
            f"Write {folder}/{DEFAULT_MANIFEST_PATH} for this app (and leave the root one to "
            f"{manifest.name!r}), then deploy again.")


def refuses_in_production(source: str, name: str) -> bool:
    """The code throws (or exits) when `name` is set under production: `name`, `production`
    and a throw/exit within a few lines of each other."""
    lines = source.splitlines()
    for i, line in enumerate(lines):
        if name not in line:
            continue
        window = "\n".join(lines[max(0, i - 4):i + 5])
        if re.search(r"production", window) and re.search(
                r"\bthrow\b|process\.exit\(|\braise\b|sys\.exit\(|panic\(", window):
            return True
    return False


_SERVED_SUFFIXES = (".js", ".mjs", ".cjs", ".ts", ".py", ".go", ".rb", ".java", ".kt", ".rs",
                    ".php", ".conf", ".yml", ".yaml", ".json", ".toml")


def _build_wrote(connection: Connection, run_id: Optional[str], path: str) -> bool:
    """Whether any attempt of this run's lineage wrote `path` (the manifest)."""
    want = str(path or "").strip().lstrip("./")
    if not run_id or not want:
        return False
    return any(str(f).strip().lstrip("./") == want
               for attempt in lineage_of(connection, run_id)
               for f in ((attempt["state"] or {}).get("impl_files") or []))


def _served_text(connection: Connection, run_id: Optional[str], workdir: str,
                 read: Callable[[str], Optional[str]]) -> Optional[str]:
    """The source the build wrote under the manifest's directory, joined (bounded), or None when
    it cannot be told — then a route is not judged."""
    if not run_id:
        return None
    files: list[str] = []
    for attempt in lineage_of(connection, run_id):
        for f in (attempt["state"] or {}).get("impl_files") or []:
            f = str(f).strip().lstrip("./")
            if f and f not in files and f.endswith(_SERVED_SUFFIXES) and (
                    not workdir or f.startswith(workdir + "/")) \
                    and f.rsplit("/", 1)[-1] != DEFAULT_MANIFEST_PATH:
                files.append(f)
    if not files:
        return None
    texts = [t for t in (read(f) for f in files[:40]) if t]
    return "\n".join(texts) if texts else None


def undeclared_deploy_refusal(connection: Connection, run_id: Optional[str]) -> Optional[str]:
    """Why a build's app cannot take the one-container deploy, in words — or None.

    The one-container path guesses how an app runs and ships no database. It stays for an app
    that never said otherwise; it is refused for one that
      * was ASKED for its own boss.deploy.yaml (the deploy brief) and wrote none (one that took
        another app's root manifest instead is refused at plan: root_manifest_misfit); or
      * uses Postgres (state.build_database.postgres) and declares no manifest to bring it.
    Asked before the deploy's approval is raised (the runner's gate) and by the deploy itself."""
    run_id = str(run_id or "").strip()
    if not run_id or declared_manifest_for_run(connection, run_id):
        return None
    from .app_deploy_manifest import app_folder
    from .workflow_capability_runner import DEPLOY_BRIEF_MARKER

    attempts = lineage_of(connection, run_id)
    if not attempts:
        return None
    files = [str(f) for a in attempts for f in ((a["state"] or {}).get("impl_files") or [])]
    folder = app_folder(files)
    where = f"{folder}/{DEFAULT_MANIFEST_PATH}" if folder else DEFAULT_MANIFEST_PATH
    briefed = bool(connection.execute(
        text("SELECT 1 FROM code_task_runs WHERE id = ANY(:ids) AND strpos(spec::text, :m) > 0 "
             "LIMIT 1"), {"ids": [a["id"] for a in attempts], "m": DEPLOY_BRIEF_MARKER}).first())
    if briefed:
        return (f"The build was asked to declare how its app runs ({where}) and wrote none. "
                "Nothing was deployed: BOSS would only be guessing how to run it. Retry "
                f"the build with the note \"write {where}\", or add it to the pull request, "
                "then deploy again.")
    if app_needs_postgres(connection, run_id):
        return (f"This app uses Postgres, and it declares no {where} to bring one; the "
                "one-container deploy would ship it without a database. Add the manifest with "
                "`resources: {db: {capability: database.relational, engine: postgres}}`, then "
                "deploy again.")
    return None


def own_place_on_host(engine: Any, manifest_text: str, manifest: Any,
                      target: Mapping[str, Any], host: Mapping[str, Any]) -> tuple[str, Any]:
    """(manifest text, manifest) with THIS target's own folder and compose project on the host,
    when the manifest names neither and another target on the host already holds the default.

    SHARED HOSTS MADE THIS POSSIBLE. A verified host is usable by every project in its
    organisation, so two projects' apps with the same name — boss-dogfood's old helpdesk-ai and
    the helpdesk-ai project's HelpDesk AI — both defaulted to /opt/boss-apps/helpdesk-ai and the
    compose project helpdesk-ai on 2.29.2.202, and the second deploy was refused (2026-11-06).
    The target keeps the place it last used; a new one is named after the target's id. The
    text with the place written in is what the plan stores, so apply renders the same place."""
    import yaml

    from .app_deploy_manifest import DEFAULT_BASE_DIR, parse_manifest

    try:
        data = yaml.safe_load(manifest_text) or {}
    except yaml.YAMLError:
        return manifest_text, manifest
    if not isinstance(data, dict) or data.get("dir") or (
            isinstance(data.get("compose"), dict) and data["compose"].get("project")):
        return manifest_text, manifest
    tid, hid = str(target.get("id") or ""), str(host.get("id") or "")
    if not tid or not hid:
        return manifest_text, manifest
    with engine.connect() as connection:
        last = connection.execute(
            text("SELECT deploy_dir FROM stack_deployments WHERE target_id = :t AND host_id = :h "
                 "AND COALESCE(deploy_dir, '') <> '' ORDER BY created_at DESC LIMIT 1"),
            {"t": tid, "h": hid}).scalar()
        taken = connection.execute(
            text("SELECT 1 FROM stack_deployments d JOIN deployment_targets t "
                 "ON t.id = d.target_id WHERE d.host_id = :h AND d.deploy_dir = :dir "
                 "AND d.target_id <> :t AND t.status = 'active' AND d.status <> 'rejected' "
                 "LIMIT 1"), {"h": hid, "dir": manifest.dir, "t": tid}).first()
    if last and str(last) == manifest.dir:
        return manifest_text, manifest
    if not taken and not last:
        return manifest_text, manifest
    suffix = re.sub(r"[^a-z0-9]", "", tid.rsplit("_", 1)[-1].lower())[:6]
    place = (str(last).rstrip("/").rsplit("/", 1)[-1] if last
             else f"{manifest.name}-{suffix}")
    data["dir"] = f"{DEFAULT_BASE_DIR}/{place}" if not last else str(last)
    if manifest.kind == "compose":
        data["compose"] = {**(data.get("compose") or {}), "project": place}
    placed = ("# BOSS: another app on this host uses the default place; this target's own.\n"
              + yaml.safe_dump(data, sort_keys=False))
    try:
        return placed, parse_manifest(data, source_text=placed)
    except Exception:  # noqa: BLE001 — never worse than the manifest as written
        return manifest_text, manifest


def _imported_app_name(connection: Connection, project_id: str, run: Mapping[str, Any],
                       repository: str, manifest_path: str) -> str:
    """The project's own name when this build adds to the app the project imported: no recorded
    app name, the project's primary repository, and the app's manifest at that repository's
    root (the repo IS the app; `app_dir` is only where the build worked, e.g. `web`). Else "".

    ADDING TO AN EXISTING APP IS THE MAIN USE CASE. Such a build has no app name of its own, so
    its title stood in: helpdesk-ai's SLA build made a second target, "Add SLA timers to
    tickets", beside "HelpDesk AI", and deployed to neither (2026-11-06)."""
    fctx = run.get("finalize_ctx") or {}
    if isinstance(fctx, str):
        try:
            fctx = json.loads(fctx)
        except ValueError:
            fctx = {}
    if str((fctx or {}).get("app_name") or "").strip() or "/" in str(manifest_path or "/"):
        return ""
    row = connection.execute(
        text("SELECT p.name FROM projects p JOIN github_repository_bindings b "
             "ON b.project_id = p.id AND b.role = 'primary' AND b.status = 'active' "
             "WHERE p.id = :p AND lower(b.full_name) = lower(:r)"),
        {"p": project_id, "r": repository}).first()
    return str(row[0]).strip() if row and row[0] else ""


def _same_app(target: Mapping[str, Any], app_name: str) -> bool:
    from .naming import identity_key

    return bool(identity_key(app_name)) and \
        identity_key(str(target.get("app_name") or "")) == identity_key(app_name)


def _first_target(connection: Connection, project_id: str, run: Mapping[str, Any],
                  app_name: str, repository: str, environment: str, host_id: str, *,
                  create: bool) -> Optional[dict]:
    """The target the first deploy of a declared app ships to: made now (`create`), or named as
    the one that will be (`pending_create`). None when the build has not passed or no host can
    be chosen — the legacy path then says which question to answer."""
    if not create:
        return {"id": None, "pending_create": True, "project_id": project_id,
                "app_name": app_name, "environment": environment, "repo": repository,
                "host_id": host_id or None, "status": "active"}
    from .deployment_fleet import create_deployment_target
    from .run_fencing import build_not_passed

    status = str(connection.execute(
        text("SELECT status FROM code_task_runs WHERE id = :i"),
        {"i": str(run.get("id") or "")}).scalar() or "") if run.get("id") else ""
    if status and status not in ("completed", "succeeded", "passed"):
        return None
    if run.get("id") and build_not_passed(connection, str(run["id"])):
        return None
    made = create_deployment_target(
        connection, project_id=project_id, app_name=app_name, environment=environment,
        repo=repository or None, host_id=host_id or None, actor="workflow_runner")
    if not made.get("ok"):
        return None
    return _target(connection, str(made["target_id"]))


def _monitoring_pack_card(connection: Connection, run_id: Optional[str],
                          step_id: Optional[str]) -> Optional[dict[str, Any]]:
    from .monitoring_pack import card_for_step_id

    return card_for_step_id(connection, run_id, step_id)


def plan_for_workflow_step(engine, settings: Settings, *, project_id: str, actor: str,
                           target: dict, inputs: dict[str, Any],
                           workflow_run_id: Optional[str],
                           approver_role: Optional[str] = None,
                           step_id: Optional[str] = None,
                           approval_decision_id: Optional[str] = None,
                           commit: Optional[str] = None,
                           retire_projects: Optional[list[str]] = None,
                           person_required: bool = False) -> dict[str, Any]:
    """Plan the build's pull-request head onto a stack target; the step waits on it.

    `person_required`: the step promised a person (deploy_policy.promises_a_person), so the
    environment's policy may not approve this deployment.

    `commit`: ship exactly this commit of the pull request instead of its head — pass 1's deploy
    ships the head pass 1 left (pinned when pass 2 started), never what pass 2 pushed beside it.

    No preview is required: the manifest declares how the stack runs and the health checks
    prove it, and a platform like BOSS cannot be previewed as one container anyway.

    ONE APPROVAL. The step is not held at a gate of its own first (see the runner's
    `_gate_subject`): the deployment's decision is the step's gate, raised for the step's
    `approver_role`, and the step waits on the deployment until it is healthy, failed or
    rejected.
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
    built = build_repository(run["spec"], str(getattr(settings, "target_github_owner", "")
                                              or ""))
    repository = str(target.get("repo") or "")
    if "/" not in repository:
        repository = built or ""
    if not _same_repository(built, repository):
        # pull/N names a pull request only inside its own repository. Planning the target's
        # repository at the build's number would deploy somebody else's change.
        return {"capability": "deploy", "tool": None, "status": "failed",
                "error": (f"the build worked in {built or 'an unnamed repository'}, but target "
                          f"{target.get('app_name')!r} deploys {repository or 'nothing'}; "
                          "bind the deploy step to a stack target for the build's repository.")}
    ref = f"pull/{int(run['pull_request_number'])}"
    if commit and _SHA.match(str(commit)):
        ref = str(commit)
    # NEVER AN UNMERGED BRANCH AS IF MERGED (pr_merge, PR #229): this ships the pull request's
    # head; whether that change is merged comes from GitHub or a recorded merge, and when it is
    # not — above all a merge approval that asked for a MANUAL merge — the step says so plainly.
    from . import pr_merge

    with engine.begin() as connection:
        merge = pr_merge.observe(connection, settings, project_id, run_id)
        # MERGED SHIPS WHAT WAS MERGED. A workflow's deploy now waits for the merge, so its pinned
        # pass-1 head (or the PR's head) is behind what landed: helpdesk-ai planned 2d0ea0a, the
        # PR as first opened, while main held its repair and follow-up at merge 256bb1d.
        if merge.get("state") == pr_merge.MERGED and _SHA.match(str(merge.get("sha") or "")):
            ref = str(merge["sha"])
        unmerged = pr_merge.sentence(merge)
        if unmerged:
            from .core import emit_event

            emit_event(connection, project_id, "workflow.deploy_unmerged", actor,
                       "workflow_run", str(workflow_run_id or ""),
                       {"code_task_run_id": run_id, "step_id": step_id,
                        "pull_request_number": merge.get("number"),
                        "merge_state": merge.get("state"),
                        "manual_merge_pending": bool(merge.get("manual")),
                        "said": unmerged})
    try:
        deployment = plan_stack_deploy(
            engine, settings, project_id=project_id, target_id=target["id"],
            repository=repository, ref=ref, actor=actor,
            run_id=run_id, workflow_run_id=workflow_run_id, approver_role=approver_role,
            workflow_step_id=step_id,
            manifest_path=None if is_stack_target(target) else target.get("manifest_path"),
            approval_decision_id=approval_decision_id, retire_projects=retire_projects,
            person_required=person_required)
    except StackDeployError as exc:
        refused: dict[str, Any] = {"capability": "deploy", "tool": None, "status": "failed",
                                   "error": f"{exc.headline} {exc.action}"}
        if exc.cause:
            manifest_text = str(getattr(exc, "manifest_text", "") or "")
            # WHOSE FIX IT IS, for the runner (deploy_repair): an app-side refusal — a program
            # the machine lacks, a manifest that does not fit — is repaired by a build of the
            # app, not by a person; nothing was planned or written, so nothing needs undoing.
            refused["deploy_failure"] = {
                "phase": "plan", "cause": exc.cause, "headline": exc.headline,
                "action": exc.action, "host_id": target.get("host_id"),
                "target_id": target.get("id"), "target_name": target.get("app_name"),
                "commit": getattr(exc, "commit", None),
                "manifest_path": getattr(exc, "manifest_path", None),
                **({"manifest_text": manifest_text[:6000]} if manifest_text else {}),
                **({"host_facts": exc.facts} if exc.facts else {})}
        return refused
    return {
        "capability": "deploy", "tool": None, "status": "awaiting",
        "await": {"kind": "stack_deployment", "id": deployment["id"]},
        "stack_deployment_id": deployment["id"],
        "decision_id": deployment.get("decision_id"),
        "commit": deployment.get("commit"),
        # Where it ships, for the console's deploy step — never `target_id`, which the
        # per-app container path reads as ITS bound target.
        "stack_target_id": target.get("id"),
        "stack_target_name": target.get("app_name"),
        "stack_host": (deployment.get("preflight") or {}).get("host"),
        **({"approver_role": approver_role} if approver_role else {}),
        "merge": {k: v for k, v in merge.items() if k != "unread"},
        **({"manual_merge_pending": True} if merge.get("manual") else {}),
        "note": " ".join(p for p in (unmerged, planned_note(deployment, target.get("app_name")))
                         if p),
    }


def planned_note(deployment: Mapping[str, Any], app_name: Any) -> str:
    """What happens next, as it will: a deploy the environment's rule approved runs now. It said
    "it runs once the deployment decision is approved" when no decision had been opened."""
    where = f"{deployment.get('repository')}@{str(deployment.get('commit'))[:12]} onto {app_name}"
    if deployment.get("decision_id"):
        return f"Planned {where}; it runs once the deployment decision is approved."
    return f"Planned {where}; no approval is needed here, so it is starting now."
