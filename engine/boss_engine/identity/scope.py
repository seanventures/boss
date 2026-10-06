"""Where a permission is checked: scope declarations (resolvers) and resolved scopes.

FROZEN IN PHASE 0. IDENTITY.md §6.3 and §6.7.

A route declares HOW to find its scope (`Scope.org()`, `Scope.project(path="project_id")`,
`Scope.of("workflow_run", path="run_id")`); the enforcement point resolves that declaration against
the request into a `ResolvedScope`, which is what `authz.check` receives.

The rule that matters: WHEN A REQUEST NAMES AN OBJECT, THE SCOPE COMES FROM THE OBJECT, NEVER FROM
THE REQUEST. `Scope.of(kind, ...)` loads the object and takes its project; an object that does not
exist (or that the caller cannot read) resolves to `ScopeNotFound`, which the enforcement point turns
into a 404 — never a 403 that would confirm the object exists.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Callable, Mapping, Optional


class ScopeKind(StrEnum):
    ORG = "org"
    PROJECT = "project"


class ScopeSource(StrEnum):
    ORG = "org"            # no lookup: an org-level check
    PROJECT = "project"    # the project id is in the request (path, body or query)
    OBJECT = "object"      # the request names an object; its project is loaded


@dataclass(frozen=True)
class ScopeSpec:
    """A scope DECLARATION. Build with the `Scope` helpers, never directly."""
    source: ScopeSource
    object_kind: Optional[str] = None
    path: Optional[str] = None
    body: Optional[str] = None
    query: Optional[str] = None

    @property
    def location(self) -> tuple[str, str] | None:
        for where in ("path", "body", "query"):
            name = getattr(self, where)
            if name:
                return where, name
        return None

    def describe(self) -> str:
        if self.source == ScopeSource.ORG:
            return "org"
        where, name = self.location or ("?", "?")
        if self.source == ScopeSource.PROJECT:
            return f"project({where}={name})"
        return f"{self.object_kind}({where}={name})"


def _one_location(path: Optional[str], body: Optional[str], query: Optional[str]) -> None:
    if sum(1 for v in (path, body, query) if v) != 1:
        raise ValueError("a scope resolver names exactly one of path=, body= or query=")


class Scope:
    """Declaration helpers: `Scope.org()`, `Scope.project(...)`, `Scope.of(kind, ...)`."""

    @staticmethod
    def org() -> ScopeSpec:
        return ScopeSpec(ScopeSource.ORG)

    @staticmethod
    def project(*, path: Optional[str] = None, body: Optional[str] = None,
                query: Optional[str] = None) -> ScopeSpec:
        _one_location(path, body, query)
        return ScopeSpec(ScopeSource.PROJECT, path=path, body=body, query=query)

    @staticmethod
    def of(kind: str, *, path: Optional[str] = None, body: Optional[str] = None,
           query: Optional[str] = None) -> ScopeSpec:
        if not kind or not kind.replace("_", "").isalnum():
            raise ValueError(f"object kind must be a snake_case name, got {kind!r}")
        _one_location(path, body, query)
        return ScopeSpec(ScopeSource.OBJECT, object_kind=kind, path=path, body=body, query=query)


@dataclass(frozen=True)
class ResolvedScope:
    """The scope a check runs at. `project_id` is None for an org-level check."""
    kind: ScopeKind
    org_id: str
    project_id: Optional[str] = None
    object_kind: Optional[str] = None
    object_id: Optional[str] = None

    def label(self) -> str:
        """`org` or `project:<id>`: the binding-scope notation used by `explain()` and audit rows."""
        return "org" if self.kind == ScopeKind.ORG else f"project:{self.project_id}"

    @classmethod
    def org_level(cls, org_id: str) -> "ResolvedScope":
        return cls(ScopeKind.ORG, org_id)

    @classmethod
    def in_project(cls, org_id: str, project_id: str, *, object_kind: Optional[str] = None,
                   object_id: Optional[str] = None) -> "ResolvedScope":
        return cls(ScopeKind.PROJECT, org_id, project_id, object_kind, object_id)


class ScopeNotFound(LookupError):
    """The object does not exist, or is invisible to the caller: answered as 404."""


class ScopeUnresolved(ValueError):
    """The request lacks what the declaration names (e.g. no `project_id` in the body)."""


@dataclass(frozen=True)
class RequestValues:
    """The parts of a request a resolver may read. Built by the enforcement point."""
    path_params: Mapping[str, Any]
    query: Mapping[str, Any]
    body: Any = None   # parsed JSON body, when the declaration needs it


#: object kind -> loader(db, object_id) -> (org_id, project_id) or None when it does not exist.
ObjectLoader = Callable[[Any, str], Optional[tuple[str, str]]]
_OBJECT_LOADERS: dict[str, ObjectLoader] = {}


def register_object_kind(kind: str, loader: ObjectLoader) -> None:
    """Register how to find an object's project. Stream S2 registers one per object kind used in
    a `Scope.of(...)` declaration; the coverage test fails on a declared kind with no loader."""
    if kind in _OBJECT_LOADERS and _OBJECT_LOADERS[kind] is not loader:
        raise ValueError(f"object kind {kind!r} already has a loader")
    _OBJECT_LOADERS[kind] = loader


def registered_object_kinds() -> frozenset[str]:
    return frozenset(_OBJECT_LOADERS)


def load_object(kind: str, db: Any, object_id: str) -> Optional[tuple[str, str]]:
    """(org_id, project_id) of one object by its registered kind, or None when it does not exist.
    Raises NotImplementedError for a kind with no loader (fail closed: the guard answers 503)."""
    loader = _OBJECT_LOADERS.get(kind)
    if loader is None:
        raise NotImplementedError(f"no object loader registered for {kind!r}")
    return loader(db, str(object_id))


def _read(values: RequestValues, where: str, name: str) -> Optional[str]:
    if where == "path":
        raw = values.path_params.get(name)
    elif where == "query":
        raw = values.query.get(name)
    else:
        raw = values.body.get(name) if isinstance(values.body, Mapping) else None
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    return str(raw)


def resolve(spec: ScopeSpec, values: RequestValues, *, org_id: str, db: Any = None) -> ResolvedScope:
    """Resolve a declaration against a request.

    Raises ScopeUnresolved when the request lacks the named value, ScopeNotFound when the object
    does not exist or belongs to another org, and NotImplementedError for an object kind with no
    registered loader (Phase 0: most kinds; stream S2 registers them).
    """
    if spec.source == ScopeSource.ORG:
        return ResolvedScope.org_level(org_id)
    where, name = spec.location or ("", "")
    value = _read(values, where, name)
    if value is None:
        raise ScopeUnresolved(f"{where} value {name!r} is missing")
    if spec.source == ScopeSource.PROJECT:
        return ResolvedScope.in_project(org_id, value)
    loader = _OBJECT_LOADERS.get(spec.object_kind or "")
    if loader is None:
        raise NotImplementedError(f"no object loader registered for {spec.object_kind!r} (S2)")
    found = loader(db, value)
    if found is None or found[0] != org_id:
        raise ScopeNotFound(f"{spec.object_kind} {value!r}")
    return ResolvedScope.in_project(org_id, found[1], object_kind=spec.object_kind, object_id=value)
