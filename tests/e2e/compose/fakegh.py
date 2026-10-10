"""GitHub, faked, as a compose service next to a real BOSS stack (`fakegh` in build-override.yml).

It IS the harness's fakes (tests/e2e/stubs: the intercepting proxy, the git-backed REST fake,
git http-backend on bare repos, the blocked-egress log) plus the half of GitHub a real install
reaches only through a person's browser and the App's own JWT:

  ACCOUNT TYPE: FAKEGH_ACCOUNT_TYPE (User | Organization; POST /_github_account switches it, with
  a fresh App world). Every account-settings page exists ONLY in that type's tree, as at GitHub:
  a personal account's /settings/..., an organisation's /organizations/<org>/settings/...; the
  other tree answers 404 (a BOSS that built the wrong link fails here as it did for paramg).
  The App's permissions and the installation's ACCEPTED permissions are kept apart: adding
  Workflows on the App's permissions page leaves the installation waiting until "Accept new
  permissions" on the installation's page, and tokens are minted against what was accepted.

  browser   POST github.com/settings/apps/new?state=      the manifest form -> a "Create GitHub App"
                                                           page -> back to the manifest's
                                                           redirect_url with ?code=&state=
            GET  github.com/apps/<slug>/installations/new  "Install" page -> back to setup_url with
                                                           ?installation_id=&setup_action=install&state=
  App JWT   POST /app-manifests/{code}/conversions         id, slug, pem (a fresh RSA key), client secret
            GET  /app, /app/installations, /app/installations/{id}
            POST /app/installations/{id}/access_tokens     a ghs_ token, scoped to the asked repos
  token     GET  /installation/repositories                the installation's repos
            DELETE /installation/token                     revokes it (a revoked token is then 401)

Every App-JWT call is VERIFIED (RS256 against the key this fake issued, `iss` = the App id,
not expired): a BOSS that signed wrongly would fail here as it would at GitHub.

THREE WAYS IN, nothing in BOSS pointed at it by URL:
  :8080  the proxy (HTTPS_PROXY/HTTP_PROXY for api, worker, egress; the browser's proxy) and the
         fake's own /_log, /_world, /_github_app (state for the driver);
  :443   direct TLS for the clients that do not use a proxy (BOSS's SSRF-safe identity client
         resolves api.github.com itself): the compose network aliases api.github.com and
         github.com to this container, the leaf is minted per SNI name by the same CA;
  CA     /e2e-ca/ca.pem, shared read-only with the services (SSL_CERT_FILE and friends). It
         survives a restart of this container (key in the state volume), so a restarted fake
         does not break the services' trust.

Every request is in /_log with the client address, so the driver can tell the browser's traffic
(the host gateway) from the services'. Hosts with no fake get a 502 and `blocked: true`.
Never prints a token or key.
"""
from __future__ import annotations

import base64
import datetime as _dt
import json
import os
import secrets
import shutil
import socket
import ssl
import sys
import threading
import time
from html import escape
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlsplit

sys.path.insert(0, os.environ.get("FAKEGH_STUBS", str(Path(__file__).resolve().parents[1] / "stubs")))

import egress as _egress  # noqa: E402
import fakes  # noqa: E402
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

STATE = Path(os.environ.get("FAKEGH_STATE", "/srv/fakegh"))
CA_OUT = Path(os.environ.get("FAKEGH_CA_OUT", "/e2e-ca"))
OWNER = os.environ.get("FAKEGH_ACCOUNT", "fxd-org")
REPOS = [r for r in os.environ.get("FAKEGH_REPOS", "fxd-org/fxd-build-app").split(",") if r]
INSTALLATION_ID = int(os.environ.get("FAKEGH_INSTALLATION_ID", "7301"))
ACCOUNT_TYPE = "User" if os.environ.get("FAKEGH_ACCOUNT_TYPE", "Organization").lower() == "user" else "Organization"
USER_LOGIN = os.environ.get("FAKEGH_USER_LOGIN", "fxd-owner")
_LOCAL = threading.local()


# ---- the CA, kept across restarts ----------------------------------------------------------------

