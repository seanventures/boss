"""`boss.deploy.yaml`: how an application says it is deployed, so BOSS never has to guess.

Every deploy BOSS knew how to perform was ONE container it built itself — a static site, a
Python file or a Node script, one port, one health check. A platform like BOSS is a database,
an API, a worker, a console and an identity store, and how those come up is knowledge that
belongs to the application, not to BOSS. Guessing it is how the preview resolver once deployed
the wrong app; hard-coding it is how a second app becomes a second code path.

So the application declares it, at the root of its repository:

    version: 1
    name: boss                         # host dir default /opt/boss-apps/<name>
    kind: commands | compose
    compose: {file: docker-compose.yml, project: boss}   # kind: compose only
    env:
      PUBLIC_URL: "{target.public_url}"
      DB_PASSWORD: {generate: password}  # minted once per (target, stack), reused forever
      GITHUB_TOKEN: {secret: github-token}
    install: ["./scripts/install.sh"]  # first deploy to a target
    update:  ["./scripts/upgrade.sh"]  # later deploys (defaults to install)
    health:  [{url: "http://127.0.0.1:8000/health", expect: 200, timeout_seconds: 600}]
    open:    {url: "{target.public_url}"}
    teardown: ["docker compose down"]
    requires: {sudo: false, docker: true}
    metrics: {path: /metrics}          # optional: BOSS scrapes it after deploy (app_metrics)
    resources:                         # optional: infrastructure by capability (boss.plugin/v1)
      db: {capability: database.relational, engine: postgres, backups: true, public: false}
      legacy: {capability: database.relational, mode: attached,
               attach: {bindings: {LEGACY_DATABASE_URL: LEGACY_DB_URL}}}

`resources:` entries are ResourceRequests (engine/boss_engine/platform/contract.py). A plugin
provides each; its bindings (DATABASE_URL, ...) reach the app as vault REFERENCES, never values
in this file.

This module only PARSES and RENDERS. It never touches a host: `stack_deploy` does that, and
only inside an approved Resource Plane ChangeSet. Every error is a sentence an operator can act
on, naming the field — a manifest that fails at minute nine of a deploy is the failure this
exists to prevent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

#: Where a stack lives on its host unless the manifest says otherwise.
DEFAULT_BASE_DIR = "/opt/boss-apps"
#: The manifest's conventional location at the repository root.
DEFAULT_MANIFEST_PATH = "boss.deploy.yaml"


def declared_manifest(files: Any) -> Optional[str]:
    """The boss.deploy.yaml an app declares itself with, from the files its build wrote, or None.

    THE ONE RULE for "how is this app deployed": an app that says how it runs is deployed the
    way it says, never guessed at. HelpDesk wrote helpdesk-ai/boss.deploy.yaml (compose: the
    Node server and its Postgres), the deploy guessed "static", and nginx served its unbuilt
    index.html — a blank page that passed a health check on `/`.

    A repository holds several apps, and its ROOT manifest can be another app's (boss-sandbox's
    belongs to stack-demo). So a manifest inside a directory wins, and among those the one whose
    directory holds most of what the build wrote; the root manifest only when it is the only one.
    """
    paths = [str(f).strip().removeprefix("./").lstrip("/") for f in (files or [])
             if str(f or "").strip()]
    manifests = [p for p in paths if p.rsplit("/", 1)[-1] == DEFAULT_MANIFEST_PATH]
    if not manifests:
        return None
    nested = [m for m in manifests if "/" in m]
    if not nested:
        home = app_folder(paths)
        if home:
            # THE ROOT MANIFEST IS ANOTHER APP'S when everything this build wrote lives in one
            # folder of its own: Onboarding (crun_2d097414) edited boss-sandbox's root
            # boss.deploy.yaml — stack-demo's — and wrote its app under
            # onboarding-path-builder/, so it declared nothing of its own.
            return None
        return DEFAULT_MANIFEST_PATH

    def held(manifest: str) -> tuple[int, int]:
        directory = manifest.rsplit("/", 1)[0] + "/"
        return (sum(1 for p in paths if p.startswith(directory)), manifest.count("/"))

    return max(nested, key=held)


#: Folders that hold part of a root app, never an app of their own.
_SOURCE_DIRS = frozenset({"src", "lib", "app", "apps", "server", "client", "web", "public", "static",
                          "test", "tests", "spec", "scripts", "migrations", "db", "config",
                          "docs", "assets", "components", "pages", "routes", "api", ".github"})


def app_folder(files: Any) -> Optional[str]:
    """The one top-level folder every non-manifest file a build wrote lives in — the app's own
    folder in a repository of several apps — or None (the app is at the root, or spread out)."""
    paths = [str(f).strip().removeprefix("./").lstrip("/") for f in (files or [])
             if str(f or "").strip()]
    others = [p for p in paths if p != DEFAULT_MANIFEST_PATH]
    if not others or any("/" not in p for p in others):
        return None
    tops = {p.split("/", 1)[0] for p in others}
    if len(tops) != 1:
        return None
    top = next(iter(tops))
    return None if top.lower() in _SOURCE_DIRS else top


def manifest_workdir(manifest_path: Optional[str]) -> str:
    """The directory a manifest's paths and commands are relative to: its own ("" = root)."""
    path = str(manifest_path or "").strip().strip("/")
    return path.rsplit("/", 1)[0] if "/" in path else ""
