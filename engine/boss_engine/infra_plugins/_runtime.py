"""What every official plugin shares: commands on the host, compose, probes, secrets, redaction.

Contract 1.1 facts this module is built around:

  * WHERE: every command runs through `ctx.host.run(...)`: the deploy's host over BOSS's
    governed SSH, or this machine (LocalHost) in the harness. A plugin never opens its own
    connection, so nothing here calls subprocess.
  * WHAT LASTS: `ctx.workdir` is a fresh temporary directory per hook and `ctx.state` is the
    only thing BOSS keeps between hooks. Anything durable lives on the HOST (compose services,
    named volumes, backups inside a volume) or in state (non-secret facts and secret REFS).
  * SECRETS: the broker stores only the binding names a manifest declares. So a plugin keeps
    no admin password: admin work runs INSIDE the resource container (`docker exec`, the
    database's local socket), and a resource service that needs a value reads the app's own
    binding (`${REDIS_URL}`-style interpolation), never a second secret.

How a value travels: `run(argv, env=...)` sends the env as a base64 line on stdin, the remote
shell exports it and `exec`s the command; `docker run -e NAME` (no `=value`) then reads it from
that environment. A value is never in a command line, a written file or a fragment.

Every container, volume and probe carries `boss.plugin`, `boss.resource`, `boss.app` and the
caller's labels (`ctx.labels`), so cleanup is checkable by label. Data services publish NO port:
verify reaches them the way the app does, with a probe container on the app's compose network
using the app's binding.
"""
from __future__ import annotations

import base64
import re
import secrets as _secrets
import shlex
import time
from pathlib import Path
from typing import Any, Iterable, Optional, Union

import yaml

from ..platform.contract import (
    Hook, HookContext, InfraPlugin, LocalHost, OperationResult, OperationStatus, PlannedChange,
    PortSpec, ResourceMode, ResourcePlan, SecretRef,
)

#: A pinned container image: `name@sha256:<64 hex>` or `name:Tag@sha256:<64 hex>` (tags may hold
#: upper case, e.g. MinIO's RELEASE.*).
_PINNED = re.compile(r"^[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9_.-]{1,128})?@sha256:[0-9a-f]{64}$")
#: Never shorter than this is a value worth scrubbing (a 1-char "secret" would scrub every log).
_MIN_SCRUB = 6
DEFAULT_TIMEOUT = 180


class HookFailure(Exception):
    """Raised inside a hook; `OfficialPlugin.run` turns it into a scrubbed result."""

    def __init__(self, code: str, message: str, *, retryable: bool = False,
                 status: OperationStatus = OperationStatus.FAILED) -> None:
        super().__init__(message)
        self.code, self.message, self.retryable, self.status = code, message, retryable, status


def official_dir() -> Path:
    """`plugins/official/`, under the registry's plugins root."""
    from ..platform.registry import plugins_root
    return plugins_root() / "official"


# ---------------------------------------------------------------------------------------------
# Naming. Deterministic, so every hook (and the compose provider) derives the same names.
# ---------------------------------------------------------------------------------------------

