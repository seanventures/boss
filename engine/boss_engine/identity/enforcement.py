"""The enforcement point's framework-free core: modes, evaluation, the shadow ledger and the
coverage report. The FastAPI adapter is apps/api/app/identity_guard.py; the MCP adapter is the
hook in tools/registry.py. FROZEN IN PHASE 0 (IDENTITY_BUILD.md §2.4).

Modes (BOSS_AUTHZ_MODE, overridable per router with BOSS_AUTHZ_ROUTER_MODES="key=mode,..."):
    off      nothing is evaluated (rollback switch, M2)
    shadow   everything is evaluated; what WOULD be denied is recorded; nothing is refused (default)
    enforce  a deny is refused with its status and error body; an evaluation that cannot be made
             (a stream's piece missing, a resolver error) is refused 503 — fail closed
The identity API's own routers (router key `identity*`) are always enforced unless the mode is off.

Coverage (BOSS_AUTHZ_COVERAGE=warn|fail; default warn in shadow, fail in enforce): at boot every
route is walked; an undeclared one is listed, and with `fail` the API refuses to start (D5).
"""
from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any, Callable, Iterable, Optional

from .authz import Decision, DenyReason
from .declarations import Access, Authenticated, Permit, Public
from .principal import AuthContext
from .scope import RequestValues, ResolvedScope, ScopeNotFound, ScopeSpec, ScopeUnresolved

class AuthzMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    ENFORCE = "enforce"


class CoveragePolicy(StrEnum):
    WARN = "warn"
    FAIL = "fail"


ALWAYS_ENFORCED_PREFIX = "identity"


def parse_router_modes(raw: str) -> dict[str, AuthzMode]:
    """`"setup=enforce,github=shadow"` -> {setup: ENFORCE, github: SHADOW}. Unknown modes raise."""
    out: dict[str, AuthzMode] = {}
    for part in (raw or "").split(","):
        if not part.strip():
            continue
        key, sep, mode = part.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"BOSS_AUTHZ_ROUTER_MODES entry {part!r} is not key=mode")
        out[key.strip()] = AuthzMode(mode.strip().lower())
    return out


def mode_for(router_key: str, *, global_mode: str, router_modes: str = "") -> AuthzMode:
    base = AuthzMode((global_mode or "shadow").strip().lower())
    if base == AuthzMode.OFF:
        return AuthzMode.OFF
    if router_key.startswith(ALWAYS_ENFORCED_PREFIX):
        return AuthzMode.ENFORCE
    return parse_router_modes(router_modes).get(router_key, base)


def coverage_policy(*, global_mode: str, coverage: str = "") -> CoveragePolicy:
    if coverage:
        return CoveragePolicy(coverage.strip().lower())
    return CoveragePolicy.FAIL if (global_mode or "").strip().lower() == "enforce" else CoveragePolicy.WARN


# ---- evaluation ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Outcome:
    """What the enforcement point concluded for one request."""
    allowed: bool
    reason: Optional[DenyReason]
    permission: str = ""
    declaration: str = "undeclared"
    principal_kind: str = ""
    detail: str = ""             # e.g. "authn: not implemented (S1)"; never a secret
    decision: Optional[Decision] = None
    #: An authentication refusal that is not a plain 401 (S1-1): `csrf_failed` is 403 and a cookie
    #: plus a bearer on one request (`ambiguous_credentials`) is 400. The code is S1's stable string.
    authn_status: Optional[int] = None
    authn_code: str = ""

    @property
    def status(self) -> int:
        if self.allowed:
            return 200
        if self.authn_status is not None:
            return self.authn_status
        if self.decision is not None:
            return self.decision.status
        from .authz import DENY_STATUS
        return DENY_STATUS.get(self.reason or DenyReason.MISSING_PERMISSION, 403)

    def error_body(self) -> dict[str, Any]:
        if self.authn_status is not None and self.authn_status != 401 and self.authn_code:
            return {"error": self.authn_code}
        if self.decision is not None:
            return self.decision.error_body()
        if self.reason == DenyReason.UNAUTHENTICATED:
            return {"error": "unauthenticated"}
        if self.reason == DenyReason.NOT_FOUND:
            return {"error": "not_found"}
        if self.reason == DenyReason.NOT_EVALUATED:
            return {"error": "identity_unavailable"}
        body: dict[str, Any] = {"error": "forbidden"}
        if self.permission:
            body["permission"] = self.permission
        return body


