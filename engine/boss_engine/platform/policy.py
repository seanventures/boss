"""Infrastructure policy (spec §4.6, §4.7). Pure: a plan in, findings out.

    refuse    the plan may not run at all (a plugin opening a public port nobody asked for,
              two resources binding the same env name, destroying an attached resource)
    approval  a person must approve this before it runs: `public: true`, any destructive change
              (destroy, replace, a destructive migration), a production environment

The deploy approval IS that approval: its preview lists every `approval` finding, and the
decision's content hash pins the plan that carries them. Outside a deploy (Infra › resources),
a destroy opens its own decision.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable, Literal

from .contract import ResourceMode, ResourcePlan, ResourceRequest

#: Capabilities whose ports are never public unless the request says `public: true` (and a person
#: approves it).
DATA_CAPABILITIES = ("database", "cache", "object-storage", "vector", "search", "queue")
PRODUCTION_ENVIRONMENTS = frozenset({"production", "prod"})


@dataclass(frozen=True)
class Finding:
    code: str
    resource: str
    kind: Literal["refuse", "approval"]
    message: str

    def as_dict(self) -> dict:
        return asdict(self)


def is_data_capability(capability: str) -> bool:
    return capability.split(".", 1)[0] in DATA_CAPABILITIES


def evaluate(pairs: Iterable[tuple[ResourceRequest | None, ResourcePlan]], *, environment: str) -> list[Finding]:
    """`pairs` is (request, plan) per resource; request is None for a resource being removed."""
    out: list[Finding] = []
    bound: dict[str, str] = {}
    for request, plan in pairs:
        name = request.name if request else plan.resourceId.rsplit("/", 1)[-1]
        public_ports = [p for p in plan.ports if p.public]
        asked_public = bool(request and request.public)
        if (public_ports or plan.publicAccess) and not asked_public:
            out.append(Finding("public_port_not_requested", name, "refuse",
                               f"{name}: the plugin planned a public port the request did not ask for; "
                               "set public: true to ask for it"))
        if asked_public:
            what = "a data store" if request and is_data_capability(request.capability) else "a resource"
            out.append(Finding("public_access", name, "approval",
                               f"{name}: public: true exposes {what} beyond the host; a person must approve it"))
        for change in plan.changes:
            if change.action == "delete" and plan.mode == ResourceMode.ATTACHED:
                out.append(Finding("attached_destroy", name, "refuse",
                                   f"{name} is attached: BOSS never destroys a resource it did not create"))
            elif change.destructive or change.action in ("delete", "replace"):
                out.append(Finding("destructive", name, "approval",
                                   f"{name}: {change.action} {change.target} is destructive"
                                   + (f" ({change.detail})" if change.detail else "")))
        for env_name in plan.bindings:
            if env_name in bound and bound[env_name] != name:
                out.append(Finding("binding_clash", name, "refuse",
                                   f"{env_name} is bound by both {bound[env_name]} and {name}; "
                                   "rename one with bindings:"))
            bound.setdefault(env_name, name)
    if environment in PRODUCTION_ENVIRONMENTS:
        out.append(Finding("production", "*", "approval",
                           f"{environment} infrastructure changes always need a person's approval"))
    return out


def destroy_refusal(mode: str) -> str | None:
    """The one rule every destroy path checks, whatever started it."""
    if mode == ResourceMode.ATTACHED.value or mode == ResourceMode.ATTACHED:
        return "attached resources are never destroyed: BOSS did not create them"
    return None
