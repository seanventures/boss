"""Bindings by reference: the secret broker plugins talk to, and the redaction around it.

A plugin hands a connection value (`DATABASE_URL`) to `ctx.secrets.put(...)` and returns the
SecretRef it gets back. BOSS stores the value in the app secret vault (vault kind `app_secret`,
table `app_secret_bindings`, env `deploy`, mode `generated`), so the existing deploy paths pass it
to the app by NAME and write it into `.boss/app.env` / the stack's env file at deploy. A value
never reaches a prompt, a log, a PR, a decision or an API response.

    ref = app_secret://<env>/<app_key>/<NAME>

Scope: a broker serves ONE resource of ONE app. It accepts only the binding names the plugin's
manifest declares (renamed by the request's `bindings:` map), and resolves only refs of that app.
Every value it sees is remembered, so `Redactor` can strip them from anything the plugin emits.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional, Union

from .contract import ContractError, PluginManifest, ResourceRequest, SecretRef

REF_SCHEME = "app_secret"
_REF = re.compile(r"^app_secret://(?P<env>[a-z]+)/(?P<app>[^/]+)/(?P<name>[A-Z][A-Z0-9_]*)$")


#: What an app key may hold AS-IS inside a reference (contract.SECRET_REF, minus its separators
#: "/" and ":" — ":" starts an escape). Anything else is written ":<2 hex>" per UTF-8 byte.
_SAFE_APP_CHARS = re.compile(r"[A-Za-z0-9_.@-]")


def _encode_app(app_key: str) -> str:
    """An app key as it may stand in a reference. App keys are people's names ("helpdesk ai"),
    and the contract's reference pattern refuses a space: HelpDesk AI's first deploy failed
    "provision failed: ValidationError … 'app_secret://deploy/helpdesk ai/DATABASE_URL'". The
    escape is reversible (parse_ref), so the vault is still keyed by the app's own key."""
    return "".join(ch if _SAFE_APP_CHARS.fullmatch(ch)
                   else "".join(f":{b:02x}" for b in ch.encode("utf-8"))
                   for ch in str(app_key))


def _decode_app(encoded: str) -> str:
    return re.sub(rb":([0-9a-f]{2})", lambda m: bytes([int(m.group(1), 16)]),
                  encoded.encode("utf-8")).decode("utf-8")


def make_ref(env: str, app_key: str, name: str) -> SecretRef:
    return SecretRef(secretRef=f"{REF_SCHEME}://{env}/{_encode_app(app_key)}/{name}")


def parse_ref(ref: Union[SecretRef, str]) -> tuple[str, str, str]:
    raw = ref.secretRef if isinstance(ref, SecretRef) else str(ref)
    m = _REF.match(raw)
    if not m:
        raise ContractError([f"{raw!r} is not an app_secret:// reference"], "secret reference")
    return m.group("env"), _decode_app(m.group("app")), m.group("name")


class Redactor:
    """Strips every value a broker saw, then BOSS's own credential patterns."""

    def __init__(self) -> None:
        self.values: set[str] = set()

    def remember(self, value: Optional[str]) -> None:
        if value and len(value) >= 4:
            self.values.add(value)
            # A URL's password is a secret on its own: it can appear without the rest of the URL.
            m = re.match(r"^[a-z][a-z0-9+.-]*://[^:/@]*:([^@]+)@", value)
            if m and len(m.group(1)) >= 4:
                self.values.add(m.group(1))

    def __call__(self, text_value: str) -> str:
        from ..services.redaction import redact_secrets
        out = str(text_value)
        for v in sorted(self.values, key=len, reverse=True):
            out = out.replace(v, "[redacted]")
        return redact_secrets(out)

    def leaks(self, text_value: str) -> list[str]:
        """Which remembered values appear in a text (by position only, never the value)."""
        return [f"value #{i}" for i, v in enumerate(sorted(self.values)) if v in text_value]


def binding_name(manifest: PluginManifest, request: ResourceRequest, name: str) -> str:
    """The env name the app gets for a plugin binding: the request may rename it."""
    if name not in manifest.spec.bindings:
        raise ContractError([f"{manifest.id} does not declare a binding named {name}"], "binding")
    return request.bindings.get(name, name)


class MemoryBroker:
    """An in-memory broker (tests, the contract harness, a dry run). Same scope rules."""

    def __init__(self, *, app_key: str, env: str = "deploy", manifest: Optional[PluginManifest] = None,
                 request: Optional[ResourceRequest] = None, redactor: Optional[Redactor] = None) -> None:
        self.app_key, self.env, self.manifest, self.request = app_key, env, manifest, request
        self.redactor = redactor or Redactor()
        self.store: dict[str, str] = {}

    def _name(self, name: str) -> str:
        if self.manifest is not None and self.request is not None:
            return binding_name(self.manifest, self.request, name)
        return name

    def put(self, name: str, value: str) -> SecretRef:
        env_name = self._name(name)
        self.redactor.remember(value)
        ref = make_ref(self.env, self.app_key, env_name)
        self.store[ref.secretRef] = value
        return ref

    def resolve(self, ref: Union[SecretRef, str]) -> str:
        env, app, _name = parse_ref(ref)
        raw = ref.secretRef if isinstance(ref, SecretRef) else str(ref)
        if app != self.app_key or env != self.env or raw not in self.store:
            raise ContractError([f"{raw} is not a secret this resource may read"], "secret reference")
        value = self.store[raw]
        self.redactor.remember(value)
        return value

    def delete(self, ref: Union[SecretRef, str]) -> None:
        raw = ref.secretRef if isinstance(ref, SecretRef) else str(ref)
        self.store.pop(raw, None)


