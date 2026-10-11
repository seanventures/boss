"""Resolver and declarative plan (spec §4.2).

    resolved = resolve(requests, registry)          # a plugin per resource, dependency order
    plan     = build_plan(resolved, app_key=..., environment=..., existing=ledger_rows)

`resolve` picks a plugin per request: the named `provider` (at the request's version pin and the
operator's), or the best usable plugin for the capability, features and engine (certified first).
A plugin's `requires` (pgvector needs database.relational) must be met by another resource in
the same file, which becomes an implied dependency. Cycles and unmet needs are refused with one
line each.

`build_plan` runs only read-only hooks (validate, plan) with a broker that refuses writes, then
applies policy. The result is a plain JSON document: resources, order, bindings (names only),
ports, publicAccess, destructive actions, expected cost, approvals required, refusals, the plugin
versions and checksums used, and its `digest` - the content an approval pins.
"""
from __future__ import annotations

import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .bindings import ReadOnlyBroker, Redactor
from .contract import (
    API_VERSION, ContractError, Hook, HookContext, InfraPlugin, OperationStatus, PlannedChange,
    ResourceMode, ResourcePlan, ResourceRequest, digest_of, idempotency_key,
)
from .policy import Finding, evaluate
from .registry import RegisteredPlugin, Registry, RegistryError, version_matches

PLAN_KIND = "boss.infra.plan/v1"


@dataclass
class Resolved:
    request: ResourceRequest
    plugin: RegisteredPlugin
    depends_on: list[str] = field(default_factory=list)


def _provides(plugin: RegisteredPlugin, capability: str) -> bool:
    caps = plugin.manifest.spec.capabilities if plugin.manifest else []
    return any(c == capability or c.startswith(capability + ".") for c in caps)


def resolve(requests: list[ResourceRequest], registry: Registry) -> list[Resolved]:
    reasons: list[str] = []
    picked: dict[str, Resolved] = {}
    for r in requests:
        try:
            if r.provider:
                plugin = registry.resolve_version(r.provider, r.version)
                if not _provides(plugin, r.capability):
                    reasons.append(f"{r.name}: {r.provider} does not provide {r.capability}")
                    continue
            else:
                found = [p for p in registry.search(r.capability, r.features, r.engine)
                         if version_matches(p.version, r.version)]
                if not found:
                    reasons.append(f"{r.name}: no enabled plugin provides {r.capability}"
                                   + (f" with {', '.join(r.features)}" if r.features else "")
                                   + (f" for {r.engine}" if r.engine else ""))
                    continue
                plugin = registry.resolve_version(found[0].id, r.version)
        except RegistryError as exc:
            reasons.append(f"{r.name}: {exc}")
            continue
        missing = set(r.features) - set(plugin.manifest.spec.features)
        if missing:
            reasons.append(f"{r.name}: {plugin.id} lacks {', '.join(sorted(missing))}")
            continue
        picked[r.name] = Resolved(r, plugin, list(r.dependsOn))
    # Implied dependencies: what a plugin requires must be provided by another resource here.
    for res in picked.values():
        for need in res.plugin.manifest.spec.requires:
            providers = [o.request.name for o in picked.values()
                         if o is not res and (o.request.capability == need or _provides(o.plugin, need))]
            if not providers:
                reasons.append(f"{res.request.name}: {res.plugin.id} needs a {need} resource in the same file")
            elif not set(providers) & set(res.depends_on):
                res.depends_on.append(providers[0])
    if reasons:
        raise ContractError(reasons, "resources")
    return _topological(list(picked.values()))


def _topological(items: list[Resolved]) -> list[Resolved]:
    by = {r.request.name: r for r in items}
    order: list[Resolved] = []
    state: dict[str, int] = {}

    def visit(name: str, path: list[str]) -> None:
        if state.get(name) == 2:
            return
        if state.get(name) == 1:
            raise ContractError([f"dependency cycle: {' -> '.join(path + [name])}"], "resources")
        state[name] = 1
        for d in by[name].depends_on:
            if d in by:
                visit(d, path + [name])
        state[name] = 2
        order.append(by[name])

    for name in sorted(by):
        visit(name, [])
    return order


