"""A worker lease route is exempt from the hold, and an exemption is only ever a worker lease route.

The API holds a non-human write whose path ends in a high-risk word (`merge`, `execute`, ...) for
a person's confirmation. The worker is not a person, so each of its routes must be registered as a
worker protocol (boss_engine/mcp/api_surface.py) or the worker is answered 409 — which is how
approved multi-repo change sets silently never merged (POST /v1/change-sets/{cs_id}/merge).

The exemption is safe only because the route is fenced to the worker: declared `worker:lease` and
calling require_worker_principal(). An exemption on an ordinary route would let any non-human key
holding its permission skip the hold. Both directions are checked here, over every mounted route.
"""
from __future__ import annotations

import inspect

from apps.api.app import identity_guard
from apps.api.app.main import app
from boss_engine.identity.declarations import Permit
from boss_engine.mcp import api_surface
from boss_engine.mcp.api_surface import exclusion_reason, needs_operator_confirmation

CHECK_NAMES = ("require_worker_principal(", "require_worker()")


def _routes():
    """(method, path, access, endpoint source) for every mounted write route."""
    endpoints = {}
    for route in app.routes:
        for method in getattr(route, "methods", ()) or ():
            if getattr(route, "endpoint", None) is not None:
                endpoints[(method, route.path)] = route.endpoint
    out = []
    for entry in identity_guard.route_inventory(app):
        for label in entry.labels():
            method, _, path = label.partition(" ")
            if method not in ("POST", "PUT", "PATCH", "DELETE") or (method, path) not in endpoints:
                continue
            try:
                source = inspect.getsource(endpoints[(method, path)])
            except (OSError, TypeError):
                source = ""
            out.append((method, path, entry.access, source))
    return out


ROUTES = _routes()


def _is_worker_route(access, source: str) -> bool:
    return (isinstance(access, Permit) and access.permission == "worker:lease") or any(
        name in source for name in CHECK_NAMES)


def test_routes_were_read():
    assert sum(1 for m, p, a, s in ROUTES if _is_worker_route(a, s)) > 20


def test_every_worker_lease_route_is_exempt_from_the_hold():
    held = [f"{m} {p}" for m, p, a, s in ROUTES
            if _is_worker_route(a, s) and needs_operator_confirmation(m, p) and not exclusion_reason(m, p)]
    assert not held, (f"the worker would be answered 409 on {held}: register each in "
                      "api_surface._EXCLUDED_EXACT with a *_worker_lease_protocol reason")


def test_every_worker_lease_exemption_is_fenced_to_the_worker():
    lease_paths = {path for path, reason in api_surface._EXCLUDED_EXACT.items()  # noqa: SLF001
                   if reason.endswith("worker_lease_protocol")}
    mounted = {p for _m, p, _a, _s in ROUTES}
    assert lease_paths <= mounted, f"exemptions for routes that are not mounted: {sorted(lease_paths - mounted)}"
    open_to_others = [f"{m} {p}" for m, p, a, s in ROUTES if p in lease_paths
                      and not (isinstance(a, Permit) and a.permission == "worker:lease"
                               and any(name in s for name in CHECK_NAMES))]
    assert not open_to_others, (
        f"{open_to_others} skip the hold as a worker lease protocol but are not fenced to the worker "
        "(@permit(P.WORKER_LEASE) + require_worker_principal()): any non-human key could call them")
