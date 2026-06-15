"""GitHubAppAdapter — developer impersonation via a customer-installed GitHub App
(architecture doc §6). Instead of a long-lived PAT held by BOSS, BOSS mints
**short-lived, repo-scoped installation access tokens** from the App the customer
installs and can revoke at any time.

Token flow:
  1. Sign a short (<=10 min) JWT with the App's private key (RS256), iss=app_id.
  2. POST /app/installations/{installation_id}/access_tokens with that JWT to get
     an installation token (~1h TTL), optionally narrowed to specific repos and
     permissions.
  3. Hand the installation token to the sandbox (clone) and GitHubAdapter (PR).

Properties this preserves:
  - No shared/long-lived credentials in BOSS — tokens expire in ~1h.
  - Customer-revocable — uninstalling the App invalidates everything instantly.
  - Attributable — commits are authored by the App's bot identity.

This adapter is inert unless ``github_app_id`` + a private key + an installation
id are configured; otherwise BOSS falls back to the PAT path (Phase 0).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Optional

import httpx

from ..config import Settings


class GitHubAppConfigurationError(RuntimeError):
    pass


@dataclass
class InstallationToken:
    token: str
    expires_at: str  # ISO8601 from GitHub
    permissions: dict
    repository_selection: Optional[str]


class GitHubAppAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._cache: dict[str, tuple[float, InstallationToken]] = {}

    # -- configuration ------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(
            self.settings.github_app_id
            and self._private_key_pem()
            and self.settings.github_app_installation_id
        )

    def _private_key_pem(self) -> Optional[str]:
        if self.settings.github_app_private_key:
            # Allow `\n`-escaped single-line env values.
            return self.settings.github_app_private_key.replace("\\n", "\n")
        path = self.settings.github_app_private_key_path
        if path:
            p = Path(path).expanduser()
            if not p.exists():
                raise GitHubAppConfigurationError(
                    f"GitHub App private key not found at {p}"
                )
            return p.read_text(encoding="utf-8")
        return None

    # -- JWT ----------------------------------------------------------------
    def _app_jwt(self) -> str:
        # Imported lazily so the PAT-only (Phase 0) path never requires PyJWT.
        try:
            import jwt
        except ImportError as exc:  # pragma: no cover
            raise GitHubAppConfigurationError(
                "PyJWT is required for the GitHub App path. Install it: pip install pyjwt"
            ) from exc
        pem = self._private_key_pem()
        if not pem or not self.settings.github_app_id:
            raise GitHubAppConfigurationError("GitHub App id / private key not configured.")
        now = int(time.time())
        payload = {
            "iat": now - 60,        # clock-skew slack
            "exp": now + 9 * 60,    # <=10 min per GitHub's limit
            "iss": str(self.settings.github_app_id),
        }
        return jwt.encode(payload, pem, algorithm="RS256")

    # -- installation token -------------------------------------------------
    def installation_token(
        self,
        *,
        repositories: Optional[list[str]] = None,
        permissions: Optional[dict] = None,
        installation_id: Optional[str] = None,
    ) -> InstallationToken:
        """Mint (or reuse a cached, still-valid) installation token, optionally
        scoped to specific repos + permissions (least privilege)."""
        inst = installation_id or self.settings.github_app_installation_id
        if not inst:
            raise GitHubAppConfigurationError("GitHub App installation id not configured.")

        cache_key = f"{inst}:{','.join(sorted(repositories or []))}"
        cached = self._cache.get(cache_key)
        # reuse if more than 5 minutes of life remains
        if cached and cached[0] - time.time() > 300:
            return cached[1]

        body: dict = {}
        if repositories:
            body["repositories"] = repositories
        if permissions:
            body["permissions"] = permissions

        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._app_jwt()}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"https://api.github.com/app/installations/{inst}/access_tokens"
        with httpx.Client(timeout=20) as client:
            resp = client.post(url, headers=headers, json=body or None)
        if resp.status_code not in (200, 201):
            raise GitHubAppConfigurationError(
                f"Failed to mint installation token ({resp.status_code}): {resp.text[:300]}"
            )
        data = resp.json()
        token = InstallationToken(
            token=data["token"],
            expires_at=data.get("expires_at", ""),
            permissions=data.get("permissions", {}),
            repository_selection=data.get("repository_selection"),
        )
        # Cache keyed on GitHub's own expiry, parsed coarsely (1h default).
        expiry_epoch = time.time() + 3600
        self._cache[cache_key] = (expiry_epoch, token)
        return token

    def token_for_repo(self, owner: str, repo: str) -> str:
        """Convenience: a token scoped to a single repo with contents+PR write —
        exactly what the sandbox clone and PR creation need."""
        tok = self.installation_token(
            repositories=[repo],
            permissions={"contents": "write", "pull_requests": "write"},
        )
        return tok.token
