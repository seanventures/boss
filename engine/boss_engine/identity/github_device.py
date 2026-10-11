"""Sign in with GitHub: OAuth DEVICE FLOW against ONE GitHub App the BOSS project publishes.

WHY DEVICE FLOW. Every other GitHub sign-in needs GitHub to send the browser back to BOSS, so it
needs a callback URL GitHub accepts — which a laptop (localhost), a tailnet box or a private host
never has. Device flow has no callback: BOSS asks GitHub for a short code, the person types it at
github.com/login/device in any browser, and BOSS polls until GitHub says yes. So one published App
(like the `gh` CLI's own client id) serves every install, with no BOSS_PUBLIC_URL and no manifest.

WHAT THE TOKEN IS. A GitHub App user-to-server token: it acts as the person, and reaches only
what BOTH the person and the App's installations reach. It is stored exactly as the "act as me on
GitHub" connection (`oauth_user`, provider github) — so signing in IS linking "as me", and the
separate My account › Agents › Connect GitHub trip is not needed. Nothing about what an agent may
do changes: runs still get `grant ∩ every passport ceiling ∩ step needs` (github_app.run_permissions)
and use this token only when the person's own grant says "act as me on GitHub".

CONFIG. The published App's identity comes from the environment, empty by default:
  BOSS_GITHUB_APP_CLIENT_ID   the App's client id ("Iv23…"); unset = this install uses the advanced path
  BOSS_GITHUB_APP_SLUG        the App's URL name (github.com/apps/<slug>), for "Install BOSS on <org>"
An operator who publishes their own App sets the same two variables (the "env wins" escape hatch).

THE POLL HANDLE. GitHub's `device_code` is what claims the token, so it never reaches the browser
in the clear: the browser holds an AES-GCM sealed handle (vault-derived key) naming the person it
was started for and when it expires. A handle opened by anyone else is refused.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode

from . import github_app, github_urls, jwks

CLIENT_ID_ENV = github_app.CLIENT_ID_ENV          # BOSS_GITHUB_APP_CLIENT_ID
SLUG_ENV = "BOSS_GITHUB_APP_SLUG"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
CONNECTION_NAME = "GitHub (signed in)"
#: GitHub's own minimum between polls; slow_down adds 5 seconds each time it is said.
DEFAULT_INTERVAL = 5
SLOW_DOWN_STEP = 5
#: Repositories a person can push to are what BOSS can build in; read-only ones are counted, not listed.
WRITE_PERMS = ("push", "maintain", "admin")


def published_app() -> dict[str, Optional[str]]:
    """The App every install signs in with, or blanks when this install has none (advanced path)."""
    cid = (os.environ.get(CLIENT_ID_ENV) or "").strip() or None
    slug = (os.environ.get(SLUG_ENV) or "").strip() or None
    return {"client_id": cid, "slug": slug}


def configured() -> bool:
    return bool(published_app()["client_id"])


def _form(url: str, fields: dict[str, str]) -> dict[str, Any]:
    resp = github_app.TRANSPORT("POST", url, {"Accept": "application/json",
                                              "Content-Type": "application/x-www-form-urlencoded",
                                              "User-Agent": "boss-identity"},
                                urlencode(fields).encode())
    try:
        data = resp.json() if resp.body else {}
    except ValueError:
        data = {}
    if resp.status >= 500:
        raise github_app.GitHubRefused("github_unreachable", 502)
    return data if isinstance(data, dict) else {}


# ---- the sealed poll handle --------------------------------------------------------------------------

def _aead():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from . import vault

    return AESGCM(vault.derived_key("github-device-flow"))


def _seal(payload: dict[str, Any]) -> str:
    nonce = os.urandom(12)
    sealed = _aead().encrypt(nonce, json.dumps(payload, separators=(",", ":")).encode(), b"boss-device")
    return jwks.b64u(nonce + sealed)


def _open(handle: str) -> dict[str, Any]:
    try:
        raw = jwks.b64u_decode(handle)
        data = json.loads(_aead().decrypt(raw[:12], raw[12:], b"boss-device"))
    except Exception as exc:  # noqa: BLE001 — forged, truncated, or sealed by another key
        raise github_app.GitHubRefused("bad_handle", 400) from exc
    if not isinstance(data, dict):
        raise github_app.GitHubRefused("bad_handle", 400)
    return data


# ---- 1. start: ask GitHub for a code ---------------------------------------------------------------------

def start(person_id: str, *, now: Optional[int] = None) -> dict[str, Any]:
    """`POST /login/device/code`. Returns what the person needs (the code and where to type it) and
    the sealed handle the console polls with. GitHub Apps take no `scope`: the App's permissions are
    the scope."""
    cid = published_app()["client_id"]
    if not cid:
        raise github_app.GitHubRefused("signin_not_configured", 409)
    data = _form(github_urls.device_code(), {"client_id": cid})
    if data.get("error"):
        # device_flow_disabled: the App's "Enable Device Flow" box is not ticked (OPERATOR steps).
        raise github_app.GitHubRefused(str(data["error"]), 409)
    if not data.get("device_code") or not data.get("user_code"):
        raise github_app.GitHubRefused("device_code_failed", 502)
    now = int(time.time()) if now is None else now
    expires_in = int(data.get("expires_in") or 900)
    interval = max(int(data.get("interval") or DEFAULT_INTERVAL), 1)
    handle = _seal({"p": person_id, "d": str(data["device_code"]), "e": now + expires_in, "i": interval,
                    "c": cid})
    return {"user_code": str(data["user_code"]),
            "verification_uri": str(data.get("verification_uri") or github_urls.device_verify()),
            "expires_in": expires_in, "interval": interval, "handle": handle}


# ---- 2. poll: has the person said yes? ----------------------------------------------------------------------

#: GitHub's device-flow answers that END the attempt, and the words the console says for each.
TERMINAL = {"expired_token": "expired", "access_denied": "denied", "token_expired": "expired",
            "incorrect_device_code": "failed", "device_flow_disabled": "failed",
            "unsupported_grant_type": "failed", "incorrect_client_credentials": "failed"}


def poll(*, person_id: str, org_id: str, handle: str, db: Any, now: Optional[int] = None) -> dict[str, Any]:
    """`POST /login/oauth/access_token` with the device code, once. Answers
      {"state": "pending", "interval": n}            authorization_pending — ask again in n seconds
      {"state": "pending", "interval": n+5}          slow_down — GitHub asked for a longer wait
      {"state": "expired" | "denied" | "failed", "error": code}
      {"state": "connected", "login": "...", "item": vlt_…}  — stored as the person's own connection
    The handle is bound to the person who started it; anyone else's poll is refused."""
    data = _open(handle)
    if data.get("p") != person_id:
        raise github_app.GitHubRefused("not_your_signin", 403)
    now = int(time.time()) if now is None else now
    if int(data.get("e", 0)) < now:
        return {"state": "expired", "error": "expired_token"}
    interval = int(data.get("i") or DEFAULT_INTERVAL)
    answer = _form(github_urls.oauth_access_token(),
                   {"client_id": str(data.get("c") or ""), "device_code": str(data.get("d") or ""),
                    "grant_type": DEVICE_GRANT})
    error = str(answer.get("error") or "")
    if error == "authorization_pending":
        return {"state": "pending", "interval": interval}
    if error == "slow_down":
        return {"state": "pending", "interval": int(answer.get("interval") or interval + SLOW_DOWN_STEP),
                "slow_down": True}
    if error:
        return {"state": TERMINAL.get(error, "failed"), "error": error}
    token = str(answer.get("access_token") or "")
    if not token:
        return {"state": "failed", "error": "no_token"}
    item, login = link(person_id=person_id, org_id=org_id, token=token, refresh=answer.get("refresh_token"),
                       expires_in=answer.get("expires_in"), db=db)
    return {"state": "connected", "login": login, "item": item}