PluginFor = Callable[[RegisteredPlugin], InfraPlugin]


def _default_plugin_for(reg: RegisteredPlugin) -> InfraPlugin:
    from .runner import plugin_for
    return plugin_for(reg)


def read_only_context(hook: Hook, res: Resolved, *, app_key: str, state: dict[str, Any],
                      redactor: Redactor, workdir: Path, host: Any = None) -> HookContext:
    m = res.plugin.manifest
    rid = res.request.resource_id(app_key)
    kw: dict[str, Any] = {"host": host} if host is not None else {}
    return HookContext(
        hook=hook, operation_id=f"op-{uuid.uuid4().hex[:12]}",
        idempotency_key=idempotency_key(m.id, m.version, rid, hook, None), resource=res.request,
        resource_id=rid, app_key=app_key, workdir=workdir, state=dict(state),
        secrets=ReadOnlyBroker(app_key=app_key, manifest=m, request=res.request, redactor=redactor),
        labels={"boss.infra.resource": rid, "boss.infra.plugin": m.id}, log=lambda _l: None,
        plugin_id=m.id, plugin_version=m.version, **kw)


def plan_resource(res: Resolved, plugin: InfraPlugin, *, app_key: str, state: dict[str, Any],
                  workdir: Path, host: Any = None) -> ResourcePlan:
    """validate + plan for one resource; the ids, mode and bindings are BOSS's, not the plugin's."""
    m = res.plugin.manifest
    rid = res.request.resource_id(app_key)
    bindings = [res.request.bindings.get(b, b) for b in m.spec.bindings]
    if res.request.mode == ResourceMode.ATTACHED:
        return ResourcePlan(resourceId=rid, pluginId=m.id, pluginVersion=m.version, mode=ResourceMode.ATTACHED,
                            changes=[PlannedChange(action="attach", target=res.request.name,
                                                   detail="connect to the existing resource; never destroyed")],
                            bindings=sorted(res.request.attach.bindings) if res.request.attach else [],
                            dependsOn=res.depends_on, limitations=list(m.spec.limitations))
    redactor = Redactor()
    v = plugin.run(read_only_context(Hook.VALIDATE, res, app_key=app_key, state=state, redactor=redactor,
                                     workdir=workdir, host=host))
    if v.status != OperationStatus.VALID:
        raise ContractError([f"{res.request.name}: {e.message}" for e in v.errors]
                            or [f"{res.request.name}: {m.id} validate said {v.status.value}"], "resources")
    p = plugin.run(read_only_context(Hook.PLAN, res, app_key=app_key, state=state, redactor=redactor,
                                     workdir=workdir, host=host))
    if p.status != OperationStatus.PLANNED or p.plan is None:
        raise ContractError([f"{res.request.name}: {e.message}" for e in p.errors]
                            or [f"{res.request.name}: {m.id} returned no plan"], "resources")
    return p.plan.model_copy(update={"resourceId": rid, "pluginId": m.id, "pluginVersion": m.version,
                                     "mode": res.request.mode, "bindings": bindings,
                                     "dependsOn": res.depends_on})


