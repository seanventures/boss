"""The token vault: every long-lived external secret BOSS owns, envelope-encrypted. Owned by S3.

FROZEN IN PHASE 0: the AAD format, the purposes, the dataclasses and signatures. IDENTITY.md §7.8.

ONLY THIS MODULE DECRYPTS. A test (R9) fails if any other module imports `_decrypt`. There is never
a plaintext fallback: a decryption failure is a hard error plus a `vault.decrypt_failed` event.

How a row is protected
----------------------
    KEK   32 bytes from BOSS_MASTER_KEY_FILE (preferred) or BOSS_MASTER_KEY (base64). Never in the DB;
          the DB holds only its key check value (KCV) in identity_vault_keks.
    DEK   32 random bytes per row, wrapped as nonce(12) ‖ AES-256-GCM(KEK, DEK, aad=AAD ‖ US ‖ "dek").
    data  secret_enc = AES-256-GCM(DEK, secret, aad=AAD), nonce in secret_nonce;
          refresh_enc  = AES-256-GCM(DEK, refresh, aad=AAD ‖ US ‖ "refresh").
    AAD   aad(item_id, owner, provider, kind) — so a ciphertext (or a wrapped DEK) copied to another
          row, or a refresh token swapped into the secret column, fails to decrypt (A52).

Refresh is single-flight: `SELECT … FOR UPDATE` on the row, re-read, refresh only if still due,
write the rotated refresh token in the same transaction that consumed the old one (A57). If the
provider refuses the refresh token (revoked, expired or REUSED — reuse detection is the provider's
rotating refresh), or rotated but our write fails, the item becomes `needs_reauth`: it is never
retried in a loop with a refresh token the provider has already burned.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import logging
import os
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .principal import AuthContext, PrincipalKind, TaskBinding

log = logging.getLogger(__name__)

AAD_SEPARATOR = b"\x1f"            # ASCII unit separator; cannot appear in an id
MASTER_KEY_FILE_ENV = "BOSS_MASTER_KEY_FILE"   # preferred (Docker secret)
MASTER_KEY_ENV = "BOSS_MASTER_KEY"             # 32 bytes, base64


class VaultItemKind(StrEnum):
    OAUTH_USER = "oauth_user"                    # a person's GitHub / Google / YouTube connection
    APP_PRIVATE_KEY = "app_private_key"          # the GitHub App key
    PROVIDER_CREDENTIAL = "provider_credential"  # migrated from secret_refs over time
    OIDC_CLIENT_SECRET = "oidc_client_secret"
    TOTP_SECRET = "totp_secret"
    SIGNING_KEY = "signing_key"                  # run/actor token private keys
    LEGACY_PAT = "legacy_pat"                    # classic GitHub PATs, Admin-only, red in posture
    APP_SECRET = "app_secret"                    # an app's secret per env (F2 §2.4); never a sandbox's for live


class CredentialPurpose(StrEnum):
    """Why a short-lived credential is asked for. Each purpose has its own down-scoping rule."""
    GITHUB_RUN = "github.run"            # per-run installation token for the egress proxy
    GITHUB_AS_USER = "github.as_user"    # the grant says "act as me on GitHub"
    GITHUB_JIT = "github.jit"            # policy-gated fallback; audited credential.handed_out
    PROVIDER_CALL = "provider.call"      # e.g. YouTube upload through a grant
    SSO_CLIENT = "sso.client"            # OIDC client secret for a token call
    TOTP_VERIFY = "totp.verify"
    TOKEN_SIGNING = "token.signing"


@dataclass(frozen=True)
class ConnectionRef:
    """Which vaulted connection. The owner always comes from the caller's verified token `sub`,
    never from a request field (A54); an org-level item has owner None."""
    provider: str
    owner_principal_id: Optional[str]
    item_id: Optional[str] = None


@dataclass(frozen=True)
class ShortLivedCredential:
    token: str = field(repr=False)      # a secret: never logged, never returned to a sandbox
    expires_at: datetime = field(default_factory=datetime.now)
    provider: str = ""
    scopes: frozenset[str] = frozenset()
    item_id: Optional[str] = None


def aad(item_id: str, owner_principal_id: Optional[str], provider: Optional[str], kind: str) -> bytes:
    """Additional authenticated data binding a ciphertext to its row (IDENTITY.md §7.8):
    item_id ‖ owner_principal_id ‖ provider ‖ kind. A ciphertext moved to another row fails (A52)."""
    parts = (item_id, owner_principal_id or "", provider or "", kind)
    for part in parts:
        if AAD_SEPARATOR.decode() in part:
            raise ValueError("AAD components cannot contain the separator")
    return AAD_SEPARATOR.join(p.encode("utf-8") for p in parts)


# ---- errors ------------------------------------------------------------------------------------------

class VaultError(Exception):
    """Base. `code` is a stable, secret-free reason; `status` the HTTP status a route maps it to."""

    status = 500

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class MasterKeyMissing(VaultError):
    pass


class MasterKeyMismatch(VaultError):
    """The configured master key is not the one the vault was written with (A53): refuse to boot."""


class VaultDecryptError(VaultError):
    pass


class VaultForbidden(VaultError):
    status = 403


class VaultNeedsReauth(VaultError):
    status = 409


class VaultNotFound(VaultError):
    status = 404


# ---- the master key ----------------------------------------------------------------------------------

_KCV_LABEL = b"BOSS-VAULT-KCV-v1"
_kek_lock = threading.Lock()
_kek_cache: Optional[tuple[str, bytes]] = None


def _read_master_key(environ: Optional[Mapping[str, str]] = None) -> bytes:
    env = os.environ if environ is None else environ
    raw: Optional[str] = None
    path = (env.get(MASTER_KEY_FILE_ENV) or "").strip()
    if path:
        # NEVER created here (#10): a read at runtime that finds no file is an error, not a new
        # key. The file is created only by `boot()` at FIRST INSTALL (an empty vault), logged.
        try:
            raw = Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise MasterKeyMissing("master_key_file_unreadable") from exc
    elif env.get(MASTER_KEY_ENV):
        raw = env[MASTER_KEY_ENV].strip()
    if not raw:
        raise MasterKeyMissing("master_key_not_configured")
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise MasterKeyMissing("master_key_not_base64") from exc
    if len(key) != 32:
        raise MasterKeyMissing("master_key_wrong_length")
    return key


#: A compose install names a key FILE on its own volume and lets the API create it at FIRST
#: INSTALL ONLY (an empty vault: no registered KEK, no item), explicitly and logged. Set to 0 to
#: require an operator-provided file even then.
MASTER_KEY_AUTOCREATE_ENV = "BOSS_MASTER_KEY_AUTOCREATE"


class BootRefused(VaultError):
    """The API must not start: the vault's key is wrong or missing for a vault that has data."""


