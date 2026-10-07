"""THE UPGRADE PROMISE, ON A REAL COMPOSE STACK (FX-A: 7fd142f8, 54bb1009, e1d75542, 0d74d2a9, ea7a85f8).

Five scenarios, each against a real stack this walk brings up (compose project bossfxd3, images tag
`fxda`) and always takes down again (`down -v`), driving the REAL host tools (boss.sh, checkup.sh,
make mcp-setup, upgrade.sh, install.sh) and a real browser through the real console:

  shared_password   before the claim the shared console password (no email) answers 409
                    claim_first; after it, it signs in through /login as usr_legacy_shared with the
                    Operator role and nothing more, the countdown banner is on every page, the session
                    never outlives the cutoff, a wrong one is the uniform invalid_credentials; the
                    Owner's one extension is capped at 30 days and audited; `boss identity legacy
                    retire --path shared_password` ends it (existing session included); and, on a
                    fresh stack, MOVING THE CLOCK past the cutoff ends it the same way, in words on
                    the page.
  host_key          `boss identity host-key` keeps a boss_sak_ key in .env (never printed); boss status,
                    doctor and open work with it; it is the "Host tools" service account (role
                    host_tools) the Owner sees; it cannot change people or access; with the shared
                    BOSS_API_KEY retired the boss commands still work; revoked, the next boss
                    command issues a new one.
  mcp_setup         after an rc5 `make mcp-setup` (the rc5 script, a hex key bound to mcp_operator),
                    the new `make mcp-setup` makes service account "MCP (<project>)" with exactly the
                    chosen capabilities' permissions, a key pinned to that project, and unbinds the
                    hex key.
  sandbox_fence     an api without NET_ADMIN (compose/fence-off.yml) under BOSS_SANDBOX_NETWORK=enforce
                    starts, says so loudly, writes .network-unfenced, boss doctor FAILs naming the fix,
                    and a build is HELD (blocked, sandbox_unavailable, the fix in the reason, no
                    sandbox uid ever used); =required refuses to start; =off starts with no fence and
                    lets builds run.
  upgrade_rollback  a scratch install (its own compose project, bossfxdup) of this branch, installed
                    by install.sh, upgraded by upgrade.sh to a release whose API cannot start: the
                    previous release is put back and running (health, same data, sign-in) with the
                    failure explained; --no-rollback leaves it down.

    cd tests && python3 -m e2e.identity.upgrade_walk [--only shared_password,host_key,...] --out <dir>

Report: <out>/upgrade-walk.json (default out: the id-fxd scratch folder); screenshots under --shots.
A check that fails because of a known product bug is `xfail` naming the fix stream; assertions are
never weakened. Exit 0 when nothing failed (xfails allowed). Never prints a secret.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from e2e.harness import identity as I  # noqa: E402
from e2e.identity import kit as K  # noqa: E402
from e2e.identity import upgrade_stack as U  # noqa: E402
from e2e.identity.kit import Journey, short  # noqa: E402
from e2e.identity.stack2 import docker_headroom_gib  # noqa: E402

SCRATCH = Path("/private/tmp/claude-501/-Users-param-startup-sean-boss/b8a9beba-5883-4930-93e4-516678b875a0/scratchpad")
DEFAULT_OUT = SCRATCH / "id-fxd"
DEFAULT_SHOTS = SCRATCH / "id-fxd-shots" / "upgrade"
PROJECT = "boss-dogfood"
OTHER_PROJECT = "fxd-other"
SCENARIOS = ("shared_password", "host_key", "mcp_setup", "sandbox_fence", "upgrade_rollback")
FENCE_OVERRIDE = K.REPO / "tests" / "e2e" / "compose" / "fence-off.yml"
SAK = re.compile(r"boss_sak_[A-Za-z0-9]+")
KNOWN = "FX-A"
CACHE_S = 6   # > the 5 s caches: the console's feed cache (lib/feed-cache.ts) and legacy._CACHE_TTL

#: the pages walked for the countdown banner (all render inside ConsoleShell)
BANNER_PAGES = ("/", "/?area=setup", "/?area=factory", "/account", "/account/sessions", "/identity/people")


# ---- the walk's environment ---------------------------------------------------------------------

class Env:
    def __init__(self, args: argparse.Namespace, browser: Any):
        self.args = args
        self.browser = browser
        self.out = Path(args.out)
        self.work = self.out / "upgrade-scratch"
        self.work.mkdir(parents=True, exist_ok=True)
        self.shots = Path(args.shots)
        self.st: Optional[U.Stack3] = None
        self.inst: Optional[Path] = None
        self.shim = self.work / "shimbin"
        U.make_shim(self.shim)
        self.A: dict[str, Any] = {}
        self.owner: Optional[I.Claimant] = None
        self.pre: dict[str, Any] = {}           # what the unclaimed install answered (S1 setup)
        self.rc5: dict[str, Any] = {}           # the rc5 MCP state made before the claim
        self.state = ""                          # "", "s1", "dirty"
        self.findings: list[dict[str, str]] = []

    # -- docker memory
    def wait_headroom(self, need: float = 2.0) -> None:
        deadline = time.time() + 20 * 60
        while True:
            free = docker_headroom_gib()
            if free >= need:
                return
            if time.time() > deadline:
                raise RuntimeError(f"docker VM headroom {free:.2f} GiB < {need} GiB after 20 min; stopping")
            print(f"[walk] docker headroom {free:.2f} GiB; waiting", flush=True)
            time.sleep(60)

    # -- the stack
    def tools(self, **extra: str) -> dict[str, str]:
        return U.tool_env("bossfxd3", self.shim, extra=extra or None)

    def boss(self, *cmd: str, timeout: int = 600) -> dict[str, Any]:
        assert self.inst is not None
        r = U.run_tool(["bash", str(self.inst / "scripts/install/boss.sh"), "--dir", str(self.inst), *cmd],
                       self.tools(), self.inst, timeout=timeout)
        self.st.secrets_now()
        return r

    def env_value(self, key: str) -> str:
        v = U.read_env(self.st.env_file).get(key, "")
        K.secret(v)
        return v

    def fresh_stack(self, *, override: Optional[str] = None, extra_env: Optional[dict[str, str]] = None,
                    claim: bool = True, seed_people: bool = False, rc5_mcp: bool = False) -> U.Stack3:
        """A brand-new bossfxd3 (down -v first), seeded, with the scratch install pointing at it."""
        env_file = Path(self.args.env_file)
        if self.st is not None:
            self.st.down()
        elif env_file.exists():      # a bossfxd3 left by an earlier, interrupted run
            U.Stack3("bossfxd3", env_file, env_file.parent / "bossfxd3.override.yml", dict(U.PORTS3)).down()
        self.wait_headroom()
        st = U.Stack3.create(Path(self.args.shape_env), env_file, extra=extra_env)
        self.st = st
        self.inst = U.make_install(self.work, env_file)
        st.use_env(self.inst / ".env")
        st.secrets_now()
        st.up_with(override)
        st.seed()
        self.pre = {}
        if rc5_mcp:
            self.make_rc5_mcp()
        self.pre["shared"] = login_api(st.web, "", self.env_value("BOSS_CONSOLE_PASSWORD"))
        self.pre["wrong"] = login_api(st.web, "", "fxd-not-the-shared-password")
        self.pre["options"] = K.http("GET", st.web + "/v1/auth/options")
        if claim:
            host = st.host()
            token = K.secret(I.setup_token(host))
            self.owner = I.Claimant(name="FXD3 Owner", email="fxd3-owner@example.test",
                                    password=K.secret(I.claim_password()))
            I.claim_owner(st.web, token, self.owner)
        if seed_people:
            out = self.work / "accounts3.json"
            I.seed_accounts(st.host(), self.owner.email, out,
                            copy_out=I.compose_copy_out(st.project, K.COMPOSE_FILE, st.env_file))
            self.A = I.load_accounts(out)
            for v in self.A.values():
                for attr in ("password", "totp_secret", "key"):
                    K.secret(getattr(v, attr, None))
        return st

    def make_rc5_mcp(self) -> None:
        """The state an rc5 `make mcp-setup` left behind, made by hand as rc5 wrote it: the project
        member `mcp_operator` (rc5 POSTed it; rc6 refuses member_type system over HTTP), and in .env a
        48-hex key in BOSS_API_KEYS bound to it, BOSS_MCP_API_KEY and BOSS_MCP_PROJECT_ID; then
        the api restarted so it reads them (what rc5's --restart-api did)."""
        import secrets as _s
        hexkey = _s.token_hex(24)
        K.secret(hexkey)
        self.psql("INSERT INTO project_members (id, project_id, member_id, display_name, member_type, role_keys, "
                  "permissions, status, created_by) VALUES (:'id', :'p', 'mcp_operator', 'MCP operator', 'system', "
                  "'[\"mcp_operator\"]'::jsonb, :'perms'::jsonb, 'active', 'ceo') ON CONFLICT (project_id, member_id) DO NOTHING",
                  id=f"pm_{PROJECT}_mcp_operator", p=PROJECT,
                  perms=json.dumps(["agent_chat", "command_submit", "task_create", "workflow_manage",
                                    "workflow_execute", "health_refresh", "audit_export"]))
        vals = U.read_env(self.st.env_file)
        api, worker = vals["BOSS_API_KEY"], vals["BOSS_WORKER_API_KEY"]
        vals.update({"BOSS_MCP_API_KEY": hexkey, "BOSS_MCP_PROJECT_ID": PROJECT,
                     "BOSS_API_KEYS": f"{api},{worker},{hexkey}",
                     "BOSS_API_KEY_PRINCIPALS": f"{api}:ceo,{worker}:system,{hexkey}:mcp_operator"})
        U.write_env(self.st.env_file, vals)
        self.st.compose("up", "-d", "--no-deps", "--force-recreate", "api", timeout=300)
        self.st.wait_ready(240)
        self._rc5_key = hexkey
        self.rc5 = {"hex_shape": True, "bound": True,
                    "projects_before_claim": self.st.stack.bearer("GET", "/v1/projects", hexkey)[0]}

    def ensure_s1(self) -> U.Stack3:
        """The claimed, seeded stack the S1 scenarios share (made once per run)."""
        if self.state != "s1":
            self.fresh_stack(seed_people=True, rc5_mcp="mcp_setup" in self.args.selected)
            self.state = "s1"
        return self.st

    def owner_page(self, j: Journey) -> tuple[Any, Any]:
        return K.signed_in_page(self.browser, self.st.web, self.A["owner"], j)

    def psql(self, sql: str, **params: str) -> str:
        return self.st.stack.psql(sql, params or None)

    def operator_set(self) -> set[str]:
        r = self.st.in_api("python", "-c", "import json; from boss_engine.identity.permissions_gen import ROLE_PERMISSIONS as R; "
                           "print(json.dumps(sorted(R['operator'])))")
        return set(json.loads(r.stdout.strip().splitlines()[-1]))

    def finding(self, title: str, detail: str, owner: str) -> None:
        self.findings.append({"title": title, "detail": K.scrub(detail), "owner": owner})


# ---- small helpers ------------------------------------------------------------------------------

def login_api(web: str, email: str, password: str) -> dict[str, Any]:
    """POST /v1/auth/login at the console origin: status, body, and the session cookie's Max-Age."""
    status, headers, body = K.http_raw("POST", web + "/v1/auth/login",
                                       data=json.dumps({"email": email, "password": password}).encode(),
                                       headers={"Content-Type": "application/json", "Origin": web})
    try:
        parsed = json.loads(body) if body else None
    except ValueError:
        parsed = body[:300]
    max_age = None
    for c in headers.get("set-cookie", []):
        m = re.search(r"Max-Age=(\d+)", c, re.I)
        if m and "boss_session" in c.split("=", 1)[0]:
            max_age = int(m.group(1))
    if isinstance(parsed, dict):
        for k in ("session_id", "csrf", "csrf_token"):
            K.secret(parsed.get(k))
    return {"status": status, "body": parsed, "max_age": max_age}


def iso(s: Any) -> Optional[float]:
    from datetime import datetime
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def open_shared_form(page: Any, web: str, password: str) -> bool:
    """/login → "Use the shared password" → the shared password typed in, NOT submitted."""
    page.goto(f"{web}/login", wait_until="domcontentloaded")
    page.wait_for_selector("#email", timeout=30000)
    K.settle(page, 10000)
    toggle = page.locator("[data-testid=shared-password-toggle]")
    if not toggle.count():
        return False
    toggle.click()
    page.wait_for_selector("[data-testid=shared-password-note]", timeout=10000)
    page.fill("#password", password)
    return True


def submit(page: Any) -> None:
    page.click("[data-testid=email-submit]")


def error_text(page: Any, timeout_ms: int = 15000) -> str:
    try:
        page.wait_for_selector("#signin-error", timeout=timeout_ms)
        return page.inner_text("#signin-error")
    except Exception:  # noqa: BLE001
        return ""


def no_csrf(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("<csrf>" if "csrf" in k else no_csrf(v)) for k, v in obj.items()}
    return obj


def legacy_row(env: Env, kind: str = "shared_password") -> dict[str, str]:
    row = env.psql("SELECT coalesce(retire_after::text,'') || '|' || coalesce(retired_at::text,'') || '|' || "
                   "principal_id FROM identity_legacy_mappings WHERE legacy_kind = :'k'", k=kind)
    parts = (row.splitlines()[0] if row else "||").split("|")
    return {"retire_after": parts[0], "retired_at": parts[1], "principal_id": parts[2] if len(parts) > 2 else ""}


def event_count(env: Env, event_type: str, **where: str) -> int:
    cond = "".join(f" AND {k} = :'{k}'" for k in where)
    return int(env.psql(f"SELECT count(*) FROM identity_security_events WHERE event_type = :'et'{cond}",
                        et=event_type, **where) or 0)


def host_key_last_used(env: Env) -> str:
    """'' after reset_last_used(); a timestamp once the API saw the key (first use always writes)."""
    return env.psql("SELECT coalesce(max(last_used_at)::text,'') FROM identity_api_credentials "
                    "WHERE principal_id = 'sva_host_tools' AND revoked_at IS NULL")


def reset_last_used(env: Env) -> None:
    """last_used_at is written at most once a minute (api_keys.LAST_USE_WRITE_SECONDS) unless it is
    empty, so empty it: the next call that uses the key writes it, the proof the command used it."""
    env.psql("UPDATE identity_api_credentials SET last_used_at = NULL WHERE principal_id = 'sva_host_tools' "
             "AND revoked_at IS NULL")


def shot_banner(env: Env, j: Journey, page: Any, path: str, n: int) -> tuple[bool, str]:
    page.goto(env.st.web + path, wait_until="domcontentloaded")
    K.settle(page, 12000)
    loc = page.locator('.idn-banner[data-kind="legacy_password"]')
    try:
        loc.first.wait_for(state="visible", timeout=15000)
    except Exception:  # noqa: BLE001
        pass
    seen = loc.count() > 0 and loc.first.is_visible()
    text = loc.first.inner_text() if seen else K.body_text(page, 200)
    j.shot(page, f"banner-{n}-{path.strip('/').replace('/', '-').replace('?', '').replace('=', '-') or 'home'}")
    return seen, text


# ---- 1. the shared password (S1: claim, operator, banner, extension, CLI retire) ----------------

def j_shared_password(env: Env, j: Journey) -> None:
    st = env.ensure_s1()
    web = st.web
    pw = env.env_value("BOSS_CONSOLE_PASSWORD")
    pre = env.pre
    j.check("before the claim, the shared password with no email answers 409 claim_first",
            pre["shared"]["status"] == 409 and (pre["shared"]["body"] or {}).get("error") == "claim_first",
            expect="409 {error: claim_first}", actual=pre["shared"])
    j.check("before the claim, the sign-in page does not offer the shared password (it only claims)",
            pre["options"][0] == 200 and (pre["options"][1] or {}).get("shared_password") is None,
            expect="/v1/auth/options shared_password null", actual=(pre["options"][0], (pre["options"][1] or {}).get("shared_password")))
    j.check("before the claim, a wrong shared password is the uniform 401 invalid_credentials",
            pre["wrong"]["status"] == 401 and pre["wrong"]["body"] == {"error": "invalid_credentials"},
            expect="401 {error: invalid_credentials}", actual=pre["wrong"])

    st_opts = K.http("GET", web + "/v1/auth/options")
    offer = (st_opts[1] or {}).get("shared_password") or {}
    row = legacy_row(env)
    start = env.psql("SELECT policy->'extra'->>'legacy_window_start' FROM identity_settings LIMIT 1")
    until_ts, start_ts = iso(offer.get("until")), iso(start)
    j.check("after the claim the sign-in page offers the shared password until claim + 30 days",
            bool(until_ts and start_ts) and abs((until_ts - start_ts) - 30 * 86400) < 120,
            expect="options.shared_password.until = legacy_window_start + 30 d",
            actual={"until": offer.get("until"), "window_start": start})

    # -- sign in through the real /login
    ctx, page = K.new_page(env.browser, j)
    ok = open_shared_form(page, web, pw)
    j.check("/login shows 'Use the shared password'; it asks for no email and says Operator + the end date",
            ok and page.locator("#email").count() == 0 and "Operator" in page.inner_text("[data-testid=shared-password-note]"),
            expect="toggle → no #email, note 'Signs you in as Operator until …'",
            actual=page.inner_text("[data-testid=shared-password-note]") if ok else "no toggle", fatal=True)
    j.shot(page, "shared-password-form")
    submit(page)
    page.wait_for_url(lambda u: "/login" not in u, timeout=30000)
    K.settle(page)
    me = K.me(page)
    body = me.get("body") or {}
    K.secret(body.get("csrf_token"))
    j.check("signed in through /login as usr_legacy_shared", me["status"] == 200 and body.get("principal_id") == "usr_legacy_shared",
            expect="/v1/identity/me principal usr_legacy_shared", actual=no_csrf(body), fatal=True)
    j.check("its access roles are exactly [\"operator\"]", body.get("access_roles") == ["operator"],
            expect='access_roles == ["operator"]', actual=body.get("access_roles"))
    mine, op = K.perms(page), env.operator_set()
    j.check("/v1/identity/me/permissions is exactly the Operator catalogue set (permissions_gen ROLE_PERMISSIONS['operator'])",
            mine == op, expect=f"{len(op)} permissions, equal sets",
            actual={"n": len(mine), "extra": sorted(mine - op)[:20], "missing": sorted(op - mine)[:20]})
    inv = K.api(page, "POST", "/v1/identity/users", {"email": "fxd3-nobody@example.test",
                                                      "bindings": [{"role_key": "viewer", "scope_type": "org"}], "group_ids": []})
    j.check("an identity write (inviting a person) is refused 403", inv["status"] == 403,
            expect="403 forbidden user:invite", actual=inv)
    sa = K.api(page, "POST", "/v1/identity/service-accounts", {"name": "fxd3-nope", "description": "x"})
    j.check("making a service account is refused 403", sa["status"] == 403, expect="403", actual=sa)
    act = K.api(page, "POST", "/v1/code-tasks", {"project_id": PROJECT, "title": "fxd3 operator action", "simulated": True})
    j.check("an operator action is allowed (start a code task, code_task:create)", act["status"] == 200,
            expect="200 simulated code task", actual=act)
    for path, payload in (("/v1/identity/me/password", {"new": K.strong_password("shared")}),
                          ("/v1/identity/me/mfa/totp", None), ("/v1/identity/me/passkeys/options", {})):
        r = K.api(page, "POST", path, payload)
        j.check(f"the shared session cannot give itself a credential: POST {path} is refused (4xx)",
                400 <= r["status"] < 500, expect="4xx (people_only / not a person with an account)", actual=r,
                known=(f"{KNOWN} (P0-1): POST /v1/identity/me/password from the shared-password session is a 500 "
                       "(identity_me.change_password asserts users.by_id(usr_legacy_shared), which has no user row)"
                       if path.endswith("/password") else None),
                repro="sign in with the shared password; POST /v1/identity/me/password {new: <strong>} with the CSRF header → 500")
    pat = K.api(page, "POST", "/v1/identity/me/api-keys", {"name": "fxd3-shared-pat", "scopes": ["project:read"]})
    shared_pat = K.secret(((pat.get("body") or {}).get("key") or (pat.get("body") or {}).get("token"))) if pat["status"] == 201 else None
    j.check("the shared session cannot mint a personal API key (it would outlive the cutoff)", 400 <= pat["status"] < 500,
            expect="4xx", actual=pat)
    env.shared_pat = shared_pat
    ext = K.api(page, "POST", "/v1/identity/legacy/shared_password/extend", {"until": "2099-01-01T00:00:00Z"})
    j.check("the shared session cannot extend its own cutoff (403)", ext["status"] == 403, expect="403", actual=ext)

    # -- the session never outlives the cutoff
    exp = iso(body.get("session_expires_at"))
    row = legacy_row(env)
    j.check("the session's expiry is not after the cutoff", bool(exp and iso(row["retire_after"])) and exp <= iso(row["retire_after"]) + 1,
            expect="me.session_expires_at ≤ the shared password's retire_after",
            actual={"session_expires_at": body.get("session_expires_at"), "retire_after": row["retire_after"]})
    bad = env.psql("SELECT count(*) FROM identity_sessions s, identity_legacy_mappings m WHERE s.principal_id = "
                   "'usr_legacy_shared' AND m.legacy_kind = 'shared_password' AND s.absolute_expires_at > m.retire_after")
    j.check("no shared-password session in the database expires after the cutoff", bad == "0",
            expect="0 sessions with absolute_expires_at > retire_after", actual=bad)
    old = row["retire_after"]
    env.psql("UPDATE identity_legacy_mappings SET retire_after = now() + interval '20 minutes' WHERE legacy_kind = 'shared_password'")
    try:
        near = login_api(web, "", pw)
        cut = env.psql("SELECT extract(epoch FROM retire_after) FROM identity_legacy_mappings WHERE legacy_kind = 'shared_password'")
        e2 = iso((near["body"] or {}).get("expires_at"))
        j.check("20 minutes before the cutoff a new shared session ends AT the cutoff (expires_at and cookie Max-Age)",
                near["status"] == 200 and bool(e2) and e2 <= float(cut) + 1 and (near["max_age"] is None or near["max_age"] <= 20 * 60 + 5),
                expect="expires_at ≤ cutoff (now + 20 min), session cookie Max-Age ≤ 1205",
                actual={"status": near["status"], "expires_at": (near["body"] or {}).get("expires_at"),
                        "shared_password_until": (near["body"] or {}).get("shared_password_until"), "max_age": near["max_age"]})
    finally:
        env.psql("UPDATE identity_legacy_mappings SET retire_after = :'old'::timestamptz WHERE legacy_kind = 'shared_password'", old=old)
        env.psql("UPDATE identity_sessions SET revoked_at = now(), revoked_reason = 'fxd test cleanup' WHERE principal_id = "
                 "'usr_legacy_shared' AND revoked_at IS NULL AND absolute_expires_at < now() + interval '25 minutes'")

    # -- the countdown banner on every page
    for n, path in enumerate(BANNER_PAGES, 1):
        seen, text = shot_banner(env, j, page, path, n)
        j.check(f"the countdown banner shows on {path}", seen and "shared password" in text.lower() and bool(re.search(r"\d{4}-\d{2}-\d{2}|\d+ days?", text)),
                expect="a legacy_password banner: 'signed in with the shared password … stops working in N days (date)'",
                actual=text[:200])
    note = ""
    try:
        note = (K.me(page).get("body") or {}).get("banners")
    except Exception:  # noqa: BLE001
        pass
    j.note(f"banner data: {short(note, 300)}")

    # -- a wrong shared password is the uniform failure
    wrong = login_api(web, "", "fxd-not-the-shared-password")
    wrong_email = login_api(web, "fxd3-nobody@example.test", "fxd-not-the-shared-password")
    j.check("a wrong shared password is the uniform 401 invalid_credentials (same body as a wrong email + password)",
            wrong["status"] == 401 and wrong["body"] == wrong_email["body"] == {"error": "invalid_credentials"},
            expect="401 {error: invalid_credentials} for both", actual={"shared": wrong, "email": wrong_email})

    # -- the Owner's one extension (≤ 30 days, audited)
    octx, opage = env.owner_page(j)
    try:
        start_ts = iso(env.psql("SELECT policy->'extra'->>'legacy_window_start' FROM identity_settings LIMIT 1"))
        from datetime import datetime, timezone
        far = datetime.fromtimestamp(start_ts + 75 * 86400, tz=timezone.utc).isoformat()
        r1 = K.stepped(opage, env.A["owner"], "POST", "/v1/identity/legacy/shared_password/extend", {"until": far}, j)
        got = iso((r1.get("body") or {}).get("retire_after"))
        j.check("the Owner extends the cutoff once, and an ask for claim + 75 days is capped at claim + 60 (30-day extension)",
                r1["status"] == 200 and bool(got) and abs(got - (start_ts + 60 * 86400)) < 120,
                expect="200 retire_after = window_start + 60 d", actual=r1)
        ev = K.audit_has(opage, "policy.changed", target="shared_password")
        details = json.dumps((ev or {}).get("raw", ev) or {})
        j.check("the extension is audited (policy.changed on legacy_path shared_password, extension_days 30)",
                bool(ev) and '"extension_days": 30' in details, expect="policy.changed with legacy_cutoff.extension_days = 30",
                actual=short(ev, 500))
        again = datetime.fromtimestamp(start_ts + 70 * 86400, tz=timezone.utc).isoformat()
        r2 = K.stepped(opage, env.A["owner"], "POST", "/v1/identity/legacy/shared_password/extend", {"until": again}, j)
        j.check("a second extension past the extended cutoff is refused (409 cannot_extend, 'already extended once')",
                r2["status"] == 409 and "once" in json.dumps(r2.get("body") or {}), expect="409 cannot_extend", actual=r2)
        offer2 = (K.http("GET", web + "/v1/auth/options")[1] or {}).get("shared_password") or {}
        j.check("the sign-in page's end date follows the extension", abs((iso(offer2.get("until")) or 0) - (start_ts + 60 * 86400)) < 120,
                expect="options.shared_password.until = window_start + 60 d", actual=offer2)
    finally:
        pass

    # -- `boss identity legacy retire --path shared_password` from the host shell
    k2, waiting = K.new_page(env.browser, j)
    form = open_shared_form(waiting, web, pw)       # opened before the retirement, submitted after
    before = K.me(page)["status"]
    r = env.boss("identity", "legacy", "retire", "--path", "shared_password")
    time.sleep(CACHE_S)   # the console's feed cache and the API's bridge cache keep an answer ≤ 5 s
    env.finding("A retired or revoked credential keeps answering for up to 5 s",
                "The console's /v1 proxy reuses a GET for 5 s per session (apps/web/lib/feed-cache.ts, DEFAULT_TTL_MS): "
                "GET /v1/identity/me through the console still returned 200 for a session the API already refused (401 "
                "direct). The API's legacy bridge cache (legacy._CACHE_TTL = 5 s, per process) keeps a just-retired "
                "BOSS_API_KEY authenticating in the api process for up to 5 s after `boss identity legacy retire` (the CLI "
                "invalidates only its own process). Writes are not cached. By design per the code comments; the walk waits "
                "6 s before asserting.", "unassigned (low)")
    j.note(f"boss identity legacy retire: rc={r['rc']} {K.scrub(r['out'])[-200:]} {K.scrub(r['err'])[-200:]}")
    j.check("`boss identity legacy retire --path shared_password` succeeds from the host shell",
            r["rc"] == 0 and '"retired": true' in r["out"], expect='rc 0, {"retired": true}', actual=K.scrub(r["out"] + r["err"])[-300:])
    after = K.raw_fetch(page, "GET", "/v1/identity/me", csrf=False)
    j.check("the EXISTING shared session stops working on its next request", before == 200 and after["status"] == 401,
            expect="200 before, 401 after", actual={"before": before, "after": after["status"]})
    page.goto(web + "/", wait_until="domcontentloaded")
    K.settle(page)
    j.check("the console sends the ended session to /login", "/login" in page.url or "/signin" in page.url,
            expect="redirect to /login", actual=page.url,
            known="FX-C (P1-17 dead ends): middleware only checks that a session cookie is PRESENT; with a refused one "
                  "the console renders 'Select your organization / Onboard a new organization' (no orgs) instead of /login",
            repro="sign in with the shared password; boss identity legacy retire --path shared_password; wait 6 s; open / "
                  "→ the empty org picker, not /login")
    j.shot(page, "after-cli-retire-existing-session")
    api_after = login_api(web, "", pw)
    j.check("signing in with it now answers 403 shared_password_retired",
            api_after["status"] == 403 and (api_after["body"] or {}).get("error") == "shared_password_retired",
            expect="403 {error: shared_password_retired}", actual=api_after)
    if form:
        submit(waiting)
        words = error_text(waiting)
        j.shot(waiting, "after-cli-retire-page-says-so")
        j.check("the page says so in words", "no longer works" in words.lower(),
                expect="'The shared password no longer works. Ask an Owner to invite you.'", actual=words)
    if getattr(env, "shared_pat", None):
        dead = st.stack.bearer("GET", "/v1/identity/me", env.shared_pat)
        j.check("the API key the shared session minted stops with it", dead[0] == 401, expect="401", actual=dead)
    fresh = K.http("GET", web + "/v1/auth/options")
    j.check("after the retirement /login no longer offers the shared password",
            (fresh[1] or {}).get("shared_password") is None, expect="options.shared_password null",
            actual=(fresh[1] or {}).get("shared_password"))
    status = env.psql("SELECT status FROM identity_principals WHERE id = 'usr_legacy_shared'")
    row = legacy_row(env)
    j.check("usr_legacy_shared is disabled and the mapping retired", status == "disabled" and bool(row["retired_at"]),
            expect="principal disabled, mapping retired_at set", actual={"status": status, **row})
    j.check("legacy.path_retired is in the security log (actor the host CLI)",
            event_count(env, "legacy.path_retired", target_id="usr_legacy_shared", actor_principal_id="sys_host_cli") >= 1,
            expect="≥1 legacy.path_retired by sys_host_cli", actual=event_count(env, "legacy.path_retired"))
    for c in (ctx, k2, octx):
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
    env.state = "s1"  # still claimed; the shared password is now retired on this stack


def j_shared_password_cutoff(env: Env, j: Journey) -> None:
    """A fresh stack; the clock moved past the cutoff (every date the claim set, moved back)."""
    st = env.fresh_stack(seed_people=False)
    env.state = "dirty"
    web = st.web
    pw = env.env_value("BOSS_CONSOLE_PASSWORD")
    ctx, page = K.new_page(env.browser, j)
    if not open_shared_form(page, web, pw):
        raise RuntimeError("no shared-password toggle on a freshly claimed stack")
    submit(page)
    page.wait_for_url(lambda u: "/login" not in u, timeout=30000)
    K.settle(page)
    me0 = K.raw_fetch(page, "GET", "/v1/identity/me", csrf=False)
    j.check("signed in with the shared password (fresh stack)", me0["status"] == 200 and (me0["body"] or {}).get("principal_id") == "usr_legacy_shared",
            expect="200 usr_legacy_shared", actual=me0["status"], fatal=True)
    k2, waiting = K.new_page(env.browser, j)
    form = open_shared_form(waiting, web, pw)
    j.shot(waiting, "cutoff-form-before")
    moved = env.psql("""
BEGIN;
CREATE TEMP TABLE fxd_delta AS SELECT (now() - interval '1 minute') - retire_after AS d
  FROM identity_legacy_mappings WHERE legacy_kind = 'shared_password';
UPDATE identity_legacy_mappings SET retire_after = retire_after + (SELECT d FROM fxd_delta)
  WHERE legacy_kind = 'shared_password';
UPDATE identity_settings SET policy = jsonb_set(policy, '{extra,legacy_window_start}', to_jsonb(to_char(
  ((policy->'extra'->>'legacy_window_start')::timestamptz + (SELECT d FROM fxd_delta)) AT TIME ZONE 'UTC',
  'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"'))), epoch = epoch + 1;
UPDATE identity_role_bindings SET expires_at = expires_at + (SELECT d FROM fxd_delta)
  WHERE subject_type = 'principal' AND subject_id = 'usr_legacy_shared' AND expires_at IS NOT NULL;
UPDATE identity_sessions SET absolute_expires_at = absolute_expires_at + (SELECT d FROM fxd_delta)
  WHERE principal_id = 'usr_legacy_shared' AND revoked_at IS NULL;
SELECT 'delta_days=' || round(extract(epoch FROM (SELECT d FROM fxd_delta)) / 86400.0, 2);
COMMIT;""")
    j.note("clock moved: " + " ".join(x for x in moved.splitlines() if x.startswith("delta_days")))
    start = env.psql("SELECT policy->'extra'->>'legacy_window_start' FROM identity_settings LIMIT 1")
    time.sleep(CACHE_S)
    j.check("the claim (legacy_window_start) is now more than 30 days ago", (time.time() - (iso(start) or time.time())) > 30 * 86400,
            expect="window start < now - 30 d", actual=start)
    nxt = K.raw_fetch(page, "GET", "/v1/identity/me", csrf=False)
    j.check("the EXISTING shared session stops working on its next request", nxt["status"] == 401,
            expect="401", actual=nxt["status"])
    api = login_api(web, "", pw)
    j.check("sign-in answers 403 shared_password_retired", api["status"] == 403 and (api["body"] or {}).get("error") == "shared_password_retired",
            expect="403 {error: shared_password_retired}", actual=api)
    if form:
        submit(waiting)
        words = error_text(waiting)
        j.shot(waiting, "cutoff-page-says-so")
        j.check("the page says so in words", "no longer works" in words.lower(),
                expect="'The shared password no longer works. Ask an Owner to invite you.'", actual=words)
    page.goto(web + "/", wait_until="domcontentloaded")
    K.settle(page)
    j.shot(page, "cutoff-existing-session-sent-to-login")
    j.check("the console sends the old session to /login", "/login" in page.url or "/signin" in page.url,
            expect="/login", actual=page.url,
            known="FX-C (P1-17 dead ends): middleware only checks that a session cookie is PRESENT; with a refused one "
                  "the console renders 'Select your organization / Onboard a new organization' (no orgs) instead of /login",
            repro="sign in with the shared password; boss identity legacy retire --path shared_password; wait 6 s; open / "
                  "→ the empty org picker, not /login")
    _, fresh = K.new_page(env.browser, j)
    fresh.goto(web + "/login", wait_until="domcontentloaded")
    fresh.wait_for_selector("#email", timeout=30000)
    K.settle(fresh, 8000)
    j.shot(fresh, "cutoff-login-no-toggle")
    j.check("a fresh /login offers no shared password", fresh.locator("[data-testid=shared-password-toggle]").count() == 0,
            expect="no toggle", actual=fresh.locator("[data-testid=shared-password-toggle]").count())
    row = legacy_row(env)
    status = env.psql("SELECT status FROM identity_principals WHERE id = 'usr_legacy_shared'")
    j.check("the cutoff retired the mapping and disabled usr_legacy_shared", bool(row["retired_at"]) and status == "disabled",
            expect="retired_at set, principal disabled", actual={"status": status, **row})
    j.check("legacy.path_retired (reason 'cutoff reached') is in the security log",
            int(env.psql("SELECT count(*) FROM identity_security_events WHERE event_type = 'legacy.path_retired' "
                         "AND target_id = 'usr_legacy_shared' AND details->>'reason' = 'cutoff reached'") or 0) >= 1,
            expect="≥1 event", actual=env.psql("SELECT count(*) FROM identity_security_events WHERE event_type = 'legacy.path_retired'"))
    for c in (ctx, k2):
        c.close()


# ---- 2. the host tools' key ---------------------------------------------------------------------

def key_shape_ok(v: str) -> bool:
    return bool(SAK.fullmatch(v or ""))


def j_host_key(env: Env, j: Journey) -> None:
    hard = [n for n, line in enumerate((K.REPO / "scripts/install/checkup.sh").read_text().splitlines(), 1)
            if "boss-community" in line and not line.lstrip().startswith("#")]
    if hard:
        env.finding("checkup.sh (boss doctor) hardcodes the compose project's container names",
                    f"scripts/install/checkup.sh lines {hard}: `docker exec boss-community-api-1|web-1` and `ps --filter "
                    "name=boss-community`. boss.sh honours COMPOSE_PROJECT_NAME, but on an install under any other project "
                    "name boss doctor reads ANOTHER install's containers (or none): the sandbox-fence and claim checks then "
                    "report on the wrong BOSS. The walk runs doctor through a docker shim that maps the names.", "unassigned")
    st = env.ensure_s1()
    had = bool(env.env_value("BOSS_HOST_API_KEY"))
    j.note(f"BOSS_HOST_API_KEY in .env before: {had}")
    r = env.boss("identity", "host-key")
    key = env.env_value("BOSS_HOST_API_KEY")
    j.check("`boss identity host-key` succeeds and says the key is in .env", r["rc"] == 0 and "BOSS_HOST_API_KEY" in r["out"],
            expect="rc 0, names BOSS_HOST_API_KEY", actual=K.scrub(r["out"] + r["err"])[-300:])
    j.check("the key is never printed by boss.sh", not SAK.search(r["out"] + r["err"]),
            expect="no boss_sak_… in stdout/stderr", actual="key-shaped text present" if SAK.search(r["out"] + r["err"]) else "none")
    j.check(".env holds BOSS_HOST_API_KEY with the shape boss_sak_…, file mode 0600",
            key_shape_ok(key) and (os.stat(st.env_file).st_mode & 0o777) == 0o600,
            expect="boss_sak_[A-Za-z0-9]+, 0600", actual={"shape": key_shape_ok(key), "mode": oct(os.stat(st.env_file).st_mode & 0o777)})
    once = st.in_api("python", "-m", "boss_engine.identity.cli", "host-key", "--keep-others")
    printed = SAK.findall(once.stdout)
    for p in printed:
        K.secret(p)
    j.check("the host-shell CLI prints a new key exactly once (to the shell that asked)",
            once.returncode == 0 and len(printed) == 1 and not SAK.search(once.stderr),
            expect="one boss_sak_ token on stdout", actual={"rc": once.returncode, "tokens": len(printed)})
    me = st.stack.bearer("GET", "/v1/identity/me", key)
    j.check("the .env key authenticates as the service account 'Host tools'",
            me[0] == 200 and (me[1] or {}).get("principal_id") == "sva_host_tools" and (me[1] or {}).get("display_name") == "Host tools",
            expect="200 sva_host_tools 'Host tools'", actual=no_csrf(me[1]) if isinstance(me[1], dict) else me)

    for cmd in (("status",), ("doctor",), ("open",)):
        reset_last_used(env)
        t0 = host_key_last_used(env)
        rr = env.boss(*cmd)
        t1 = host_key_last_used(env)
        okword = {"status": "healthy", "doctor": "BOSS is ready", "open": "http://127.0.0.1:"}[cmd[0]]
        uses_key = cmd[0] == "status" or (t1 > t0)
        j.check(f"`boss {cmd[0]}` succeeds" + ("" if cmd[0] == "status" else " and called the API with the host key"),
                rr["rc"] == 0 and okword.lower() in (rr["out"] + rr["err"]).lower() and uses_key,
                expect=f"rc 0, '{okword}'" + ("" if cmd[0] == "status" else ", host key last_used_at advanced"),
                actual={"rc": rr["rc"], "tail": K.scrub(rr["out"])[-260:], "last_used": [t0, t1]})

    octx, opage = env.owner_page(j)
    try:
        lst = K.api(opage, "GET", "/v1/identity/service-accounts")
        names = [(s.get("principal_id") or s.get("id"), s.get("name") or s.get("display_name"))
                 for s in (lst.get("body") or {}).get("service_accounts", [])]
        j.check("the Owner sees 'Host tools' under /v1/identity/service-accounts",
                lst["status"] == 200 and any(n == "Host tools" for _, n in names), expect="a service account named Host tools",
                actual=names)
        one = K.api(opage, "GET", "/v1/identity/service-accounts/sva_host_tools")
        j.check("it holds the host_tools role", one["status"] == 200 and "host_tools" in ((one.get("body") or {}).get("access_roles") or []),
                expect="access_roles ∋ host_tools", actual=(one.get("body") or {}).get("access_roles"))
        opage.goto(st.web + "/identity/service-accounts", wait_until="domcontentloaded")
        K.settle(opage)
        j.shot(opage, "owner-service-accounts")
        j.check("the console's Service accounts screen lists Host tools", "Host tools" in K.body_text(opage, 20000),
                expect="'Host tools' on /identity/service-accounts", actual=K.body_text(opage, 300))

        for path, body in (("/v1/identity/users", {"email": "fxd3-hk@example.test", "bindings": [{"role_key": "viewer", "scope_type": "org"}]}),
                           ("/v1/identity/service-accounts", {"name": "fxd3-hk-sa", "description": "x"})):
            w = st.stack.bearer("POST", path, key, body)
            j.check(f"the host key cannot change people or access: POST {path} → 403", w[0] == 403, expect="403", actual=w)
        ok = st.stack.bearer("GET", f"/v1/settings?project_id={PROJECT}", key)
        j.check("the host key does ordinary work (GET /v1/settings)", ok[0] == 200, expect="200", actual=ok[0])

        shared = env.env_value("BOSS_API_KEY")
        b0 = st.stack.bearer("GET", "/v1/projects", shared)
        rr = env.boss("identity", "legacy", "retire", "--path", "console_api_key")
        time.sleep(CACHE_S)
        b1 = st.stack.bearer("GET", "/v1/projects", shared)
        j.check("the shared BOSS_API_KEY is retired from the host shell (worked before, refused after)",
                b0[0] == 200 and rr["rc"] == 0 and b1[0] in (401, 403), expect="200 → retire → 401/403",
                actual={"before": b0[0], "retire_rc": rr["rc"], "after": b1})
        for cmd in (("status",), ("doctor",)):
            reset_last_used(env)
            t0 = host_key_last_used(env)
            rr = env.boss(*cmd)
            t1 = host_key_last_used(env)
            j.check(f"with the shared key retired, `boss {cmd[0]}` still works (host key used)",
                    rr["rc"] == 0 and (cmd[0] == "status" or t1 > t0), expect="rc 0" + ("" if cmd[0] == "status" else ", last_used advanced"),
                    actual={"rc": rr["rc"], "tail": K.scrub(rr["out"])[-200:], "last_used": [t0, t1]})

        keys = K.api(opage, "GET", "/v1/identity/service-accounts/sva_host_tools/keys")
        live = [k for k in (keys.get("body") or {}).get("keys", []) if not k.get("revoked_at") and k.get("status") not in ("revoked",)]
        j.note(f"live host keys before revoke: {len(live)}")
        revoked = []
        for k in live:
            kid = k.get("id") or k.get("key_id")
            rv = K.stepped(opage, env.A["owner"], "POST", f"/v1/identity/service-accounts/sva_host_tools/keys/{kid}/revoke", {}, j)
            revoked.append(rv["status"])
        dead = st.stack.bearer("GET", "/v1/identity/me", key)
        j.check("the Owner revokes the host keys; the old key is refused", bool(revoked) and all(s in (200, 204) for s in revoked) and dead[0] == 401,
                expect="revoke 200, old key 401", actual={"revoke": revoked, "old_key": dead[0]})
        rr = env.boss("doctor")
        new = env.env_value("BOSS_HOST_API_KEY")
        me2 = st.stack.bearer("GET", "/v1/identity/me", new) if new else (0, None)
        j.check("the next boss command issues a new key into .env, and it works",
                rr["rc"] == 0 and key_shape_ok(new) and new != key and me2[0] == 200 and (me2[1] or {}).get("principal_id") == "sva_host_tools"
                and not SAK.search(rr["out"] + rr["err"]),
                expect="doctor rc 0, a different boss_sak_ key in .env, 200 as sva_host_tools, nothing printed",
                actual={"rc": rr["rc"], "changed": new != key, "me": me2[0]})
    finally:
        octx.close()


# ---- 3. make mcp-setup ----------------------------------------------------------------------------

def j_mcp_setup(env: Env, j: Journey) -> None:
    st = env.ensure_s1()
    rc5 = env.rc5
    hexkey = getattr(env, "_rc5_key", "")
    j.note(f"rc5 make mcp-setup before the claim: {short(rc5, 400)}")
    works = st.stack.bearer("GET", "/v1/projects", hexkey)[0] if hexkey else None
    j.check("the rc5 state exists: a hex MCP key bound to mcp_operator in .env, working after the claim",
            bool(hexkey) and rc5.get("bound") and works == 200,
            expect="hex key in BOSS_API_KEYS bound to mcp_operator, GET /v1/projects 200", actual={**rc5, "after_claim": works})
    r = U.run_tool(["make", "mcp-setup"], env.tools(), env.inst, timeout=600)
    st.secrets_now()
    j.check("`make mcp-setup` succeeds and prints no key", r["rc"] == 0 and not SAK.search(r["out"] + r["err"])
            and (not hexkey or hexkey not in r["out"] + r["err"]),
            expect="rc 0, 'MCP access ready', no key", actual=K.scrub(r["out"] + r["err"])[-300:])
    st.wait_ready(240)
    vals = U.read_env(st.env_file)
    key = vals.get("BOSS_MCP_API_KEY", "")
    K.secret(key)
    j.check(".env: BOSS_MCP_API_KEY is a boss_sak_ key for project boss-dogfood", key_shape_ok(key) and vals.get("BOSS_MCP_PROJECT_ID") == PROJECT,
            expect="boss_sak_…, BOSS_MCP_PROJECT_ID=boss-dogfood", actual={"shape": key_shape_ok(key), "project": vals.get("BOSS_MCP_PROJECT_ID")})
    if hexkey:
        j.check("the rc5 hex key is gone from BOSS_API_KEYS and its mcp_operator binding removed",
                hexkey not in vals.get("BOSS_API_KEYS", "") and hexkey not in vals.get("BOSS_API_KEY_PRINCIPALS", ""),
                expect="hex key absent from both", actual={"in_keys": hexkey in vals.get("BOSS_API_KEYS", ""),
                                                            "in_principals": hexkey in vals.get("BOSS_API_KEY_PRINCIPALS", "")})
        old = st.stack.bearer("GET", "/v1/projects", hexkey)
        j.check("the rc5 hex key no longer works after the api restart", old[0] == 401, expect="401", actual=old[0])
    sess = st.stack.bearer("GET", "/v1/auth/session", key)
    sb = sess[1] or {}
    j.check('it is service account "MCP (boss-dogfood)" and the key is pinned to that project',
            sess[0] == 200 and (sb.get("principal") or {}).get("kind") == "service_account"
            and (sb.get("principal") or {}).get("display_name") == f"MCP ({PROJECT})" and sb.get("key_projects") == [PROJECT],
            expect="principal.kind service_account, display_name MCP (boss-dogfood), key_projects [boss-dogfood]",
            actual={k: sb.get(k) for k in ("principal", "key_projects")})
    pid = (sb.get("principal") or {}).get("id", "")
    want = st.in_api("python", "-c", "import json; from boss_engine.identity.host_tools import mcp_permissions; "
                     "print(json.dumps(sorted(mcp_permissions())))")
    want_set = set(json.loads(want.stdout.strip().splitlines()[-1])) if want.returncode == 0 else set()
    role_perms = set(filter(None, env.psql(
        "SELECT p.permission_key FROM identity_role_bindings b JOIN identity_access_role_permissions p ON p.role_id = b.role_id "
        "WHERE b.subject_type = 'principal' AND b.subject_id = :'p' AND b.scope_type = 'project' AND b.scope_id = :'proj'",
        p=pid, proj=PROJECT).splitlines()))
    other_bindings = env.psql("SELECT count(*) FROM identity_role_bindings WHERE subject_id = :'p' AND NOT "
                              "(scope_type = 'project' AND scope_id = :'proj')", p=pid, proj=PROJECT)
    scopes = set(filter(None, env.psql("SELECT unnest(scopes) FROM identity_api_credentials WHERE principal_id = :'p' "
                                      "AND revoked_at IS NULL", p=pid).splitlines()))
    j.check("its role (bound in boss-dogfood only) holds exactly the chosen capabilities' permissions",
            bool(want_set) and role_perms == want_set and other_bindings == "0",
            expect=f"{len(want_set)} permissions = host_tools.mcp_permissions(defaults); no other binding",
            actual={"n": len(role_perms), "extra": sorted(role_perms - want_set)[:10], "missing": sorted(want_set - role_perms)[:10],
                    "other_bindings": other_bindings})
    j.check("the key's scopes are exactly that set", scopes == want_set, expect="equal sets",
            actual={"n": len(scopes), "extra": sorted(scopes - want_set)[:10], "missing": sorted(want_set - scopes)[:10]})
    mp = st.stack.bearer("GET", f"/v1/identity/me/permissions?project_id={PROJECT}", key)
    org = st.stack.bearer("GET", "/v1/identity/me/permissions", key)
    j.check("its effective permissions: that set in boss-dogfood, nothing org-wide",
            mp[0] == 200 and set((mp[1] or {}).get("permissions") or []) == want_set and (org[1] or {}).get("permissions") == [],
            expect="project set == chosen, org []", actual={"project_n": len((mp[1] or {}).get("permissions") or []),
                                                          "want_n": len(want_set),
                                                          "extra": sorted(set((mp[1] or {}).get("permissions") or []) - want_set),
                                                          "missing": sorted(want_set - set((mp[1] or {}).get("permissions") or [])),
                                                          "org": (org[1] or {}).get("permissions")},
            known=f"{KNOWN} (P0-1): host_tools.mcp_permissions adds MEMBER_BASELINE's org-scope permissions (org:read, "
                  "directory:read, access_role:read, api_key:manage_own) to a role bound at PROJECT scope, where they never "
                  "apply: the role and key list 64 permissions, the key can use 60")
    env.psql("INSERT INTO projects (id, workspace_id, name, slug, created_at) SELECT :'o', workspace_id, 'FXD other', :'o', now() "
             "FROM projects WHERE id = :'p' ON CONFLICT DO NOTHING", o=OTHER_PROJECT, p=PROJECT)
    here = st.stack.bearer("GET", f"/v1/settings?project_id={PROJECT}", key)
    there = st.stack.bearer("GET", f"/v1/settings?project_id={OTHER_PROJECT}", key)
    j.check("a read naming its own project is allowed; naming another project is refused",
            here[0] == 200 and there[0] in (403, 404), expect="200 / 403|404", actual={"own": here[0], "other": there})
    t_here = st.stack.bearer("POST", "/v1/code-tasks", key, {"project_id": PROJECT, "title": "fxd3 mcp task", "simulated": True})
    t_there = st.stack.bearer("POST", "/v1/code-tasks", key, {"project_id": OTHER_PROJECT, "title": "fxd3 mcp task", "simulated": True})
    j.check("a chosen capability (task_create) works in its project and is refused in another",
            t_here[0] == 200 and t_there[0] in (403, 404), expect="200 / 403|404", actual={"own": t_here[0], "other": t_there})
    w = st.stack.bearer("POST", "/v1/identity/users", key, {"email": "fxd3-mcp@example.test", "bindings": []})
    j.check("it cannot change people or access (403)", w[0] == 403, expect="403", actual=w)
    lst = st.stack.bearer("GET", f"/v1/code-tasks?project_id={PROJECT}", key)
    j.check("it can list its own project's code tasks (it holds code_task:read there)", lst[0] == 200,
            expect="GET /v1/code-tasks?project_id=boss-dogfood → 200", actual=lst,
            known=f"{KNOWN} (P0-1): GET /v1/code-tasks is permitted at Scope.org() (apps/api/app/main.py list_code_tasks_endpoint), "
                  "so a project-pinned MCP key with code_task:read in its project gets 403",
            repro="make mcp-setup → curl -H 'Authorization: Bearer $BOSS_MCP_API_KEY' :API/v1/code-tasks?project_id=boss-dogfood → 403 code_task:read")
    again = U.run_tool(["make", "mcp-setup"], env.tools(), env.inst, timeout=600)
    j.check("running make mcp-setup again keeps the same key (idempotent)", again["rc"] == 0 and U.read_env(st.env_file).get("BOSS_MCP_API_KEY") == key,
            expect="rc 0, key unchanged", actual={"rc": again["rc"], "same": U.read_env(st.env_file).get("BOSS_MCP_API_KEY") == key})
    octx, opage = env.owner_page(j)
    try:
        lst2 = K.api(opage, "GET", "/v1/identity/service-accounts")
        names = [s.get("name") or s.get("display_name") for s in (lst2.get("body") or {}).get("service_accounts", [])]
        j.check(f'the Owner sees "MCP ({PROJECT})" under service accounts', f"MCP ({PROJECT})" in names, expect="listed", actual=names)
    finally:
        octx.close()


# ---- 5. the sandbox fence -------------------------------------------------------------------------

def sandbox_uid_activity(env: Env) -> dict[str, Any]:
    ps = env.st.in_api("sh", "-c", "ps -eo uid=,args= | awk '$1>=61000 && $1<62000' | wc -l; ls /run/boss-shim", user="root")
    lines = ps.stdout.split()
    return {"procs": int(lines[0]) if lines and lines[0].isdigit() else -1,
            "locks": [x for x in lines[1:] if x.startswith("uid-")], "shim": lines[1:]}


def start_build(env: Env, key: str, title: str) -> tuple[int, str]:
    r = env.st.stack.bearer("POST", "/v1/code-tasks", key, {"project_id": PROJECT, "title": title,
                                                             "instructions": "Add one line to the README.",
                                                             "owner": "fxd-org", "repo": "fxd-none"})
    return r[0], ((r[1] or {}).get("run_id") or "") if isinstance(r[1], dict) else ""


def run_state(env: Env, run_id: str) -> tuple[str, str]:
    row = env.psql("SELECT status || '|' || coalesce(error::text, '') FROM code_task_runs WHERE id = :'r'", r=run_id)
    st, _, err = (row or "|").partition("|")
    return st, err


def j_sandbox_fence(env: Env, j: Journey) -> None:
    override = FENCE_OVERRIDE.read_text()
    st = env.fresh_stack(override=override, seed_people=False)
    env.state = "dirty"
    health = st.api_health()
    j.check("without NET_ADMIN, under the default BOSS_SANDBOX_NETWORK=enforce, the api starts and is healthy",
            health == 200 and st.container_state().startswith("running"), expect="/health 200, running",
            actual={"health": health, "state": st.container_state()})
    caps = subprocess.run(["docker", "inspect", "-f", "{{json .HostConfig.CapAdd}}", f"{st.project}-api-1"], capture_output=True, text=True).stdout.strip()
    j.note(f"api CapAdd: {caps}")
    logs = st.logs("api", 400)
    j.check("the logs carry the loud warning", "BOSS IS STARTING WITHOUT IT" in logs and "could not install the sandbox network rules" in logs,
            expect="'could not install the sandbox network rules' … 'BOSS IS STARTING WITHOUT IT'",
            actual=[l for l in logs.splitlines() if "boss:" in l][:6])
    marker = st.in_api("sh", "-c", "test -f /run/boss-shim/.network-unfenced && cat /run/boss-shim/.network-unfenced; "
                       "test -f /run/boss-shim/.network-fenced && echo FENCED", user="root")
    j.check("/run/boss-shim/.network-unfenced exists (and .network-fenced does not)",
            "owner-match" in marker.stdout and "FENCED" not in marker.stdout, expect="the marker with its reason",
            actual=marker.stdout.strip())
    hk = env.boss("identity", "host-key")
    key = env.env_value("BOSS_HOST_API_KEY")
    doc = env.boss("doctor")
    line = next((l for l in K.scrub(doc["out"]).splitlines() if "sandbox" in l), "")
    j.check("`boss doctor` FAILs the sandbox check and names the fix",
            doc["rc"] == 1 and "FAIL" in line and "BOSS_SANDBOX_NETWORK=off" in line and "SANDBOX.md" in line,
            expect="rc 1, 'FAIL sandbox: this host cannot fence the build network … BOSS_SANDBOX_NETWORK=off …'",
            actual={"rc": doc["rc"], "line": re.sub(r"\x1b\[[0-9;]*m", "", line)})
    before = sandbox_uid_activity(env)
    code, run_id = start_build(env, key, "fxd3 fence held")
    j.check("a build can be started (the Host tools key, POST /v1/code-tasks)", code == 200 and bool(run_id),
            expect="200 queued", actual=code, fatal=True)
    deadline = time.time() + 180
    status, err = "", ""
    while time.time() < deadline:
        status, err = run_state(env, run_id)
        if status != "running":
            break
        time.sleep(3)
    j.check("the build is HELD: blocked, never run, with a reason that names the fix",
            status == "blocked" and "Sandbox unavailable" in err and "cannot fence" in err and "BOSS_SANDBOX_NETWORK=off" in err,
            expect="status blocked, 'Sandbox unavailable: this host cannot fence … BOSS_SANDBOX_NETWORK=off …'",
            actual={"status": status, "error": err[:400]})
    try:
        j.note("run row: " + env.psql("SELECT row_to_json(r)::text FROM (SELECT status, phase FROM code_task_runs "
                                      "WHERE id = :'r') r", r=run_id)[:300])
    except Exception as exc:  # noqa: BLE001
        j.note(f"run row not read: {type(exc).__name__}")
    after = sandbox_uid_activity(env)
    j.check("no build process ever ran under a sandbox uid (no uid lease, no process)",
            after["procs"] == 0 and not after["locks"], expect="0 processes in 61000-61999, no uid-*.lock",
            actual={"before": before, "after": after})
    # required: refuse to start
    vals = U.read_env(st.env_file)
    vals["BOSS_SANDBOX_NETWORK"] = "required"
    U.write_env(st.env_file, vals)
    st.compose("up", "-d", "--no-deps", "--force-recreate", "api", timeout=300)
    time.sleep(25)
    state = st.container_state()
    logs = st.logs("api", 120)
    j.check("BOSS_SANDBOX_NETWORK=required: the api refuses to start", st.api_health() != 200 and "refusing to start" in logs, expect="no /health, container exited/restarting, 'BOSS_SANDBOX_NETWORK=required: refusing to start.'",
            actual={"health": st.api_health(), "state": state, "log": [l for l in logs.splitlines() if "refusing" in l][:2]})

    # off: no fence, builds allowed
    vals["BOSS_SANDBOX_NETWORK"] = "off"
    U.write_env(st.env_file, vals)
    st.compose("up", "-d", "--no-deps", "--force-recreate", "api", timeout=300)
    deadline = time.time() + 180
    while time.time() < deadline and st.api_health() != 200:
        time.sleep(3)
    logs = st.logs("api", 200)
    marker = st.in_api("sh", "-c", "ls -a /run/boss-shim", user="root")
    j.check("BOSS_SANDBOX_NETWORK=off: the api starts with no fence, and says builds can reach its networks",
            st.api_health() == 200 and "BOSS_SANDBOX_NETWORK=off" in logs and ".network-unfenced" not in marker.stdout,
            expect="/health 200, the off line in the log, no .network-unfenced",
            actual={"health": st.api_health(), "shim": marker.stdout.split(), "log": [l for l in logs.splitlines() if "SANDBOX_NETWORK" in l][:2]})
    doc = env.boss("doctor")
    j.check("with the fence off by choice, boss doctor no longer fails the sandbox check",
            "cannot fence" not in doc["out"], expect="no sandbox FAIL line", actual={"rc": doc["rc"]})
    key = env.env_value("BOSS_HOST_API_KEY")
    code, run_id = start_build(env, key, "fxd3 fence off")
    deadline = time.time() + 90
    leased, status, err = False, "", ""
    while time.time() < deadline:
        status, err = run_state(env, run_id)
        act = sandbox_uid_activity(env)
        leased = leased or bool(act["locks"]) or act["procs"] > 0
        if "Sandbox unavailable" in err or (status not in ("running", "") and leased):
            break
        if leased:
            break
        time.sleep(3)
    j.check("BOSS_SANDBOX_NETWORK=off: a build is allowed (not held; it takes a sandbox uid)",
            code == 200 and "Sandbox unavailable" not in err and leased,
            expect="no sandbox_unavailable; a uid lease/process appears", actual={"status": status, "error": err[:200], "leased": leased})
    j.note("BOSS_SANDBOX_NETWORK=off means builds run without the network fence: they can reach this container's networks "
           "(the database, the host). That is the operator's choice, reported by boss doctor and posture.")


# ---- 4. upgrade.sh rolls back ---------------------------------------------------------------------

UP_PROJECT = "bossfxdup"
UP_PORTS = {"BOSS_API_PORT": "58400", "BOSS_POSTGRES_PORT": "55804", "BOSS_PREVIEW_PORT": "58401",
            "BOSS_WEB_PORT": "53400", "INFISICAL_PORT": "58480"}
OLD_TAG, BAD_TAG = "v9.0.0-fxdold", "v9.0.1-fxdbroken"
BROKEN = """
    # FXD BROKEN RELEASE (tests/e2e/identity/upgrade_walk.py): this API can never start.
    command: ["sh", "-c", "echo 'fxd-broken-release: this API refuses to start' >&2; exit 3"]
"""


def git(*a: str, cwd: Optional[Path] = None, check: bool = True) -> str:
    done = subprocess.run(["git", *a], cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=300)
    if check and done.returncode != 0:
        raise RuntimeError(f"git {' '.join(a[:3])} failed: {done.stderr.strip()[-300:]}")
    return done.stdout.strip()


def up_compose(inst: Path, *args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "compose", "-p", UP_PROJECT, "-f", str(inst / "scripts/install/community-compose.yml"),
                           "--env-file", str(inst / ".env"), *args], capture_output=True, text=True, timeout=timeout)