def _slug(value: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")[:limit] or "x"


def project_name(ctx: HookContext) -> str:
    """One compose project per (app, environment). `bossapp-` keeps it clear of BOSS's own
    `boss-community` project, whatever an app is called."""
    return f"bossapp-{_slug(ctx.app_key)}-{_slug(ctx.resource.environment, 16)}"


def network_name(ctx: HookContext) -> str:
    return f"{project_name(ctx)}_default"


def service_name(ctx_or_name: Union[HookContext, str]) -> str:
    """`res-<resource>`: a prefix an app's own compose services do not use, so a merge can
    never silently replace an app service with a resource (or the reverse)."""
    name = ctx_or_name if isinstance(ctx_or_name, str) else ctx_or_name.resource.name
    return f"res-{_slug(name, 48)}"


def resource_id_of(ctx: HookContext, name: str) -> str:
    """The resource id of ANOTHER resource of the same app and environment."""
    return f"{ctx.resource.environment}/{ctx.app_key}/{name}"


def labels(ctx: HookContext, plugin_id: str) -> dict[str, str]:
    out = {"boss.plugin": plugin_id, "boss.resource": ctx.resource_id, "boss.app": ctx.app_key}
    out.update({str(k): str(v) for k, v in (ctx.labels or {}).items()})
    return out


def env_name(ctx: HookContext, binding: str) -> str:
    """The env name the app gets for one of this plugin's bindings (the request may rename)."""
    return ctx.resource.bindings.get(binding, binding)


def new_password(nbytes: int = 24) -> str:
    """Hex: URL-safe with no percent-encoding."""
    return _secrets.token_hex(nbytes)


# ---------------------------------------------------------------------------------------------
# A config schema check: the subset of JSON Schema the official manifests use.
# ---------------------------------------------------------------------------------------------

_TYPES = {"string": str, "integer": int, "boolean": bool, "object": dict, "array": list,
          "number": (int, float)}


def schema_problems(value: Any, schema: dict, where: str = "config") -> list[str]:
    """type, properties, required, additionalProperties: false, enum, pattern, minimum, maximum,
    items: enough for every official manifest."""
    out: list[str] = []
    t = schema.get("type")
    if t:
        if isinstance(value, bool) and t in ("integer", "number"):
            return [f"{where} must be {t}"]
        if not isinstance(value, _TYPES[t]):
            return [f"{where} must be {t}"]
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{where} must be one of {', '.join(map(str, schema['enum']))}")
    if isinstance(value, str) and "pattern" in schema and not re.fullmatch(schema["pattern"], value):
        out.append(f"{where} does not match {schema['pattern']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(f"{where} must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            out.append(f"{where} must be <= {schema['maximum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                out.append(f"{where}.{key} is required")
        for key, sub in value.items():
            if key in props:
                out.extend(schema_problems(sub, props[key], f"{where}.{key}"))
            elif schema.get("additionalProperties") is False:
                out.append(f"{where}.{key} is not a setting this plugin has")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            out.extend(schema_problems(item, schema["items"], f"{where}[{i}]"))
    return out


# ---------------------------------------------------------------------------------------------
# The per-hook session
# ---------------------------------------------------------------------------------------------

class Outcome:
    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class Session:
    def __init__(self, ctx: HookContext, plugin_id: str) -> None:
        self.ctx = ctx
        self.plugin_id = plugin_id
        self._values: set[str] = set()
        self.host = getattr(ctx, "host", None) or LocalHost()

    # -- secrets ------------------------------------------------------------------------------
    def remember(self, value: Optional[str]) -> None:
        if value and len(value) >= _MIN_SCRUB:
            self._values.add(value)

    def put(self, binding: str, value: str) -> SecretRef:
        """Store a binding value; the broker files it under the (renamed) env name."""
        self.remember(value)
        ref = self.ctx.secrets.put(binding, value)
        return ref if isinstance(ref, SecretRef) else SecretRef(secretRef=str(ref))

    def get(self, ref: Union[str, SecretRef, None]) -> str:
        if not ref:
            raise HookFailure("state_lost", "a binding this resource needs is missing from its state; "
                              "provision it again")
        value = self.ctx.secrets.resolve(ref)
        self.remember(value)
        return value

    def scrub(self, text: Any) -> str:
        s = "" if text is None else str(text)
        for v in sorted(self._values, key=len, reverse=True):
            s = s.replace(v, "[redacted]")
        # a URL's userinfo is a credential even when this session never saw the value
        # (no '/', quote or space in either part, so a scheme in JSON never reaches a later '@')
        return re.sub(r"(://[^:/@\s\"']*:)[^@\s/\"']+@", r"\1[redacted]@", s)

    def log(self, line: str) -> None:
        try:
            self.ctx.log(self.scrub(line))
        except Exception:  # a logger must never fail the operation
            pass

    def check_cancel(self) -> None:
        if self.ctx.is_cancelled():
            raise HookFailure("cancelled", "the operation was cancelled", status=OperationStatus.CANCELLED)

    def _timeout(self, timeout: Optional[float]) -> int:
        t = float(timeout or DEFAULT_TIMEOUT)
        if self.ctx.deadline is not None:
            t = min(t, max(1.0, self.ctx.deadline - time.monotonic()))
        return max(1, int(t))

    # -- commands on the host -----------------------------------------------------------------
    def run(self, argv: list[str], *, env: Optional[dict[str, str]] = None, stdin: str = "",
            timeout: Optional[float] = None, check: bool = True, what: str = "") -> Outcome:
        """One command on ctx.host. `env` values travel on stdin (base64), never in the command."""
        self.check_cancel()
        for v in (env or {}).values():
            self.remember(v)
        command = "exec " + shlex.join(argv)
        stdin_text = stdin or None
        if env:
            exports = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in env.items())
            command = ('IFS= read -r __boss_env && eval "$(printf %s "$__boss_env" | base64 -d)" && '
                       "unset __boss_env && " + command)
            stdin_text = base64.b64encode(exports.encode()).decode() + "\n" + (stdin or "")
        raw = self.host.run(command, timeout=self._timeout(timeout), stdin_text=stdin_text)
        out = Outcome(int(getattr(raw, "returncode", 1)), str(getattr(raw, "stdout", "") or ""),
                      str(getattr(raw, "stderr", "") or ""))
        if out.returncode == 124 and "timed out" in out.stderr:
            raise HookFailure("timeout", f"{what or argv[0]} did not finish in time", retryable=True)
        if check and out.returncode != 0:
            err = self.scrub(out.stderr.strip() or out.stdout.strip())[-600:]
            raise HookFailure("command_failed", f"{what or ' '.join(argv[:3])} failed: {err}", retryable=True)
        return out

    def text(self, out: Outcome) -> str:
        return self.scrub(out.stdout)

    # -- compose ------------------------------------------------------------------------------
    def check_fragment(self, fragment: dict) -> str:
        """The fragment holds `${VAR}` placeholders, never a value: checked, not hoped."""
        text = yaml.safe_dump(fragment, sort_keys=False)
        for v in self._values:
            if v in text:
                raise HookFailure("secret_in_file", "a secret value reached a compose fragment; refused")
        return text

    def compose(self, args: list[str], fragment: dict, *, env: Optional[dict[str, str]] = None,
                timeout: Optional[float] = None, check: bool = True, what: str = "") -> Outcome:
        """`docker compose -p <project> -f - <args>`: the fragment travels on stdin, so nothing is
        written on the host and the same call works locally and over SSH."""
        return self.run(["docker", "compose", "-p", project_name(self.ctx), "-f", "-"] + args,
                        env=env, stdin=self.check_fragment(fragment), timeout=timeout, check=check,
                        what=what or "docker compose")

    def compose_up(self, fragment: dict, service: str, env: Optional[dict[str, str]] = None, *,
                   wait: bool = True, timeout: float = 240) -> None:
        args = ["up", "-d"] + (["--wait", "--wait-timeout", str(int(timeout))] if wait else []) + [service]
        self.compose(args, fragment, env=env, timeout=timeout + 30, what=f"starting {service}")

    def container_id(self, service: str, *, rid: Optional[str] = None) -> Optional[str]:
        """The container of `service` in this app's project (or of resource `rid`, any service)."""
        flt = (["--filter", f"label=boss.resource={rid}"] if rid else
               ["--filter", f"label=com.docker.compose.project={project_name(self.ctx)}",
                "--filter", f"label=com.docker.compose.service={service}"])
        ids = self.run(["docker", "ps", "-aq"] + flt, check=False).stdout.split()
        return ids[0] if ids else None

    def require_container(self, service: str) -> str:
        cid = self.container_id(service)
        if not cid:
            raise HookFailure("not_provisioned", f"{self.ctx.resource_id} has no {service} container")
        return cid

    def compose_destroy(self, service: str, volumes: Iterable[str] = ()) -> list[str]:
        """Remove the service's container, its named volumes and, when nothing else uses it, the
        project network. Returns what was removed (names only)."""
        removed: list[str] = []
        cid = self.container_id(service)
        if cid:
            self.run(["docker", "rm", "-f", "-v", cid], what=f"removing {service}")
            removed.append(f"container:{service}")
        for vol in volumes:
            if self.run(["docker", "volume", "rm", "-f", f"{project_name(self.ctx)}_{vol}"], check=False).returncode == 0:
                removed.append(f"volume:{vol}")
        self.remove_network_if_unused()
        return removed

    def remove_network_if_unused(self) -> None:
        net = network_name(self.ctx)
        out = self.run(["docker", "network", "inspect", net, "--format", "{{len .Containers}}"], check=False)
        if out.returncode == 0 and out.stdout.strip() == "0":
            self.run(["docker", "network", "rm", net], check=False)

    def published_ports(self, cid: str) -> list[str]:
        out = self.run(["docker", "port", cid], check=False)
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]

    # -- probes and exec ----------------------------------------------------------------------
    def probe(self, image: str, argv: list[str], *, env: Optional[dict[str, str]] = None, stdin: str = "",
              network: Optional[str] = None, entrypoint: Optional[str] = None, timeout: float = 90,
              check: bool = True, what: str = "probe") -> Outcome:
        """A throwaway container on the app's network, connecting as the app would."""
        cmd = ["docker", "run", "--rm", "-i", "--network", network or network_name(self.ctx)]
        for k, v in {**labels(self.ctx, self.plugin_id), "boss.probe": "1"}.items():
            cmd += ["--label", f"{k}={v}"]
        for k in (env or {}):
            cmd += ["-e", k]
        if entrypoint is not None:
            cmd += ["--entrypoint", entrypoint]
        return self.run(cmd + [image] + argv, env=env, stdin=stdin, timeout=timeout, check=check, what=what)

    def exec(self, cid: str, argv: list[str], *, env: Optional[dict[str, str]] = None, stdin: str = "",
             timeout: float = 120, check: bool = True, what: str = "") -> Outcome:
        """A command INSIDE a resource container (admin work over its local socket)."""
        cmd = ["docker", "exec", "-i"]
        for k in (env or {}):
            cmd += ["-e", k]
        return self.run(cmd + [cid] + argv, env=env, stdin=stdin, timeout=timeout, check=check, what=what)


# ---------------------------------------------------------------------------------------------
# The base class
# ---------------------------------------------------------------------------------------------

def _hook_method(hook: Hook):
    def method(self: "OfficialPlugin", ctx: HookContext) -> OperationResult:
        ctx.hook = hook
        return self.run(ctx)
    method.__name__ = hook.value
    method.__doc__ = f"The {hook.value} hook (OfficialPlugin.run dispatches it to do_{hook.value})."
    return method


class OfficialPlugin(InfraPlugin):
    """An official plugin: the manifest is `plugins/official/<directory>/boss-plugin.yaml`.

    Subclasses implement `_validate`, `_changes`, `_ports`, `_risks` and the `do_<hook>` methods;
    this class owns schema checks, attached-mode handling, timing and redaction."""

    directory: str = ""

    def __init__(self) -> None:
        if type(self).manifest is None:
            from ..platform.contract import load_manifest
            type(self).manifest = load_manifest(official_dir() / self.directory)

    @property
    def plugin_id(self) -> str:
        return self.manifest.id if self.manifest else f"official.{self.directory}"

    # -- dispatch -----------------------------------------------------------------------------
    def run(self, ctx: HookContext) -> OperationResult:
        ctx.plugin_id = ctx.plugin_id or self.plugin_id
        ctx.plugin_version = ctx.plugin_version or self.manifest.version
        s = Session(ctx, self.plugin_id)
        started = time.monotonic()
        if not self.manifest.implements(ctx.hook):
            result = ctx.result(OperationStatus.UNSUPPORTED)
        else:
            try:
                if ctx.hook is not Hook.VALIDATE:
                    problems = self.problems(ctx)
                    if problems:
                        raise HookFailure("invalid_request", "; ".join(problems))
                if ctx.resource.mode is ResourceMode.ATTACHED and ctx.hook in (Hook.PROVISION, Hook.CONFIGURE):
                    # Nothing to create: BOSS binds the vault secrets the operator already set.
                    result = ctx.result(state={"mode": "attached"},
                                        evidence=[evidence("attach", True, "config",
                                                           "nothing created; bound to existing secrets")])
                elif ctx.resource.mode is ResourceMode.ATTACHED and ctx.hook in (Hook.BACKUP, Hook.RESTORE,
                                                                                  Hook.DESTROY):
                    raise HookFailure("attached_resource", f"{ctx.resource_id} is attached: BOSS did not create "
                                      f"it and never runs {ctx.hook.value} on it", status=OperationStatus.REFUSED)
                else:
                    result = getattr(self, f"do_{ctx.hook.value}")(ctx, s)
            except HookFailure as exc:
                result = ctx.result(exc.status, errors=[{"code": exc.code, "message": s.scrub(exc.message)[:1000],
                                                         "retryable": exc.retryable}])
            except Exception as exc:  # a plugin bug is a failed operation, never a crashed worker
                result = ctx.fail("plugin_error", s.scrub(f"{type(exc).__name__}: {exc}")[:900])
        result.durationMs = int((time.monotonic() - started) * 1000)
        dumped = result.model_dump_json()
        if s.scrub(dumped) != dumped:  # last line of defence
            return ctx.fail("secret_in_result", "the result carried a secret value; it was withheld")
        return result

    # -- validate -----------------------------------------------------------------------------
    def problems(self, ctx: HookContext) -> list[str]:
        r = ctx.resource
        out = []
        if r.capability not in self.manifest.spec.capabilities:
            out.append(f"{self.plugin_id} provides {', '.join(self.manifest.spec.capabilities)}, not {r.capability}")
        if r.engine and self.manifest.spec.engines and r.engine not in self.manifest.spec.engines:
            out.append(f"engine {r.engine!r} is not one of {', '.join(self.manifest.spec.engines)}")
        out += schema_problems(r.config, self.manifest.spec.configSchema or {"type": "object"})
        image = r.config.get("image") if isinstance(r.config, dict) else None
        if image and not _PINNED.fullmatch(str(image)):
            out.append("config.image must be pinned by digest (name@sha256:...)")
        if r.public and not self.public_allowed(ctx):
            out.append(f"{r.capability} is never published publicly by {self.plugin_id}; "
                       "the app reaches it on its private compose network")
        out += self._validate(ctx)
        return out

    def public_allowed(self, ctx: HookContext) -> bool:
        return False

    def _validate(self, ctx: HookContext) -> list[str]:
        return []

    def do_validate(self, ctx: HookContext, s: Session) -> OperationResult:
        problems = self.problems(ctx)
        if problems:
            return ctx.result(OperationStatus.INVALID,
                              errors=[{"code": "invalid_request", "message": p[:1000]} for p in problems])
        return ctx.result()

    # -- plan ---------------------------------------------------------------------------------
    def _changes(self, ctx: HookContext) -> list[PlannedChange]:
        return []

    def _ports(self, ctx: HookContext) -> list[PortSpec]:
        return []

    def _risks(self, ctx: HookContext) -> list[str]:
        return []

    def do_plan(self, ctx: HookContext, s: Session) -> OperationResult:
        r = ctx.resource
        if ctx.state.get("desired") == "absent":
            changes = ([PlannedChange(action="noop", target=ctx.resource_id,
                                      detail="attached: BOSS never deletes a resource it did not create")]
                       if r.mode is ResourceMode.ATTACHED else
                       [PlannedChange(action="delete", target=ctx.resource_id, destructive=True,
                                      detail="remove the resource and its data")])
        elif r.mode is ResourceMode.ATTACHED:
            changes = [PlannedChange(action="attach", target=ctx.resource_id,
                                     detail="bind to the existing resource's vault secrets")]
        else:
            changes = self._changes(ctx)
        ports = self._ports(ctx)
        plan = ResourcePlan(
            resourceId=ctx.resource_id, pluginId=self.plugin_id, pluginVersion=self.manifest.version,
            mode=r.mode, changes=changes, ports=ports, publicAccess=any(p.public for p in ports),
            bindings=[env_name(ctx, b) for b in self.manifest.spec.bindings],
            dependsOn=list(r.dependsOn), risks=self._risks(ctx),
            limitations=list(self.manifest.spec.limitations),
            cost={"monthlyUsd": 0.0, "basis": "runs on the app's own host; no metered service"})
        return ctx.result(plan=plan)

    # -- guards and bindings ------------------------------------------------------------------
    def guard_destroy(self, ctx: HookContext) -> None:
        """Refuse a resource other resources still depend on (`state.dependents`). Attached
        resources never get here (run() refuses them). Approval is BOSS's: the executor runs a
        mutating hook only with the approved plan's digest."""
        dependents = [d for d in (ctx.state.get("dependents") or []) if d]
        if dependents:
            raise HookFailure("has_dependents", f"{ctx.resource_id} is still used by {', '.join(dependents)}; "
                              "destroy those first", status=OperationStatus.REFUSED)

    def store_bindings(self, ctx: HookContext, s: Session, mint: dict[str, Any]) -> dict[str, str]:
        """Env name -> ref for this plugin's bindings: refs already in state are kept (the value
        is never minted twice); a missing one is minted by calling `mint[binding]()` and stored."""
        refs = dict(ctx.state.get("bindingRefs") or {})
        for binding, make in mint.items():
            name = env_name(ctx, binding)
            if name not in refs:
                refs[name] = s.put(binding, make() if callable(make) else make).secretRef
        return refs

    def ref(self, ctx: HookContext, binding: str) -> Optional[str]:
        return (ctx.state.get("bindingRefs") or {}).get(env_name(ctx, binding))

    @staticmethod
    def as_bindings(refs: dict[str, str]) -> dict[str, SecretRef]:
        return {name: SecretRef(secretRef=ref) for name, ref in refs.items()}

    def delete_bindings(self, ctx: HookContext) -> list[str]:
        removed = []
        for name, ref in (ctx.state.get("bindingRefs") or {}).items():
            ctx.secrets.delete(ref)
            removed.append(f"secret:{name}")
        return removed


for _hook in Hook:
    setattr(OfficialPlugin, _hook.value, _hook_method(_hook))


def evidence(check: str, passed: bool, kind: str = "other", detail: str = "") -> dict:
    return {"check": check, "passed": bool(passed), "kind": kind, "detail": detail[:500]}