def boot(*, db: Any) -> str:
    """The API's boot step for the vault (A53, #10). Refuses (BootRefused) — the caller must stop
    the process, never serve on — when:
      * the configured key does not match the registered KEK, or vault rows exist without one;
      * the key file is missing for a vault that already has a KEK or items (restore the backup);
      * no key is configured at all but the vault has data.
    Creates the key file ONLY at first install (no KEK registered, no item) and logs it. Returns
    the kek id, or "" when no key is configured and the vault is empty (nothing to protect)."""
    from sqlalchemy import text

    keks = int(db.execute(text("SELECT count(*) FROM identity_vault_keks WHERE retired_at IS NULL")).scalar_one())
    items = int(db.execute(text("SELECT count(*) FROM identity_vault_items WHERE status <> 'revoked'")).scalar_one())
    first_install = keks == 0 and items == 0
    path = (os.environ.get(MASTER_KEY_FILE_ENV) or "").strip()
    if path and not Path(path).exists():
        if not first_install:
            raise BootRefused("master_key_file_missing")
        if (os.environ.get(MASTER_KEY_AUTOCREATE_ENV) or "1").strip() == "0":
            raise BootRefused("master_key_file_missing")
        _provision_key_file(Path(path))
        log.warning("[identity] FIRST INSTALL: created the vault master key file %s. Back it up with "
                    "the database; without it nothing in the vault can be decrypted.", path)
    if not path and not os.environ.get(MASTER_KEY_ENV):
        if first_install:
            return ""
        raise BootRefused("master_key_not_configured")
    reset_master_key_cache()
    try:
        return check_master_key(db=db)
    except MasterKeyMismatch as exc:
        raise BootRefused("master_key_mismatch") from exc
    except MasterKeyMissing as exc:
        raise BootRefused(exc.code) from exc


def _provision_key_file(path: Path) -> None:
    """Create the master key file once: 32 random bytes, base64, mode 0600, O_EXCL so two processes
    (api and worker) starting together cannot write two different keys — the loser reads the
    winner's. A file that later goes missing is NOT silently replaced with a usable one: the boot
    check (check_master_key) refuses a key whose KCV does not match the registered KEK."""
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    except OSError:
        return   # read below reports master_key_file_unreadable
    try:
        os.write(fd, base64.b64encode(secrets.token_bytes(32)) + b"\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    logging.getLogger(__name__).warning(
        "[identity] created the vault master key file %s (back it up with the database)", path)


def kcv_of(key: bytes) -> bytes:
    """Key check value: HMAC-SHA256(key, label)[:16]. Reveals nothing usable about the key."""
    return hmac.new(key, _KCV_LABEL, hashlib.sha256).digest()[:16]


def kek_id_of(key: bytes) -> str:
    return "kek_" + kcv_of(key)[:8].hex()


def _kek() -> tuple[str, bytes]:
    global _kek_cache
    with _kek_lock:
        if _kek_cache is None:
            key = _read_master_key()
            _kek_cache = (kek_id_of(key), key)
        return _kek_cache


def reset_master_key_cache() -> None:
    """Tests and `rotate_master_key` callers only."""
    global _kek_cache
    with _kek_lock:
        _kek_cache = None
    from . import audit

    audit.reset_chain_key_cache()


def derived_key(label: str) -> bytes:
    """A purpose-separated key derived from the KEK (HMAC-SHA256(KEK, "BOSS-DERIVED|" + label)), for
    signing short-lived state such as an OAuth `state`. The KEK itself never leaves this module."""
    _, key = _kek()
    return hmac.new(key, b"BOSS-DERIVED|" + label.encode("utf-8"), hashlib.sha256).digest()


