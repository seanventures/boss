"""The plugin contract harness: run a plugin's hooks for real and check the contract.

    report = run_contract(Path("plugins/community/example"))
    report.ok, report.to_dict()

    python -m boss_engine.platform test plugins/community/example

What it checks (one Check each; the report is ok only when every check passed):
  manifest            boss-plugin.yaml is valid boss.plugin/v1
  hooks               every declared hook is implemented
  validate / plan     return valid / planned, and change nothing (no labelled container,
                      volume or network appears)
  private by default  the plan exposes no public port when the request is not public
  provision x2        both succeed with the same resourceId and the same binding refs
  labels              a docker plugin labels what it creates (so BOSS can clean it up)
  configure / observe when declared
  verify              succeeds AND proves a real connection (`connection` evidence)
  backup / restore    when both declared; verify again after the restore
  redaction           no secret value the broker saw appears in a result, a log line, an
                      artifact name or an error
  destroy / cleanup   destroy succeeds and nothing labelled with this run remains
  attached            BOSS never calls destroy for an attached resource

The plugin runs against ephemeral containers on free ports on this machine (LocalHost), with an
in-memory secret broker (bindings.MemoryBroker; validate and plan get a
ReadOnlyBroker). Whatever the run leaves behind is force-removed at the end unless
`keep=True`.
"""
from __future__ import annotations

import json
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

import yaml

from .contract import (
    ContractError, Hook, HookContext, InfraPlugin, LocalHost, OperationResult, OperationStatus,
    PluginManifest, PluginSource, ResourceMode, ResourceRequest, idempotency_key, load_manifest,
)
from .bindings import MemoryBroker, ReadOnlyBroker, Redactor
from .sdk import docker_available

