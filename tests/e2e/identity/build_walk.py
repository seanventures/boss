"""THE CONSOLE BUILD, END TO END, IN ENFORCE MODE, ON A REAL COMPOSE STACK — every outside service faked.

A real browser drives the real console as the seeded Owner (seed-owner):

  1. Setup › GitHub access › "Create the App on GitHub": the manifest form goes to (fake) GitHub,
     the App is created, BOSS converts the code, the App is installed, BOSS keeps the installation;
  2. Setup › Repository: the App's repository is picked and registered;
  3. "+ New build" › Fast fix: the first start answers 409 consent_required and opens the consent
     sheet; the Owner grants (TOTP step-up when asked);
  4. the build runs: a run token bound to the build, the agent under a per-run sandbox uid
     (watched in the api container's /proc), git through boss-egress with no credential in any
     clone URL, a push to the run's branch only (a push to main from inside the run is refused);
  5. a PR is opened on the fake GitHub (with BOSS's commit trailers when implemented) and the
     Factory shows the build and its PR;
  6. the fake's egress log shows no other host reached, and the audit log has the run's
     delegation and token events.

The stack must run with tests/e2e/compose/build-override.yml (the `fakegh` service, the proxy, the
fake CA, the scripted model and the stub `claude`). See that file for how it is wired.

    cd tests && python3 -m e2e.identity.build_walk --stack-dir <dir with stack.env + accounts.json> \\
        --shots <dir> [--report <json>] [--headed]

Exit status 0 when no check failed (xfails allowed). Never prints a secret.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from e2e.harness import identity as I  # noqa: E402
from e2e.identity import kit as K  # noqa: E402
from e2e.identity.kit import Journey, api, audit_events, short  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
OVERRIDE = REPO_ROOT / "tests" / "e2e" / "compose" / "build-override.yml"
APP_OWNER = "fxd-org"
REPO = "fxd-org/fxd-build-app"
PROJECT = "boss-dogfood"
WATCH_IN_API = "/fxd/tests/e2e/compose/sandbox_watch.py"


class Walk:
    def __init__(self, args: argparse.Namespace, browser: Any):
        self.args = args
        self.browser = browser
        self.web = args.console.rstrip("/")
        self.fake = args.fake.rstrip("/")
        sd = Path(args.stack_dir)
        self.stack = K.Stack(project=args.project, web=self.web, api=args.api.rstrip("/"), env_file=sd / "stack.env")
        self.A = I.load_accounts(sd / "accounts.json")
        for v in self.A.values():
            for attr in ("password", "totp_secret", "key"):
                K.secret(getattr(v, attr, None))
        self.owner = self.A["owner"]
        self.j = Journey("build", "The console build in enforce mode, every outside service faked",
                         "GitHub App connect → repo → Fast fix → consent → sandboxed build via egress → PR → Factory",
                         Path(args.shots).parent if Path(args.shots).name == "build" else Path(args.shots))
        self.state: dict[str, Any] = {"stamp": time.strftime("%Y%m%d-%H%M%S")}

    # ---- plumbing ------------------------------------------------------------------------------
    def fake_get(self, path: str) -> Any:
        with urllib.request.urlopen(self.fake + path, timeout=20) as r:
            return json.loads(r.read() or b"null")

    def in_container(self, service: str, argv: list[str], *, user: str = "root", detach: bool = False,
                     timeout: int = 120) -> subprocess.CompletedProcess:
        cmd = ["docker", "exec"] + (["-d"] if detach else []) + ["-u", user, f"{self.args.project}-{service}-1", *argv]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def psql(self, sql: str, **params: str) -> str:
        return self.stack.psql(sql, params or None)

    def shot(self, page: Any, name: str) -> None:
        self.j.shot(page, name)

    def page(self) -> tuple[Any, Any]:
        mine = self.j.shots / ".owner.storage.json"          # this walk's own saved session (0600)
        ctx = self.browser.new_context(viewport={"width": 1360, "height": 900}, ignore_https_errors=True)
        page = ctx.new_page()
        page.on("response", lambda r: (r.status >= 500 and "/v1/" in r.url)
                and self.j.note(f"HTTP {r.status} {r.request.method} {r.url.split('?')[0][-90:]}"))
        for attempt in range(4):          # other walks sign in seed-owner too: a TOTP step can be spent
            try:
                K.sign_in(page, self.web, self.owner)
                ctx.storage_state(path=str(mine))
                os.chmod(mine, 0o600)
                break
            except I.IdentitySetupError:
                if attempt == 3:
                    ctx.close()
                    ctx = None
                    break
                time.sleep(35)
        if ctx is not None:
            return ctx, page
        for saved in (mine, Path(self.args.stack_dir) / "owner.storage.json"):
            if not saved.exists():
                continue                 # a live seed-owner session: no TOTP race with the other walks
            ctx = self.browser.new_context(viewport={"width": 1360, "height": 900}, ignore_https_errors=True,
                                           storage_state=str(saved))
            page = ctx.new_page()
            page.goto(f"{self.web}/identity", wait_until="domcontentloaded", timeout=90000)
            if K.me(page)["status"] == 200:
                self.j.note(f"using a saved seed-owner session ({saved.name})")
                page.on("response", lambda r: (r.status >= 500 and "/v1/" in r.url)
                        and self.j.note(f"HTTP {r.status} {r.request.method} {r.url.split('?')[0][-90:]}"))
                return ctx, page
            ctx.close()
        raise I.IdentitySetupError("owner: no fresh sign-in (MFA budget or TOTP race) and no live saved session")

    # ---- 0. the stack is the faked one --------------------------------------------------------------
    def preflight(self, page: Any) -> None:
        j = self.j
        health = self.fake_get("/health")
        j.check("the fake GitHub (fakegh) answers", bool(health and health.get("ok")), expect="fakegh /health ok",
                actual=health, fatal=True)
        env = self.in_container("api", ["sh", "-c", "for k in HTTPS_PROXY SSL_CERT_FILE BOSS_CLAUDE_CLI_PATH "
                                        "BOSS_AGENT_ENV_ALLOW BOSS_OUTBOUND_ALLOW_PRIVATE BOSS_AUTHZ_MODE "
                                        "BOSS_GITHUB_API_URL BOSS_GITHUB_GIT_URL BOSS_GITHUB_APP_ID; do "
                                        "eval v=\\${$k:-}; echo \"$k=$v\"; done"])
        got = dict(line.split("=", 1) for line in env.stdout.splitlines() if "=" in line)
        j.check("the api runs enforce mode with the fake world (proxy, fake CA, stub claude) and NO GitHub URL override",
                got.get("BOSS_AUTHZ_MODE") == "enforce" and got.get("HTTPS_PROXY", "").startswith("http://fakegh")
                and got.get("SSL_CERT_FILE") == "/e2e-ca/ca.pem" and got.get("BOSS_CLAUDE_CLI_PATH", "").endswith("/stubs/bin/claude")
                and not got.get("BOSS_GITHUB_API_URL") and not got.get("BOSS_GITHUB_GIT_URL") and not got.get("BOSS_GITHUB_APP_ID"),
                expect="enforce, HTTPS_PROXY=fakegh, SSL_CERT_FILE=/e2e-ca/ca.pem, stub claude, no BOSS_GITHUB_*_URL/APP_ID",
                actual=got, fatal=True)
        stub = self.in_container("api", ["python", "-c", "import urllib.request;print(urllib.request.urlopen("
                                         "'http://127.0.0.1:58700/health',timeout=3).status)"], user="boss")
        j.check("the scripted model answers on the api container's loopback", stub.stdout.strip() == "200",
                expect="200", actual=stub.stdout.strip() or stub.stderr[-200:], fatal=True)
        self.state["log_start"] = len(self.fake_get("/_log")["entries"])
        self.state["audit_seq0"] = K.audit_seq(page)

    # ---- 1. connect the GitHub App ------------------------------------------------------------------
    def connect_app(self, page: Any) -> None:
        j = self.j
        status = api(page, "GET", "/v1/identity/github")
        if (status.get("body") or {}).get("app", {}).get("configured"):
            j.note("an App is already connected (an earlier walk): disconnecting it to walk the connect again")
            r = K.stepped(page, self.owner, "DELETE", "/v1/identity/github/app", None, j)
            j.check("an earlier walk's App disconnects", r["status"] in (200, 204), expect="200/204",
                    actual=(r["status"], short(r.get("body"))))
        page.goto(f"{self.web}/?area=setup&focus=repository", wait_until="domcontentloaded", timeout=90000)
        K.settle(page)
        row = page.locator("[data-testid=su-row-github-access]")
        row.wait_for(timeout=20000)
        row.scroll_into_view_if_needed()
        self.shot(page, "setup-github-access-before")
        before = page.locator("[data-testid=gh-mode]").get_attribute("data-mode")
        j.check("Setup › GitHub access is not 'app' before the App is connected (none, or as_me when a person "
                "connected their own GitHub in an earlier walk)", before in ("none", "as_me"),
                expect="data-mode none|as_me", actual=before)
        page.fill("[data-testid=gh-app-owner]", APP_OWNER)
        csp: list[str] = []
        page.on("console", lambda m: m.type == "error" and "Content Security Policy" in m.text and csp.append(m.text))
        with page.expect_response(lambda r: "/v1/identity/github/app/manifest" in r.url, timeout=20000) as mr:
            page.click("[data-testid=gh-app-create]")
        resp = mr.value
        if resp.status == 401:
            K.answer_step_up_sheet(page, self.owner)
        j.check("POST /v1/identity/github/app/manifest answers 200 (or after a step-up)", resp.status in (200, 401),
                expect="200", actual=resp.status)
        out = resp.json() if resp.status == 200 else {}
        left = True
        try:
            page.wait_for_url(re.compile(r"https://github\.com/.*settings/apps/new"), timeout=10000)
        except Exception:  # noqa: BLE001
            left = False
        j.check("'Create the App on GitHub' takes the browser to GitHub with the manifest form", left,
                expect="the console's form POST reaches github.com/…/settings/apps/new",
                actual=("blocked by the console's own CSP: " + re.sub(r"\?state=[^']*", "?state=…", csp[0])[:300]) if csp else page.url,
                repro="Setup › GitHub access › GitHub org 'fxd-org' › Create the App on GitHub: nothing happens; the "
                      "devtools console shows \"Sending form data to 'https://github.com/…/settings/apps/new' violates "
                      "… form-action 'self'\" (apps/web CSP form-action 'self'; the manifest flow POSTs a form to GitHub)")
        if not left:
            # WORKAROUND (recorded above as a failure, not hidden): do in this browser exactly what the
            # console's postManifest() does — a form POST of `manifest` to `post_url` — from a page that
            # carries no CSP, so the rest of the flow (GitHub → BOSS callbacks) is still walked for real.
            j.note("continuing past the CSP block: the same form POST from a CSP-free page (workaround)")
            self.state["workaround_manifest_form"] = True
            html = ("<!doctype html><form id=f method=post action='" + out["post_url"].replace("'", "&#39;") + "'>"
                    "<input type=hidden name=manifest></form><script>document.querySelector('[name=manifest]').value="
                    + json.dumps(json.dumps(out["manifest"])) + ";document.getElementById('f').submit()</script>")
            page.route("https://github.com/__fxd_manifest_relay", lambda route: route.fulfill(
                status=200, content_type="text/html", body=html))
            page.goto("https://github.com/__fxd_manifest_relay")
            page.wait_for_url(re.compile(r"https://github\.com/.*settings/apps/new"), timeout=30000)
        page.wait_for_selector("[data-testid=fake-gh-create]", timeout=20000)
        self.shot(page, "github-create-app-from-manifest")
        name = page.inner_text("[data-testid=fake-gh-app-name]")
        j.check("the manifest reached GitHub's form with BOSS's App name and the org path",
                name.startswith("BOSS ") and f"/organizations/{APP_OWNER}/" in page.url,
                expect=f"name 'BOSS …', /organizations/{APP_OWNER}/settings/apps/new", actual=(name, page.url.split("?")[0]))
        page.click("[data-testid=fake-gh-create]")
        # GitHub → /v1/identity/github/app/created (BOSS converts the code) → GitHub's install page
        page.wait_for_selector("[data-testid=fake-gh-install]", timeout=45000)
        self.shot(page, "github-install-app")
        page.click("[data-testid=fake-gh-install]")
        page.wait_for_url(re.compile(re.escape(self.web) + r"/.*github="), timeout=45000)
        landed = page.url
        K.settle(page)
        self.shot(page, "setup-after-github-redirect")
        j.check("GitHub's install redirect lands back on Setup with github=app_connected", "github=app_connected" in landed,
                expect="?github=app_connected", actual=landed.replace(self.web, ""),
                repro="Setup › GitHub access › org fxd-org › Create the App on GitHub › Create › Install")
        page.goto(f"{self.web}/?area=setup&focus=repository", wait_until="domcontentloaded", timeout=90000)
        K.settle(page)
        page.locator("[data-testid=su-row-github-access]").scroll_into_view_if_needed()
        page.wait_for_timeout(1200)
        self.shot(page, "setup-github-app-connected")
        mode = page.locator("[data-testid=gh-mode]")
        j.check("the console shows the App connected with its installation (pill green, mode app, @fxd-org)",
                mode.get_attribute("data-mode") == "app" and APP_OWNER in mode.inner_text(),
                expect="data-mode=app, label 'GitHub App · @fxd-org'", actual=(mode.get_attribute("data-mode"), mode.inner_text()))
        st = (api(page, "GET", "/v1/identity/github").get("body") or {}).get("app") or {}
        j.check("/v1/identity/github: configured, installed, account fxd-org, source setup",
                st.get("configured") and st.get("installed") and st.get("account") == APP_OWNER and st.get("source") == "setup",
                expect="configured+installed, account fxd-org, source setup", actual=st)
        fake = self.fake_get("/_github_app")
        j.check("the fake GitHub has the App created and installed (BOSS's App JWT verified on every App call)",
                bool(fake.get("app")) and fake.get("installed"), expect="app + installed", actual=fake.get("app"))
        self.state["app"] = fake.get("app")
        inst = api(page, "GET", "/v1/identity/github/app/installations")
        j.check("the installation picker lists the installation (App JWT accepted by GitHub)",
                inst["status"] == 200 and any(i.get("account") == APP_OWNER for i in (inst.get("body") or {}).get("installations", [])),
                expect="200 with fxd-org", actual=(inst["status"], short(inst.get("body"))))
        for step in ("created", "installed"):
            ev = next((e for e in audit_events(page, event="vault.connection.linked", limit="50")
                       if ((e.get("raw") or {}).get("details") or {}).get("step") == step), None)
            j.check(f"audit: vault.connection.linked step={step}", ev is not None, expect="an event", actual=ev)

    # ---- 2. register the repository -----------------------------------------------------------------
    def register_repo(self, page: Any) -> None:
        j = self.j
        page.goto(f"{self.web}/?area=setup&focus=repository", wait_until="domcontentloaded", timeout=90000)
        K.settle(page)
        self.shot(page, "setup-repository-before")
        page.wait_for_timeout(1500)
        head = K.body_text(page, 1500)
        if REPO in head and not page.locator(f"[data-testid='su-repo-option-{REPO}']").count() \
                and not page.locator("[data-testid=su-use-app]").count():
            j.note("the repository is already registered (an earlier walk); not registering it twice")
            j.check("Setup › Repository shows fxd-org/fxd-build-app connected", "Working" in head or "Ready" in head,
                    expect=f"{REPO} · Working", actual=head[:300])
            return
        use_app = page.locator("[data-testid=su-use-app]")
        if use_app.count():
            use_app.first.click()
            page.wait_for_timeout(1500)
        opt = page.locator(f"[data-testid='su-repo-option-{REPO}']")
        opt.first.wait_for(timeout=20000)
        self.shot(page, "setup-repository-picker")
        opt.first.click()
        with page.expect_response(lambda r: ("/api/setup" in r.url or "/v1/" in r.url)
                                  and r.request.method in ("POST", "PUT", "PATCH"), timeout=120000) as rr:
            page.click("[data-testid=su-repo-connect]")
        j.note(f"repo connect answered {rr.value.status} {rr.value.url.split('?')[0][-60:]}")
        page.wait_for_timeout(2500)
        K.settle(page)
        self.shot(page, "setup-repository-registered")
        txt = K.body_text(page, 6000)
        j.check("Setup › Repository shows fxd-org/fxd-build-app connected", REPO in txt, expect=REPO, actual=txt[:400])
        rows = self.psql("SELECT count(*) FROM github_repository_bindings WHERE lower(owner || '/' || repo) = :'r'", r=REPO) \
            if self._has_cols("github_repository_bindings", ("owner", "repo")) else ""
        j.note(f"github_repository_bindings rows for the repo: {rows or 'n/a'}")

    def _has_cols(self, table: str, cols: tuple[str, ...]) -> bool:
        got = set(self.psql("SELECT column_name FROM information_schema.columns WHERE table_name = :'t'", t=table).split())
        return all(c in got for c in cols)

    # ---- 3. Fast fix + consent ----------------------------------------------------------------------
    def fast_fix(self, page: Any) -> None:
        j = self.j
        title = f"fxd-build-{self.state['stamp']}: add e2e_feature (one agent does both)"
        self.state["title"] = title
        page.goto(f"{self.web}/?area=workflows", wait_until="domcontentloaded", timeout=90000)
        K.settle(page)
        opener = page.locator("[data-testid=dw-composer-open]")
        page.wait_for_selector("[data-testid=dw-composer-open], [data-testid=np-mode]", timeout=90000)
        if opener.count():
            opener.first.click()
        page.click("[data-testid=np-mode-new]", timeout=30000)
        page.click("[data-testid=np-path-fast_fix]", timeout=30000)
        page.wait_for_timeout(800)
        page.locator("textarea").first.fill(title)
        anyway = page.get_by_text(re.compile("Start anyway"))
        if anyway.count():
            anyway.first.click()
        self.shot(page, "fast-fix-form")
        seen: list[tuple[int, str, Any]] = []

        def capture(r: Any) -> None:
            if r.request.method in ("POST", "PUT") and r.url.startswith(self.web) and "/_next/" not in r.url:
                try:
                    body = r.json()
                except Exception:  # noqa: BLE001
                    body = None
                seen.append((r.status, r.url.split("?")[0], body))
        page.on("response", capture)
        page.get_by_role("button", name=re.compile(r"^Start( the project)?$")).last.click()
        page.wait_for_selector("[data-testid=consent-durations]", timeout=30000)
        page.wait_for_timeout(800)
        self.shot(page, "consent-sheet")
        first409 = next(((s, u, b) for s, u, b in seen if s == 409), None)
        j.check("the first start answers 409 consent_required", bool(first409) and "consent" in json.dumps(first409[2] or {}),
                expect="409 {error: consent_required…}", actual=[(s, u[-50:], short(b, 200)) for s, u, b in seen])
        offers = [b.inner_text() for b in page.locator("[data-testid=consent-durations] button").all()]
        j.note(f"consent offers: {offers}")
        sheet = K.body_text(page, 5000)
        j.check("the consent sheet names the repository the grant covers", REPO in sheet or "fxd-build-app" in sheet,
                expect=REPO, actual=sheet[:600], known=None)
        n_before = len(seen)
        self.consent_click(page)
        after = seen[n_before:]
        j.note("after consent: " + "; ".join(f"{s_} {u[-45:]} {short(b, 160)}" for s_, u, b in after))
        lacking = next((b for s_, u, b in after if s_ == 403 and "github_access_lacking" in json.dumps(b or {})), None)
        if lacking is not None:
            # D28: on a multi-person install a grant naming repos needs the PERSON's own GitHub access.
            self.shot(page, "consent-refused-github-access")
            sheet = K.body_text(page, 5000)
            j.check("a consent refused for lack of GitHub access says so (and how to fix it) in the sheet",
                    "GitHub" in sheet.split("For 30 days", 1)[-1] and "didn't work" not in sheet,
                    expect="an explanation: connect your GitHub / you lack push on fxd-org/fxd-build-app",
                    actual=sheet[sheet.find("For this run only"):][:240],
                    known="FX-C P1-12 (real error states, no raw codes): the sheet shows only 'That didn't work'",
                    repro="seed-owner (no own GitHub connection) › New project › Fast fix › Start › For this run only: "
                          "POST /v1/identity/me/delegations answers 403 github_access_lacking; the sheet says 'That didn't work'")
            if self.my_github_connected():
                j.note("seed-owner's own GitHub is already connected (an earlier walk): the refusal above is the "
                       "guard's cached 'no' from before that connection")
            else:
                self.connect_my_github(page.context)
            n_before = len(seen)
            self.consent_click(page)
            after = seen[n_before:]
            j.note("after consent (2nd, with my GitHub connected): " + "; ".join(
                f"{s_} {u[-45:]} {short(b, 160)}" for s_, u, b in after))
            still = any(s_ == 403 and "github_access_lacking" in json.dumps(b or {}) for s_, u, b in after)
            j.check("once the person connects their GitHub, the next consent is checked afresh (not a cached 'no')",
                    not still, expect="POST /v1/identity/me/delegations → 201 right after connecting",
                    actual="403 github_access_lacking again; no GET /repos/{repo} with the person's token reached GitHub",
                    repro="engine/boss_engine/identity/grants.py check_user_repo_access caches a NEGATIVE result for "
                          "GUARD_TTL_SECONDS=900 in _GUARD_CACHE and nothing calls clear_guard_cache() when a person "
                          "connects GitHub: consent → 403 github_access_lacking → My account › Agents › Connect GitHub "
                          "(succeeds) → consent again → still 403 for up to 15 minutes (unassigned)")
            waited = 0
            while still and waited < self.args.guard_wait:
                j.note(f"waiting for the cached refusal to expire ({waited}s of up to {self.args.guard_wait}s)")
                time.sleep(60)
                waited += 60
                n_before = len(seen)
                self.consent_click(page)
                after = seen[n_before:]
                still = any(s_ == 403 and "github_access_lacking" in json.dumps(b or {}) for s_, u, b in after)
        created = [b for s_, u, b in after if s_ in (200, 201) and u.endswith("/v1/identity/me/delegations")]
        self.state["grant_created"] = bool(created)
        j.check("the consent POST creates the grant (201)", bool(created), expect="POST /v1/identity/me/delegations → 200/201",
                actual=[(s_, u[-45:]) for s_, u, _ in after])
        self.shot(page, "build-started")
        grants = (api(page, "GET", "/v1/identity/me/delegations").get("body") or {})
        items = grants.get("grants") or grants.get("delegations") or grants.get("items") or (grants if isinstance(grants, list) else [])
        mine = [g for g in items if isinstance(g, dict) and g.get("status", "active") == "active"]
        self.state["grant_ids"] = [g.get("id") for g in mine]
        j.check("a delegation grant exists for seed-owner after consent", bool(mine), expect="≥1 active grant",
                actual=short(grants, 500))
        page.remove_listener("response", capture)

    def builder_check(self) -> None:
        """A governed build with no role_key is wired to DEFAULT_BUILDER 'forge' (identity/runs.py
        start_code_task_run, consent.DEFAULT_BUILDER); read-only check that such a builder exists."""
        # the same rule as identity.runs.agent_for_role: a role key, or the persona callsign the
        # presets and DEFAULT_BUILDER use ('forge' is the shipped software_engineer)
        forge = self.psql("SELECT coalesce(r.agent_id,'') FROM roles r JOIN identity_agent_passports p "
                          "ON p.agent_id = r.agent_id WHERE (r.role_key = 'forge' OR "
                          "lower(r.spec->'persona'->>'callsign') = 'forge') LIMIT 1")
        self.j.check("the Fast fix build's builder resolves to an agent with a Passport", bool(forge),
                     expect="a 'forge' role with a Passport (the default builder a Fast fix build is wired to), or a "
                            "build step that names its role",
                     actual="no 'forge' role in the project's roster: the code task fails 'authority refused: "
                            "builder_has_no_passport' and the run stops at Build",
                     repro="enforce stack seeded by the installer › New project › Fast fix › consent (grant for "
                           "engineering_captain, program_manager, deployment_manager) › the code.task job deadletters: "
                           "governed_code_task_service → identity.runs.start_code_task_run(role_key=None) → "
                           "agent_for_role(.., 'forge') → None → NoAuthority('builder_has_no_passport') (unassigned; FX-B area)")

    def consent_click(self, page: Any) -> None:
        page.locator("[data-testid=consent-durations] button").first.click()
        page.wait_for_timeout(2500)
        if K.code_box(page).count():
            self.shot(page, "consent-step-up")
            K.answer_step_up_sheet(page, self.owner)
        page.wait_for_timeout(5000)
        K.settle(page)

    def my_github_connected(self) -> bool:
        return self.psql("SELECT count(*) FROM identity_vault_items WHERE kind = 'oauth_user' AND provider = 'github' "
                         "AND status = 'active' AND owner_principal_id = :'p'", p=self.owner.principal_id) not in ("", "0")

    def connect_my_github(self, ctx: Any) -> None:
        """My account › Agents › Connect GitHub: the person's own GitHub (user-to-server OAuth through
        the App made in Setup), so the D28 guard can check THEIR access to the repo."""
        j = self.j
        page = ctx.new_page()
        page.goto(f"{self.web}/account/agents", wait_until="domcontentloaded", timeout=90000)
        K.settle(page)
        page.wait_for_selector("[data-testid=connect-github]", timeout=60000)
        self.shot(page, "my-github-before")
        page.click("[data-testid=connect-github]")
        page.wait_for_timeout(1500)
        if K.code_box(page).count():
            K.answer_step_up_sheet(page, self.owner)
        confirm = page.get_by_role("button", name=re.compile(r"^(Connect|Continue|Yes)", re.I))
        if not page.url.startswith("https://github.com") and confirm.count():
            confirm.last.click()
        page.wait_for_selector("[data-testid=fake-gh-authorize]", timeout=45000)
        self.shot(page, "github-authorize-as-me")
        page.click("[data-testid=fake-gh-authorize]")
        page.wait_for_url(re.compile(re.escape(self.web) + r"/account/agents\?"), timeout=45000)
        landed = page.url
        K.settle(page)
        self.shot(page, "my-github-connected")
        j.check("My account › Agents › Connect GitHub lands back connected", "connected=github" in landed,
                expect="/account/agents?connected=github", actual=landed.replace(self.web, ""))
        page.close()

    # ---- 4. the run ---------------------------------------------------------------------------------
    def find_run(self) -> Optional[dict[str, str]]:
        row = self.psql(
            "SELECT id || '|' || coalesce(on_behalf_of,'') || '|' || coalesce(grant_id,'') || '|' || status "
            "FROM workflow_runs WHERE created_at > now() - interval '30 minutes' "
            "AND input::text ILIKE :'t' ORDER BY created_at DESC LIMIT 1", t=f"%{self.state['stamp']}%")
        if not row:
            return None
        rid, who, grant, status = row.split("|")
        return {"run": rid, "on_behalf_of": who, "grant": grant, "status": status}

    def run_build(self, page: Any) -> None:
        j = self.j
        out = f"/tmp/fxd-watch-{self.state['stamp']}.json"
        self.state["watch"] = out
        run = K.wait_for(self.find_run, timeout_s=60, every_s=2)
        j.check("the Fast fix created a workflow run on behalf of seed-owner under the grant",
                bool(run) and run["on_behalf_of"] == self.owner.principal_id and bool(run["grant"]),
                expect=f"on_behalf_of={self.owner.principal_id}, grant set", actual=run)
        if not run:
            return
        self.state.update(run)
        grant = run["grant"]
        deadline = time.time() + self.args.build_timeout
        crun = ""
        while time.time() < deadline:
            crun = self.psql("SELECT id FROM code_task_runs WHERE created_at > now() - interval '40 minutes' AND "
                             "(title ILIKE :'t' OR spec::text ILIKE :'t') ORDER BY created_at DESC LIMIT 1",
                             t=f"%{self.state['stamp']}%")
            if crun:
                break
            time.sleep(2)
        self.state["code_task_run"] = crun
        j.note(f"run {run['run']} grant {grant} code task {crun or '-'}")
        last = ""
        while time.time() < deadline:
            last = self.psql("SELECT status || '|' || coalesce(phase,'') FROM code_task_runs WHERE id = :'c'", c=crun) if crun else ""
            if last.split("|")[0] in ("failed",) or last.split("|")[0] in ("completed", "succeeded", "failed", "cancelled", "blocked", "awaiting_review",
                                       "pr_opened", "done", "error"):
                break
            prs = self.fake_get("/_world").get("prs") or []
            if any(p.get("repo") == REPO for p in prs) and last:
                time.sleep(5)
                break
            time.sleep(3)
        self.state["code_task_status"] = last
        err = self.psql("SELECT left(coalesce(error::text,''), 300) FROM code_task_runs WHERE id = :'c'", c=crun) if crun else ""
        wf = self.psql("SELECT status || ' — ' || left(coalesce(blocked_reason,''), 200) FROM workflow_runs WHERE id = :'r'",
                       r=run["run"])
        self.state["code_task_error"] = err
        j.note(f"code task {crun or '-'} ended: {last} {err}; workflow run: {wf}")
        j.check("the build's code task runs (not refused before it starts)", bool(crun) and not last.startswith("failed"),
                expect="a code task that clones, builds and opens a PR", actual=f"{last} {err} | run {wf}",
                repro="see 'the Fast fix build's builder resolves to an agent with a Passport'" if "passport" in err else "")
        j.check("a refused build tells the person why in words (no raw 500 / internal URL)",
                not any(x in f"{wf} {err}" for x in ("500 Internal Server Error", "api:8000")),
                expect="e.g. 'The builder has no agent identity; …'", actual=f"code task: {err[:160]} | run: {wf[:160]}",
                known="FX-C P1-12 (real error states, no raw codes)")
        self.state["build_reached"] = bool(crun) and not last.startswith("failed")
        tok = self.psql("SELECT string_agg(run_id || ' ' || aud || CASE WHEN revoked_at IS NULL THEN ' live' "
                        "ELSE ' revoked' END, ', ') FROM identity_run_credentials WHERE grant_id = :'g'", g=grant)
        self.state["run_tokens"] = tok
        j.check("run tokens were minted for the build under the grant", bool(tok) and tok != "0",
                expect="identity_run_credentials rows for the grant", actual=tok)
        bound = crun and crun in (tok or "")
        j.check("the run token is bound to THIS build's run id", bool(bound), expect=f"run_id contains {crun}",
                actual=tok)

    def watch_start(self) -> None:
        out = f"/tmp/fxd-watch-{self.state['stamp']}.json"
        self.state["watch"] = out
        r = self.in_container("api", ["python", WATCH_IN_API, out, str(self.args.build_timeout)], detach=True)
        self.j.note(f"sandbox watcher started in the api container ({r.returncode})")

    def watch_read(self) -> dict[str, Any]:
        out = self.state.get("watch")
        self.in_container("api", ["touch", f"{out}.stop"])
        time.sleep(1)
        r = self.in_container("api", ["cat", out])
        try:
            return json.loads(r.stdout)
        except ValueError:
            return {}

    def sandbox_checks(self) -> None:
        j = self.j
        w = self.watch_read()
        procs = w.get("processes") or []
        self.state["sandbox"] = {"api_uid": w.get("api_uid"), "n": len(procs),
                                 "uids": sorted({p["uid"] for p in procs}),
                                 "argv0": sorted({(p["argv"] or ["?"])[0].rsplit("/", 1)[-1] for p in procs})}
        j.note(f"sandbox processes: {self.state['sandbox']}")
        uids = {p["uid"] for p in procs}
        j.check("the build ran under a per-run sandbox uid (61000-61999), not the API's uid",
                bool(procs) and len(uids) >= 1 and w.get("api_uid") not in uids,
                expect="≥1 process in the uid pool; api uid not among them", actual=self.state["sandbox"])
        j.check("no sandbox process had a credential in its argv", not any(p["argv_has_credential"] for p in procs),
                expect="none", actual=[p["argv"] for p in procs if p["argv_has_credential"]])
        j.check("no sandbox process held a GitHub token in its environment", not any(p["env_has_github_token"] for p in procs),
                expect="none", actual=[p["pid"] for p in procs if p["env_has_github_token"]])
        rems = [r for p in procs for r in p.get("remotes") or []]
        j.check("the run checkout's clone URL carries no credential", bool(rems) and not any(r["has_credential"] for r in rems),
                expect="remotes seen, none with user:pass@", actual=rems[:4])
        probe = w.get("probe") or {}
        main = probe.get("main") or {}
        refused = main.get("rc") not in (0, None)
        j.check("a push to main from inside the run (its uid, its env, its checkout) is refused", refused,
                expect="git push HEAD:main fails (egress refuses the ref)", actual=probe)
        self.state["probe"] = probe

    # ---- 5. PR + Factory ----------------------------------------------------------------------------
    def pr_checks(self, page: Any) -> None:
        j = self.j
        world = self.fake_get("/_world")
        prs = [p for p in world.get("prs") or [] if p.get("repo") == REPO]
        pr = prs[-1] if prs else None
        self.state["pr"] = pr and {k: pr.get(k) for k in ("number", "head", "base", "title", "state")}
        j.check("a PR was opened on the (fake) GitHub for the repo", bool(pr), expect="a PR on fxd-org/fxd-build-app",
                actual=self.state["pr"])
        if not pr:
            return
        refs = self.in_container("fakegh", ["git", "--git-dir", f"/srv/fakegh/git/{REPO}.git", "for-each-ref",
                                            "--format=%(refname:short) %(objectname:short)", "refs/heads"]).stdout
        j.note(f"fake repo branches: {refs.strip()}")
        seed_main = self.in_container("fakegh", ["git", "--git-dir", f"/srv/fakegh/git/{REPO}.git", "log", "-1",
                                                 "--format=%s", "main"]).stdout.strip()
        j.check("main on GitHub is untouched (still the seed commit); the work is on the run's branch",
                seed_main == "e2e seed" and pr["head"] != "main", expect="main = 'e2e seed', PR head ≠ main",
                actual=(seed_main, pr["head"]))
        crun = self.state.get("code_task_run") or ""
        j.check("the PR's head branch is the run's branch", bool(crun) and crun.split("_")[-1][:8] in pr["head"] or "boss/" in pr["head"],
                expect="boss/<run>… branch", actual=pr["head"])
        msgs = self.in_container("fakegh", ["git", "--git-dir", f"/srv/fakegh/git/{REPO}.git", "log", "--format=%B%x00",
                                            f"main..{pr['head']}"]).stdout
        has = "On-behalf-of:" in msgs and "Boss-Lineage:" in msgs
        j.check("the PR's commits carry BOSS's trailers (On-behalf-of + Boss-Lineage)", has,
                expect="On-behalf-of: Seed Owner <…> / Boss-Lineage: dlg_…/run_…", actual=msgs[:300],
                known="FX-B P1-14 (commit_trailers on PRs: github_app.commit_trailers has no caller)")
        self.factory_view(page, pr)

    def factory_view(self, page: Any, pr: Optional[dict[str, Any]]) -> None:
        j = self.j
        page.goto(f"{self.web}/?area=factory", wait_until="domcontentloaded", timeout=90000)
        K.settle(page)
        page.wait_for_timeout(2500)
        self.shot(page, "factory")
        txt = K.body_text(page, 20000)
        j.check("the Factory shows the build", self.state["stamp"] in txt,
                expect=f"the build titled fxd-build-{self.state['stamp']}…", actual=txt[:500])
        if pr is not None:
            j.check("the Factory shows the build's PR", f"#{pr['number']}" in txt or f"pull/{pr['number']}" in page.content(),
                    expect=f"PR #{pr['number']} linked", actual=txt[:500])
        link = page.get_by_text(re.compile(r"fxd-build-" + re.escape(self.state["stamp"]))).first
        if link.count():
            link.click()
            page.wait_for_timeout(2500)
            K.settle(page)
            self.shot(page, "factory-build-detail")

    # ---- 6. egress log + audit ----------------------------------------------------------------------
    def egress_checks(self) -> None:
        j = self.j
        log = self.fake_get("/_log")["entries"][self.state.get("log_start", 0):]
        browser_ip = {e.get("client") for e in log if e.get("via") == "local"} | {"172.17.0.1"}
        svc = [e for e in log if e.get("egress") and e.get("client") not in browser_ip]
        hosts = sorted({e["egress"].split(":")[0] for e in svc})
        blocked = [e for e in svc if e.get("blocked")]
        self.state["egress"] = {"service_hosts": hosts, "service_requests": len(svc), "blocked": [
            (e["egress"], e.get("path")) for e in blocked][:20],
            "browser_hosts": sorted({e["egress"].split(":")[0] for e in log if e.get("egress") and e.get("client") in browser_ip})}
        j.note(f"egress: {self.state['egress']}")
        j.check("the services reached only api.github.com and github.com (both answered by the fake)",
                set(hosts) <= {"api.github.com", "github.com"} and bool(hosts), expect="⊆ {api.github.com, github.com}",
                actual=hosts)
        j.check("the blocked-egress log has no service request (no real host was even tried)", not blocked,
                expect="no blocked entries from api/worker/egress", actual=self.state["egress"]["blocked"])
        git = [e for e in svc if e["egress"].startswith("github.com") and "/git-" in str(e.get("path"))]
        j.check("git smart-HTTP to GitHub went through with an installation token injected by egress (never anonymous)",
                bool(git) and all(str(e.get("auth", "")).startswith(("basic:ghs", "bearer")) for e in git if e.get("method") == "POST"),
                expect="git-upload/receive-pack with Basic x-access-token:ghs_…", actual=[(e.get("path"), e.get("auth"), e.get("status")) for e in git][:8])
        toks = self.fake_get("/_github_app").get("tokens") or []
        j.note(f"installation tokens minted: {len(toks)}; revoked: {sum(1 for t in toks if t['revoked'])}; "
               f"repos: {sorted({r for t in toks for r in t['repositories']})}")
        j.check("every installation token was scoped to the one repo", bool(toks) and all(t["repositories"] == [REPO] for t in toks),
                expect=f"repositories == [{REPO}]", actual=[t["repositories"] for t in toks])

    def audit_checks(self, page: Any) -> None:
        j = self.j
        grant = self.state.get("grant") or ""
        crun = self.state.get("code_task_run") or ""
        seq0 = int(self.state.get("audit_seq0") or 0)
        found: dict[str, int] = {}
        for ev in ("delegation.created", "token.issued", "delegation.used_first", "egress.denied", "token.denied",
                   "vault.connection.linked"):
            n = 0
            for e in audit_events(page, event=ev, limit="200"):
                raw = e.get("raw") or {}
                if int(e.get("seq") or 0) <= seq0:
                    continue
                blob = json.dumps(raw)
                if ev == "vault.connection.linked" or grant in blob or (crun and crun in blob):
                    n += 1
            found[ev] = n
        self.state["audit"] = found
        j.note(f"audit events for this walk: {found}")
        j.check("audit: delegation.created for this grant", found.get("delegation.created", 0) >= 1, expect="≥1", actual=found)
        j.check("audit: token.issued for this run", found.get("token.issued", 0) >= 1, expect="≥1", actual=found)
        page.goto(f"{self.web}/identity/audit", wait_until="domcontentloaded", timeout=90000)
        K.settle(page)
        page.wait_for_timeout(1500)
        self.shot(page, "audit-log")

    # ---- the whole walk -----------------------------------------------------------------------------
    def run(self) -> None:
        j = self.j
        ctx, page = self.page()
        try:
            self.preflight(page)
            with j.tolerate("connect the GitHub App from Setup"):
                self.connect_app(page)
            with j.tolerate("register the repository from Setup"):
                self.register_repo(page)
            self.builder_check()
            self.watch_start()
            with j.tolerate("start a Fast fix and consent"):
                self.fast_fix(page)
            if not self.state.get("grant_created"):
                j.check("the walk reached a build (a grant was created)", False, expect="a grant, then a build",
                        actual="no grant: the build, sandbox, PR and Factory steps were not reached")
                self.watch_read()
                with j.tolerate("egress log"):
                    self.egress_checks()
                return
            with j.tolerate("the build runs"):
                self.run_build(page)
            if self.state.get("build_reached"):
                with j.tolerate("the sandbox"):
                    self.sandbox_checks()
                with j.tolerate("the PR and the Factory"):
                    self.pr_checks(page)
            else:
                self.watch_read()
                j.check("the build reached the sandbox, egress push and PR steps", False,
                        expect="sandbox uid / clone / push / PR assertions run",
                        actual="not reached: " + str(self.state.get("code_task_error") or "no code task")[:200])
                with j.tolerate("the Factory"):
                    self.factory_view(page, None)
            with j.tolerate("egress log"):
                self.egress_checks()
            with j.tolerate("audit"):
                self.audit_checks(page)
        except Exception as exc:  # noqa: BLE001
            j.error = K.fmt_exc(exc)
            j.note(f"ABORTED: {j.error}")
            try:
                self.shot(page, "aborted")
            except Exception:  # noqa: BLE001
                pass
        finally:
            j.finished = time.time()
            ctx.close()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stack-dir", required=True, help="dir with stack.env and accounts.json (seed-test-accounts)")
    ap.add_argument("--project", default="bossfxd")
    ap.add_argument("--console", default="http://localhost:53100")
    ap.add_argument("--api", default="http://127.0.0.1:58100")
    ap.add_argument("--fake", default="http://127.0.0.1:58180", help="fakegh's published port (proxy + /_log)")
    ap.add_argument("--shots", required=True, help="screenshots go to <shots> (named …/build) or <shots>/build")
    ap.add_argument("--report", default=None)
    ap.add_argument("--build-timeout", type=int, default=600)
    ap.add_argument("--guard-wait", type=int, default=960,
                    help="seconds to wait out the D28 guard's cached refusal after connecting GitHub (bug)")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args(argv)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        # github.com and every other non-local host go to the fake's proxy (TLS from the fake CA,
        # hence ignore_https_errors in the context); the console stays direct.
        browser = pw.chromium.launch(headless=not args.headed, proxy={
            "server": args.fake, "bypass": "localhost,127.0.0.1"})
        walk = Walk(args, browser)
        walk.run()
        browser.close()
    result = {**walk.j.as_dict(), "state": json.loads(K.scrub(json.dumps(walk.state, default=str)))}
    report = Path(args.report) if args.report else walk.j.shots / "build_walk.json"
    K.write_private(report, json.dumps(result, indent=1))
    print(f"[build] {result['status']} — report {report}")
    return 1 if result["status"] == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