#: A step with no timeout of its own. Installs of a platform pull images and build; ten minutes
#: is routine, so the default is generous and a step may still ask for more.
DEFAULT_STEP_TIMEOUT_SECONDS = 1800
MAX_STEP_TIMEOUT_SECONDS = 4 * 3600
DEFAULT_HEALTH_TIMEOUT_SECONDS = 600

#: The only values a manifest may interpolate. Anything else is a typo or a secret someone
#: hoped would be substituted, and both must fail at plan time, not as a literal `{x}` on a host.
PLACEHOLDERS = frozenset({
    "dir", "commit", "ref", "target.host", "target.public_url", "target.user",
})
GENERATORS = frozenset({"password", "hex32", "token"})
KINDS = frozenset({"commands", "compose"})

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
#: `{word}` or `{a.b}` NOT preceded by `$` — `${HOME}` is the shell's, and `{a,b}`, `{}` and
#: awk's `{print $1}` never match because they contain characters a placeholder cannot.
_PLACEHOLDER = re.compile(r"(?<!\$)\{([A-Za-z0-9_.]+)\}")


class ManifestError(ValueError):
    """A manifest BOSS refuses, with the field and the fix in the message."""


@dataclass(frozen=True)
class EnvVar:
    key: str
    #: literal | generate | secret
    source: str
    #: the literal template, the generator name, or the secret name
    value: str


@dataclass(frozen=True)
class Step:
    name: str
    run: str
    timeout_seconds: int = DEFAULT_STEP_TIMEOUT_SECONDS


@dataclass(frozen=True)
class HealthCheck:
    url: str
    expect: int = 200
    timeout_seconds: int = DEFAULT_HEALTH_TIMEOUT_SECONDS


@dataclass(frozen=True)
class DeployManifest:
    version: int
    name: str
    kind: str
    dir: str
    env: tuple[EnvVar, ...] = ()
    install: tuple[Step, ...] = ()
    update: tuple[Step, ...] = ()
    health: tuple[HealthCheck, ...] = ()
    open_url: str = "{target.public_url}"
    teardown: tuple[Step, ...] = ()
    requires_sudo: bool = False
    requires_docker: bool = False
    compose_file: Optional[str] = None
    compose_project: Optional[str] = None
    metrics: Optional[dict[str, Any]] = None
    resources: tuple[Any, ...] = ()        # platform.contract.ResourceRequest, in file order
    source_text: str = field(default="", repr=False, compare=False)

    def env_keys(self) -> list[str]:
        return [item.key for item in self.env]

    def summary(self) -> dict[str, Any]:
        """What the console shows about a manifest. Names only — never a value."""
        return {
            "name": self.name,
            "kind": self.kind,
            "dir": self.dir,
            # The compose project its containers are labelled with (kind: compose), so a log
            # reader finds the app's containers on the host without guessing their names.
            "compose_project": self.compose_project,
            "steps": {
                "install": [s.name for s in self.install],
                "update": [s.name for s in self.update],
                "teardown": [s.name for s in self.teardown],
            },
            "env_keys": self.env_keys(),
            "generated_env_keys": [e.key for e in self.env if e.source == "generate"],
            "secret_env_keys": [e.key for e in self.env if e.source == "secret"],
            # KEY -> the BOSS secret it reads, so whoever approves sees WHICH secret goes to
            # the host, not only that some key does.
            "secret_env": {e.key: e.value for e in self.env if e.source == "secret"},
            "health": [{"url": h.url, "expect": h.expect,
                        "timeout_seconds": h.timeout_seconds} for h in self.health],
            "open_url": self.open_url,
            "requires": {"sudo": self.requires_sudo, "docker": self.requires_docker},
            # The scrape declaration travels on the deploy record (stack_deployments.manifest),
            # which is where the monitor reads it: the HEALTHY deploy's declaration counts.
            "metrics": dict(self.metrics) if self.metrics else None,
            "resources": [{"name": r.name, "capability": r.capability, "mode": r.mode.value,
                           "provider": r.provider, "public": r.public} for r in self.resources],
        }