def _not_evaluated(access: Access, detail: str, permission: str = "") -> Outcome:
    return Outcome(False, DenyReason.NOT_EVALUATED, permission, access.describe(), detail=detail)


def evaluate(
    access: Optional[Access],
    *,
    authenticate: Callable[[], Optional[AuthContext]],
    values: Callable[[], RequestValues],
    org_id: Callable[[AuthContext], str],
    check: Callable[[AuthContext, str, ResolvedScope], Decision],
    resolve: Callable[[ScopeSpec, RequestValues, str], ResolvedScope],
) -> Outcome:
    """Decide one request from its declaration. Never raises: a missing piece of a stream
    (NotImplementedError) or any error becomes NOT_EVALUATED, which shadow records and enforce
    refuses (fail closed)."""
    if access is None:
        return Outcome(False, DenyReason.UNDECLARED_ROUTE)
    if isinstance(access, Public):
        return Outcome(True, None, declaration=access.describe())
    try:
        ctx = authenticate()
    except NotImplementedError as exc:
        return _not_evaluated(access, f"authn: {exc}", getattr(access, "permission", ""))
    except Exception as exc:  # noqa: BLE001 — an AuthnError is a deny, anything else unexplained
        code = getattr(exc, "code", None)
        if code:
            status = getattr(exc, "status", 401)
            return Outcome(False, DenyReason.UNAUTHENTICATED, getattr(access, "permission", ""),
                           access.describe(), detail=f"authn: {code}",
                           authn_status=int(status) if isinstance(status, int) else 401,
                           authn_code=str(code))
        return _not_evaluated(access, f"authn error: {type(exc).__name__}")
    if ctx is None:
        return Outcome(False, DenyReason.UNAUTHENTICATED, getattr(access, "permission", ""),
                       access.describe())
    kind = ctx.principal.kind.value
    if not ctx.principal.active:
        return Outcome(False, DenyReason.PRINCIPAL_INACTIVE, getattr(access, "permission", ""),
                       access.describe(), kind)
    if isinstance(access, Authenticated):
        return Outcome(True, None, declaration=access.describe(), principal_kind=kind)
    assert isinstance(access, Permit)
    try:
        scope = resolve(access.scope, values(), org_id(ctx))
    except ScopeNotFound:
        return Outcome(False, DenyReason.NOT_FOUND, access.permission, access.describe(), kind)
    except ScopeUnresolved as exc:
        return Outcome(False, DenyReason.MISSING_PERMISSION, access.permission, access.describe(),
                       kind, detail=f"scope: {exc}")
    except NotImplementedError as exc:
        return _not_evaluated(access, f"scope: {exc}", access.permission)
    except Exception as exc:  # noqa: BLE001
        return _not_evaluated(access, f"scope error: {type(exc).__name__}", access.permission)
    try:
        decision = check(ctx, access.permission, scope)
    except NotImplementedError as exc:
        return _not_evaluated(access, f"authz: {exc}", access.permission)
    except Exception as exc:  # noqa: BLE001
        return _not_evaluated(access, f"authz error: {type(exc).__name__}", access.permission)
    return Outcome(decision.allowed, decision.reason, access.permission, access.describe(), kind,
                   decision=decision)


# ---- the shadow ledger -------------------------------------------------------------------------------

@dataclass(frozen=True)
class LedgerKey:
    day: date
    router_key: str
    method: str
    path_template: str
    permission: str
    reason: str
    principal_kind: str


