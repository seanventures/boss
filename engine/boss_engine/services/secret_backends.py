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
import time
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
            # External to this process. A redeploy does not take it with it, which is what
            # `durable_secret_backend` is asking — a store that forgets to say so is skipped,
            # and the credential lands in Postgres instead.
            return {"ok": True, "kind": self.kind, "writable": True, "durable": True}
        except SecretUnavailable as exc:
            return {"ok": False, "kind": self.kind, "error": str(exc)}



class InfisicalBackend:
    """Infisical over its HTTP API, authenticated as a machine identity.

    Added because it is what the operator's own platform already runs, so an installation can
    keep its secrets in the store it has rather than in the store BOSS would otherwise fall
    back to — Postgres, which this module says plainly is not a good place for a token.

    Two things about Infisical shape this class:

    * Auth is an exchange, not a header. A Client ID and Client Secret are POSTed to
      `/api/v1/auth/universal-auth/login` and come back as an access token that expires — two
      hours by default. So the token is cached and re-fetched before it lapses; sending a
      stale one would surface as an authentication failure in the middle of unrelated work.
    * A secret is addressed by (project, environment, path, name), not by name alone. The
      first three come from configuration and the last from the caller, so `SecretRef` keys
      keep working unchanged.
    """

    kind = "infisical"

    #: Refresh this long before the token actually expires, so a call that starts just under
    #: the wire does not finish just over it.
    _EXPIRY_MARGIN_SECONDS = 120

    def __init__(self, base_url: str, client_id: str, client_secret: str, project_id: str,
                 environment: str = "prod", secret_path: str = "/", timeout: int = 15,
                 token: str = ""):
        self._base = base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        #: A machine-identity token used directly, skipping the login exchange. This is what
        #: `infisical bootstrap` hands back when BOSS provisions its own instance — an identity
        #: token, not a client id/secret pair — so the bundled case has no credentials to
        #: exchange and would otherwise be unable to authenticate at all.
        self._static_token = (token or "").strip()
        self._project = project_id
        self._environment = environment or "prod"
        self._path = secret_path or "/"
        self._timeout = timeout
        self._token = ""
        self._token_expires_at = 0.0

    # ---------------------------------------------------------------- transport
    def _request(self, method: str, url: str, *, payload: Optional[dict] = None,
                 token: Optional[str] = None) -> Any:
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            url, method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                body = resp.read().decode() or "{}"
        except urllib.error.HTTPError as exc:
            # The status is safe to surface; the body can echo the request, and the request
            # carried either the client secret or the secret value itself.
            raise SecretUnavailable(
                f"infisical returned {exc.code} for {method} {urllib.parse.urlsplit(url).path}"
            ) from None
        except Exception as exc:  # noqa: BLE001
            raise SecretUnavailable(
                f"infisical unreachable: {type(exc).__name__}") from None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {}

    def _access_token(self) -> str:
        if self._static_token:
            return self._static_token
        if self._token and time.time() < self._token_expires_at:
            return self._token
        data = self._request(
            "POST", f"{self._base}/api/v1/auth/universal-auth/login",
            payload={"clientId": self._client_id, "clientSecret": self._client_secret})
        token = (data or {}).get("accessToken") or ""
        if not token:
            raise SecretUnavailable("infisical did not return an access token")
        ttl = float((data or {}).get("expiresIn") or 0) or 7200.0
        self._token = token
        self._token_expires_at = time.time() + max(ttl - self._EXPIRY_MARGIN_SECONDS, 30.0)
        return token

    def _secret_url(self, key: str = "") -> str:
        # v3 `raw`, because that is what the software serves. The published API reference
        # documents a v4 surface with `projectId`; a real instance answers 404 for every v4
        # secrets path and 200 for these. Verified by probing a running container, which is the
        # only source that cannot be out of date.
        base = f"{self._base}/api/v3/secrets/raw"
        return f"{base}/{urllib.parse.quote(key, safe='')}" if key else base

    def _scope(self) -> dict:
        #: `workspaceId`, not `projectId` — the v3 name for the same thing.
        return {"workspaceId": self._project, "environment": self._environment,
                "secretPath": self._path}

    def _query(self) -> str:
        return urllib.parse.urlencode(self._scope())

    # ---------------------------------------------------------------- contract
    def get(self, key: str) -> str:
        data = self._request("GET", f"{self._secret_url(key)}?{self._query()}",
                             token=self._access_token())
        secret = (data or {}).get("secret") or data or {}
        value = secret.get("secretValue") if isinstance(secret, dict) else None
        if not value:
            raise SecretUnavailable(f"{key} is not present in infisical")
        return value

    def put(self, key: str, value: str) -> None:
        token = self._access_token()
        payload = {**self._scope(), "secretValue": value}
        try:
            self._request("POST", self._secret_url(key), payload=payload, token=token)
        except SecretUnavailable as create_error:
            # Already there. Infisical separates create from update, so an upsert is two calls
            # and the second one is not an error path — a rotated credential takes this branch
            # every single time.
            #
            # BUT NOT EVERY FAILED CREATE IS A DUPLICATE. Treating any failure as "already
            # there" turned a malformed key into a PATCH that failed the same way, and the
            # operator was shown the PATCH — so the message named the wrong verb, the wrong
            # cause, and implied the secret existed. Both are reported now, in the order they
            # happened, because the FIRST one is the real one.
            try:
                self._request("PATCH", self._secret_url(key), payload=payload, token=token)
            except SecretUnavailable as update_error:
                raise SecretUnavailable(
                    f"could not store {key}: create failed with {create_error}; "
                    f"update then failed with {update_error}") from create_error

    def list_keys(self) -> list[str]:
        data = self._request("GET", f"{self._secret_url()}?{self._query()}",
                             token=self._access_token())
        secrets = (data or {}).get("secrets") or []
        return sorted(s.get("secretKey") or s.get("secretName") or ""
                      for s in secrets if isinstance(s, dict))

    def delete(self, key: str) -> None:
        self._request("DELETE", self._secret_url(key), payload=self._scope(),
                      token=self._access_token())

    def health(self) -> dict[str, Any]:
        try:
            keys = self.list_keys()
            # External to this process. See PlatformSecretStoreBackend.health: without
            # `durable`, a configured Infisical is skipped and channel secrets go to Postgres.
            return {"ok": True, "kind": self.kind, "writable": True, "durable": True,
                    "keys": len(keys), "environment": self._environment, "path": self._path}
        except SecretUnavailable as exc:
            return {"ok": False, "kind": self.kind, "error": str(exc)}


