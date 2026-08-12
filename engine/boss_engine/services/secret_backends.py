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


# Where the file backend puts a secret that was given a NAME rather than a path.
# 0700, outside any repo, and never the working directory.
SECRETS_DIR = os.path.expanduser(
    os.environ.get("BOSS_SECRETS_DIR", "~/.config/boss/secrets"))


class FileBackend:
    """A 0600 file on the BOSS host.

    `key` may be an absolute/`~` path (an operator-managed key that BOSS only references) or a
    bare NAME, which resolves under SECRETS_DIR.

    A bare name used to be passed to `open()` unchanged, so a generated key landed in the
    process's working directory — which, for the API, is the git repository. A private key one
    `git add -A` away from being committed is a real incident, and the fix is that a name can
    never resolve relative to wherever the process happens to be running.

    Refuses a group- or world-readable file for the same reason ssh does: a private key anyone
    on the box can read is not a private key.
    """

    kind = "file"

    @staticmethod
    def _resolve(key: str) -> str:
        expanded = os.path.expanduser(key or "")
        if os.path.isabs(expanded):
            return expanded
        return os.path.join(SECRETS_DIR, os.path.basename(expanded))

    def get(self, key: str) -> str:
        path = self._resolve(key)
        if not os.path.isfile(path):
            raise SecretUnavailable(f"{key} does not exist")
        if os.stat(path).st_mode & 0o077:
            raise SecretUnavailable(f"{key} is group/world readable; refusing to use it")
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def put(self, key: str, value: str) -> None:
        path = self._resolve(key)
        os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(value if value.endswith("\n") else value + "\n")

    def list_keys(self) -> list[str]:
        return []  # a filesystem is not enumerable as a secret store

    def delete(self, key: str) -> None:
        try:
            os.unlink(self._resolve(key))
        except FileNotFoundError:
            pass

    def health(self) -> dict[str, Any]:
        return {"ok": True, "kind": self.kind, "writable": True}