class ReadOnlyBroker(MemoryBroker):
    """For validate and plan: nothing may be stored."""

    def put(self, name: str, value: str) -> SecretRef:  # noqa: ARG002
        self.redactor.remember(value)
        raise ContractError([f"{name}: validate and plan may not store secrets"], "secret broker")


class VaultBroker:
    """The real broker: the app secret vault, env `deploy`, one app, one resource. `engine` is
    a SQLAlchemy engine: each call is its own short transaction, never one held open across a
    plugin's (slow) provisioning."""

    env = "deploy"

    def __init__(self, engine: Any, *, org_id: str, project_id: str, app_key: str,
                 manifest: PluginManifest, request: ResourceRequest, actor_id: Optional[str],
                 redactor: Optional[Redactor] = None, allowed: Iterable[str] = ()) -> None:
        self.engine, self.org_id, self.project_id, self.app_key = engine, org_id, project_id, app_key
        self.manifest, self.request, self.actor_id = manifest, request, actor_id
        self.redactor = redactor or Redactor()
        # Refs this resource owns already (from the ledger) may be resolved; so may new puts.
        self.allowed: set[str] = set(allowed)

    def put(self, name: str, value: str) -> SecretRef:
        from ..templates import secrets as app_secrets
        env_name = binding_name(self.manifest, self.request, name)
        self.redactor.remember(value)
        with self.engine.begin() as c:
            app_secrets.bind_issued(c, org_id=self.org_id, project_id=self.project_id,
                                    app_key=self.app_key, name=env_name, env=self.env, value=value,
                                    actor_id=self.actor_id)
        ref = make_ref(self.env, self.app_key, env_name)
        self.allowed.add(ref.secretRef)
        return ref

    def resolve(self, ref: Union[SecretRef, str]) -> str:
        from sqlalchemy import text

        from ..identity import vault
        env, app, name = parse_ref(ref)
        raw = ref.secretRef if isinstance(ref, SecretRef) else str(ref)
        if app != self.app_key or env != self.env or raw not in self.allowed:
            raise ContractError([f"{raw} is not a secret this resource may read"], "secret reference")
        with self.engine.begin() as c:
            row = c.execute(text(
                "SELECT vault_item_id FROM app_secret_bindings WHERE project_id = :p AND app_key = :a "
                "AND env = :e AND name = :n"), {"p": self.project_id, "a": app, "e": env, "n": name}).first()
            if not row or not row[0]:
                raise ContractError([f"{raw} has no value"], "secret reference")
            value = vault.read_app_secret(row[0], org_id=self.org_id, project_id=self.project_id,
                                          app_key=app, env=env, name=name, db=c)
        self.redactor.remember(value)
        return value

    def delete(self, ref: Union[SecretRef, str]) -> None:
        from ..templates import secrets as app_secrets
        from ..templates.service import TemplateError
        env, app, name = parse_ref(ref)
        if app != self.app_key or env != self.env:
            return
        try:
            with self.engine.begin() as c:
                app_secrets.delete(c, project_id=self.project_id, app_key=app, name=name, env=env)
        except TemplateError:
            pass


def bind_attached(connection: Any, *, org_id: str, project_id: str, app_key: str,
                  request: ResourceRequest, actor_id: Optional[str]) -> dict[str, SecretRef]:
    """An attached resource's connection values are ALREADY vault secrets of the app; the env
    name the app gets is bound to the same value. Refs come back; values never leave here."""
    from ..templates import secrets as app_secrets
    assert request.attach is not None
    from ..templates.base_url import deploy_secret_values

    values, _withheld = deploy_secret_values(connection, org_id=org_id, project_id=project_id,
                                             app_key=app_key)
    out: dict[str, SecretRef] = {}
    missing = [src for src in request.attach.bindings.values() if src not in values]
    if missing:
        raise ContractError([f"{request.name}: the app has no deploy secret named {m}" for m in missing],
                            "attached resource")
    for env_name, src in request.attach.bindings.items():
        if env_name != src:
            app_secrets.bind_issued(connection, org_id=org_id, project_id=project_id, app_key=app_key,
                                    name=env_name, env="deploy", value=values[src], actor_id=actor_id)
        out[env_name] = make_ref("deploy", app_key, env_name)
    return out