def up_down(inst: Optional[Path]) -> None:
    if inst and (inst / ".env").exists():
        up_compose(inst, "--profile", "web", "down", "-v", "--remove-orphans", timeout=600)
    else:
        subprocess.run(["docker", "compose", "-p", UP_PROJECT, "down", "-v", "--remove-orphans"], capture_output=True, timeout=600)


def j_upgrade_rollback(env: Env, j: Journey) -> None:
    root = env.work / "upgrade"
    if root.exists():
        up_down(root / "install")
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    origin, work, inst, home, bins, backups = (root / "origin.git", root / "work", root / "install", root / "home",
                                               root / "bin", root / "backups")
    for d in (home, bins, backups):
        d.mkdir()
    env.wait_headroom(1.5)
    git("clone", "--bare", "--no-local", "-q", str(K.REPO), str(origin))
    git("--git-dir", str(origin), "tag", OLD_TAG, "HEAD")
    git("clone", "-q", str(origin), str(work))
    git("-C", str(work), "checkout", "-q", "--detach", OLD_TAG)
    compose = work / "scripts/install/community-compose.yml"
    text = compose.read_text()
    anchor = "    user: \"0:0\"\n"
    if anchor not in text:
        raise RuntimeError("the compose file changed shape: no api `user: \"0:0\"` line to anchor the broken command")
    compose.write_text(text.replace(anchor, anchor + BROKEN.lstrip("\n"), 1))
    git("-C", str(work), "-c", "user.name=FXD", "-c", "user.email=fxd@example.test", "commit", "-q", "-am",
        "fxd: a release whose API cannot start")
    git("-C", str(work), "tag", BAD_TAG)
    git("-C", str(work), "push", "-q", "origin", BAD_TAG)
    old_commit = git("--git-dir", str(origin), "rev-parse", f"{OLD_TAG}^{{commit}}")
    bad_commit = git("--git-dir", str(origin), "rev-parse", f"{BAD_TAG}^{{commit}}")
    git("clone", "-q", str(origin), str(inst))
    git("-C", str(inst), "checkout", "-q", "--detach", OLD_TAG)
    U.make_shim(env.shim)
    tenv = U.tool_env(UP_PROJECT, env.shim, home=home, tag=UP_PROJECT, extra={
        **UP_PORTS, "BOSS_REPO_URL": f"file://{origin}", "BOSS_BIN_DIR": str(bins)})
    tenv.pop("BOSS_COMMUNITY_IMAGE_TAG", None)          # install.sh tags images by the project: bossfxdup
    t0 = time.time()
    install_args = ["bash", str(inst / "install.sh"), "--dir", str(inst), "--ref", OLD_TAG, "--yes", "--skip-model-key",
                    "--no-web", "--no-boss-command", "--no-discover", "--no-local-model",
                    "--api-port", UP_PORTS["BOSS_API_PORT"], "--postgres-port", UP_PORTS["BOSS_POSTGRES_PORT"],
                    "--backup-dir", str(backups)]
    default = U.run_tool(install_args, tenv, inst, timeout=2400)
    (root / "install-default.log").write_text(K.scrub(default["out"] + "\n--- stderr\n" + default["err"]))
    probe = next((l.strip() for l in (default["out"] + default["err"]).splitlines() if "Edition probe" in l), "")
    j.check("a fresh install of this branch with the defaults (BOSS_AUTHZ_MODE=enforce) succeeds", default["rc"] == 0,
            expect="install.sh rc 0", actual={"rc": default["rc"], "seconds": round(time.time() - t0), "line": K.scrub(probe)},
            known="unassigned (P0, pre-existing since 29624fad 'enforce by default'): install.sh's last check GET "
                  "/v1/edition?project_id=boss-dogfood with BOSS_API_KEY answers 403 \"Actor 'sys_legacy_console' is not an "
                  "active member of org 'org_boss'\" under enforce (200 under shadow), so install.sh exits 1 'Stopped during: "
                  "Verifying Community independence' with BOSS up",
            repro="fresh compose stack of this branch, enforce, bootstrap seed; curl -H 'Authorization: Bearer $BOSS_API_KEY' "
                  ":API/v1/edition?project_id=boss-dogfood → 403")
    if default["rc"] != 0:
        # The rollback under test needs an install.sh that completes: the same install in shadow mode.
        tenv["BOSS_AUTHZ_MODE"] = "shadow"
        j.note("installing again with BOSS_AUTHZ_MODE=shadow so upgrade.sh's rollback can be exercised")
    t0 = time.time()
    first = U.run_tool(install_args, tenv, inst, timeout=2400) if default["rc"] != 0 else default
    (root / "install-first.log").write_text(K.scrub(first["out"] + "\n--- stderr\n" + first["err"]))
    api = f"http://127.0.0.1:{UP_PORTS['BOSS_API_PORT']}"
    j.check("the previous release installs with the real install.sh (own compose project)",
            first["rc"] == 0 and K.http("GET", api + "/health")[0] == 200, expect="rc 0, /health 200",
            actual={"rc": first["rc"], "seconds": round(time.time() - t0), "tail": K.scrub(first["err"] or first["out"])[-400:]}, fatal=True)
    for k, v in U.read_env(inst / ".env").items():
        if any(w in k for w in ("KEY", "PASSWORD", "SECRET", "TOKEN")):
            K.secret(v)
    # data + a person who signs in
    host = I.compose_host(UP_PROJECT, inst / "scripts/install/community-compose.yml", inst / ".env")
    token = K.secret(I.setup_token(host))
    person = I.Claimant(name="FXD Up Owner", email="fxd-up-owner@example.test", password=K.secret(I.claim_password()))
    I.claim_owner(api, token, person)
    up_db = f"{UP_PROJECT}-db-1"

    def upsql(sql: str) -> str:
        d = subprocess.run(["docker", "exec", "-i", up_db, "psql", "-U", "boss", "-d", "boss", "-At", "-v", "ON_ERROR_STOP=1"],
                           input=sql, capture_output=True, text=True, timeout=60)
        return d.stdout.strip() if d.returncode == 0 else "ERR " + K.scrub(d.stderr)[-200:]

    upsql("INSERT INTO projects (id, workspace_id, name, slug, created_at) SELECT 'fxd-marker', workspace_id, "
          "'FXD marker', 'fxd-marker', now() FROM projects LIMIT 1 ON CONFLICT DO NOTHING")
    fingerprint = upsql("SELECT (SELECT count(*) FROM projects WHERE id = 'fxd-marker') || '|' || (SELECT count(*) FROM identity_principals) "
                "|| '|' || (SELECT principal_id FROM identity_users WHERE email = 'fxd-up-owner@example.test') || '|' || "
                "(SELECT count(*) FROM identity_security_events WHERE event_type = 'bootstrap.claimed')")
    sign0 = login_api(api, person.email, person.password)
    j.note(f"before: data {fingerprint}; sign-in {sign0['status']} next={(sign0['body'] or {}).get('next')}")

    # the failing upgrade, with rollback
    t1 = time.time()
    up = U.run_tool(["bash", str(inst / "scripts/install/upgrade.sh"), "--dir", str(inst), "--ref", BAD_TAG,
                     "--backup-dir", str(backups)], tenv, inst, timeout=2400)
    log = K.scrub(up["out"] + "\n--- stderr\n" + up["err"])
    (root / "upgrade-rollback.log").write_text(log)
    head = git("-C", str(inst), "rev-parse", "HEAD")
    health = K.http("GET", api + "/health")[0]
    j.check("upgrade.sh to a release whose API cannot start fails (exit ≠ 0)", up["rc"] != 0, expect="rc ≠ 0",
            actual={"rc": up["rc"], "seconds": round(time.time() - t1)})
    j.check("the failure is explained (the API did not become healthy; the previous release is being put back)",
            "did not become healthy" in log and "Putting back the previous release" in log and "Rolled back" in log,
            expect="'API did not become healthy' … 'Putting back the previous release' … 'Rolled back: … running again'",
            actual=[l for l in log.splitlines() if any(w in l for w in ("healthy", "Putting back", "Rolled back", "rollback", "Upgrade failed"))][:8])
    j.check("the broken release's own log reached the operator (fxd-broken-release)", "fxd-broken-release" in log,
            expect="the api's last log lines include the broken release's message", actual="present" if "fxd-broken-release" in log else "absent")
    j.check("the previous release's source is back (HEAD = the old tag's commit)", head == old_commit,
            expect=old_commit[:12], actual=head[:12])
    cmd = subprocess.run(["docker", "inspect", "-f", "{{json .Config.Cmd}}", f"{UP_PROJECT}-api-1"], capture_output=True, text=True).stdout
    j.check("the previous release is running again: /health 200, the api runs its own command", health == 200 and "fxd-broken" not in cmd,
            expect="200, no broken command", actual={"health": health, "cmd": cmd.strip()[:120]})
    fp2 = upsql("SELECT (SELECT count(*) FROM projects WHERE id = 'fxd-marker') || '|' || (SELECT count(*) FROM identity_principals) "
                "|| '|' || (SELECT principal_id FROM identity_users WHERE email = 'fxd-up-owner@example.test') || '|' || "
                "(SELECT count(*) FROM identity_security_events WHERE event_type = 'bootstrap.claimed')")
    a, b = fingerprint.split("|"), fp2.split("|")
    j.check("the same data is there: the marker project, the Owner, the claim, no principal lost",
            "ERR" not in fingerprint + fp2 and len(a) == len(b) == 4 and a[0] == b[0] == "1" and a[2] == b[2]
            and a[2].startswith("usr_") and a[3] == b[3] == "1" and int(b[1]) >= int(a[1]),
            expect=f"marker 1, owner {a[2] if len(a) > 2 else '?'}, claimed 1, principals ≥ {a[1] if len(a) > 1 else '?'}",
            actual=fp2)
    if len(a) == len(b) == 4 and int(b[1]) > int(a[1]):
        j.note(f"principals {a[1]} → {b[1]}: the installer's re-seed adds principals on each run (not data loss)")
    sign1 = login_api(api, person.email, person.password)
    j.check("people can still sign in (the Owner's password is accepted)", sign1["status"] == 200 and sign0["status"] == 200
            and (sign1["body"] or {}).get("next") == (sign0["body"] or {}).get("next"),
            expect=f"200 next={(sign0['body'] or {}).get('next')}", actual={"status": sign1["status"], "next": (sign1["body"] or {}).get("next")})
    rel = (inst / ".boss-release").read_text() if (inst / ".boss-release").exists() else ""
    j.check("the release record names the previous release again", f"BOSS_RELEASE_REF={OLD_TAG}" in rel,
            expect=f"BOSS_RELEASE_REF={OLD_TAG}", actual=[l for l in rel.splitlines() if l.startswith("BOSS_RELEASE")])

    # --no-rollback leaves it down
    t2 = time.time()
    nr = U.run_tool(["bash", str(inst / "scripts/install/upgrade.sh"), "--dir", str(inst), "--ref", BAD_TAG,
                     "--backup-dir", str(backups), "--no-rollback"], tenv, inst, timeout=2400)
    nlog = K.scrub(nr["out"] + "\n--- stderr\n" + nr["err"])
    (root / "upgrade-no-rollback.log").write_text(nlog)
    head2 = git("-C", str(inst), "rev-parse", "HEAD")
    h2 = K.http("GET", api + "/health")[0]
    j.check("--no-rollback: the upgrade fails and leaves the new release in place, down",
            nr["rc"] != 0 and head2 == bad_commit and h2 != 200 and "Putting back" not in nlog,
            expect="rc ≠ 0, HEAD = the broken release, /health not 200, no rollback",
            actual={"rc": nr["rc"], "head": head2[:12], "health": h2, "seconds": round(time.time() - t2),
                    "tail": [l for l in nlog.splitlines() if "Upgrade failed" in l or "Inspect Compose" in l][:3]})
    up_down(inst)
    left = subprocess.run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={UP_PROJECT}", "-q"],
                          capture_output=True, text=True).stdout.split()
    j.check("the scratch upgrade project is removed (down -v)", not left, expect="no containers", actual=len(left))