def _infisical_token(settings: Settings) -> str:
    """A machine-identity token, from settings or a file."""
    value = (getattr(settings, "infisical_token", "") or "").strip()
    if value:
        return value
    path = (getattr(settings, "infisical_token_file", "") or "").strip()
    if path:
        expanded = os.path.expanduser(path)
        if os.path.isfile(expanded):
            with open(expanded, "r", encoding="utf-8") as handle:
                return handle.read().strip()
    return ""


def _infisical_secret(settings: Settings) -> str:
    """The machine identity's client secret, from settings or a file. Never a data channel."""
    value = (getattr(settings, "infisical_client_secret", "") or "").strip()
    if value:
        return value
    path = (getattr(settings, "infisical_client_secret_file", "") or "").strip()
    if path:
        expanded = os.path.expanduser(path)
        if os.path.isfile(expanded):
            with open(expanded, "r", encoding="utf-8") as handle:
                return handle.read().strip()
    return ""


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
            # External to this process. See PlatformSecretStoreBackend.health.
            return {"ok": True, "kind": self.kind, "writable": True, "durable": True,
                    "addr": self._addr}
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


class DatabaseBackend:
    """AES-256-GCM ciphertext in Postgres. The only store that survives a container.

    THE BUG THIS EXISTS FOR: every other local backend writes under BOSS_SECRETS_DIR, which
    on a containerised install is the container's own filesystem. Deploying BOSS therefore
    DESTROYED every host credential it had generated — the deployment_hosts row still said
    "credential bound", the secret it pointed at was gone, and the console reported
    "secret 'ssh_key' is bound but unusable: … does not exist" on a host that had been
    working an hour earlier. A self-hosted operator should not have to know to mount a
    volume before the product stops losing its own keys.

    Encrypted with the same envelope as LocalVaultBackend, so the database holds ciphertext
    and the master key stays in the environment: a DB dump is not a key compromise.
    """

    kind = "database"

    def __init__(self, engine: Any = None, master_key: Optional[bytes] = None):
        self._engine = engine
        self._provisioned_key = False
        if master_key is not None:
            self._key = master_key
            return
        try:
            self._key = _load_kms_master_key()
        except SecretUnavailable:
            # NO CONFIGURED MASTER KEY. Provision one, persist it, and be honest about what
            # that is worth.
            #
            # Refusing here would reproduce the defect this class exists to fix: the previous
            # default backend was also unconfigured out of the box, so a new operator's first
            # action returned HTTP 500 and the obvious workaround (`file`) then lost the key
            # on the next deploy. A self-hosted product has to work before it is tuned.
            #
            # WHAT THIS IS AND IS NOT: with BOSS_KMS_MASTER_KEY set, the database holds
            # ciphertext and the key lives in the environment — a DB dump is not a key
            # compromise. Without it, the key sits beside the ciphertext, so this protects
            # against casual inspection and file-level leaks, NOT against someone who has the
            # database. `health()` reports which mode is in effect rather than implying the
            # stronger one.
            self._key = self._ensure_provisioned_key()
            self._provisioned_key = True

    def _ensure_provisioned_key(self) -> bytes:
        import base64

        with self._conn() as c:
            row = c.execute(sql("SELECT ciphertext FROM secret_material "
                                "WHERE ref_key = '__boss_master_key__'")).mappings().first()
            if row:
                return base64.urlsafe_b64decode(bytes(row["ciphertext"]))
            generated = os.urandom(32)
            c.execute(sql(
                "INSERT INTO secret_material (ref_key, ciphertext) "
                "VALUES ('__boss_master_key__', :v) ON CONFLICT (ref_key) DO NOTHING"),
                {"v": base64.urlsafe_b64encode(generated)})
            row = c.execute(sql("SELECT ciphertext FROM secret_material "
                                "WHERE ref_key = '__boss_master_key__'")).mappings().first()
        return base64.urlsafe_b64decode(bytes(row["ciphertext"]))

    def _conn(self):
        if self._engine is None:
            from ..db import get_engine

            self._engine = get_engine()
        return self._engine.begin()

    def get(self, key: str) -> str:
        with self._conn() as c:
            row = c.execute(sql("SELECT ciphertext FROM secret_material WHERE ref_key = :k"),
                            {"k": key}).mappings().first()
        if not row:
            raise SecretUnavailable(f"{key} does not exist")
        blob = bytes(row["ciphertext"])
        try:
            return _aes_gcm_decrypt(self._key, blob).decode("utf-8")
        except Exception:  # noqa: BLE001
            # An operator who starts without BOSS_KMS_MASTER_KEY and sets one later would
            # otherwise find every previously-stored secret undecryptable. Fall back to the
            # auto-provisioned key so that upgrade path stays non-destructive; put() re-encrypts
            # under the configured key, so material migrates as it is rewritten.
            fallback = self._provisioned_key_if_present()
            if fallback is None or fallback == self._key:
                raise
            return _aes_gcm_decrypt(fallback, blob).decode("utf-8")

    def _provisioned_key_if_present(self) -> Optional[bytes]:
        import base64

        with self._conn() as c:
            row = c.execute(sql("SELECT ciphertext FROM secret_material "
                                "WHERE ref_key = '__boss_master_key__'")).mappings().first()
        return base64.urlsafe_b64decode(bytes(row["ciphertext"])) if row else None

    def put(self, key: str, value: str) -> None:
        blob = _aes_gcm_encrypt(self._key, value.encode("utf-8"))
        with self._conn() as c:
            c.execute(sql(
                "INSERT INTO secret_material (ref_key, ciphertext, updated_at) "
                "VALUES (:k, :v, CURRENT_TIMESTAMP) "
                "ON CONFLICT (ref_key) DO UPDATE SET ciphertext = EXCLUDED.ciphertext, "
                "                                    updated_at = CURRENT_TIMESTAMP"),
                {"k": key, "v": blob})

    def list_keys(self) -> list[str]:
        # The provisioned master key shares this table but is not a secret anyone stored,
        # so it must not appear as one (or be deletable through the normal path).
        with self._conn() as c:
            rows = c.execute(sql("SELECT ref_key FROM secret_material "
                                 "WHERE ref_key <> '__boss_master_key__' ORDER BY ref_key")).all()
        return [r[0] for r in rows]

    def delete(self, key: str) -> None:
        if key == "__boss_master_key__":
            raise SecretUnavailable(
                "refusing to delete the master key — every stored secret would become "
                "undecryptable")
        with self._conn() as c:
            c.execute(sql("DELETE FROM secret_material WHERE ref_key = :k"), {"k": key})

    def health(self) -> dict[str, Any]:
        try:
            info: dict[str, Any] = {
                "ok": True,
                "kind": self.kind,
                "keys": len(self.list_keys()),
                "durable": True,
            }
            if self._provisioned_key:
                info["key_mode"] = "auto_provisioned"
                info["note"] = (
                    "No BOSS_KMS_MASTER_KEY is configured, so BOSS generated one and stored it "
                    "in the same database as the secrets. Secrets survive redeploys and are not "
                    "readable by casual inspection, but anyone with the database has both halves. "
                    "Set BOSS_KMS_MASTER_KEY to keep the key out of the database.")
            else:
                info["key_mode"] = "configured"
            return info
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "kind": self.kind, "error": str(exc)}


