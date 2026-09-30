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

import logging
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Optional

import httpx

from ..config import Settings

logger = logging.getLogger("boss.github_app")


def repo_token(settings, owner: str, repo: str, *, api_base_url: str = "", app=None,
               connection=None, project_id: str = "") -> str:
    """A usable GitHub token for THIS repo: installation token if the App covers it, else PAT.

    One decision, shared by every caller, because three of them made it independently and all
    three made it the same wrong way: prefer the App, and treat "the App has no installation
    for this repo" as fatal — even with a working PAT configured beside it.

    The consequence was specific and expensive. BOSS could not open a pull request against its
    OWN repository: the App is installed on the sandbox repo only, so every attempt died with
    `Failed to mint installation token (422): There is at least one repository that does not
    exist or is not accessible to the parent installation`. A system meant to improve itself
    could build applications all day and never touch its own source.

    The App is still preferred where it applies — short-lived, repo-scoped, attributable. The
    PAT is the fallback for repos outside the installation, which is exactly the case the App
    cannot serve.
    """
    # Callers pass their long-lived adapter so its installation-token cache is reused; a
    # fresh one per call would re-sign a JWT and re-mint on every request.
    if app is None:
        app = GitHubAppAdapter(settings, api_base_url=api_base_url) if api_base_url \
            else GitHubAppAdapter(settings)
    if app.configured:
        try:
            token, missing = app.usable_token_for_repo(owner, repo)
            if not missing:
                return token
            # The App is installed here but was not granted enough to do the work. Without
            # this branch the token is used anyway and dies at push time with a bare 403.
            if not getattr(settings, "github_token", ""):
                return token
            logging.getLogger(__name__).warning(
                "github: App installation for %s/%s is missing %s; using the configured "
                "token instead", owner, repo, ", ".join(missing))
        except GitHubAppConfigurationError:
            # Not an outage: this repo is simply outside the installation. Fall through to the
            # PAT rather than failing work the platform is perfectly able to do.
            if not getattr(settings, "github_token", ""):
                raise
            logging.getLogger(__name__).info(
                "github: %s/%s is outside the App installation; using the configured token",
                owner, repo)
    token = getattr(settings, "github_token", "")
    if token:
        return token
    # A THIRD SOURCE: THE TOKEN THIS PROJECT REGISTERED.
    #
    # The setup wizard writes a connection and a repository binding, and `create_code_task`
    # already resolves the repo NAME through that registry — but every path that pushes read
    # this environment variable. So a wizard-connected install said "Connected — owner/name is
    # the build target" and its first build died right here, on a message telling the operator
    # to set an environment variable the wizard exists to replace.
    #
    # Deliberately AFTER the environment, not before it. Every install configured the old way —
    # which is every deployment that predates the wizard — must keep behaving exactly as it did;
    # a stale connection row must not be able to take a working build's credential away. The
    # registry is the fallback, which is the position the PAT itself occupies relative to the
    # App, and for the same reason.
    if connection is not None and project_id:
        try:
            from .github_connections import GitHubConnectionService

            registered = GitHubConnectionService(settings).token_for_repository(
                connection, project_id=project_id, owner=owner, repo=repo)
        except Exception as error:  # noqa: BLE001 — never a new way for a build to die
            logging.getLogger(__name__).debug(
                "github: registered-credential lookup failed for %s/%s: %s", owner, repo, error)
            registered = None
        if registered:
            return registered
    raise GitHubAppConfigurationError(
        "No GitHub credentials for %s/%s: the App has no installation for it, "
        "BOSS_GITHUB_TOKEN is not set, and this project has no repository connected in Setup."
        % (owner, repo))


class GitHubAppConfigurationError(RuntimeError):
    pass


@dataclass
class InstallationToken:
    token: str
    expires_at: str  # ISO8601 from GitHub
    permissions: dict
    repository_selection: Optional[str]


class GitHubAppAdapter:
    def __init__(
        self,
        settings: Settings,
        *,
        api_base_url: str = "https://api.github.com",
    ):
        self.settings = settings
        self.api_base_url = api_base_url.rstrip("/")
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
        # Pydantic represents an empty Path environment value as ``Path('.')``.
        # Treat that common partial-configuration state as unset, not as a key file.
        if path and str(path).strip() not in {"", "."}:
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

        permission_scope = ",".join(
            f"{key}={value}" for key, value in sorted((permissions or {}).items())
        )
        cache_key = (
            f"{inst}:{','.join(sorted(repositories or []))}:{permission_scope}"
        )
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
        url = f"{self.api_base_url}/app/installations/{inst}/access_tokens"
        with httpx.Client(timeout=20) as client:
            resp = client.post(url, headers=headers, json=body or None)
            if (
                resp.status_code == 422
                and permissions
                and "permissions requested are not granted" in resp.text
            ):
                # The installation grants fewer permissions than we tried to
                # downscope to (GitHub 422s the whole mint, it does not
                # intersect). Fall back to the installation's own permission
                # set — still repo-scoped — instead of failing the pipeline
                # over an optional permission (e.g. issues:write not granted).
                logger.warning(
                    "github_app: installation %s lacks some requested permissions "
                    "(%s); retrying with the installation's granted set",
                    inst,
                    ",".join(sorted(permissions)),
                )
                body.pop("permissions", None)
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

    #: What BOSS must be able to do to deliver work: commit code and open the PR.
    #: `issues` is requested too but is genuinely optional, so it is not required here.
    REQUIRED_PERMISSIONS = {"contents": "write", "pull_requests": "write"}

    def token_for_repo(self, owner: str, repo: str) -> str:
        """Return a single-repository token for governed code and community work."""
        return self.usable_token_for_repo(owner, repo)[0]

    def usable_token_for_repo(self, owner: str, repo: str) -> tuple[str, list[str]]:
        """Return (token, missing_permissions) for this repo.

        Minting can *succeed* and still hand back a token that cannot push. When the
        installation grants less than we ask for, GitHub 422s the whole request rather than
        intersecting, so `installation_token` retries with the installation's own granted set
        — a deliberate choice, since an ungranted optional permission should not fail a
        pipeline. The cost is that a token with no `contents:write` looks exactly like a good
        one, and the failure surfaces much later as an opaque 403 at git-push time.

        So report what was actually granted instead of discarding it, and let the caller
        choose a credential that can do the job.
        """
        tok = self.installation_token(
            repositories=[repo],
            permissions={
                "contents": "write",
                "issues": "write",
                "pull_requests": "write",
            },
        )
        granted = tok.permissions or {}
        missing = sorted(
            name for name, level in self.REQUIRED_PERMISSIONS.items()
            if granted.get(name) != level
        )
        return tok.token, missing