def link(*, person_id: str, org_id: str, token: str, refresh: Optional[str], expires_in: Any,
         db: Any) -> tuple[str, Optional[str]]:
    """AUTO-LINK "as me on GitHub": the sign-in's token is stored as the person's own GitHub
    connection, the same vault kind the OAuth round trip on My account › Agents stores. An older
    connection of the same person is retired (one GitHub identity per person). No grant is
    created, widened or changed here: what an agent may do with it is still the person's grant."""
    from sqlalchemy import text

    from . import audit, vault

    me = github_app._call("GET", "/user", token=token)  # noqa: SLF001
    login = (me.json() or {}).get("login") if me.status == 200 else None
    old = [r[0] for r in db.execute(text(
        "SELECT id FROM identity_vault_items WHERE owner_principal_id = :p AND provider = 'github' "
        "AND kind = 'oauth_user' AND status = 'active'"), {"p": person_id}).all()]
    item = vault.store(org_id=org_id, owner_principal_id=person_id, kind=vault.VaultItemKind.OAUTH_USER,
                       provider="github", secret=token, refresh=str(refresh) if refresh else None,
                       access_expires_at=(datetime.now(timezone.utc) + timedelta(seconds=int(expires_in)))
                       if expires_in else None,
                       provider_subject=login, connection_name=CONNECTION_NAME, db=db,
                       granted_scopes=["via:device_flow"])
    if old:
        db.execute(text("UPDATE identity_vault_items SET status = 'revoked' WHERE id = ANY(:ids)"), {"ids": old})
    audit.record(audit.SecurityEvent(org_id=org_id, event_type="vault.connection.linked",
                                     outcome=audit.Outcome.SUCCESS, actor_principal_id=person_id, actor_kind="user",
                                     target_type="vault_item", target_id=item,
                                     details={"provider": "github", "how": "device_flow", "login": login}), db=db)
    return item, login


# ---- 3. what the signed-in person can reach ------------------------------------------------------------------