def backend_for(kind: str, settings: Settings) -> SecretBackend:
    """Map a stored `ref_kind`/`storage_backend` onto an implementation."""
    normalised = (kind or "").strip().lower()
    if normalised in ("env", "environment"):
        return EnvBackend()
    if normalised in ("database", "db", "postgres"):
        return DatabaseBackend()
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
    if normalised == "infisical":
        base = (getattr(settings, "infisical_url", "") or "").strip()
        client_id = (getattr(settings, "infisical_client_id", "") or "").strip()
        project = (getattr(settings, "infisical_project_id", "") or "").strip()
        client_secret = _infisical_secret(settings)
        token = _infisical_token(settings)
        # Either credential shape is enough: a machine identity to exchange, or a token that
        # already is one. A bundled instance is bootstrapped and hands back the latter.
        authenticable = bool(token) or bool(client_id and client_secret)
        if not (base and project and authenticable):
            raise SecretUnavailable(
                "infisical is not configured (BOSS_INFISICAL_URL and BOSS_INFISICAL_PROJECT_ID, "
                "plus either BOSS_INFISICAL_TOKEN or BOSS_INFISICAL_CLIENT_ID with "
                "BOSS_INFISICAL_CLIENT_SECRET or _FILE)")
        return InfisicalBackend(
            base, client_id, client_secret, project,
            environment=(getattr(settings, "infisical_environment", "") or "prod"),
            secret_path=(getattr(settings, "infisical_secret_path", "") or "/"),
            token=token)
    if normalised == "platform_secret_store":
        base = (getattr(settings, "platform_secret_store_url", "") or "").strip()
        token = _platform_token(settings)
        if not base or not token:
            raise SecretUnavailable(
                "platform secret store is not configured "
                "(BOSS_PLATFORM_SECRET_STORE_URL + _TOKEN or _TOKEN_FILE)")
        return PlatformSecretStoreBackend(base, token)
    raise SecretUnavailable(f"no secret backend for {kind!r}")