def check_master_key(*, db: Any) -> str:
    """Boot check (A53). Registers the KEK's KCV on first use; refuses when vault rows or KEKs exist
    and none matches the configured key. Returns the kek id."""
    from sqlalchemy import text

    kek_id, key = _kek()
    kcv = kcv_of(key)
    rows = db.execute(text("SELECT kek_id, kcv FROM identity_vault_keks WHERE retired_at IS NULL")
                      ).mappings().all()
    if any(hmac.compare_digest(bytes(r["kcv"]), kcv) for r in rows):
        return kek_id
    items = db.execute(text("SELECT count(*) FROM identity_vault_items WHERE status <> 'revoked'")
                       ).scalar_one()
    if rows or items:
        raise MasterKeyMismatch("master_key_mismatch")
    source = "file" if os.environ.get(MASTER_KEY_FILE_ENV) else "env"
    db.execute(text("INSERT INTO identity_vault_keks (kek_id, kcv, source) VALUES (:k, :c, :s) "
                    "ON CONFLICT (kek_id) DO NOTHING"), {"k": kek_id, "c": kcv, "s": source})
    return kek_id


# ---- envelope crypto (private) -------------------------------------------------------------------------

def _wrap_dek(kek: bytes, dek: bytes, row_aad: bytes) -> bytes:
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(kek).encrypt(nonce, dek, row_aad + AAD_SEPARATOR + b"dek")


def _unwrap_dek(kek: bytes, wrapped: bytes, row_aad: bytes) -> bytes:
    return AESGCM(kek).decrypt(wrapped[:12], wrapped[12:], row_aad + AAD_SEPARATOR + b"dek")


def _field_aad(row_aad: bytes, field_name: str) -> bytes:
    return row_aad if field_name == "secret" else row_aad + AAD_SEPARATOR + field_name.encode()


def _encrypt(dek: bytes, plaintext: str, row_aad: bytes, field_name: str) -> tuple[bytes, bytes]:
    nonce = secrets.token_bytes(12)
    return AESGCM(dek).encrypt(nonce, plaintext.encode("utf-8"), _field_aad(row_aad, field_name)), nonce


def _row_aad(row: Mapping[str, Any]) -> bytes:
    return aad(row["id"], row.get("owner_principal_id"), row.get("provider"), row["kind"])


def _decrypt(item_row: Any, *, field_name: str = "secret") -> str:
    """Private. Only vault.py calls this (R9). Raises VaultDecryptError; never falls back."""
    row = dict(item_row)
    try:
        kek_id, kek = _kek()
        if row.get("kek_id") != kek_id:
            raise VaultDecryptError("kek_mismatch")
        row_aad = _row_aad(row)
        dek = _unwrap_dek(kek, bytes(row["dek_wrapped"]), row_aad)
        enc = row.get(f"{field_name}_enc")
        nonce = row.get(f"{field_name}_nonce")
        if enc is None or nonce is None:
            raise VaultDecryptError("no_such_field")
        return AESGCM(dek).decrypt(bytes(nonce), bytes(enc), _field_aad(row_aad, field_name)).decode("utf-8")
    except VaultDecryptError:
        raise
    except Exception as exc:  # noqa: BLE001 — InvalidTag, bad lengths: all one hard error
        raise VaultDecryptError("decrypt_failed") from exc


def _record_decrypt_failure(db: Any, row: Mapping[str, Any], code: str) -> None:
    """A critical event, written OUTSIDE the caller's transaction so the caller's rollback on the
    error it is about to receive does not erase it."""
    from . import audit

    event = audit.SecurityEvent(org_id=row.get("org_id") or "", event_type="vault.decrypt_failed",
                                outcome=audit.Outcome.FAILURE, target_type="vault_item",
                                target_id=row.get("id"), details={"reason": code, "critical": True})
    try:
        engine = getattr(db, "engine", None)
        if engine is not None:
            with engine.begin() as side:
                audit.record(event, db=side)
        else:
            audit.record(event, db=db)
    except Exception:  # noqa: BLE001 — the hard error still happens; the id is not a secret
        log.error("vault.decrypt_failed for %s (event write failed)", row.get("id"))


def _open(db: Any, row: Mapping[str, Any], field_name: str = "secret") -> str:
    try:
        return _decrypt(row, field_name=field_name)
    except VaultDecryptError as exc:
        _record_decrypt_failure(db, row, exc.code)
        raise


# ---- store / read / revoke -----------------------------------------------------------------------------

_COLUMNS = ("id, org_id, owner_principal_id, kind, provider, provider_subject, connection_name, "
            "granted_scopes, dek_wrapped, kek_id, secret_enc, secret_nonce, refresh_enc, refresh_nonce, "
            "access_expires_at, refresh_expires_at, status, last_refreshed_at, provider_revoke_pending")