class PersistentCA(_egress.CertificateAuthority):
    """The stubs' CA, but its key is kept in the state volume: the services trust ca.pem from a
    shared volume, and a restarted fake must still be the CA they trust."""

    def __init__(self, directory: Path):
        key_path, cert_path = directory / "ca.key", directory / "ca.pem"
        if key_path.exists() and cert_path.exists():
            self.directory = directory
            self._lock = threading.Lock()
            self._contexts = {}
            self.key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
            self.cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            self.ca_path = cert_path
            return
        super().__init__(directory)
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()))


# ---- the GitHub App (one per fake; what the manifest flow creates) --------------------------------

class AppWorld:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data: dict[str, Any] = {"manifests": {}, "app": None, "installed": False, "tokens": {}}
        if path.exists():
            self.data = json.loads(path.read_text())

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(self.data, fh)
        tmp.replace(self.path)

    def public_view(self) -> dict[str, Any]:
        """What the driver may see: no key, no client secret, no token values."""
        with self.lock:
            app = dict(self.data.get("app") or {})
            for k in ("pem", "client_secret", "webhook_secret", "public_pem"):
                app.pop(k, None)
            toks = [{"id": t["id"], "repositories": t["repositories"], "permissions": t["permissions"],
                     "revoked": t["revoked"], "issued_at": t["issued_at"], "uses": t.get("uses", 0)}
                    for t in self.data["tokens"].values()]
            return {"app": app or None, "installed": self.data["installed"],
                    "installation_id": INSTALLATION_ID if self.data["installed"] else None,
                    "manifests": len(self.data["manifests"]), "tokens": toks,
                    "user_tokens": len(self.data.get("user_tokens", []))}


APPS = AppWorld(STATE / "github-app.json")


def _b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _verify_app_jwt(auth: str) -> Optional[str]:
    """None when the bearer is a valid App JWT for the App this fake issued; else why not."""
    app = APPS.data.get("app")
    if not app:
        return "no App was created"
    if not auth.startswith("Bearer "):
        return "no bearer"
    parts = auth[7:].strip().split(".")
    if len(parts) != 3:
        return "not a JWT"
    try:
        header, claims = json.loads(_b64u_decode(parts[0])), json.loads(_b64u_decode(parts[1]))
        key = serialization.load_pem_public_key(app["public_pem"].encode())
        key.verify(_b64u_decode(parts[2]), f"{parts[0]}.{parts[1]}".encode(), padding.PKCS1v15(), hashes.SHA256())
    except Exception as exc:  # noqa: BLE001
        return f"bad signature ({type(exc).__name__})"
    if header.get("alg") != "RS256":
        return "alg is not RS256"
    if str(claims.get("iss")) != str(app["id"]):
        return "iss is not this App"
    now = time.time()
    if not (claims.get("iat", now + 999) <= now + 60 and claims.get("exp", 0) > now):
        return "expired or not yet valid"
    if claims["exp"] - claims["iat"] > 600:
        return "lifetime over 10 minutes"
    return None


def settings_root() -> str:
    """The account's settings tree, as GitHub has it for this account type."""
    return "/settings" if ACCOUNT_TYPE == "User" else f"/organizations/{OWNER}/settings"


def _accepted() -> dict[str, str]:
    """What the installation ACCEPTED (differs from the App's permissions until accepted)."""
    acc = APPS.data.get("accepted")
    return dict(acc) if isinstance(acc, dict) else dict((APPS.data.get("app") or {}).get("permissions", {}))


def _installation() -> dict[str, Any]:
    return {"id": INSTALLATION_ID, "account": {"login": OWNER, "type": ACCOUNT_TYPE},
            "repository_selection": "selected", "app_id": (APPS.data.get("app") or {}).get("id"),
            "app_slug": (APPS.data.get("app") or {}).get("slug"),
            "permissions": _accepted(),
            "html_url": f"https://github.com{settings_root()}/installations/{INSTALLATION_ID}"}


def configure(account: str, kind: str, repos: list[str]) -> None:
    """Switch the fake to another account (a person or an organisation) with a fresh App world."""
    global OWNER, ACCOUNT_TYPE, REPOS
    OWNER = account
    ACCOUNT_TYPE = "User" if kind.lower() == "user" else "Organization"
    REPOS = repos or [f"{account}/boss-sandbox"]
    with APPS.lock:
        APPS.data = {"manifests": {}, "app": None, "installed": False, "tokens": {}}
        APPS.save()