def _check_placeholders(value: str, where: str) -> None:
    for name in _PLACEHOLDER.findall(value):
        if name not in PLACEHOLDERS:
            raise ManifestError(
                f"{where} uses {{{name}}}, which is not a placeholder BOSS fills in. "
                f"Allowed: {', '.join('{' + p + '}' for p in sorted(PLACEHOLDERS))}. "
                "For a shell variable write ${" + name + "}; for a secret, declare it under "
                "env with {secret: <name>} and read it as $KEY.")


def _timeout(raw: Any, where: str, default: int) -> int:
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ManifestError(f"{where} must be a whole number of seconds, not {raw!r}.")
    if value <= 0 or value > MAX_STEP_TIMEOUT_SECONDS:
        raise ManifestError(
            f"{where} must be between 1 and {MAX_STEP_TIMEOUT_SECONDS} seconds, not {value}.")
    return value


def _steps(raw: Any, section: str) -> tuple[Step, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise ManifestError(f"{section} must be a list of shell commands.")
    steps: list[Step] = []
    for index, item in enumerate(raw, start=1):
        where = f"{section}[{index}]"
        if isinstance(item, str):
            run, name, timeout = item, None, None
        elif isinstance(item, dict):
            unknown = set(item) - {"run", "name", "timeout_seconds"}
            if unknown:
                raise ManifestError(
                    f"{where} has unknown field(s) {sorted(unknown)}; a step is a command "
                    "string or {run, name, timeout_seconds}.")
            run, name, timeout = item.get("run"), item.get("name"), item.get("timeout_seconds")
        else:
            raise ManifestError(f"{where} must be a shell command string.")
        run = str(run or "").strip()
        if not run:
            raise ManifestError(f"{where} is empty; remove it or give it a command.")
        _check_placeholders(run, where)
        steps.append(Step(
            name=str(name or f"{section} {index}").strip()[:80],
            run=run,
            timeout_seconds=_timeout(timeout, f"{where}.timeout_seconds",
                                     DEFAULT_STEP_TIMEOUT_SECONDS),
        ))
    return tuple(steps)


def _env(raw: Any) -> tuple[EnvVar, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ManifestError("env must be a mapping of NAME: value.")
    out: list[EnvVar] = []
    for key, value in raw.items():
        key = str(key)
        if not _ENV_KEY.match(key):
            raise ManifestError(
                f"env key {key!r} is not a valid shell variable name (letters, digits, _).")
        where = f"env.{key}"
        if isinstance(value, dict):
            kinds = [k for k in ("generate", "secret") if k in value]
            if len(kinds) != 1 or len(value) != 1:
                raise ManifestError(
                    f"{where} must be a string, {{generate: password|hex32|token}} or "
                    "{secret: <name>} — exactly one of them.")
            kind = kinds[0]
            name = str(value[kind] or "").strip()
            if kind == "generate" and name not in GENERATORS:
                raise ManifestError(
                    f"{where}: generate must be one of {sorted(GENERATORS)}, not {name!r}.")
            if kind == "secret" and not name:
                raise ManifestError(f"{where}: secret needs the name of a secret set in BOSS.")
            out.append(EnvVar(key=key, source=kind, value=name))
            continue
        if isinstance(value, (list, tuple)):
            raise ManifestError(f"{where} must be a single value, not a list.")
        if isinstance(value, bool):
            text = "true" if value else "false"
        else:
            text = "" if value is None else str(value)
        if "\n" in text:
            raise ManifestError(f"{where} must be one line.")
        _check_placeholders(text, where)
        out.append(EnvVar(key=key, source="literal", value=text))
    return tuple(out)


def _health(raw: Any) -> tuple[HealthCheck, ...]:
    if raw is None:
        return ()
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise ManifestError("health must be a list of {url, expect, timeout_seconds}.")
    checks: list[HealthCheck] = []
    for index, item in enumerate(raw, start=1):
        where = f"health[{index}]"
        if isinstance(item, str):
            item = {"url": item}
        if not isinstance(item, dict):
            raise ManifestError(f"{where} must be {{url, expect, timeout_seconds}}.")
        url = str(item.get("url") or "").strip()
        # `{target.public_url}` always renders to an http(s) URL, so a check written against
        # the app's own address is one — HelpDesk's `{target.public_url}/readyz` was refused
        # here, which made the only honest health check for a host target unwritable.
        if not re.match(r"^(?:https?://|\{target\.public_url\}(?:/|$))", url):
            raise ManifestError(
                f"{where}.url must be an http:// or https:// URL; it is probed ON THE HOST, "
                "so http://127.0.0.1:<port>/... is the usual answer.")
        _check_placeholders(url, f"{where}.url")
        try:
            expect = int(item.get("expect", 200))
        except (TypeError, ValueError):
            raise ManifestError(f"{where}.expect must be an HTTP status code.")
        if not 100 <= expect <= 599:
            raise ManifestError(f"{where}.expect must be an HTTP status code, not {expect}.")
        checks.append(HealthCheck(
            url=url, expect=expect,
            timeout_seconds=_timeout(item.get("timeout_seconds"), f"{where}.timeout_seconds",
                                     DEFAULT_HEALTH_TIMEOUT_SECONDS)))
    return tuple(checks)


def _bool(raw: Any, where: str) -> bool:
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    raise ManifestError(f"{where} must be true or false.")


#: Which route the scrape takes: the health probe's public origin, the host channel, or both in turn.
METRICS_VIAS = ("auto", "public", "host")


def _metrics(raw: Any) -> Optional[dict[str, Any]]:
    """`metrics: {path: /metrics, interval: 60, via: auto, alert: {...}}`, validated.

    The path is a PATH: no scheme, no host. BOSS scrapes the app's own origin — the one its
    health probe already asks — so a manifest cannot point the monitor at another machine.
    """
    from .app_metrics import DEFAULT_ALERT, MAX_INTERVAL_SECONDS, MIN_INTERVAL_SECONDS

    if raw is None:
        return None
    if isinstance(raw, str):
        raw = {"path": raw}
    if not isinstance(raw, dict):
        raise ManifestError("metrics must be {path: /metrics} (optionally interval, via, alert).")
    unknown = set(raw) - {"path", "interval", "via", "alert"}
    if unknown:
        raise ManifestError(f"metrics has unknown field(s) {sorted(unknown)}; known: alert, "
                            "interval, path, via.")
    path = str(raw.get("path") or "").strip()
    if not path.startswith("/") or path.startswith("//") or "://" in path:
        raise ManifestError(
            f"metrics.path {path!r} must be a path on the app starting with '/', such as "
            "/metrics — no scheme or host: BOSS scrapes the app where its health probe reaches "
            "it.")
    if not re.match(r"^/[A-Za-z0-9._~/%-]*$", path) or ".." in path.split("/"):
        raise ManifestError(
            f"metrics.path {path!r} may hold only letters, digits and . _ ~ / % - (no query, "
            "spaces or '..').")
    interval = raw.get("interval")
    out: dict[str, Any] = {"path": path}
    if interval is not None:
        try:
            interval = int(interval)
        except (TypeError, ValueError):
            raise ManifestError(f"metrics.interval must be whole seconds, not {interval!r}.")
        if not MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS:
            raise ManifestError(
                f"metrics.interval must be between {MIN_INTERVAL_SECONDS} and "
                f"{MAX_INTERVAL_SECONDS} seconds, not {interval}.")
        out["interval_seconds"] = interval
    via = str(raw.get("via") or "auto").strip()
    if via not in METRICS_VIAS:
        raise ManifestError(f"metrics.via must be one of {list(METRICS_VIAS)}, not {via!r}.")
    out["via"] = via
    alert = raw.get("alert")
    if alert is not None:
        if alert is False:
            alert = {"enabled": False}
        if not isinstance(alert, dict):
            raise ManifestError("metrics.alert must be {error_rate_percent, window_minutes, "
                                "min_requests, enabled}.")
        unknown = set(alert) - set(DEFAULT_ALERT)
        if unknown:
            raise ManifestError(f"metrics.alert has unknown field(s) {sorted(unknown)}; known: "
                                f"{', '.join(sorted(DEFAULT_ALERT))}.")
        rule: dict[str, Any] = {}
        bounds = {"error_rate_percent": (0.1, 100.0, float), "window_minutes": (1, 60, int),
                  "min_requests": (1, 1_000_000, int)}
        for key, (lo, hi, kind) in bounds.items():
            if key in alert:
                try:
                    value = kind(alert[key])
                except (TypeError, ValueError):
                    raise ManifestError(f"metrics.alert.{key} must be a number, not "
                                        f"{alert[key]!r}.")
                if not lo <= value <= hi:
                    raise ManifestError(f"metrics.alert.{key} must be between {lo} and {hi}, "
                                        f"not {value}.")
                rule[key] = value
        if "enabled" in alert:
            rule["enabled"] = _bool(alert["enabled"], "metrics.alert.enabled")
        out["alert"] = rule
    return out


_TOP_LEVEL = frozenset({
    "version", "name", "kind", "compose", "env", "install", "update", "health", "open",
    "teardown", "requires", "dir", "metrics", "resources",
})


def _resources(raw: Any) -> tuple[Any, ...]:
    """`resources:` as ResourceRequests; every problem named, one per line."""
    if raw is None:
        return ()
    from ..platform.contract import ContractError, validate_resources
    try:
        return tuple(validate_resources(raw))
    except ContractError as exc:
        raise ManifestError("resources is not valid: " + "; ".join(exc.reasons))


def parse_manifest(data: Any, *, source_text: str = "") -> DeployManifest:
    """Validate an already-decoded manifest."""
    if not isinstance(data, dict):
        raise ManifestError("boss.deploy.yaml must be a mapping at the top level.")
    unknown = set(data) - _TOP_LEVEL
    if unknown:
        raise ManifestError(
            f"boss.deploy.yaml has unknown field(s) {sorted(unknown)}. "
            f"Known fields: {', '.join(sorted(_TOP_LEVEL))}.")
    if data.get("version") != 1:
        raise ManifestError(
            f"version must be 1 (found {data.get('version')!r}); this BOSS reads version 1.")
    name = str(data.get("name") or "").strip()
    if not _NAME.match(name):
        raise ManifestError(
            f"name {name!r} must be lower-case letters, digits and dashes (e.g. 'boss'); it "
            "names the directory on the host.")
    kind = str(data.get("kind") or "commands").strip()
    if kind not in KINDS:
        raise ManifestError(f"kind must be 'commands' or 'compose', not {kind!r}.")

    directory = str(data.get("dir") or f"{DEFAULT_BASE_DIR}/{name}").strip().rstrip("/")
    if not directory.startswith("/") or ".." in directory.split("/") \
            or not re.match(r"^/[A-Za-z0-9._/-]+$", directory) or directory.count("/") < 2:
        raise ManifestError(
            f"dir {directory!r} must be an absolute path at least two levels deep, made of "
            "letters, digits, '.', '_', '-' and '/'.")

    install = _steps(data.get("install"), "install")
    update = _steps(data.get("update"), "update")
    teardown = _steps(data.get("teardown"), "teardown")
    compose_file = compose_project = None
    requires = data.get("requires") or {}
    if not isinstance(requires, dict):
        raise ManifestError("requires must be {sudo: bool, docker: bool}.")
    unknown_req = set(requires) - {"sudo", "docker"}
    if unknown_req:
        raise ManifestError(f"requires has unknown field(s) {sorted(unknown_req)}.")
    requires_docker = _bool(requires.get("docker"), "requires.docker")
    requires_sudo = _bool(requires.get("sudo"), "requires.sudo")

    if kind == "compose":
        compose = data.get("compose") or {}
        if not isinstance(compose, dict) or not str(compose.get("file") or "").strip():
            raise ManifestError("kind: compose needs compose: {file: <path>, project: <name>}.")
        if install or update:
            raise ManifestError(
                "kind: compose writes install and update itself (docker compose up -d "
                "--build); use kind: commands to write your own steps.")
        compose_file = str(compose["file"]).strip()
        compose_project = str(compose.get("project") or name).strip()
        if not re.match(r"^[A-Za-z0-9._/-]+$", compose_file) or compose_file.startswith("/") \
                or ".." in compose_file.split("/"):
            raise ManifestError(
                f"compose.file {compose_file!r} must be a path inside the repository.")
        if not _NAME.match(compose_project):
            raise ManifestError(
                f"compose.project {compose_project!r} must be lower-case letters, digits and "
                "dashes.")
        base = (f"docker compose -f {compose_file} -p {compose_project} "
                "--env-file {dir}/.boss/deploy.env")
        install = (Step(name="compose up", run=f"{base} up -d --build"),)
        if not teardown:
            teardown = (Step(name="compose down", run=f"{base} down"),)
        requires_docker = True
    elif data.get("compose"):
        raise ManifestError("compose: is only read with kind: compose.")

    if not install:
        raise ManifestError(
            "install must list at least one command: it is what brings the stack up on a "
            "target the first time.")
    if not update:
        update = install

    open_cfg = data.get("open")
    open_url = "{target.public_url}"
    if open_cfg is not None:
        if isinstance(open_cfg, str):
            open_cfg = {"url": open_cfg}
        if not isinstance(open_cfg, dict) or not str(open_cfg.get("url") or "").strip():
            raise ManifestError("open must be {url: <link the console opens>}.")
        open_url = str(open_cfg["url"]).strip()
        _check_placeholders(open_url, "open.url")

    return DeployManifest(
        version=1, name=name, kind=kind, dir=directory, env=_env(data.get("env")),
        install=install, update=update, health=_health(data.get("health")),
        open_url=open_url, teardown=teardown, requires_sudo=requires_sudo,
        requires_docker=requires_docker, compose_file=compose_file,
        compose_project=compose_project, metrics=_metrics(data.get("metrics")),
        resources=_resources(data.get("resources")), source_text=source_text,
    )


def load_manifest(text_or_path: Union[str, Path]) -> DeployManifest:
    """Parse `boss.deploy.yaml` from its text or from a path on disk."""
    import yaml

    if isinstance(text_or_path, Path):
        text = text_or_path.read_text(encoding="utf-8")
    else:
        text = str(text_or_path or "")
    if not text.strip():
        raise ManifestError("boss.deploy.yaml is empty.")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        raise ManifestError(f"boss.deploy.yaml is not valid YAML{where}: "
                            f"{getattr(exc, 'problem', None) or exc}")
    return parse_manifest(data, source_text=text)


def substitute(value: str, ctx: dict[str, str]) -> str:
    """Fill placeholders from `ctx`. Unknown names were refused at load time."""
    def fill(match: re.Match) -> str:
        name = match.group(1)
        if name not in ctx:
            raise ManifestError(f"no value for {{{name}}} on this target.")
        return str(ctx[name])
    return _PLACEHOLDER.sub(fill, value)


def render(manifest: DeployManifest, ctx: dict[str, str]) -> dict[str, Any]:
    """Concrete steps for one deployment: every placeholder filled, nothing executed.

    `ctx` carries commit, ref, target.host, target.public_url and target.user; `dir` comes from
    the manifest. Environment values that are generated or secret are NOT resolved here — they
    are only names until the governed apply reads them from the secret store.
    """
    full = {**{k: str(v) for k, v in ctx.items()}, "dir": manifest.dir}
    missing = sorted(PLACEHOLDERS - set(full))
    if missing:
        raise ManifestError(f"cannot render without {', '.join(missing)}.")

    def steps(items: tuple[Step, ...]) -> list[dict[str, Any]]:
        return [{"name": s.name, "run": substitute(s.run, full),
                 "timeout_seconds": s.timeout_seconds} for s in items]

    return {
        "name": manifest.name,
        "dir": manifest.dir,
        "install": steps(manifest.install),
        "update": steps(manifest.update),
        "teardown": steps(manifest.teardown),
        "health": [{"url": substitute(h.url, full), "expect": h.expect,
                    "timeout_seconds": h.timeout_seconds} for h in manifest.health],
        "open_url": substitute(manifest.open_url, full),
        "env_literals": {e.key: substitute(e.value, full)
                         for e in manifest.env if e.source == "literal"},
        "env_generated": {e.key: e.value for e in manifest.env if e.source == "generate"},
        "env_secrets": {e.key: e.value for e in manifest.env if e.source == "secret"},
    }