def store(*, org_id: str, owner_principal_id: Optional[str], kind: VaultItemKind,
          provider: Optional[str], secret: str, refresh: Optional[str] = None,
          access_expires_at: Optional[datetime] = None, db: Any,
          provider_subject: Optional[str] = None, connection_name: Optional[str] = None,
          granted_scopes: Optional[list[str]] = None) -> str:
    """Encrypt and store a secret; returns the vlt_ item id. The caller never sees ciphertext."""
    from sqlalchemy import text

    from .ids import new_id

    kind = VaultItemKind(kind)
    kek_id, kek = _kek()
    item_id = new_id("vlt")
    row_aad = aad(item_id, owner_principal_id, provider, kind.value)
    dek = AESGCM.generate_key(bit_length=256)
    secret_enc, secret_nonce = _encrypt(dek, secret, row_aad, "secret")
    refresh_enc = refresh_nonce = None
    if refresh is not None:
        refresh_enc, refresh_nonce = _encrypt(dek, refresh, row_aad, "refresh")
    db.execute(text(
        "INSERT INTO identity_vault_items (id, org_id, owner_principal_id, kind, provider, "
        " provider_subject, connection_name, granted_scopes, dek_wrapped, kek_id, secret_enc, "
        " secret_nonce, refresh_enc, refresh_nonce, access_expires_at, status) VALUES "
        "(:id, :org, :owner, :kind, :provider, :psub, :cname, :scopes, :dek, :kek, :senc, :snon, "
        " :renc, :rnon, :exp, 'active')"),
        {"id": item_id, "org": org_id, "owner": owner_principal_id, "kind": kind.value,
         "provider": provider, "psub": provider_subject, "cname": connection_name,
         "scopes": granted_scopes, "dek": _wrap_dek(kek, dek, row_aad), "kek": kek_id,
         "senc": secret_enc, "snon": secret_nonce, "renc": refresh_enc, "rnon": refresh_nonce,
         "exp": access_expires_at})
    return item_id


def _load(db: Any, item_id: str, *, for_update: bool = False) -> dict[str, Any]:
    from sqlalchemy import text

    row = db.execute(text(f"SELECT {_COLUMNS} FROM identity_vault_items WHERE id = :id"
                          + (" FOR UPDATE" if for_update else "")), {"id": item_id}).mappings().first()
    if row is None:
        raise VaultNotFound("not_found")
    return dict(row)


def org_github_token(item_id: str, *, db: Any) -> str:
    """The org's FALLBACK GitHub token (a fine-grained PAT held as an org connection: no owner).
    Only boss-egress calls this, to act for a run whose repos the connection covers; it never
    reaches a sandbox. A person's own connection is never readable here (get_credential)."""
    row = _load(db, item_id)
    if (row["kind"] != VaultItemKind.LEGACY_PAT.value or row.get("owner_principal_id") is not None
            or row.get("provider") != "github"):
        raise VaultForbidden("not_an_org_github_connection")
    if row["status"] != "active":
        raise VaultNeedsReauth(row["status"])
    token = _open(db, row)
    from ..services.env_refs import provider_binding, register_host_credential

    register_host_credential(token, provider_binding("github"), "The org's GitHub token")  # rc6 N-1
    return token


def read_secret(item_id: str, *, db: Any, kind: VaultItemKind) -> str:
    """Internal decrypt for identity's OWN org-level material (signing keys, the App key, OIDC and
    TOTP secrets). Refuses user connections: those go through `get_credential` and its owner check."""
    row = _load(db, item_id)
    if row["kind"] != VaultItemKind(kind).value:
        raise VaultForbidden("wrong_kind")
    if row["kind"] in (VaultItemKind.OAUTH_USER.value, VaultItemKind.LEGACY_PAT.value):
        raise VaultForbidden("use_get_credential")
    if row["kind"] == VaultItemKind.APP_SECRET.value:
        raise VaultForbidden("use_read_app_secret")   # bound to its app and env, checked there
    if row["status"] != "active":
        raise VaultNeedsReauth(row["status"])
    return _open(db, row)



# ---- app secrets (APP_TEMPLATES_AND_SECURITY_LOOP.md §2.4, stream S2) ------------------------------
#: The AAD of an app secret binds it to (org, project, app, env, name): those five go into the
#: item's `provider` column, which `aad()` covers, so a ciphertext copied to another app's or
#: another environment's binding fails to decrypt.
APP_SECRET_PROVIDER_PREFIX = "app_secret:"


def app_secret_provider(org_id: str, project_id: str, app_key: str, env: str, name: str) -> str:
    parts = (org_id, project_id, app_key, env, name)
    if any(not p or "/" in p or AAD_SEPARATOR.decode() in p for p in parts):
        raise ValueError("an app secret's binding parts must be non-empty and contain no '/'")
    return APP_SECRET_PROVIDER_PREFIX + "/".join(parts)


def store_app_secret(*, org_id: str, project_id: str, app_key: str, env: str, name: str,
                     value: str, db: Any) -> str:
    """Encrypt an app's secret; returns the vlt_ id the binding row references. Org-level (no
    owner): the app's, not a person's."""
    return store(org_id=org_id, owner_principal_id=None, kind=VaultItemKind.APP_SECRET,
                 provider=app_secret_provider(org_id, project_id, app_key, env, name),
                 secret=value, db=db)