def _token_of(auth: str) -> Optional[dict[str, Any]]:
    raw = auth.split(" ", 1)[1].strip() if " " in auth else ""
    for tok in APPS.data["tokens"].values():
        if secrets.compare_digest(tok["value"], raw):
            return tok
    return None


def app_api(method: str, path: str, auth: str, body: Any) -> Optional[tuple[int, Any]]:
    """The App's REST surface. None: not an App route (the git-backed fake answers it)."""
    m = path.rstrip("/")
    if m.startswith("/app-manifests/") and m.endswith("/conversions") and method == "POST":
        code = m.split("/")[2]
        with APPS.lock:
            manifest = APPS.data["manifests"].pop(code, None)
            if manifest is None:
                return 404, {"message": "Not Found"}
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                    serialization.NoEncryption()).decode()
            slug = "".join(c if c.isalnum() else "-" for c in str(manifest.get("name") or "boss").lower()).strip("-")
            app = {"id": 900000 + secrets.randbelow(99999), "slug": slug, "name": manifest.get("name"),
                   "node_id": "A_fxd", "owner": {"login": OWNER, "type": ACCOUNT_TYPE},
                   "html_url": f"https://github.com/apps/{slug}", "client_id": "Iv1.fxd" + secrets.token_hex(6),
                   "client_secret": secrets.token_hex(20), "webhook_secret": None, "pem": pem,
                   "public_pem": key.public_key().public_bytes(
                       serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
                   "permissions": manifest.get("default_permissions") or {}, "events": [],
                   "setup_url": manifest.get("setup_url"), "redirect_url": manifest.get("redirect_url"),
                   "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ")}
            APPS.data.update(app=app, installed=False, accepted=None)
            APPS.save()
        out = {k: v for k, v in app.items() if k not in ("public_pem", "setup_url", "redirect_url")}
        return 201, out
    if m == "/app" or m.startswith("/app/"):
        why = _verify_app_jwt(auth)
        if why:
            return 401, {"message": f"A JSON web token could not be decoded ({why})"}
        app = APPS.data["app"]
        if m == "/app":
            return 200, {k: app[k] for k in ("id", "slug", "name", "owner", "html_url", "client_id", "permissions")}
        if m == "/app/installations":
            return 200, [_installation()] if APPS.data["installed"] else []
        if m == f"/app/installations/{INSTALLATION_ID}" and APPS.data["installed"]:
            return 200, _installation()
        if m == f"/app/installations/{INSTALLATION_ID}/access_tokens" and method == "POST" and APPS.data["installed"]:
            body = body if isinstance(body, dict) else {}
            granted = _accepted()
            asked = body.get("permissions") or dict(granted)
            levels = {"none": 0, "read": 1, "write": 2}
            for name, level in asked.items():
                if levels.get(level, 9) > levels.get(granted.get(name, "none"), 0):
                    return 422, {"message": f"The permissions requested are not granted to this installation ({name})"}
            names = [str(r).split("/")[-1] for r in (body.get("repositories") or [])]
            known = {r.split("/")[1]: r for r in REPOS}
            if any(n not in known for n in names):
                return 422, {"message": "There is at least one repository that does not exist or is not accessible"}
            tid = "tok" + secrets.token_hex(4)
            value = "ghs_" + secrets.token_urlsafe(27)
            tok = {"id": tid, "value": value, "repositories": [known[n] for n in names] or list(REPOS),
                   "permissions": asked, "revoked": False, "issued_at": time.time(), "uses": 0}
            with APPS.lock:
                APPS.data["tokens"][tid] = tok
                APPS.save()
            return 201, {"token": value, "expires_at": (_dt.datetime.now(_dt.timezone.utc)
                                                         + _dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "permissions": asked, "repository_selection": "selected",
                         "repositories": [{"full_name": r, "name": r.split("/")[1]} for r in tok["repositories"]]}
        return 404, {"message": "Not Found"}
    if m.startswith("/users/") and method == "GET" and m.count("/") == 2:
        login = m.split("/")[2]
        if login.lower() == OWNER.lower():
            return 200, {"login": OWNER, "type": ACCOUNT_TYPE}
        if login.lower() == USER_LOGIN.lower():
            return 200, {"login": USER_LOGIN, "type": "User"}
        return 404, {"message": "Not Found"}
    if m == "/user" and auth.startswith("Bearer ghu_"):
        raw = auth[7:].strip()
        if any(secrets.compare_digest(u["value"], raw) for u in APPS.data.get("user_tokens", [])):
            return 200, {"login": USER_LOGIN, "id": 4242, "type": "User"}
        return 401, {"message": "Bad credentials"}
    if m.startswith("/applications/") and m.endswith("/grant") and method == "DELETE":
        return 204, b""
    if m in ("/installation/repositories", "/installation/token"):
        tok = _token_of(auth)
        if tok is None or tok["revoked"]:
            return 401, {"message": "Bad credentials"}
        if m == "/installation/token" and method == "DELETE":
            with APPS.lock:
                tok["revoked"] = True
                tok["revoked_at"] = time.time()
                APPS.save()
            return 204, b""
        repos = [{"full_name": r, "name": r.split("/")[1], "private": True, "default_branch": "main",
                  "owner": {"login": r.split("/")[0], "type": ACCOUNT_TYPE}} for r in tok["repositories"]]
        return 200, {"total_count": len(repos), "repositories": repos}
    return None


# ---- the browser half (github.com pages) -----------------------------------------------------------

def _page(title: str, inner: str) -> str:
    return ("<!doctype html><html><head><meta charset=utf-8><title>" + escape(title) + " · fake GitHub</title>"
            "<style>body{font:15px system-ui;margin:0;background:#f6f8fa}header{background:#24292f;color:#fff;"
            "padding:14px 24px;font-weight:600}main{max-width:640px;margin:32px auto;background:#fff;border:1px "
            "solid #d0d7de;border-radius:8px;padding:24px}button{background:#1f883d;color:#fff;border:0;"
            "border-radius:6px;padding:8px 16px;font-size:15px}code{background:#eff1f3;padding:2px 4px}"
            ".warn{color:#9a6700;font-size:13px}</style></head><body><header>GitHub (BOSS e2e fake)</header><main>"
            + inner + "<p class=warn>This is the e2e harness's fake GitHub — no request leaves this machine.</p>"
            "</main></body></html>")


def browser_page(method: str, path: str, query: str, raw: bytes) -> Optional[tuple[int, Any, str, dict[str, str]]]:
    q = {k: v[0] for k, v in parse_qs(query).items()}
    form = {k: v[0] for k, v in parse_qs(raw.decode(errors="replace")).items()} if raw else {}
    html = "text/html; charset=utf-8"
    root = settings_root()
    if ("/settings/apps/" in path or "/settings/installations/" in path or path.endswith("/settings/apps/new")) \
            and not path.startswith(root + "/"):
        # The other account type's tree: GitHub answers 404 (the defect this fake exists to catch).
        return 404, _page("Page not found", f"<h2 data-testid=fake-gh-404>404 — not found</h2><p><code>{escape(path)}</code> "
                          f"is not a page for <b>{escape(OWNER)}</b> ({escape(ACCOUNT_TYPE)}).</p>"), html, {}
    app_now = APPS.data.get("app") or {}
    if app_now and path in (f"{root}/apps/{app_now['slug']}/permissions", f"{root}/apps/{app_now['slug']}"):
        if method == "POST":
            with APPS.lock:
                if APPS.data.get("accepted") is None:
                    APPS.data["accepted"] = dict(app_now.get("permissions") or {})
                APPS.data["app"]["permissions"] = {**(app_now.get("permissions") or {}), form.get("name", "workflows"):
                                                   form.get("level", "write")}
                APPS.save()
            return 302, b"", "", {"Location": path}
        perms = "".join(f"<li><code>{escape(k)}</code>: {escape(str(v))}</li>"
                        for k, v in sorted((app_now.get("permissions") or {}).items()))
        inner = (f"<h2>{escape(str(app_now['name']))} · Permissions &amp; events</h2><ul>{perms}</ul>"
                 f"<form method=post action='{escape(root)}/apps/{escape(app_now['slug'])}/permissions'>"
                 "<input type=hidden name=name value=workflows><input type=hidden name=level value=write>"
                 "<button data-testid=fake-gh-add-workflows type=submit>Workflows: Read and write · Save changes</button></form>")
        return 200, _page("App permissions", inner), html, {}
    if app_now and path == f"{root}/installations/{INSTALLATION_ID}" and APPS.data.get("installed"):
        pending = {k: v for k, v in (app_now.get("permissions") or {}).items() if _accepted().get(k) != v}
        if method == "POST":
            with APPS.lock:
                APPS.data["accepted"] = dict(app_now.get("permissions") or {})
                APPS.save()
            return 302, b"", "", {"Location": path}
        repos = "".join(f"<li><code>{escape(r)}</code></li>" for r in REPOS)
        ask = ("".join(f"<li><code>{escape(k)}</code>: {escape(str(v))}</li>" for k, v in sorted(pending.items()))
               if pending else "")
        inner = (f"<h2>{escape(str(app_now['name']))} on {escape(OWNER)}</h2><p>Repository access:</p><ul>{repos}</ul>"
                 + (f"<p>The App asks for new permissions:</p><ul>{ask}</ul><form method=post action='{escape(path)}'>"
                    "<button data-testid=fake-gh-accept type=submit>Accept new permissions</button></form>"
                    if pending else "<p data-testid=fake-gh-no-pending>No pending permission requests.</p>"))
        return 200, _page("Installation", inner), html, {}
    if path.endswith("/settings/apps/new") and method == "POST":
        try:
            manifest = json.loads(form.get("manifest") or "{}")
        except ValueError:
            return 422, _page("Invalid manifest", "<p>The manifest is not JSON.</p>"), html, {}
        perms = "".join(f"<li><code>{escape(k)}</code>: {escape(str(v))}</li>"
                        for k, v in sorted((manifest.get("default_permissions") or {}).items()))
        inner = (f"<h2>Create GitHub App for {escape(OWNER)}</h2><p>Name: <b data-testid=fake-gh-app-name>"
                 f"{escape(str(manifest.get('name')))}</b></p><p>Permissions:</p><ul>{perms}</ul>"
                 f"<p>Redirect: <code>{escape(str(manifest.get('redirect_url')))}</code></p>"
                 f"<form method=post action='{escape(root)}/apps/new/confirm?{escape(urlencode({'state': q.get('state', '')}))}'>"
                 f"<input type=hidden name=manifest value='{escape(json.dumps(manifest), quote=True)}'>"
                 "<button data-testid=fake-gh-create type=submit>Create GitHub App for "
                 f"{escape(OWNER)}</button></form>")
        return 200, _page("Create GitHub App", inner), html, {}
    if path.endswith("/settings/apps/new/confirm") and method == "POST":
        manifest = json.loads(form.get("manifest") or "{}")
        redirect = str(manifest.get("redirect_url") or "")
        if not redirect.startswith(("http://", "https://")):
            return 422, _page("No redirect", "<p>The manifest has no redirect_url.</p>"), html, {}
        code = "fxd" + secrets.token_hex(10)
        with APPS.lock:
            APPS.data["manifests"][code] = manifest
            APPS.save()
        return 302, b"", "", {"Location": f"{redirect}?{urlencode({'code': code, 'state': q.get('state', '')})}"}
    if path == "/login/oauth/authorize" and method == "GET":
        app = APPS.data.get("app")
        if not app or q.get("client_id") != app["client_id"]:
            return 404, _page("Not Found", "<p>Unknown OAuth client.</p>"), html, {}
        inner = (f"<h2>Authorize {escape(str(app['name']))}</h2><p>as <b>{escape(USER_LOGIN)}</b>. It will act on "
                 f"GitHub as you, within what you can do.</p><form method=post action='/login/oauth/authorize/confirm?"
                 f"{escape(urlencode({k: q.get(k, '') for k in ('client_id', 'redirect_uri', 'state')}))}'>"
                 "<button data-testid=fake-gh-authorize type=submit>Authorize</button></form>")
        return 200, _page("Authorize application", inner), html, {}
    if path == "/login/oauth/authorize/confirm" and method == "POST":
        code = "fxdu" + secrets.token_hex(10)
        with APPS.lock:
            APPS.data.setdefault("oauth_codes", {})[code] = {"client_id": q.get("client_id"),
                                                             "redirect_uri": q.get("redirect_uri")}
            APPS.save()
        return 302, b"", "", {"Location": f"{q.get('redirect_uri', '')}?{urlencode({'code': code, 'state': q.get('state', '')})}"}
    if path == "/login/oauth/access_token" and method == "POST":
        app = APPS.data.get("app") or {}
        if form.get("client_id") != app.get("client_id") or not secrets.compare_digest(
                str(form.get("client_secret") or ""), str(app.get("client_secret") or "-")):
            return 200, {"error": "incorrect_client_credentials"}, "application/json", {}
        with APPS.lock:
            if form.get("grant_type") == "refresh_token":
                ok = any(secrets.compare_digest(u["refresh"], str(form.get("refresh_token") or ""))
                         for u in APPS.data.setdefault("user_tokens", []) if not u.get("rotated"))
                if not ok:
                    return 200, {"error": "bad_refresh_token"}, "application/json", {}
            else:
                code = APPS.data.setdefault("oauth_codes", {}).pop(str(form.get("code") or ""), None)
                if code is None or code["redirect_uri"] != form.get("redirect_uri"):
                    return 200, {"error": "bad_verification_code"}, "application/json", {}
            tok = {"value": "ghu_" + secrets.token_urlsafe(27), "refresh": "ghr_" + secrets.token_urlsafe(40),
                   "issued_at": time.time()}
            APPS.data.setdefault("user_tokens", []).append(tok)
            APPS.save()
        return 200, {"access_token": tok["value"], "refresh_token": tok["refresh"], "expires_in": 28800,
                     "refresh_token_expires_in": 15897600, "scope": "", "token_type": "bearer"}, "application/json", {}
    parts = path.strip("/").split("/")
    if len(parts) == 4 and parts[0] == "apps" and parts[2] == "installations" and parts[3] in ("new", "confirm"):
        app = APPS.data.get("app")
        if not app or app["slug"] != parts[1]:
            return 404, _page("Not Found", "<p>No such App.</p>"), html, {}
        if parts[3] == "new":
            repos = "".join(f"<li><code>{escape(r)}</code></li>" for r in REPOS)
            inner = (f"<h2>Install {escape(str(app['name']))}</h2><p>on <b>{escape(OWNER)}</b>, for these "
                     f"repositories only:</p><ul>{repos}</ul>"
                     f"<form method=post action='/apps/{escape(app['slug'])}/installations/confirm?"
                     f"{escape(urlencode({'state': q.get('state', '')}))}'>"
                     "<button data-testid=fake-gh-install type=submit>Install</button></form>")
            return 200, _page("Install App", inner), html, {}
        with APPS.lock:
            APPS.data["installed"] = True
            APPS.save()
        setup = str(app.get("setup_url") or "")
        return 302, b"", "", {"Location": f"{setup}?{urlencode({'installation_id': INSTALLATION_ID, 'setup_action': 'install', 'state': q.get('state', '')})}"}
    return None


# ---- the handler: the stubs' proxy, plus the App and the pages -------------------------------------

class World(fakes.World):
    def record(self, entry: dict[str, Any]) -> None:
        extra = getattr(_LOCAL, "extra", None) or {}
        super().record({**entry, **extra})


Base = fakes.Handler


class Handler(Base):
    def setup(self) -> None:
        super().setup()
        sni = getattr(self.request, "_fxd_sni", None)
        if sni:                      # the direct-TLS listener: the SNI name is the host
            self._tunnel_host = sni
            self._direct = True

    def parse_request(self) -> bool:
        ok = super().parse_request()
        _LOCAL.extra = {"client": self.client_address[0],
                        "via": "direct-tls" if getattr(self, "_direct", False) else
                        ("proxy" if self._tunnel_host or str(getattr(self, "path", "")).startswith("http")
                         else "local")}
        return ok

    def _dispatch(self, method: str, path: str, body: Any) -> tuple[int, Any, str]:
        if path == "/_github_app":
            return 200, {**APPS.public_view(), "account": {"login": OWNER, "type": ACCOUNT_TYPE}, "repos": REPOS,
                         "accepted": _accepted() if APPS.data.get("app") else None}, "application/json"
        if path == "/_github_account" and method == "POST":
            b = body if isinstance(body, dict) else {}
            configure(str(b.get("login") or OWNER), str(b.get("type") or "Organization"),
                      [str(r) for r in (b.get("repos") or [])])
            bound = type(self)
            for full in REPOS:
                bound.git.ensure(*full.split("/"))
            return 200, {"account": {"login": OWNER, "type": ACCOUNT_TYPE}, "repos": REPOS}, "application/json"
        return super()._dispatch(method, path, body)

    def _egress(self, method: str, host: str, path: str, query: str, raw: bytes) -> None:
        h = host.split(":", 1)[0].lower()
        auth = self.headers.get("Authorization") or ""
        kind = auth.split(" ", 1)[0].lower() if auth else "none"
        if kind == "basic":
            try:
                user, _, pw = base64.b64decode(auth.split(" ", 1)[1]).decode().partition(":")
                kind = "basic:" + ("ghs" if pw.startswith("ghs_") else "other")
            except Exception:  # noqa: BLE001
                kind = "basic:?"
        _LOCAL.extra = {**(getattr(_LOCAL, "extra", None) or {}), "auth": kind}
        if h in ("github.com", "www.github.com"):
            page = browser_page(method, path, query, raw)
            if page is not None:
                status, payload, ctype, headers = page
                self.world.record({"egress": host, "method": method, "path": path, "route": "github-web",
                                   "status": status})
                self._send(status, payload, ctype, headers)
                return
            if kind.startswith("basic:ghs"):
                tok = _token_of("x " + base64.b64decode(auth.split(" ", 1)[1]).decode().partition(":")[2])
                if tok is None or tok["revoked"]:
                    self.world.record({"egress": host, "method": method, "path": path, "route": "git",
                                       "status": 401, "refused": "revoked or unknown installation token"})
                    self._send(401, b"", "text/plain", {"WWW-Authenticate": 'Basic realm="GitHub"'})
                    return
                tok["uses"] = tok.get("uses", 0) + 1
        if h == "api.github.com":
            body = self._json(raw)
            answered = app_api(method, path, auth, body)
            if answered is None and kind == "bearer" and auth[7:].startswith("ghs_"):
                tok = _token_of(auth)
                if tok is None or tok["revoked"]:
                    answered = (401, {"message": "Bad credentials"})
                else:
                    tok["uses"] = tok.get("uses", 0) + 1
            if answered is not None:
                status, payload = answered
                self.world.record({"egress": host, "method": method, "path": path, "route": "github-app",
                                   "status": status})
                self._send(status, payload, "application/json" if status != 204 else "")
                return
        super()._egress(method, host, path, query, raw)


class DirectTLSServer(ThreadingHTTPServer):
    """:443 for clients that resolve api.github.com / github.com themselves (no proxy)."""

    daemon_threads = True

    def __init__(self, addr, handler, ca: PersistentCA):
        super().__init__(addr, handler)
        self.ca = ca
        ca.context_for("api.github.com")
        self.default = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.default.load_cert_chain(str(ca.directory / "leaf-api.github.com.pem"))
        self.default.set_alpn_protocols(["http/1.1"])

        def pick(sock: ssl.SSLSocket, name: Optional[str], _ctx: ssl.SSLContext) -> None:
            name = (name or "api.github.com").lower()
            sock._fxd_sni = name  # type: ignore[attr-defined]
            sock.context = ca.context_for(name)
        self.default.sni_callback = pick

    def finish_request(self, request, client_address) -> None:  # type: ignore[override]
        try:
            tls = self.default.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return
        if not getattr(tls, "_fxd_sni", None):
            tls._fxd_sni = "api.github.com"  # type: ignore[attr-defined]
        self.RequestHandlerClass(tls, client_address, self)


def main() -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    fakes.CertificateAuthority = PersistentCA
    fakes.World = World
    fakes.Handler = Handler
    http, smtp, world = fakes.make_servers(int(os.environ.get("FAKEGH_PORT", "8080")), 0, STATE, "0.0.0.0")
    bound = http.RequestHandlerClass
    for full in REPOS:
        bound.git.ensure(*full.split("/"))
    CA_OUT.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(bound.ca.ca_path, CA_OUT / "ca.pem")
    os.chmod(CA_OUT / "ca.pem", 0o644)
    direct = DirectTLSServer(("0.0.0.0", int(os.environ.get("FAKEGH_TLS_PORT", "443"))), bound, bound.ca)
    threading.Thread(target=direct.serve_forever, daemon=True).start()
    threading.Thread(target=smtp.serve_forever, daemon=True).start()
    print(f"fakegh: proxy+fake :{http.server_address[1]}, direct TLS :{direct.server_address[1]}, repos {REPOS}, "
          f"CA {CA_OUT / 'ca.pem'}", flush=True)
    http.serve_forever()


if __name__ == "__main__":
    socket.setdefaulttimeout(None)
    main()