def my_connection(person_id: str, *, db: Any) -> Optional[dict[str, Any]]:
    from sqlalchemy import text

    row = db.execute(text(
        "SELECT id, provider_subject, status, created_at, connection_name FROM identity_vault_items "
        "WHERE owner_principal_id = :p AND provider = 'github' AND kind = 'oauth_user' "
        "AND status IN ('active', 'needs_reauth') ORDER BY created_at DESC LIMIT 1"), {"p": person_id}).mappings().first()
    if row is None:
        return None
    return {"id": row["id"], "login": row["provider_subject"], "status": row["status"],
            "linked_at": row["created_at"].isoformat() if row["created_at"] else None,
            "via_signin": row["connection_name"] == CONNECTION_NAME}


def status(person_id: str, *, db: Any) -> dict[str, Any]:
    app = published_app()
    return {"configured": bool(app["client_id"]), "slug": app["slug"],
            "install_url": github_urls.install_app(app["slug"]) if app["slug"] else None,
            "verification_uri": github_urls.device_verify(),
            "me": my_connection(person_id, db=db)}


def _get_all(path: str, token: str, key: Optional[str], *, pages: int = 3) -> Optional[list[dict[str, Any]]]:
    out: list[dict[str, Any]] = []
    sep = "&" if "?" in path else "?"
    for page in range(1, pages + 1):
        resp = github_app._call("GET", f"{path}{sep}per_page=100&page={page}", token=token)  # noqa: SLF001
        if resp.status != 200:
            return None if page == 1 else out
        body = resp.json()
        items = (body or {}).get(key) if key else body
        items = [i for i in (items or []) if isinstance(i, dict)]
        out.extend(items)
        if len(items) < 100:
            break
    return out


def reach(person_id: str, *, db: Any) -> dict[str, Any]:
    """The repositories THIS person can pick: GitHub's own answer for their token —
    `GET /user/installations` then each installation's `/user/installations/{id}/repositories`,
    which GitHub already limits to repositories the person can access AND the App is installed on.
    Only repositories they can push to are offered (BOSS writes branches); read-only ones are counted.

    INSTALL NEEDED. An account the person belongs to (themselves, `GET /user/orgs`) where the App is
    not installed is named, with GitHub's page to install it there: the first person of an org
    installs it once (or GitHub sends the org's owners a request)."""
    from . import vault

    token = vault.token_for_access_check(person_id, provider="github", db=db)
    if not token:
        raise github_app.GitHubRefused("not_signed_in", 409)
    me = github_app._call("GET", "/user", token=token)  # noqa: SLF001
    if me.status == 401:
        raise github_app.GitHubRefused("signin_expired", 409)
    if me.status != 200:
        raise github_app.GitHubRefused("github_unreachable", 502)
    user = me.json() or {}
    login = str(user.get("login") or "")
    installs = _get_all("/user/installations", token, "installations")
    if installs is None:
        raise github_app.GitHubRefused("github_unreachable", 502)
    repos: dict[str, dict[str, Any]] = {}
    read_only = 0
    installed_on: list[dict[str, Any]] = []
    for inst in installs:
        account = inst.get("account") or {}
        installed_on.append({"account": account.get("login"), "type": github_urls.account_type(account.get("type")),
                             "installation_id": str(inst.get("id") or ""),
                             "repository_selection": inst.get("repository_selection")})
        listed = _get_all(f"/user/installations/{inst.get('id')}/repositories", token, "repositories") or []
        for r in listed:
            full = str(r.get("full_name") or "")
            if "/" not in full:
                continue
            perms = r.get("permissions") or {}
            if not any(perms.get(k) for k in WRITE_PERMS):
                read_only += 1
                continue
            owner = r.get("owner") if isinstance(r.get("owner"), dict) else {}
            repos[full.lower()] = {"full_name": full, "private": bool(r.get("private")),
                                   "default_branch": r.get("default_branch"),
                                   "owner_type": str(owner.get("type") or "User")}
    orgs = _get_all("/user/orgs", token, None, pages=1) or []
    slug = published_app()["slug"]
    have = {str(i["account"] or "").lower() for i in installed_on}
    accounts = [{"login": login, "type": github_urls.USER, "id": user.get("id")}] + [
        {"login": o.get("login"), "type": github_urls.ORGANIZATION, "id": o.get("id")} for o in orgs if o.get("login")]
    needed = []
    for a in accounts:
        if str(a["login"]).lower() in have:
            continue
        needed.append({"account": a["login"], "type": a["type"],
                       "install_url": github_urls.install_app_on(slug, a.get("id")) if slug else None})
    return {"login": login, "repos": sorted(repos.values(), key=lambda r: r["full_name"].lower()),
            "read_only": read_only, "installed_on": installed_on, "install_needed": needed,
            "install_url": github_urls.install_app(slug) if slug else None}


def reachable(person_id: str, full_name: str, *, db: Any) -> bool:
    """Server-side scope check for the picker: a repository is pickable only when it is in `reach`."""
    want = (full_name or "").strip().lower()
    return any(r["full_name"].lower() == want for r in reach(person_id, db=db)["repos"])
