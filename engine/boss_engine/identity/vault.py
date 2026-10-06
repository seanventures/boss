"""The token vault: every long-lived external secret BOSS owns, envelope-encrypted. Owned by S3.

FROZEN IN PHASE 0: the AAD format, the purposes, the dataclasses and signatures. Bodies marked
NotImplementedError are S3's (IDENTITY_BUILD.md §3). IDENTITY.md §7.8.

ONLY THIS MODULE DECRYPTS. A test (R9) fails if any other module imports `_decrypt`. There is never
a plaintext fallback: a decryption failure is a hard error plus a `vault.decrypt_failed` event.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Optional

from .principal import AuthContext, TaskBinding

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


def get_credential(connection: ConnectionRef, purpose: CredentialPurpose, *, ctx: AuthContext,
                   task: Optional[TaskBinding] = None, db: Any) -> ShortLivedCredential:
    """Return a short-lived, down-scoped credential for `connection`. Refreshes on demand when the
    stored access token has <5 min left (single-flight, SELECT … FOR UPDATE); `needs_reauth` when
    the provider rotated but the write failed. GitHub run tokens are minted per run, scoped to the
    run's repo and to grant ∩ passport.external_ceiling ∩ step needs."""
    raise NotImplementedError("identity.vault.get_credential is stream S3's")


def store(*, org_id: str, owner_principal_id: Optional[str], kind: VaultItemKind,
          provider: Optional[str], secret: str, refresh: Optional[str] = None,
          access_expires_at: Optional[datetime] = None, db: Any) -> str:
    """Encrypt and store a secret; returns the vlt_ item id. The caller never sees ciphertext."""
    raise NotImplementedError("identity.vault.store is stream S3's")


def revoke(item_id: str, *, ctx: AuthContext, reason: str, db: Any) -> None:
    """Revoke at the provider (e.g. GitHub DELETE /applications/{client_id}/grant) and locally."""
    raise NotImplementedError("identity.vault.revoke is stream S3's")


def _decrypt(item_row: Any, *, field_name: str = "secret") -> str:  # pragma: no cover - S3
    """Private. Only vault.py calls this (R9)."""
    raise NotImplementedError("identity.vault._decrypt is stream S3's")