def build_plan(resolved: list[Resolved], *, app_key: str, environment: str,
               existing: Iterable[dict[str, Any]] = (), plugin_for: Optional[PluginFor] = None,
               host: Any = None) -> dict[str, Any]:
    """The declarative plan document. `existing` are this app's live ledger rows
    (infra_platform_resources), so a resource dropped from the file shows as a removal."""
    plugin_for = plugin_for or _default_plugin_for
    existing = [e for e in existing if not e.get("destroyed_at")]
    by_name = {e["name"]: e for e in existing}
    pairs: list[tuple[Optional[ResourceRequest], ResourcePlan]] = []
    with tempfile.TemporaryDirectory(prefix="boss-infra-plan-") as tmp:
        for res in resolved:
            state = dict((by_name.get(res.request.name) or {}).get("state") or {})
            plan = plan_resource(res, plugin_for(res.plugin), app_key=app_key, state=state,
                                 workdir=Path(tmp), host=host)
            pairs.append((res.request, plan))
    wanted = {r.request.name for r in resolved}
    for row in existing:
        if row["name"] in wanted:
            continue
        mode = ResourceMode(row["mode"])
        change = (PlannedChange(action="noop", target=row["name"], detail="attached: detached, not destroyed")
                  if mode == ResourceMode.ATTACHED else
                  PlannedChange(action="delete", target=row["name"], detail="no longer in boss.deploy.yaml",
                                destructive=True))
        pairs.append((None, ResourcePlan(resourceId=row["resource_id"], pluginId=row["plugin_id"],
                                         pluginVersion=row["plugin_version"], mode=mode, changes=[change])))
    findings = evaluate(pairs, environment=environment)
    return assemble(pairs, resolved, findings, app_key=app_key, environment=environment)


def assemble(pairs: list[tuple[Optional[ResourceRequest], ResourcePlan]], resolved: list[Resolved],
             findings: list[Finding], *, app_key: str, environment: str) -> dict[str, Any]:
    resources = []
    bindings: dict[str, str] = {}
    ports = []
    destructive = []
    costs = []
    for request, plan in pairs:
        name = request.name if request else plan.resourceId.rsplit("/", 1)[-1]
        body = plan.model_dump(mode="json", exclude_none=True)
        body.update({"name": name, "capability": request.capability if request else None,
                     "public": bool(request and request.public), "destructive": plan.destructive,
                     "removed": request is None})
        resources.append(body)
        for b in plan.bindings:
            bindings.setdefault(b, name)
        ports += [{**p.model_dump(mode="json", exclude_none=True), "resource": name} for p in plan.ports]
        destructive += [{"resource": name, "action": c.action, "target": c.target, "detail": c.detail}
                        for c in plan.changes if c.destructive or c.action in ("delete", "replace")]
        if plan.cost is not None:
            costs.append(plan.cost.monthlyUsd)
    known = [c for c in costs if c is not None]
    doc = {
        "kind": PLAN_KIND, "apiVersion": API_VERSION, "app": app_key, "environment": environment,
        "order": [r.request.name for r in resolved],
        "resources": resources, "bindings": bindings, "ports": ports,
        "publicAccess": any(r["public"] or r.get("publicAccess") for r in resources),
        "destructive": destructive,
        "cost": {"monthlyUsd": round(sum(known), 2) if known else None,
                 "complete": len(known) == len(pairs),
                 "basis": "plugin estimates" if known else "no plugin gave an estimate"},
        "approvalsRequired": [f.as_dict() for f in findings if f.kind == "approval"],
        "refusals": [f.as_dict() for f in findings if f.kind == "refuse"],
        "plugins": sorted({(r.plugin.id, r.plugin.version, r.plugin.checksum, r.plugin.support_level.value)
                           for r in resolved}),
    }
    doc["plugins"] = [{"id": i, "version": v, "checksum": c, "supportLevel": s} for i, v, c, s in doc["plugins"]]
    doc["digest"] = plan_digest(doc)
    return doc


def plan_digest(doc: dict[str, Any]) -> str:
    return digest_of({k: v for k, v in doc.items() if k != "digest"})


def summary_words(doc: dict[str, Any]) -> str:
    """One line for a decision's question: what infrastructure the deploy brings."""
    parts = []
    for r in doc.get("resources", []):
        verb = "remove" if r.get("removed") else ("attach" if r.get("mode") == "attached" else "provision")
        parts.append(f"{verb} {r['name']} ({r['pluginId']})")
    return "; ".join(parts)