#: Tried in order when nobody has said where a secret should go. Most protected first — and
#: `database` ahead of `local_vault`, which is not an ordering by protection.
#:
#: WHY, AND IT HAS NOW HAPPENED TWICE. `local_vault` writes encrypted files under
#: BOSS_SECRETS_DIR, which on a containerised install is the CONTAINER'S OWN FILESYSTEM. A
#: deployment that mounts a named volume for artifacts and nothing for secrets — which is what
#: the shipped compose files did — destroys the lot on every `up --force-recreate`.
#:
#:   * first it destroyed the generated SSH host credentials. `DatabaseBackend` was written in
#:     response — read its docstring, it describes this exact failure — and then left BEHIND
#:     local_vault in this tuple, so nothing started using it.
#:   * on 2026-09-27 it destroyed a customer's Google OAuth client four minutes after they
#:     pasted it in, because a deploy went out in between. The console still read
#:     "configured (…rcontent.com)" from the surviving database row while the value it pointed
#:     at was gone, and their video workflow failed with "is not present in local_vault".
#:
#: Both backends use the SAME AES-GCM envelope with the master key in the environment, so this
#: costs almost nothing in protection: a database dump is not a key compromise, which is the
#: argument DatabaseBackend's own docstring already makes. A store that silently loses what it
#: was given is not the more protected option, whatever its threat model looks like on paper.
#:
#: A deployment that DOES mount a durable secrets volume can still ask for local_vault by name;
#: this is only the default when nobody has said.
_BACKEND_PREFERENCE = ("infisical", "platform_secret_store", "vault", "database", "local_vault")