def read_app_secret(item_id: str, *, org_id: str, project_id: str, app_key: str, env: str,
                    name: str, db: Any) -> str:
    """Decrypt an app secret FOR THE BINDING IT WAS STORED FOR, or VaultForbidden. Only the
    templates engine calls this (a preview/deploy resolving a binding, the build gate's scan);
    the value is never returned by an API."""
    row = _load(db, item_id)
    if row["kind"] != VaultItemKind.APP_SECRET.value or row["org_id"] != org_id \
            or row.get("provider") != app_secret_provider(org_id, project_id, app_key, env, name):
        raise VaultForbidden("wrong_kind")
    if row["status"] != "active":
        raise VaultNeedsReauth(row["status"])
    return _open(db, row)

#: provider -> refresh(refresh_token) -> (access_token, new_refresh_token|None, expires_in_seconds).
#: Raise RefreshRejected when the provider refuses the refresh token (revoked, expired, REUSED).
REFRESHERS: dict[str, Callable[[str], tuple[str, Optional[str], int]]] = {}
#: provider -> revoke(access_token, refresh_token|None). Called on disconnect and person deletion.
REVOKERS: dict[str, Callable[[str, Optional[str]], None]] = {}

#: The providers BOSS ships a refresher and revoker for, and the module whose
#: `install_provider_hooks()` registers them. Looked up on first use (P1-14): every process that
#: opens the vault — API, worker, egress — has them, not only one that remembered a boot hook.
#: Without this, a GitHub user token was never refreshed and a disconnect "confirmed" a revocation
#: that never reached GitHub, then shredded the only copy of the token.
BUILTIN_PROVIDER_HOOKS = {"github": "boss_engine.identity.github_app"}


def _install_builtin_hooks(provider: str) -> None:
    module = BUILTIN_PROVIDER_HOOKS.get(provider)
    if module and (provider not in REFRESHERS or provider not in REVOKERS):
        import importlib

        importlib.import_module(module).install_provider_hooks()


def refresher_for(provider: Optional[str]) -> Optional[Callable[[str], tuple[str, Optional[str], int]]]:
    _install_builtin_hooks(provider or "")
    return REFRESHERS.get(provider or "")


def revoker_for(provider: Optional[str]) -> Optional[Callable[[str, Optional[str]], None]]:
    _install_builtin_hooks(provider or "")
    return REVOKERS.get(provider or "")

REFRESH_LEEWAY = timedelta(minutes=5)