# ---- the runner -----------------------------------------------------------------------------------

def run(env: Env, jid: str, title: str, proves: str, fn: Callable[[Env, Journey], None]) -> dict[str, Any]:
    j = Journey(jid, title, proves, env.shots)
    try:
        fn(env, j)
    except K.JourneyAbort as exc:
        j.error = f"aborted at: {exc}"
        j.shoot_open_pages()
    except Exception as exc:  # noqa: BLE001
        j.error = K.fmt_exc(exc)
        j.note("ERROR " + j.error)
        j.shoot_open_pages()
    finally:
        for pg in j.pages:
            try:
                pg.context.close()
            except Exception:  # noqa: BLE001
                pass
        j.finished = time.time()
    return j.as_dict()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default=",".join(SCENARIOS), help="comma list of: " + ", ".join(SCENARIOS))
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="report + scratch folder")
    ap.add_argument("--shots", default=str(DEFAULT_SHOTS))
    ap.add_argument("--shape-env", default=str(DEFAULT_OUT / "stack.env"), help="an env file whose SHAPE is copied")
    ap.add_argument("--env-file", default=str(DEFAULT_OUT / "stack3.env"))
    ap.add_argument("--keep", action="store_true", help="leave bossfxd3 up at the end (debugging)")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args(argv)
    args.selected = [s.strip() for s in args.only.split(",") if s.strip()]
    bad = [s for s in args.selected if s not in SCENARIOS]
    if bad:
        ap.error(f"unknown scenario(s): {bad}")
    Path(args.out).mkdir(parents=True, exist_ok=True)
    Path(args.shots).mkdir(parents=True, exist_ok=True)
    started = time.time()
    results: list[dict[str, Any]] = []
    report = Path(args.out) / "upgrade-walk.json"
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        env = Env(args, browser)
        plan: list[tuple[str, str, str, Callable[[Env, Journey], None]]] = []
        sel = args.selected
        if "shared_password" in sel:
            plan.append(("shared_password", "The shared password after the claim (S1)",
                         "claim_first before the claim; Operator only after it via /login; banner on every page; session ≤ cutoff; "
                         "uniform failure; one capped, audited Owner extension; CLI retire ends it and its sessions", j_shared_password))
        if "host_key" in sel:
            plan.append(("host_key", "boss identity host-key and boss.sh with BOSS_HOST_API_KEY",
                         "the host tools' own service-account key works for boss commands, cannot change people/access, outlives "
                         "the shared key, and is re-issued when revoked", j_host_key))
        if "mcp_setup" in sel:
            plan.append(("mcp_setup", "make mcp-setup: a per-project service account",
                         "MCP (<project>) with exactly the chosen permissions, key pinned to the project, rc5 hex key unbound", j_mcp_setup))
        if "shared_password" in sel:
            plan.append(("shared_password_cutoff", "The shared password at the cutoff (clock moved)",
                         "past claim + 30 days: 403 shared_password_retired, the page says so, the existing session ends",
                         j_shared_password_cutoff))
        if "sandbox_fence" in sel:
            plan.append(("sandbox_fence", "A host that cannot fence the sandbox network",
                         "enforce: up, loud, marker, doctor FAIL with the fix, builds held; required: refuses; off: no fence, builds run",
                         j_sandbox_fence))
        try:
            for jid, title, proves, fn in plan:
                print(f"\n=== {jid}: {title}", flush=True)
                results.append(run(env, jid, title, proves, fn))
                _write(report, args, results, env, started)
            if env.st is not None and not args.keep:
                env.st.down()
                env.st = None
            if "upgrade_rollback" in sel:
                print("\n=== upgrade_rollback", flush=True)
                results.append(run(env, "upgrade_rollback", "upgrade.sh rolls back a release that cannot start",
                                   "the previous release is put back and running (health, data, sign-in) with the failure "
                                   "explained; --no-rollback leaves it down", j_upgrade_rollback))
        finally:
            if env.st is not None and not args.keep:
                env.st.down()
            up_down(env.work / "upgrade" / "install")
            down = {p: subprocess.run(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={p}", "-q"],
                                      capture_output=True, text=True).stdout.split() for p in ("bossfxd3", UP_PROJECT)}
            vols = subprocess.run(["docker", "volume", "ls", "-q", "--filter", "name=bossfxd3_"], capture_output=True, text=True).stdout.split() + \
                subprocess.run(["docker", "volume", "ls", "-q", "--filter", f"name={UP_PROJECT}_"], capture_output=True, text=True).stdout.split()
            env.teardown = {"containers_left": {k: len(v) for k, v in down.items()}, "volumes_left": len(vols), "kept": args.keep}
            _write(report, args, results, env, started)
            browser.close()
    fails = sum(1 for r in results for c in r["checks"] if c["status"] == "fail") + sum(1 for r in results if r["error"])
    print(f"\nreport: {report}\nshots: {args.shots}\nfailed checks/errors: {fails}", flush=True)
    return 1 if fails else 0


def _write(path: Path, args: argparse.Namespace, results: list[dict[str, Any]], env: Env, started: float) -> None:
    doc = {"walk": "upgrade_walk", "started": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
           "seconds": round(time.time() - started), "only": args.selected, "images": "boss-community-*:fxda",
           "summary": {r["id"]: r["status"] for r in results},
           "counts": {s: sum(1 for r in results for c in r["checks"] if c["status"] == s) for s in ("pass", "fail", "xfail")},
           "findings": env.findings, "teardown": getattr(env, "teardown", None), "journeys": results}
    path.write_text(K.scrub(json.dumps(doc, indent=1, default=str)))


if __name__ == "__main__":
    raise SystemExit(main())