def operator_chosen_backend() -> Optional[str]:
    """The secret store the operator chose at install time, if they chose one.

    The installer asks "1 (database) or 2 (infisical)?" and writes the answer to
    BOSS_COMMUNITY_SECRET_STORE — and nothing read it. Picking "database" on a machine where
    Infisical was still running from an earlier install stored the next credential in Infisical
    anyway, because the preference order below takes "the most protected store that answers".
    An explicit choice is the operator's to make; the preference order is for when nobody chose.
    """
    chosen = (os.environ.get("BOSS_COMMUNITY_SECRET_STORE") or "").strip().lower()
    return chosen if chosen in ("database", "infisical") else None


def database_key_is_external() -> bool:
    """Whether the database store's master key lives OUTSIDE the database.

    With BOSS_KMS_MASTER_KEY in the environment (the installer writes it to .env), Postgres holds
    only ciphertext and a database dump is not a key compromise. Without it, DatabaseBackend
    provisions a key and keeps it in Postgres beside the ciphertext — fine against casual
    inspection, not against anyone holding a backup.
    """
    try:
        _load_kms_master_key()
    except SecretUnavailable:
        return False
    return True


def durable_secret_backend(settings: Settings) -> str:
    """A backend for a secret that MUST still be there after a redeploy.

    `default_secret_backend` answers "what is the most protected store available", and for most
    secrets that is the right question. For a credential the CONSOLE writes and a WORKER reads
    back days later, it is the wrong one: the most protected store on this installation is a
    directory inside a container, and a store that loses what it was given protects nothing.

    That is not hypothetical. It destroyed the generated SSH host credentials once, and on
    2026-09-27 it destroyed a customer's Google OAuth client minutes after they pasted it in —
    twice, because the second attempt went to the same place as the first.

    So this asks a different question and takes the first backend that answers `durable: True`.
    A backend that does not SAY it is durable is not assumed to be; the ordering is otherwise
    the same, so a managed store still wins where one exists.
    """
    if operator_chosen_backend() == "database":
        return "database"
    for kind in _BACKEND_PREFERENCE:
        try:
            impl = backend_for(kind, settings)
            health = impl.health()
        except Exception:  # noqa: BLE001 — an unusable backend is simply not this one
            continue
        if health.get("ok") is False:
            continue
        if health.get("durable") is True:
            return kind
    # NOT `local_vault`, WHICH IS WHAT THE DEFAULT WOULD HAVE PICKED. If nothing on this
    # installation claims durability, the database is still the only store here that survives
    # the container, and saying so loudly beats writing a credential somewhere it will vanish.
    return "database"


def default_secret_backend(settings: Settings) -> str:
    """The best secret backend that actually works on THIS installation.

    A setup wizard cannot ask a first-time user which secret store they have — the whole point
    is that they have not configured anything yet — and it must not fail on the installation it
    exists to serve. So it tries the most protected option first and falls back until one
    answers: a managed store if the deployment has one, a vault if it is reachable, the local
    encrypted file vault if a master key is present, and Postgres last.

    `database` is last for a reason and is not a good place for a token; it is here because a
    wizard that refuses to complete is worse than one that completes and can be improved, and
    because the alternative an operator falls back to is a plaintext env var anyway.
    """
    if operator_chosen_backend() == "database":
        return "database"
    for kind in _BACKEND_PREFERENCE:
        try:
            impl = backend_for(kind, settings)
        except SecretUnavailable:
            continue
        try:
            health = impl.health()
        except Exception:  # noqa: BLE001 — an unhealthy backend is simply not this one
            continue
        if health.get("ok") is False:
            continue
        return kind
    return "database"


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

    # THE HOST'S OWN KEY. A target with no explicit binding but a `host_id` uses the
    # credential its machine holds.
    #
    # Without this the fleet's central promise — "one key per host; rotating it never touches
    # the apps riding on it" — was words on a panel: `attach_target` set host_id and nothing
    # ever read it at deploy time, so an app onboarded through the wizard failed preflight with
    # "no ssh_key bound to this target" while its host sat verified two panels away.
    #
    # Resolved through the host rather than COPIED into the target on attach, deliberately.
    # A copy is a second writable home for one credential: rotate the host key and every
    # target still points at the retired one. Same reasoning that makes `target_ref` derived.
    if name == "ssh_key" and target.get("host_id") and connection is not None:
        row = connection.execute(
            sql("SELECT key_secret_ref FROM deployment_hosts WHERE id = :i"),
            {"i": target["host_id"]}).mappings().first()
        if row and row["key_secret_ref"]:
            return resolve_secret_ref(connection, row["key_secret_ref"], settings)
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