RUN_LABEL = "boss.infra.harness"
APP_KEY = "harness"


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class HarnessReport:
    plugin_dir: str
    plugin_id: str = ""
    version: str = ""
    run_id: str = ""
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    def add(self, name: str, passed: bool, detail: str = "") -> bool:
        self.checks.append(Check(name, bool(passed), detail))
        return bool(passed)

    def to_dict(self) -> dict[str, Any]:
        return {"pluginDir": self.plugin_dir, "pluginId": self.plugin_id, "version": self.version,
                "runId": self.run_id, "ok": self.ok,
                "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks]}

    def text(self) -> str:
        lines = [f"{self.plugin_id or self.plugin_dir} {self.version} (run {self.run_id})"]
        for c in self.checks:
            lines.append(f"  [{'pass' if c.passed else 'FAIL'}] {c.name}" + (f": {c.detail}" if c.detail else ""))
        lines.append("OK" if self.ok else "FAILED")
        return "\n".join(lines)


def guarded_run(plugin: InfraPlugin, ctx: HookContext) -> OperationResult:
    """BOSS's rule, applied before any plugin code: an attached resource is never destroyed."""
    if ctx.hook == Hook.DESTROY and ctx.resource.mode == ResourceMode.ATTACHED:
        return ctx.fail("attached_never_destroyed", "an attached resource is never destroyed",
                        ).model_copy(update={"status": OperationStatus.REFUSED})
    return plugin.run(ctx)


def request_for(plugin_dir: Path, manifest: PluginManifest) -> ResourceRequest:
    """`harness.yaml` `resource:` in the plugin dir, else a default from the manifest."""
    p = plugin_dir / "harness.yaml"
    body: dict[str, Any] = {}
    if p.is_file():
        body = dict((yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("resource") or {})
    body.setdefault("name", "harness")
    body.setdefault("capability", manifest.spec.capabilities[0])
    if manifest.spec.engines:
        body.setdefault("engine", manifest.spec.engines[0])
    body.setdefault("provider", manifest.id)
    return ResourceRequest.model_validate(body)


def _labelled(run_id: str) -> set[str]:
    """Every container, volume and network carrying this run's label."""
    host = LocalHost()
    found: set[str] = set()
    for kind, cmd in (("container", "docker ps -a -q"), ("volume", "docker volume ls -q"),
                      ("network", "docker network ls -q")):
        out = host.run(f"{cmd} --filter label={RUN_LABEL}={run_id}", timeout=60)
        found |= {f"{kind}:{line.strip()}" for line in out.stdout.splitlines() if line.strip()}
    return found


def _remove_leftovers(run_id: str) -> None:
    host = LocalHost()
    f = f"--filter label={RUN_LABEL}={run_id}"
    host.run(f"ids=$(docker ps -a -q {f}); [ -z \"$ids\" ] || docker rm -f -v $ids", timeout=120)
    host.run(f"ids=$(docker volume ls -q {f}); [ -z \"$ids\" ] || docker volume rm -f $ids", timeout=120)
    host.run(f"ids=$(docker network ls -q {f}); [ -z \"$ids\" ] || docker network rm $ids", timeout=120)


def run_contract(plugin_dir: Union[str, Path], *, keep: bool = False,
                 plugin: Optional[InfraPlugin] = None) -> HarnessReport:
    """Run the contract against the plugin in `plugin_dir`. `plugin` overrides loading (tests)."""
    from .registry import RegisteredPlugin, RegistryError, load_plugin, tree_checksum

    plugin_dir = Path(plugin_dir).resolve()
    run_id = "h" + uuid.uuid4().hex[:10]
    report = HarnessReport(str(plugin_dir), run_id=run_id)
    try:
        manifest = load_manifest(plugin_dir)
    except (ContractError, OSError) as exc:
        report.add("manifest", False, "; ".join(getattr(exc, "reasons", [str(exc)])))
        return report
    report.plugin_id, report.version = manifest.id, manifest.version
    report.add("manifest", True)

    if plugin is None:
        from .registry import SOURCE_DIRS
        from .runner import plugin_for
        # The source decides where the hooks run, as in BOSS: a builtin in-process, anything
        # else out of process with its permissions enforced by the parent.
        source = SOURCE_DIRS.get(plugin_dir.parent.name, PluginSource.COMMUNITY)
        try:
            reg = RegisteredPlugin(manifest, source, plugin_dir, tree_checksum(plugin_dir))
            load_plugin(reg)  # declared hooks are implemented (checked in-process, no hook runs)
            plugin = plugin_for(reg)
        except RegistryError as exc:
            report.add("hooks", False, str(exc))
            return report
    else:
        plugin.manifest = manifest
    report.add("hooks", True, ", ".join(h.value for h in manifest.spec.hooks))

    try:
        request = request_for(plugin_dir, manifest)
    except Exception as exc:  # a bad harness.yaml is the author's problem to see
        report.add("request", False, str(exc))
        return report

    uses_docker = manifest.spec.permissions.docker
    if uses_docker and not docker_available():
        report.add("docker", False, "the plugin needs docker and docker is not available here")
        return report

    redactor = Redactor()
    broker = MemoryBroker(app_key=APP_KEY, manifest=manifest, request=request, redactor=redactor)
    read_only = ReadOnlyBroker(app_key=APP_KEY, manifest=manifest, request=request, redactor=redactor)
    logs: list[str] = []
    results: list[OperationResult] = []
    state: dict[str, Any] = {}
    workdir = Path(tempfile.mkdtemp(prefix=f"boss-harness-{run_id}-"))
    resource_id = request.resource_id(APP_KEY)
    counter = [0]

    def ctx(hook: Hook, req: ResourceRequest = request, plan_digest: Optional[str] = None) -> HookContext:
        counter[0] += 1
        return HookContext(
            hook=hook, operation_id=f"{run_id}-{counter[0]}-{hook.value}",
            idempotency_key=idempotency_key(manifest.id, manifest.version, resource_id, hook, plan_digest),
            resource=req, resource_id=resource_id, app_key=APP_KEY,
            secrets=read_only if hook in (Hook.VALIDATE, Hook.PLAN) else broker, workdir=workdir,
            plan_digest=plan_digest, state=dict(state), labels={RUN_LABEL: run_id},
            log=logs.append, plugin_id=manifest.id, plugin_version=manifest.version)

    def call(hook: Hook, **kw: Any) -> OperationResult:
        r = guarded_run(plugin, ctx(hook, **kw))
        results.append(r)
        if r.state:
            state.update(r.state)
        return r

    def why(r: OperationResult) -> str:
        return f"status {r.status.value}" + (f" ({r.errors[0].code}: {r.errors[0].message})" if r.errors else "")

    snapshot = (lambda: _labelled(run_id)) if uses_docker else (lambda: set())
    try:
        before = snapshot()
        r = call(Hook.VALIDATE)
        report.add("validate", r.status == OperationStatus.VALID,
                   why(r) if r.status != OperationStatus.VALID else "")
        r = call(Hook.PLAN)
        planned = r.status == OperationStatus.PLANNED and r.plan is not None
        report.add("plan", planned, why(r) if not planned else "")
        plan_digest = None
        if r.plan is not None:
            from .contract import digest_of
            plan_digest = digest_of(r.plan)
            if not request.public:
                public = r.plan.publicAccess or any(p.public for p in r.plan.ports)
                report.add("private by default", not public,
                           "the plan exposes a public port for a private request" if public else "")
        after = snapshot()
        report.add("read-only hooks change nothing", after == before,
                   f"appeared: {sorted(after - before)}" if after != before else "")

        first = call(Hook.PROVISION, plan_digest=plan_digest)
        second = call(Hook.PROVISION, plan_digest=plan_digest)
        same = (first.ok and second.ok and first.resourceId == second.resourceId
                and {k: v.secretRef for k, v in first.bindings.items()}
                == {k: v.secretRef for k, v in second.bindings.items()})
        detail = ""
        if not first.ok:
            detail = "first " + why(first)
        elif not second.ok:
            detail = "second " + why(second)
        elif not same:
            detail = "the second provision returned different ids or binding refs"
        report.add("provision is idempotent", same, detail)
        undeclared = sorted(set(first.bindings) - {request.bindings.get(n, n) for n in manifest.spec.bindings})
        report.add("bindings declared", not undeclared,
                   f"undeclared: {', '.join(undeclared)}" if undeclared else "")
        if uses_docker and first.ok:
            report.add("labels", bool(snapshot()),
                       "" if snapshot() else f"nothing carries the {RUN_LABEL} label; use sdk.docker_labels_args(ctx)")

        if first.ok:
            if manifest.implements(Hook.CONFIGURE):
                r = call(Hook.CONFIGURE, plan_digest=plan_digest)
                report.add("configure", r.ok, why(r) if not r.ok else "")
            r = call(Hook.VERIFY)
            report.add("verify proves a real connection", r.ok and r.connected,
                       why(r) if not r.ok else ("" if r.connected else "no passed `connection` evidence"))
            if manifest.implements(Hook.OBSERVE):
                r = call(Hook.OBSERVE)
                report.add("observe", r.ok, why(r) if not r.ok else "")
            if manifest.implements(Hook.BACKUP) and manifest.implements(Hook.RESTORE):
                b = call(Hook.BACKUP, plan_digest=plan_digest)
                report.add("backup", b.ok, why(b) if not b.ok else "")
                if b.ok:
                    rs = call(Hook.RESTORE, plan_digest=plan_digest)
                    v = call(Hook.VERIFY)
                    report.add("restore", rs.ok and v.connected, why(rs) if not rs.ok else
                               ("" if v.connected else "verify after restore found no connection"))

        attached = request.model_copy(update={"mode": ResourceMode.ATTACHED})
        called = {"destroy": False}
        original = plugin.run

        def spy(c: HookContext) -> OperationResult:
            called["destroy"] = called["destroy"] or c.hook == Hook.DESTROY
            return original(c)

        plugin.run = spy  # type: ignore[method-assign]
        try:
            r = guarded_run(plugin, ctx(Hook.DESTROY, req=attached))
        finally:
            del plugin.run  # type: ignore[attr-defined]
        report.add("attached is never destroyed", not called["destroy"] and r.status == OperationStatus.REFUSED)

        r = call(Hook.DESTROY, plan_digest=plan_digest)
        report.add("destroy", r.ok, why(r) if not r.ok else "")
        left = snapshot()
        report.add("cleanup leaves nothing", not left, f"left: {sorted(left)}" if left else "")

        leaks = _leaks(redactor, results, logs)
        report.add("secrets are redacted", not leaks, "; ".join(leaks))
    finally:
        if uses_docker and not keep:
            _remove_leftovers(run_id)
    return report


def _leaks(redactor: Redactor, results: list[OperationResult], logs: list[str]) -> list[str]:
    out: list[str] = []
    for r in results:
        if redactor.leaks(r.model_dump_json()):
            out.append(f"a secret value is in the {r.hook.value} result")
    if any(redactor.leaks(line) for line in logs):
        out.append("a secret value is in a log line")
    return out


def report_json(report: HarnessReport) -> str:
    return json.dumps(report.to_dict(), indent=2)
