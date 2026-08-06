"""Where secret VALUES actually live, and the one place a reference becomes one.

BOSS's stance, already encoded in `secret_refs` (`raw_secret_stored: false`), is that it holds
REFERENCES and never the material. That is right and this module keeps it: a backend is an
external place that holds values, and BOSS asks for one only at the moment it is used.

The gap this closes: `deployment_targets.secret_refs` was populated on every target and read by
nothing. `deploy_service._ssh_key_file()` consulted a single global setting, so every SSH
target necessarily shared one credential and onboarding a second host meant editing `.env` and
restarting the API. A binding recorded per target and ignored per deploy is the same defect
this codebase keeps paying for — declared, stored, wired to nothing.

Rules that hold for every backend:

  * `get()` is called at the moment of use and the value is never persisted by BOSS — not into
    `deployment_runs`, not into an event, not into a plan, not into a log line.
  * `list_keys()` returns KEYS. There is no backend method that lists values, because an
    interface that can enumerate secrets will eventually enumerate them to the wrong reader.
  * A missing or unreachable secret is a *validation problem*, never an exception thrown from
    the middle of a deployment. Finding out mid-transfer is strictly worse than up front.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional, Protocol

from ..config import Settings
from ..ports import sql


class SecretUnavailable(RuntimeError):
    """A reference exists but the value cannot be obtained. Carries no secret material."""


class SecretBackend(Protocol):
    kind: str

    def get(self, key: str) -> str: ...
    def put(self, key: str, value: str) -> None: ...
    def list_keys(self) -> list[str]: ...
    def delete(self, key: str) -> None: ...
    def health(self) -> dict[str, Any]: ...


class EnvBackend:
    """Process environment. No rotation, no audit — acceptable for local development only."""

    kind = "environment"

    def get(self, key: str) -> str:
        value = os.environ.get(key)
        if not value:
            raise SecretUnavailable(f"environment variable {key} is not set")
        return value

    def put(self, key: str, value: str) -> None:
        raise SecretUnavailable(
            "the environment backend is read-only: a value written into this process would "
            "vanish on restart and mislead everyone who saw it succeed")

    def list_keys(self) -> list[str]:
        # Deliberately not every env var — that would leak the shape of the whole process.
        return sorted(k for k in os.environ if k.startswith("BOSS_"))

    def delete(self, key: str) -> None:
        raise SecretUnavailable("the environment backend is read-only")

    def health(self) -> dict[str, Any]:
        return {"ok": True, "kind": self.kind, "writable": False}


class FileBackend:
    """A 0600 file on the BOSS host. `key` is the path.

    Refuses a group- or world-readable file for the same reason ssh does: a private key anyone
    on the box can read is not a private key.
    """

    kind = "file"

    def get(self, key: str) -> str:
        path = os.path.expanduser(key)
        if not os.path.isfile(path):
            raise SecretUnavailable(f"{key} does not exist")
        if os.stat(path).st_mode & 0o077:
            raise SecretUnavailable(f"{key} is group/world readable; refusing to use it")
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def put(self, key: str, value: str) -> None:
        path = os.path.expanduser(key)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(value if value.endswith("\n") else value + "\n")

    def list_keys(self) -> list[str]:
        return []  # a filesystem is not enumerable as a secret store

    def delete(self, key: str) -> None:
        try:
            os.unlink(os.path.expanduser(key))
        except FileNotFoundError:
            pass

    def health(self) -> dict[str, Any]:
        return {"ok": True, "kind": self.kind, "writable": True}


class PlatformSecretStoreBackend:
    """The operator's platform secret store, reached over HTTP.

    Modelled on the goldeneye ops API, which already exposes exactly the right shape:
    `GET /secrets` returns KEYS with values withheld, `GET /secret?key=`, `POST /secret`,
    `DELETE /secret?key=` — and it is backed up and mirrored off-site. BOSS uses it rather than
    growing its own vault, because a second store would be a second place for a credential to
    be stale, leaked or forgotten.
    """

    kind = "platform_secret_store"

    def __init__(self, base_url: str, token: str, timeout: int = 15):
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _call(self, method: str, path: str, payload: Optional[dict] = None) -> Any:
        req = urllib.request.Request(
            f"{self._base}{path}", method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"X-Ops-Token": self._token,
                     **({"Content-Type": "application/json"} if payload is not None else {})})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                body = resp.read().decode() or "{}"
        except urllib.error.HTTPError as exc:
            # The status is safe to surface; the body may echo the request, so it is not.
            raise SecretUnavailable(
                f"platform secret store returned {exc.code} for {method} {path}") from None
        except Exception as exc:  # noqa: BLE001
            raise SecretUnavailable(
                f"platform secret store unreachable: {type(exc).__name__}") from None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {}

    def get(self, key: str) -> str:
        data = self._call("GET", f"/secret?key={urllib.parse.quote(key)}")
        value = data.get("value") if isinstance(data, dict) else None
        if not value:
            raise SecretUnavailable(f"{key} is not present in the platform secret store")
        return value

    def put(self, key: str, value: str) -> None:
        self._call("POST", "/secret", {"key": key, "value": value})

    def list_keys(self) -> list[str]:
        data = self._call("GET", "/secrets")
        if isinstance(data, dict):
            keys = data.get("keys") or data.get("secrets") or []
            return sorted(k if isinstance(k, str) else k.get("key", "") for k in keys)
        return []

    def delete(self, key: str) -> None:
        self._call("DELETE", f"/secret?key={urllib.parse.quote(key)}")

    def health(self) -> dict[str, Any]:
        try:
            self.list_keys()
            return {"ok": True, "kind": self.kind, "writable": True}
        except SecretUnavailable as exc:
            return {"ok": False, "kind": self.kind, "error": str(exc)}



def _platform_token(settings: Settings) -> str:
    """Read the store token from settings or a file. Never from a data channel.

    A token that arrives in a ticket, a chat message or a file BOSS was told to read is not a
    credential, it is an instruction from an unknown author — that guard is deliberate.
    """
    token = (getattr(settings, "platform_secret_store_token", "") or "").strip()
    if token:
        return token
    path = (getattr(settings, "platform_secret_store_token_file", "") or "").strip()
    if path:
        expanded = os.path.expanduser(path)
        if os.path.isfile(expanded):
            with open(expanded, "r", encoding="utf-8") as handle:
                return handle.read().strip()
    return ""


def backend_for(kind: str, settings: Settings) -> SecretBackend:
    """Map a stored `ref_kind`/`storage_backend` onto an implementation."""
    normalised = (kind or "").strip().lower()
    if normalised in ("env", "environment"):
        return EnvBackend()
    if normalised in ("file", "local_runner"):
        return FileBackend()
    if normalised == "platform_secret_store":
        base = (getattr(settings, "platform_secret_store_url", "") or "").strip()
        token = _platform_token(settings)
        if not base or not token:
            raise SecretUnavailable(
                "platform secret store is not configured "
                "(BOSS_PLATFORM_SECRET_STORE_URL + _TOKEN or _TOKEN_FILE)")
        return PlatformSecretStoreBackend(base, token)
    raise SecretUnavailable(f"no secret backend for {kind!r}")


def resolve_secret_ref(connection, secret_ref_id: str, settings: Settings) -> str:
    """THE place a stored reference becomes a value.

    One function so that "which backend, which key, is it usable" is answered once. A second
    resolver would drift from this one exactly as the preview resolver and the failure taxonomy
    did, and here the drift would be silent until a deployment used the wrong credential.
    """
    row = connection.execute(
        sql("SELECT ref_kind, ref_key, storage_backend, status "
            "FROM secret_refs WHERE id = :i"), {"i": secret_ref_id}).mappings().first()
    if not row:
        raise SecretUnavailable(f"secret ref {secret_ref_id} does not exist")
    if row["status"] not in ("active", "rotating", "rotation_due"):
        raise SecretUnavailable(
            f"secret ref {secret_ref_id} is {row['status']} and must not be used")
    backend = backend_for(row["storage_backend"] or row["ref_kind"], settings)
    return backend.get(row["ref_key"])


def resolve_target_secret(connection, target: dict, name: str,
                          settings: Settings) -> Optional[str]:
    """The material bound to `name` on this target, or None when nothing is bound.

    Accepts both binding shapes so existing targets keep working while the console migrates:

        {"name": "ssh_key", "secret_ref_id": "sref_..."}   preferred — a managed reference
        {"name": "ssh_key", "ref": "BOSS_SSH_..._FILE"}    legacy — an env var naming a path

    Returning None (rather than raising) is how a target with no binding falls back to the
    global setting, which is what keeps today's live deployments working while the model moves.
    """
    for binding in target.get("secret_refs") or []:
        if not isinstance(binding, dict) or binding.get("name") != name:
            continue
        ref_id = binding.get("secret_ref_id")
        if ref_id:
            return resolve_secret_ref(connection, ref_id, settings)
        legacy = binding.get("ref")
        if legacy:
            # A legacy ref names an ENV VAR whose value is a path (that is what the existing
            # targets mean by BOSS_SSH_HOST_PRIVATE_KEY_FILE). Resolve the indirection, but do
            # not invent a meaning when the variable is absent — fall through to the global.
            path = os.environ.get(legacy)
            if path:
                return FileBackend().get(path)
            return None
    return None


def fingerprint_ssh_public_key(material: str) -> Optional[str]:
    """SHA256 fingerprint of a private key's public half, for display and comparison.

    Lets an operator answer "is the key BOSS holds the one on the host?" by comparing against
    `ssh-keygen -lf`, without BOSS ever showing the secret. Returns None rather than raising:
    a fingerprint is a convenience, and failing to compute one must never fail a deployment.
    """
    import subprocess
    import tempfile

    try:
        fd, path = tempfile.mkstemp()
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(material if material.endswith("\n") else material + "\n")
            os.chmod(path, 0o600)
            out = subprocess.run(["ssh-keygen", "-lf", path], capture_output=True,
                                 text=True, timeout=15)
            if out.returncode != 0:
                return None
            parts = (out.stdout or "").split()
            return next((p for p in parts if p.startswith("SHA256:")), None)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        return None