class PlatformSecretStoreBackend:
    """The operator's platform secret store, reached over HTTP.

    Modelled on a platform ops API, which exposes exactly the right shape:
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


class LocalVaultBackend:
    """AES-256-GCM encrypted files under BOSS_SECRETS_DIR/vault/ (OSS cryptography).

    Master key: ``BOSS_KMS_MASTER_KEY`` (urlsafe base64 32 bytes) or
    ``BOSS_KMS_MASTER_KEY_FILE``. When neither is set and BOSS is in test mode
    (``BOSS_KMS_ALLOW_EPHEMERAL_MASTER=true``), a process-local key is used — never
    for production.
    """

    kind = "local_vault"

    def __init__(self, root: Optional[str] = None, master_key: Optional[bytes] = None):
        self._root = os.path.expanduser(
            root
            or os.environ.get("BOSS_KMS_VAULT_DIR")
            or os.path.join(SECRETS_DIR, "vault")
        )
        self._key = master_key or _load_kms_master_key()

    def _path(self, key: str) -> str:
        safe = "".join(c if c.isalnum() or c in "._-@" else "_" for c in key)[:200]
        return os.path.join(self._root, f"{safe}.enc")

    def get(self, key: str) -> str:
        path = self._path(key)
        if not os.path.isfile(path):
            raise SecretUnavailable(f"{key} is not present in local_vault")
        if os.stat(path).st_mode & 0o077:
            raise SecretUnavailable(f"{key} vault file is group/world readable")
        with open(path, "rb") as handle:
            blob = handle.read()
        try:
            return _aes_gcm_decrypt(self._key, blob).decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            raise SecretUnavailable(f"local_vault decrypt failed for {key}") from None

    def put(self, key: str, value: str) -> None:
        os.makedirs(self._root, mode=0o700, exist_ok=True)
        path = self._path(key)
        blob = _aes_gcm_encrypt(self._key, (value or "").encode("utf-8"))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob)

    def list_keys(self) -> list[str]:
        if not os.path.isdir(self._root):
            return []
        out = []
        for name in os.listdir(self._root):
            if name.endswith(".enc"):
                out.append(name[: -len(".enc")])
        return sorted(out)

    def delete(self, key: str) -> None:
        try:
            os.unlink(self._path(key))
        except FileNotFoundError:
            pass

    def health(self) -> dict[str, Any]:
        try:
            os.makedirs(self._root, mode=0o700, exist_ok=True)
            return {
                "ok": True,
                "kind": self.kind,
                "writable": True,
                "path": self._root,
            }
        except OSError as exc:
            return {"ok": False, "kind": self.kind, "error": str(exc)}


class VaultKvBackend:
    """HashiCorp Vault / OpenBao KV v2 HTTP client (open source).

    Env: ``BOSS_VAULT_ADDR`` (or OPENBAO_ADDR), ``BOSS_VAULT_TOKEN``,
    optional ``BOSS_VAULT_KV_MOUNT`` (default ``secret``), ``BOSS_VAULT_NAMESPACE``.
    """

    kind = "vault"

    def __init__(
        self,
        addr: str,
        token: str,
        *,
        mount: str = "secret",
        namespace: str = "",
        timeout: int = 15,
    ):
        self._addr = addr.rstrip("/")
        self._token = token
        self._mount = (mount or "secret").strip("/")
        self._namespace = namespace or ""
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        h = {"X-Vault-Token": self._token, "Content-Type": "application/json"}
        if self._namespace:
            h["X-Vault-Namespace"] = self._namespace
        return h

    def _call(self, method: str, path: str, payload: Optional[dict] = None) -> Any:
        req = urllib.request.Request(
            f"{self._addr}{path}",
            method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                body = resp.read().decode() or "{}"
        except urllib.error.HTTPError as exc:
            raise SecretUnavailable(
                f"vault returned {exc.code} for {method} {path}"
            ) from None
        except Exception as exc:  # noqa: BLE001
            raise SecretUnavailable(
                f"vault unreachable: {type(exc).__name__}"
            ) from None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {}

    def get(self, key: str) -> str:
        # KV v2: GET /v1/{mount}/data/{path}
        data = self._call("GET", f"/v1/{self._mount}/data/{urllib.parse.quote(key)}")
        payload = (data.get("data") or {}).get("data") or {}
        value = payload.get("value") or payload.get("data")
        if not value:
            raise SecretUnavailable(f"{key} is not present in vault")
        return str(value)

    def put(self, key: str, value: str) -> None:
        self._call(
            "POST",
            f"/v1/{self._mount}/data/{urllib.parse.quote(key)}",
            {"data": {"value": value}},
        )

    def list_keys(self) -> list[str]:
        # LIST via ?list=true
        try:
            data = self._call(
                "GET", f"/v1/{self._mount}/metadata?list=true"
            )
        except SecretUnavailable:
            return []
        keys = ((data.get("data") or {}).get("keys")) or []
        return sorted(str(k).rstrip("/") for k in keys)

    def delete(self, key: str) -> None:
        self._call("DELETE", f"/v1/{self._mount}/metadata/{urllib.parse.quote(key)}")

    def health(self) -> dict[str, Any]:
        try:
            self._call("GET", "/v1/sys/health")
            return {"ok": True, "kind": self.kind, "writable": True, "addr": self._addr}
        except SecretUnavailable as exc:
            return {"ok": False, "kind": self.kind, "error": str(exc)}


_EPHEMERAL_MASTER: Optional[bytes] = None


def _load_kms_master_key() -> bytes:
    global _EPHEMERAL_MASTER
    import base64

    raw = (os.environ.get("BOSS_KMS_MASTER_KEY") or "").strip()
    if not raw:
        path = (os.environ.get("BOSS_KMS_MASTER_KEY_FILE") or "").strip()
        if path and os.path.isfile(os.path.expanduser(path)):
            with open(os.path.expanduser(path), "r", encoding="utf-8") as handle:
                raw = handle.read().strip()
    if raw:
        try:
            key = base64.urlsafe_b64decode(raw.encode("ascii"))
        except Exception:
            key = raw.encode("utf-8")
        if len(key) < 32:
            key = key.ljust(32, b"\0")
        return key[:32]
    if (os.environ.get("BOSS_KMS_ALLOW_EPHEMERAL_MASTER") or "").lower() in (
        "1",
        "true",
        "yes",
    ):
        if _EPHEMERAL_MASTER is None:
            _EPHEMERAL_MASTER = os.urandom(32)
        return _EPHEMERAL_MASTER
    raise SecretUnavailable(
        "local_vault requires BOSS_KMS_MASTER_KEY or BOSS_KMS_MASTER_KEY_FILE "
        "(or BOSS_KMS_ALLOW_EPHEMERAL_MASTER=true for tests only)"
    )


def _aes_gcm_encrypt(key: bytes, plaintext: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext, None)
    return nonce + ct


def _aes_gcm_decrypt(key: bytes, blob: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(blob) < 13:
        raise ValueError("ciphertext too short")
    nonce, ct = blob[:12], blob[12:]
    return AESGCM(key).decrypt(nonce, ct, None)


def backend_for(kind: str, settings: Settings) -> SecretBackend:
    """Map a stored `ref_kind`/`storage_backend` onto an implementation."""
    normalised = (kind or "").strip().lower()
    if normalised in ("env", "environment"):
        return EnvBackend()
    if normalised in ("file", "local_runner"):
        return FileBackend()
    if normalised in ("local_vault", "kms_local", "boss_vault"):
        return LocalVaultBackend()
    if normalised in ("vault", "hashicorp_vault", "openbao"):
        addr = (
            getattr(settings, "vault_addr", None)
            or os.environ.get("BOSS_VAULT_ADDR")
            or os.environ.get("VAULT_ADDR")
            or os.environ.get("OPENBAO_ADDR")
            or ""
        ).strip()
        token = (
            getattr(settings, "vault_token", None)
            or os.environ.get("BOSS_VAULT_TOKEN")
            or os.environ.get("VAULT_TOKEN")
            or ""
        ).strip()
        mount = (
            getattr(settings, "vault_kv_mount", None)
            or os.environ.get("BOSS_VAULT_KV_MOUNT")
            or "secret"
        )
        namespace = (
            getattr(settings, "vault_namespace", None)
            or os.environ.get("BOSS_VAULT_NAMESPACE")
            or ""
        )
        if not addr or not token:
            raise SecretUnavailable(
                "vault backend requires BOSS_VAULT_ADDR and BOSS_VAULT_TOKEN"
            )
        return VaultKvBackend(addr, token, mount=str(mount), namespace=str(namespace))
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

    Accepts the binding shapes that actually exist in the wild:

        {"name": "ssh_key", "secret_ref_id": "sref_..."}   preferred — a managed reference
        {"name": "ssh_key", "ref": "BOSS_SSH_..._FILE"}    legacy — an env var naming a path
        "sref_..." / "env:BOSS_SSH_..._FILE"               API create_deployment_target shape
                                                          (list[str] via _normalize_string_list)

    The API historically stored secret_refs as bare strings. Phase 1's resolver only understood
    dicts, so a target onboarded through POST /v1/deployments with ``["sref_…"]`` silently fell
    through to the global SSH key — wrong-secret for every host that thought it was bound.
    Returning None (rather than raising) is reserved for "no binding at all", which is what
    keeps un-migrated targets falling back to the global setting.
    """
    for binding in target.get("secret_refs") or []:
        if isinstance(binding, str):
            token = binding.strip()
            if not token:
                continue
            # Bare strings have no `name` field. For the SSH connector (one required secret)
            # the list is the binding for that secret; ignore only when the caller asked for
            # something else so we do not invent a second meaning later.
            if name != "ssh_key":
                continue
            if token.startswith("env:"):
                path = os.environ.get(token[4:])
                if path:
                    return FileBackend().get(path)
                raise SecretUnavailable(
                    f"legacy env binding {token!r} is set on the target but the variable "
                    f"is unset — refusing to fall back to a different credential")
            if token.startswith("sref_") or token.startswith("secret_"):
                return resolve_secret_ref(connection, token, settings)
            # Otherwise treat as an env var naming a key file (historical BOSS_SSH_*_FILE).
            path = os.environ.get(token)
            if path:
                return FileBackend().get(path)
            raise SecretUnavailable(
                f"string binding {token!r} on the target is neither a secret_ref id nor a "
                f"set env var — refusing to fall back to a different credential")
        if not isinstance(binding, dict) or binding.get("name") != name:
            continue
        ref_id = binding.get("secret_ref_id")
        if ref_id:
            return resolve_secret_ref(connection, ref_id, settings)
        legacy = binding.get("ref")
        if legacy:
            # A legacy ref names an ENV VAR whose value is a path (that is what the existing
            # targets mean by BOSS_SSH_HOST_PRIVATE_KEY_FILE). A binding that cannot resolve
            # must not silently become a different key.
            path = os.environ.get(legacy)
            if path:
                return FileBackend().get(path)
            raise SecretUnavailable(
                f"legacy ref {legacy!r} is bound but unset — refusing global fallback")
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