class ShadowLedger:
    """In-process aggregate of every would-deny (and every enforced deny), keyed like
    identity_authz_shadow. Prints the FIRST occurrence of each key and counts the rest,
    so shadow mode is visible without flooding. Stream S5 adds the periodic flush to the table and
    the per-router report; this class is its source."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: Counter[LedgerKey] = Counter()

    def record(self, *, router_key: str, method: str, path_template: str, outcome: Outcome,
               mode: AuthzMode) -> None:
        key = LedgerKey(date.today(), router_key, method, path_template, outcome.permission,
                        (outcome.reason or DenyReason.MISSING_PERMISSION).value,
                        outcome.principal_kind)
        with self._lock:
            first = key not in self._counts
            self._counts[key] += 1
        if first:
            verb = "denied" if mode == AuthzMode.ENFORCE else "would deny"
            # print, like every other boot/ops line in the API: uvicorn does not route library
            # loggers to the container log, and a shadow report nobody can see is not a report.
            print(f"[identity] {mode.value} {verb} {method} {path_template}: {key.reason}"
                  + (f" {outcome.permission}" if outcome.permission else "")
                  + f" ({outcome.detail or outcome.declaration})", flush=True)

    def snapshot(self) -> dict[LedgerKey, int]:
        with self._lock:
            return dict(self._counts)

    def clear(self) -> None:
        with self._lock:
            self._counts.clear()


LEDGER = ShadowLedger()


# ---- coverage --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RouteEntry:
    methods: tuple[str, ...]
    path: str
    kind: str                     # http | websocket | framework | mount
    router_key: str
    access: Optional[Access]

    def labels(self) -> list[str]:
        """`GET /v1/x` per method; `WS /v1/x`; `MOUNT /x`. The baseline file's line format."""
        if self.kind == "websocket":
            return [f"WS {self.path}"]
        if self.kind == "mount":
            return [f"MOUNT {self.path}"]
        return [f"{m} {self.path}" for m in self.methods]


@dataclass
class CoverageReport:
    entries: list[RouteEntry] = field(default_factory=list)

    @property
    def declared(self) -> list[str]:
        return sorted(lbl for e in self.entries if e.access is not None for lbl in e.labels())

    @property
    def undeclared(self) -> list[str]:
        return sorted(lbl for e in self.entries if e.access is None for lbl in e.labels())

    def counts(self) -> dict[str, int]:
        c: Counter[str] = Counter()
        for e in self.entries:
            n = len(e.labels())
            if e.access is None:
                c["undeclared"] += n
            elif isinstance(e.access, Permit):
                c["permit"] += n
            elif isinstance(e.access, Public):
                c["public"] += n
            else:
                c["authenticated"] += n
        c["total"] = sum(len(e.labels()) for e in self.entries)
        return dict(c)

    def summary(self) -> str:
        c = self.counts()
        return (f"route coverage: {c.get('total', 0)} operations, "
                f"{c.get('total', 0) - c.get('undeclared', 0)} declared "
                f"(permit {c.get('permit', 0)}, public {c.get('public', 0)}, "
                f"authenticated {c.get('authenticated', 0)}), {c.get('undeclared', 0)} undeclared")


class UndeclaredRoutesError(RuntimeError):
    """Raised at boot under BOSS_AUTHZ_COVERAGE=fail. The message names every undeclared route."""


def check_coverage(entries: Iterable[RouteEntry], policy: CoveragePolicy, *,
                   echo: Callable[[str], None] = print) -> CoverageReport:
    report = CoverageReport(list(entries))
    undeclared = report.undeclared
    echo(f"[identity] {report.summary()} (coverage policy: {policy.value})")
    if undeclared and policy == CoveragePolicy.FAIL:
        raise UndeclaredRoutesError(
            f"{len(undeclared)} undeclared routes; every route needs @permit, @public or "
            f"@authenticated (IDENTITY.md §6.3): " + "; ".join(undeclared[:50])
            + (" …" if len(undeclared) > 50 else ""))
    if undeclared:
        echo(f"[identity] undeclared (first 10 of {len(undeclared)}): " + "; ".join(undeclared[:10]))
    return report


# ---- MCP tools ---------------------------------------------------------------------------------------

def shadow_tool_call(tool_name: str, permission: Optional[str], *, inherits_route: bool) -> None:
    """Phase 0 hook from ToolRegistry.invoke: record a call to a tool with no declaration. S2
    replaces it with a real `authz.check` at dispatch (IDENTITY.md §6.4)."""
    if permission or inherits_route:
        return
    LEDGER.record(router_key="mcp", method="TOOL", path_template=tool_name,
                  outcome=Outcome(False, DenyReason.UNDECLARED_ROUTE, declaration="undeclared"),
                  mode=AuthzMode.SHADOW)