class RefreshRejected(Exception):
    """The provider refused the refresh token: it was revoked, expired or REUSED."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


@contextlib.contextmanager
def _own_transaction(db: Any) -> Iterator[Any]:
    """A transaction of the vault's OWN, independent of the caller's: what the provider did (a
    rotated refresh token, a refusal) must be on disk whatever the caller's business transaction
    does next — a rollback there must never lose a refresh token the provider has already burned
    (#12), nor un-mark a connection that needs re-auth (#11)."""
    engine = getattr(db, "engine", None)
    if engine is None or not hasattr(engine, "begin"):
        yield db                                     # no engine to open one with: the caller's
        return
    with engine.begin() as own:
        yield own


def _mark_needs_reauth(db: Any, row: Mapping[str, Any], reason: str) -> None:
    """In its own transaction: it sticks even when the caller's rolls back (#11)."""
    from sqlalchemy import text

    from . import audit

    with _own_transaction(db) as own:
        own.execute(text("UPDATE identity_vault_items SET status = 'needs_reauth' WHERE id = :id "
                         "AND status = 'active'"), {"id": row["id"]})
        audit.record(audit.SecurityEvent(org_id=row["org_id"], event_type="vault.connection.needs_reauth",
                                         outcome=audit.Outcome.FAILURE, target_type="vault_item",
                                         target_id=row["id"], details={"reason": reason}), db=own)


def _refresh_locked(db: Any, item_id: str) -> dict[str, Any]:
    """Single flight, in the vault's OWN transaction: lock the row, re-read, refresh only if
    another caller has not already, and COMMIT the rotated refresh token before returning — so the
    caller's transaction rolling back afterwards cannot lose it (#12). A refusal marks the row
    `needs_reauth`, also in its own transaction (#11)."""
    from sqlalchemy import text

    from . import audit

    with _own_transaction(db) as own:
        row = _load(own, item_id, for_update=True)
        if row["status"] != "active":
            raise VaultNeedsReauth(row["status"])
        exp = row.get("access_expires_at")
        if exp is None or exp - _now() > REFRESH_LEEWAY:
            return row                               # someone else refreshed while we waited
        refresher = refresher_for(row.get("provider"))
        if row.get("refresh_enc") is None or refresher is None:
            return row
        refresh_token = _open(own, row, "refresh")
        try:
            access, new_refresh, expires_in = refresher(refresh_token)
        except RefreshRejected:
            rejected = True
        else:
            rejected = False
        if not rejected:
            kek_id, kek = _kek()
            row_aad = _row_aad(row)
            dek = _unwrap_dek(kek, bytes(row["dek_wrapped"]), row_aad)
            senc, snon = _encrypt(dek, access, row_aad, "secret")
            params: dict[str, Any] = {"id": item_id, "senc": senc, "snon": snon,
                                      "exp": _now() + timedelta(seconds=int(expires_in))}
            sets = "secret_enc = :senc, secret_nonce = :snon, access_expires_at = :exp, last_refreshed_at = now()"
            if new_refresh:
                renc, rnon = _encrypt(dek, new_refresh, row_aad, "refresh")
                params.update(renc=renc, rnon=rnon)
                sets += ", refresh_enc = :renc, refresh_nonce = :rnon"
            try:
                own.execute(text("SAVEPOINT vault_refresh"))
                own.execute(text(f"UPDATE identity_vault_items SET {sets} WHERE id = :id"), params)
                own.execute(text("RELEASE SAVEPOINT vault_refresh"))
                write_failed = False
            except Exception:  # noqa: BLE001
                own.execute(text("ROLLBACK TO SAVEPOINT vault_refresh"))
                write_failed = True
            if not write_failed:
                audit.record(audit.SecurityEvent(org_id=row["org_id"], event_type="vault.connection.refreshed",
                                                 outcome=audit.Outcome.SUCCESS, target_type="vault_item",
                                                 target_id=item_id), db=own)
                fresh = _load(own, item_id)
    if rejected:
        _mark_needs_reauth(db, row, "refresh_rejected")
        raise VaultNeedsReauth("refresh_rejected")
    if write_failed:
        # The provider has rotated (the old refresh token is burned) but we could not keep the
        # new one. Never loop: the connection needs the person to reconnect.
        _mark_needs_reauth(db, row, "refresh_write_failed")
        raise VaultNeedsReauth("refresh_write_failed")
    return fresh


def _owner_from_context(ctx: AuthContext) -> Optional[str]:
    """Whose connections this caller may use: the person a run acts for, or the person signed in.
    Never a request field."""
    if ctx.run_token is not None:
        return ctx.run_token.sub if ctx.run_token.sub.startswith("usr_") else None
    if ctx.principal.kind == PrincipalKind.USER:
        return ctx.principal.id
    return None


def _owner_active(db: Any, principal_id: str) -> bool:
    from sqlalchemy import text

    status = db.execute(text("SELECT status FROM identity_principals WHERE id = :id"),
                        {"id": principal_id}).scalar()
    return status == "active"


def _find_user_item(db: Any, connection: ConnectionRef, owner: str) -> dict[str, Any]:
    from sqlalchemy import text

    if connection.item_id:
        row = _load(db, connection.item_id)
    else:
        found = db.execute(text(
            "SELECT id FROM identity_vault_items WHERE owner_principal_id = :o AND provider = :p "
            "AND kind IN ('oauth_user','legacy_pat') AND status <> 'revoked' "
            "ORDER BY created_at DESC LIMIT 1"), {"o": owner, "p": connection.provider}).scalar()
        if not found:
            raise VaultNotFound("no_connection")
        row = _load(db, found)
    if row.get("owner_principal_id") != owner or row.get("provider") != connection.provider:
        raise VaultForbidden("not_your_connection")
    if row.get("kind") not in (VaultItemKind.OAUTH_USER.value, VaultItemKind.LEGACY_PAT.value):
        # A person's connection, never another kind of their own secret (their TOTP seed) handed
        # out as a "provider token" by naming its item id.
        raise VaultForbidden("wrong_kind")
    return row


def get_credential(connection: ConnectionRef, purpose: CredentialPurpose, *, ctx: AuthContext,
                   task: Optional[TaskBinding] = None, db: Any) -> ShortLivedCredential:
    """Return a short-lived, down-scoped credential for `connection`. Refreshes on demand when the
    stored access token has <5 min left (single-flight, SELECT … FOR UPDATE); `needs_reauth` when
    the provider rotated but the write failed. GitHub run tokens are minted per run, scoped to the
    run's repo and to grant ∩ passport.external_ceiling ∩ step needs."""
    from . import audit, policy

    purpose = CredentialPurpose(purpose)
    if ctx is None:
        raise VaultForbidden("unauthenticated")
    caller_owner = _owner_from_context(ctx)
    # A54: the owner is the verified subject. A ref naming anyone else is refused, whatever it says.
    if connection.owner_principal_id is not None and connection.owner_principal_id != caller_owner:
        raise VaultForbidden("not_your_connection")

    if purpose in (CredentialPurpose.GITHUB_RUN, CredentialPurpose.GITHUB_JIT):
        if purpose == CredentialPurpose.GITHUB_JIT:
            pol = policy.get_policy(ctx.principal.org_id, db=db)
            if not pol.github_jit_credentials:                       # A59
                raise VaultForbidden("jit_disabled")
        if ctx.run_token is None or task is None:
            raise VaultForbidden("run_token_required")
        from . import github_app

        cred = github_app.mint_for_run(ctx.run_token, task=task, db=db)
        if purpose == CredentialPurpose.GITHUB_JIT:
            audit.record(audit.SecurityEvent.from_context(
                ctx, "credential.handed_out", audit.Outcome.SUCCESS, stream=audit.Stream.LINEAGE,
                target_type="github_repo", target_id=",".join(github_app.run_repos(task)),
                details={"purpose": purpose.value, "run": task.run_id}), db=db)
        return cred

    if purpose in (CredentialPurpose.GITHUB_AS_USER, CredentialPurpose.PROVIDER_CALL):
        if caller_owner is None:
            raise VaultForbidden("no_person")
        if ctx.run_token is not None:
            from . import grants

            provider = "github" if purpose == CredentialPurpose.GITHUB_AS_USER else connection.provider
            if not grants.allows_as_user(ctx.run_token.grant_id, provider, db=db):
                raise VaultForbidden("grant_not_as_me")
        if not _owner_active(db, caller_owner):
            raise VaultForbidden("owner_inactive")
        row = _find_user_item(db, connection, caller_owner)
        if row["status"] != "active":
            raise VaultNeedsReauth(row["status"])
        exp = row.get("access_expires_at")
        if exp is not None and exp - _now() <= REFRESH_LEEWAY:
            row = _refresh_locked(db, row["id"])
        token = _open(db, row)
        from sqlalchemy import text

        db.execute(text("UPDATE identity_vault_items SET last_used_at = now() WHERE id = :id"),
                   {"id": row["id"]})
        return ShortLivedCredential(token=token,
                                    expires_at=row.get("access_expires_at") or (_now() + timedelta(hours=1)),
                                    provider=row.get("provider") or "", item_id=row["id"],
                                    scopes=frozenset(row.get("granted_scopes") or ()))
    if purpose == CredentialPurpose.TOTP_VERIFY:
        # S1's sign-in/step-up check: the person's OWN authenticator secret, never a run's, never
        # another person's (the owner is the verified subject, A54).
        if ctx.run_token is not None or caller_owner is None or not connection.item_id:
            raise VaultForbidden("totp_is_the_persons_own")
        row = _load(db, connection.item_id)
        if (row["kind"] != VaultItemKind.TOTP_SECRET.value or row.get("owner_principal_id") != caller_owner
                or row["status"] != "active"):
            raise VaultForbidden("not_your_connection")
        return ShortLivedCredential(token=_open(db, row), expires_at=_now() + timedelta(seconds=60),
                                    provider="totp", item_id=row["id"], scopes=frozenset())
    if purpose == CredentialPurpose.SSO_CLIENT:
        # An OIDC client secret, for the SSO token call identity makes itself: a system context of
        # the provider's own org only — never a person, never an agent.
        if ctx.principal.kind != PrincipalKind.SYSTEM or ctx.run_token is not None or not connection.item_id:
            raise VaultForbidden("sso_client_is_systems")
        row = _load(db, connection.item_id)
        if (row["kind"] != VaultItemKind.OIDC_CLIENT_SECRET.value or row["org_id"] != ctx.principal.org_id
                or row["status"] != "active"):
            raise VaultForbidden("wrong_kind")
        return ShortLivedCredential(token=_open(db, row), expires_at=_now() + timedelta(seconds=60),
                                    provider=row.get("provider") or "", item_id=row["id"], scopes=frozenset())
    raise VaultForbidden("purpose_not_served_here")


def token_for_access_check(owner_principal_id: str, *, provider: str, db: Any) -> Optional[str]:
    """The person's own provider token, for ONE read-only access check made on their behalf (the
    user-repo-access guard). Not reachable from any route or agent: the caller is the grant
    service acting for this same person. None when they have no usable connection."""
    from sqlalchemy import text

    if not _owner_active(db, owner_principal_id):
        return None
    found = db.execute(text(
        "SELECT id FROM identity_vault_items WHERE owner_principal_id = :o AND provider = :p "
        "AND kind = 'oauth_user' AND status = 'active' ORDER BY created_at DESC LIMIT 1"),
        {"o": owner_principal_id, "p": provider}).scalar()
    if not found:
        return None
    row = _load(db, found)
    exp = row.get("access_expires_at")
    if exp is not None and exp - _now() <= REFRESH_LEEWAY:
        try:
            row = _refresh_locked(db, found)
        except VaultNeedsReauth:
            return None
    return _open(db, row)


def revoke(item_id: str, *, ctx: AuthContext, reason: str, db: Any) -> None:
    """Revoke at the provider (e.g. GitHub DELETE /applications/{client_id}/grant) and locally.
    The ciphertext is crypto-shredded: the wrapped DEK is overwritten, so no backup reopens it."""
    from sqlalchemy import text

    from . import audit

    row = _load(db, item_id, for_update=True)
    owner = row.get("owner_principal_id")
    if ctx is not None and ctx.run_token is not None:
        raise VaultForbidden("agents_cannot_revoke")
    if ctx is not None and ctx.principal.kind != PrincipalKind.SYSTEM and owner is not None \
            and _owner_from_context(ctx) != owner:
        from . import authz
        from .scope import ResolvedScope

        decision = authz.check(ctx, "connection:revoke_any", ResolvedScope.org_level(row["org_id"]), db=db)
        if not decision.allowed:
            raise VaultForbidden("not_your_connection")
    if row["status"] == "revoked" and not row.get("provider_revoke_pending"):
        return
    # PROVIDER FIRST, THEN LOCAL (#LOW): the local ciphertext is the only way to call the provider's
    # revoke. If the provider call fails, the item is unusable here at once (status revoked) but
    # NOT shredded: `retry_provider_revocations` tries again, and shreds only after it succeeds.
    provider_ok = _revoke_at_provider(db, row)
    if provider_ok:
        db.execute(text(
            "UPDATE identity_vault_items SET status = 'revoked', dek_wrapped = :shred, "
            "secret_enc = :shred, refresh_enc = NULL, refresh_nonce = NULL, provider_revoke_pending = false "
            "WHERE id = :id"), {"id": item_id, "shred": secrets.token_bytes(60)})
    else:
        db.execute(text("UPDATE identity_vault_items SET status = 'revoked', provider_revoke_pending = true "
                        "WHERE id = :id"), {"id": item_id})
    details = {"reason": reason[:200], "provider_revoked": provider_ok}
    event = (audit.SecurityEvent.from_context(ctx, "vault.connection.revoked", audit.Outcome.SUCCESS,
                                              target_type="vault_item", target_id=item_id, details=details)
             if ctx is not None else
             audit.SecurityEvent(org_id=row["org_id"], event_type="vault.connection.revoked",
                                 outcome=audit.Outcome.SUCCESS, target_type="vault_item",
                                 target_id=item_id, details=details))
    audit.record(event, db=db)


def _revoke_at_provider(db: Any, row: Mapping[str, Any]) -> bool:
    """True when the provider confirmed (or there is nothing to revoke there)."""
    revoker = revoker_for(row.get("provider"))
    if revoker is None or row["kind"] != VaultItemKind.OAUTH_USER.value:
        return True
    try:
        access = _open(db, row)
        refresh = _open(db, row, "refresh") if row.get("refresh_enc") is not None else None
    except VaultDecryptError:
        return True                    # nothing usable to revoke with (or to leak)
    try:
        revoker(access, refresh)
        return True
    except Exception:  # noqa: BLE001
        log.warning("provider revocation failed for vault item %s; it stays pending", row["id"])
        return False


def retry_provider_revocations(*, db: Any) -> int:
    """Maintenance: retry the provider revoke of items revoked locally while the provider was
    unreachable; shred each once it succeeds. Returns how many were completed."""
    from sqlalchemy import text

    done = 0
    for item_id in db.execute(text("SELECT id FROM identity_vault_items WHERE provider_revoke_pending "
                                   "FOR UPDATE SKIP LOCKED")).scalars().all():
        row = _load(db, item_id)
        if _revoke_at_provider(db, row):
            db.execute(text("UPDATE identity_vault_items SET dek_wrapped = :shred, secret_enc = :shred, "
                            "refresh_enc = NULL, refresh_nonce = NULL, provider_revoke_pending = false "
                            "WHERE id = :id"), {"id": item_id, "shred": secrets.token_bytes(60)})
            done += 1
    return done


def rotate_master_key(new_key: bytes, *, db: Any, actor_principal_id: str = "sys_host_cli") -> int:
    """Re-wrap every DEK under `new_key` in the caller's ONE transaction (the CLI is S5's). Data
    ciphertexts never change; only `dek_wrapped` and `kek_id`. Returns the number of rows re-wrapped.
    The caller then points BOSS_MASTER_KEY(_FILE) at the new key and calls reset_master_key_cache()."""
    from sqlalchemy import text

    from . import audit

    if len(new_key) != 32:
        raise MasterKeyMissing("master_key_wrong_length")
    # One rotation at a time, and no new row written under the old KEK while it runs: an advisory
    # lock serialises rotations, and the KEK table lock blocks the boot check and `store` from
    # registering against a key that is about to retire.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('identity_vault_rotate_master_key'))"))
    db.execute(text("LOCK TABLE identity_vault_keks IN EXCLUSIVE MODE"))
    old_id, old = _kek()
    new_kek_id = kek_id_of(new_key)
    if new_kek_id == old_id:
        raise MasterKeyMismatch("same_key")
    rows = db.execute(text("SELECT id, org_id, owner_principal_id, provider, kind, dek_wrapped, kek_id "
                           "FROM identity_vault_items WHERE status <> 'revoked' OR provider_revoke_pending "
                           "FOR UPDATE")).mappings().all()
    for r in rows:
        if r["kek_id"] != old_id:
            raise MasterKeyMismatch("row_under_unknown_kek")
        row_aad = _row_aad(r)
        dek = _unwrap_dek(old, bytes(r["dek_wrapped"]), row_aad)
        db.execute(text("UPDATE identity_vault_items SET dek_wrapped = :w, kek_id = :k WHERE id = :id"),
                   {"w": _wrap_dek(new_key, dek, row_aad), "k": new_kek_id, "id": r["id"]})
    db.execute(text("UPDATE identity_vault_keks SET retired_at = now() WHERE kek_id = :k"), {"k": old_id})
    db.execute(text("INSERT INTO identity_vault_keks (kek_id, kcv, source) VALUES (:k, :c, 'rotated') "
                    "ON CONFLICT (kek_id) DO UPDATE SET retired_at = NULL"),
               {"k": new_kek_id, "c": kcv_of(new_key)})
    orgs = sorted({r["org_id"] for r in rows}) or sorted(
        db.execute(text("SELECT id FROM orgs ORDER BY id LIMIT 1")).scalars().all())
    for org in orgs:
        audit.record(audit.SecurityEvent(org_id=org, event_type="vault.master_key_rotated",
                                         outcome=audit.Outcome.SUCCESS, actor_principal_id=actor_principal_id,
                                         actor_kind="system", target_type="vault_kek", target_id=new_kek_id,
                                         details={"from": old_id, "to": new_kek_id, "rows": len(rows)}), db=db)
    return len(rows)
