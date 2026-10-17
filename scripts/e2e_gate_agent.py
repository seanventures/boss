#!/usr/bin/env python3
"""Tier B of the release gate: ONE real, model-driven build, from start to an opened PR.

    scripts/e2e-gate.sh agent [--brief <name>] [--with-connector] [--budget-min N] [--keep]

A known-good brief (tests/e2e/briefs/<name>.json) is started on the LOCAL install (the laptop's
boss-community-* stack: API http://localhost:8000, console http://localhost:3000) by a person,
through BOSS's API, the way the console starts it: signed in with email + password + the
authenticator code, the consent sheet answered (a `task` grant for this build's repository on a
409 consent_required), and a stale second factor stepped up on a 401 step_up_required. Nobody
clicks anything.

It then watches the run every POLL_S seconds and fails FAST, with the reason, on: the run blocked,
failed or cancelled; a pending decision for this build that the gate does not answer; no agent
turn for IDLE_MIN minutes; or the time budget (25 min for the plain brief, 30 with a connector).
At the end it asserts what a person would check before calling the build good:

  * completed, with a PR opened;
  * no advisory-failing gates (code_task.gate_advisory absent, or empty);
  * the app started (the latest code_task.app_serve_checked is ok=true);
  * the install is not left broken (no code_task.install_failed without a later install_fixed or
    a later successful app start, which installs from clean);
  * the test budget not over (code_task.test_budget over=false, when the run recorded one);
  * code_task.build_profile recorded.

It prints the build's stage table (`python -m boss_engine.tools.build_profile <run>` inside
boss-community-worker-1) and one PASS/FAIL line with the minutes. After a PASS it closes the PR,
deletes its branch on the sandbox repository and withdraws the build's merge question; after a
FAIL it keeps all of them and says where they are.

CREDENTIALS. Only from the 0600 file `seed-test-accounts` writes (BOSS_E2E_ACCOUNTS_FILE, default
~/.boss/e2e/seed-accounts.json). Only ever sent to localhost. Never printed: no message this
script prints carries a password, a TOTP secret, a code, a cookie or a CSRF token.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.client
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
BRIEFS = ROOT / "tests" / "e2e" / "briefs"

API = os.environ.get("BOSS_E2E_API_URL", "http://localhost:8000").rstrip("/")
CONSOLE = os.environ.get("BOSS_E2E_CONSOLE_URL", "http://localhost:3000").rstrip("/")
PROJECT = os.environ.get("BOSS_E2E_PROJECT", "boss-dogfood")
REPO = os.environ.get("BOSS_E2E_REPO", "paramg/boss-sandbox")
#: The deterministic full-stack web scaffold (POST /v1/web-app seeds it) that the reference build
#: started from. Pinned so the gate builds on exactly what the 13.7-min reference build had.
SCAFFOLD_BRANCH = os.environ.get("BOSS_E2E_BASE_BRANCH", "boss/code/crun_4b60c6ef0d7848f393fa")
ACCOUNTS = os.environ.get("BOSS_E2E_ACCOUNTS_FILE", str(Path.home() / ".boss" / "e2e" / "seed-accounts.json"))
DB_CONTAINER = os.environ.get("BOSS_E2E_DB_CONTAINER", "boss-community-db-1")
WORKER_CONTAINER = os.environ.get("BOSS_E2E_WORKER_CONTAINER", "boss-community-worker-1")
ACTOR = "ceo"

POLL_S = 15
IDLE_MIN = 8.0
MIN_FREE_GB = 20
BUSY = ("running", "queued", "provisioning")
DEAD = ("blocked", "failed", "cancelled")


class GateFail(Exception):
    """A reason the gate fails, in words with no secret in them."""


# ---- the brief ---------------------------------------------------------------------------------

def load_brief(name: str) -> dict[str, Any]:
    path = BRIEFS / f"{name}.json"
    if not path.is_file():
        have = sorted(p.stem for p in BRIEFS.glob("*.json"))
        raise GateFail(f"no brief {name!r} in {BRIEFS} (have: {', '.join(have)})")
    brief = json.loads(path.read_text())
    if brief.get("extends"):
        base = load_brief(brief["extends"])
        brief = {**base, **brief}
    return brief


def code_task_body(brief: dict[str, Any], *, base_branch: str, stamp: str) -> dict[str, Any]:
    owner, repo = REPO.split("/", 1)
    return {
        "project_id": PROJECT, "actor": ACTOR,
        "title": f"{brief['title']} · e2e gate {stamp}",
        "instructions": brief["preamble"] + "THE APP TO BUILD:\n" + brief["app"],
        "acceptance_criteria": list(brief["acceptance_criteria"]),
        "test_commands": list(brief["test_commands"]),
        "profile": brief.get("profile"), "risk_tier": brief.get("risk_tier"),
        "owner": owner, "repo": repo, "base_branch": base_branch, "open_pr": True,
    }


def apply_body(brief: dict[str, Any], *, egress: list[str], stamp: str) -> dict[str, Any]:
    conn = brief["connector"]
    return {
        "project_id": PROJECT, "new_app": {"name": f"{brief['new_app']} {stamp}"},
        "stack": conn["stack"], "framework": conn.get("framework"),
        "note": brief["note_lead"] + brief["app"], "accept_egress": egress,
    }


# ---- verdict (pure: the selftests drive it with rows) ------------------------------------------

def _latest(events: list[dict], kind: str) -> Optional[dict]:
    found = [e for e in events if e["event_type"] == kind]
    return found[-1] if found else None


def final_checks(run: dict, events: list[dict]) -> list[tuple[str, bool, str]]:
    """[(check, ok, detail)] for a run that ended. `events` are this run's, oldest first, each
    {event_type, data, created_at}."""
    out: list[tuple[str, bool, str]] = []
    pr = run.get("pull_request_url")
    out.append(("completed with a PR", run.get("status") == "completed" and bool(pr),
                f"status={run.get('status')} pr={pr or 'none'}"))

    adv = [e for e in events if e["event_type"] == "code_task.gate_advisory"]
    failing = sorted({g for e in adv for g in ((e.get("data") or {}).get("advisory_failing_gates") or [])})
    out.append(("no advisory-failing gates", not failing, ", ".join(failing) or "none"))

    # A PR CAN OPEN WITH ITS CHECKS FAILING (services/checks_failing.py: past the repair rounds
    # the build no longer parks for a waiver). "Completed with a PR" is then not a pass.
    shipped = [e for e in events if e["event_type"] in ("code_task.shipped_with_dissent",
                                                         "code_task.pr_opened_with_checks_failing")]
    red = sorted({g for e in shipped for g in ((e.get("data") or {}).get("unresolved_gates")
                                               or (e.get("data") or {}).get("failing_gates") or [])})
    out.append(("no checks failing on the PR", not shipped, ", ".join(red) or "none"))

    serve = _latest(events, "code_task.app_serve_checked")
    serve_ok = bool(serve and (serve.get("data") or {}).get("ok") is True)
    out.append(("the app started", serve_ok,
                "no code_task.app_serve_checked" if serve is None
                else f"ok={(serve.get('data') or {}).get('ok')} {((serve.get('data') or {}).get('error') or '')[:160]}".strip()))

    last_fail_i = max((i for i, e in enumerate(events) if e["event_type"] == "code_task.install_failed"), default=-1)
    healed = last_fail_i < 0 or any(
        e["event_type"] == "code_task.install_fixed"
        or (e["event_type"] == "code_task.app_serve_checked" and (e.get("data") or {}).get("ok") is True)
        for e in events[last_fail_i + 1:])
    n_fail = sum(e["event_type"] == "code_task.install_failed" for e in events)
    out.append(("the install works", healed,
                "no install failure" if last_fail_i < 0 else
                (f"{n_fail} install failure(s), fixed later" if healed else
                 f"install failed and was never fixed: {((events[last_fail_i].get('data') or {}).get('summary') or '')[:160]}")))

    budget = _latest(events, "code_task.test_budget")
    over = bool(budget and (budget.get("data") or {}).get("over"))
    out.append(("test budget not over", not over,
                "no code_task.test_budget (no written-tests stage)" if budget is None
                else json.dumps({k: (budget.get("data") or {}).get(k) for k in ("written", "budget", "over")})))

    prof = _latest(events, "code_task.build_profile")
    out.append(("build profile recorded", prof is not None, "present" if prof else "no code_task.build_profile"))
    return out


def watch_verdict(*, run: Optional[dict], now: float, started: float, last_turn: Optional[float],
                  budget_min: float, pending: list[dict], wf: Optional[dict] = None) -> Optional[str]:
    """A reason to stop NOW, or None to keep waiting. Times are epoch seconds."""
    elapsed = (now - started) / 60
    if wf and wf.get("status") in ("failed", "cancelled", "blocked") and not (run and run.get("status") == "completed"):
        return f"the workflow run {wf['id']} is {wf['status']}: {(wf.get('blocked_reason') or wf.get('error') or '')[:300]}"
    if run is not None:
        if run["status"] in DEAD:
            return f"the build is {run['status']} (phase {run.get('phase')}): {str(run.get('error') or '')[:400]}"
        if run.get("phase") == "awaiting_gate_override":
            return "the build waits for a person to override a failed gate (phase awaiting_gate_override)"
    if pending:
        d = pending[0]
        return f"a decision the gate does not answer is pending: {d['id']} — {d.get('question', '')[:160]}"
    idle_from = last_turn if last_turn is not None else started
    if (now - idle_from) / 60 >= IDLE_MIN and not (run and run["status"] == "completed"):
        return f"no agent activity for {(now - idle_from) / 60:.1f} min (agent_turns)"
    if elapsed > budget_min:
        return f"over the time budget: {elapsed:.1f} min > {budget_min:g} min"
    return None


# ---- the person, over HTTP ---------------------------------------------------------------------

def _localhost_only(url: str) -> None:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host not in ("localhost", "127.0.0.1", "::1"):
        raise GateFail(f"refusing {host or url}: the agent gate signs in only to a localhost install")


@dataclass
class Account:
    email: str
    password: str
    totp_secret: Optional[str]

    def __repr__(self) -> str:   # never a credential in a traceback
        return f"Account(email={self.email!r})"

    __str__ = __repr__


def load_owner(path: str) -> Account:
    p = Path(path).expanduser()
    if not p.is_file():
        raise GateFail(
            f"no test accounts at {p}. Seed them on the host (never over HTTP) and copy the 0600 file "
            "here, or name it in BOSS_E2E_ACCOUNTS_FILE: docker exec -u boss boss-community-api-1 python -m "
            "boss_engine.identity.cli seed-test-accounts --org org_boss --owner-email <you> --out /tmp/s.json")
    if p.stat().st_mode & 0o077:
        raise GateFail(f"{p} is readable by others; chmod 600 it first")
    text = p.read_text()
    doc = json.loads(text[text.index("{"):])
    for person in doc.get("people", []):
        if person.get("role") == "owner":
            return Account(person["email"], person["password"], person.get("totp_secret"))
    raise GateFail(f"{p} has no owner account")


def _totp(secret_b32: str, step: int) -> str:
    clean = secret_b32.replace(" ", "").upper()
    key = base64.b32decode(clean + "=" * (-len(clean) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    o = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


class Person:
    """A signed-in person: the console's cookie, its Origin and its CSRF header on every write."""

    def __init__(self, account: Account):
        _localhost_only(API)
        _localhost_only(CONSOLE)
        self.account = account
        self.cookies: dict[str, str] = {}
        self.csrf = ""
        self._last_step = -1

    def _code(self) -> str:
        if not self.account.totp_secret:
            raise GateFail("the owner account has no authenticator secret in the seed file")
        step = int(time.time() // 30)
        if step <= self._last_step:          # the API refuses a replayed step
            time.sleep(31 - time.time() % 30)
            step = int(time.time() // 30)
        self._last_step = step
        return _totp(self.account.totp_secret, step)

    def _raw(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        headers = {"Accept": "application/json", "Origin": CONSOLE}
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if method != "GET" and self.csrf:
            headers["X-Boss-CSRF"] = self.csrf
        req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                self._keep(resp.headers)
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            self._keep(exc.headers)
            raw = exc.read()
            status = exc.code
        try:
            return status, json.loads(raw or b"null")
        except json.JSONDecodeError:
            return status, raw.decode(errors="replace")[:500]

    def _keep(self, headers: http.client.HTTPMessage) -> None:
        for line in headers.get_all("Set-Cookie") or []:
            name, _, rest = line.partition("=")
            value = rest.split(";", 1)[0]
            if value:
                self.cookies[name.strip()] = value
            else:
                self.cookies.pop(name.strip(), None)

    def sign_in(self) -> None:
        status, out = self._raw("POST", "/v1/auth/login", {"email": self.account.email, "password": self.account.password})
        if status != 200:
            raise GateFail(f"sign-in refused: {status} {(out or {}).get('error') if isinstance(out, dict) else ''}")
        if isinstance(out, dict) and out.get("next") == "mfa":
            status, out = self._raw("POST", "/v1/auth/mfa/totp", {"code": self._code()})
            if status != 200:
                raise GateFail(f"the authenticator code was refused: {status} "
                               f"{(out or {}).get('error') if isinstance(out, dict) else ''}")
        if not isinstance(out, dict) or out.get("next") != "done":
            raise GateFail(f"sign-in did not finish (next={out.get('next') if isinstance(out, dict) else '?'})")
        status, me = self._raw("GET", "/v1/identity/me")
        if status != 200 or not isinstance(me, dict) or not me.get("csrf_token"):
            raise GateFail(f"/v1/identity/me answered {status} after sign-in")
        self.csrf = me["csrf_token"]

    def step_up(self) -> None:
        status, out = self._raw("POST", "/v1/auth/step-up", {"method": "totp", "code": self._code()})
        if status != 200:
            raise GateFail(f"step-up refused: {status} {(out or {}).get('error') if isinstance(out, dict) else ''}")
        _, me = self._raw("GET", "/v1/identity/me")
        if isinstance(me, dict) and me.get("csrf_token"):
            self.csrf = me["csrf_token"]

    def call(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        """As the console makes a call: a 401 step_up_required opens "Confirm it's you" and the
        call is sent once more."""
        status, out = self._raw(method, path, body)
        if status == 401 and isinstance(out, dict) and out.get("error") == "step_up_required":
            self.step_up()
            status, out = self._raw(method, path, body)
        return status, out

    def start_with_consent(self, path: str, body: dict) -> dict:
        """POST the start; on 409 consent_required grant exactly what the sheet shows, for this
        build only (kind "task", its github repos), and start again."""
        status, out = self.call("POST", path, body)
        if status == 409 and isinstance(out, dict) and out.get("error") == "consent_required":
            grant: dict[str, Any] = {
                "agents": [a["id"] for a in out.get("agents") or []], "permissions": out.get("permissions") or [],
                "projects": out.get("projects") or [], "kind": "task", "purpose": out.get("task"),
                "task_run_id": out.get("task_run_id")}
            gh = out.get("github")
            if gh and gh.get("repos"):
                grant["github"] = {"repos": list(gh["repos"]), **(gh.get("permissions") or {})}
            gstatus, gout = self.call("POST", "/v1/identity/me/delegations", grant)
            if gstatus not in (200, 201):
                raise GateFail(f"the consent grant was refused: {gstatus} {str(gout)[:300]}")
            print(f"  consent: granted a task grant ({len(grant['agents'])} agent(s), "
                  f"github {'yes' if 'github' in grant else 'no'})", flush=True)
            status, out = self.call("POST", path, body)
        if status not in (200, 201):
            raise GateFail(f"the start was refused: {status} {str(out)[:400]}")
        return out


# ---- the database (read only) ------------------------------------------------------------------

def sql_json(query: str) -> list[dict]:
    wrapped = f"SELECT coalesce(json_agg(_gate_row), '[]'::json) FROM ({query}) _gate_row"
    done = subprocess.run(["docker", "exec", DB_CONTAINER, "psql", "-U", "boss", "-d", "boss", "-At",
                           "-v", "ON_ERROR_STOP=1", "-c", "SET default_transaction_read_only = on;", "-c", wrapped],
                          capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise GateFail(f"the database read failed: {done.stderr.strip()[-300:]}")
    out = done.stdout.lstrip()
    if out.startswith("SET"):
        out = out[3:]
    return json.loads(out) if out.strip() else []      # json_agg breaks lines between rows


def _q(v: str) -> str:
    return "'" + v.replace("'", "''") + "'"


def run_row(run_id: str) -> Optional[dict]:
    rows = sql_json("SELECT id, status, phase, error, pull_request_url, pull_request_number, decision_id, "
                    "spec->>'base_branch' AS base_branch, finalize_ctx->>'workflow_run_id' AS workflow_run_id, "
                    f"created_at FROM code_task_runs WHERE id = {_q(run_id)}")
    return rows[0] if rows else None


def last_turn_epoch(run_id: str) -> Optional[float]:
    rows = sql_json(f"SELECT extract(epoch FROM max(created_at)) AS t FROM agent_turns WHERE code_task_run_id = {_q(run_id)}")
    t = rows[0]["t"] if rows else None
    return float(t) if t is not None else None


def run_events(run_id: str) -> list[dict]:
    return sql_json(f"SELECT event_type, data, created_at FROM events WHERE entity_id = {_q(run_id)} ORDER BY created_at, id")


def pending_for(run: Optional[dict], wf_id: Optional[str], title: str) -> list[dict]:
    """Pending decisions that belong to this build. The merge question a completed build leaves
    for its PR (code_task_runs.decision_id) is expected and is not counted."""
    clauses = [f"position({_q(title)} in question) > 0"]
    if wf_id:
        clauses.append(f"id IN (SELECT decision_id FROM workflow_step_runs WHERE workflow_run_id = {_q(wf_id)} "
                       "AND decision_id IS NOT NULL)")
    skip = f" AND id <> {_q(run['decision_id'])}" if run and run.get("decision_id") else ""
    skip += " AND question NOT LIKE 'Approve code change:%'"     # the merge question: expected
    return sql_json(f"SELECT id, question FROM decisions WHERE status = 'pending' AND project_id = {_q(PROJECT)} "
                    f"AND ({' OR '.join(clauses)}){skip} ORDER BY created_at")


def workflow_row(wf_id: str) -> Optional[dict]:
    rows = sql_json(f"SELECT id, status, blocked_reason, error::text AS error FROM workflow_runs WHERE id = {_q(wf_id)}")
    return rows[0] if rows else None


# ---- preflight, profile, cleanup ---------------------------------------------------------------

def preflight() -> None:
    free_gb = shutil.disk_usage("/").free / 1e9
    if free_gb < MIN_FREE_GB:
        raise GateFail(f"only {free_gb:.0f} GB free on /; a build needs ≥ {MIN_FREE_GB} GB (docker builds fill the disk)")
    busy = sql_json(f"SELECT id, status FROM code_task_runs WHERE status IN ({', '.join(_q(s) for s in BUSY)})")
    if busy:
        raise GateFail("another build is running, so the timing would lie: "
                       + ", ".join(f"{r['id']} ({r['status']})" for r in busy))
    for name in (DB_CONTAINER, WORKER_CONTAINER):
        done = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name], capture_output=True, text=True)
        if done.stdout.strip() != "true":
            raise GateFail(f"{name} is not running")
    print(f"  preflight: {free_gb:.0f} GB free, no other build running", flush=True)


def branch_exists(branch: str) -> bool:
    done = subprocess.run(["gh", "api", f"repos/{REPO}/branches/{urllib.parse.quote(branch, safe='')}",
                           "--jq", ".name"], capture_output=True, text=True)
    return done.returncode == 0 and done.stdout.strip() == branch


def stage_table(run_id: str) -> str:
    done = subprocess.run(["docker", "exec", WORKER_CONTAINER, "python", "-m", "boss_engine.tools.build_profile", run_id],
                          capture_output=True, text=True, timeout=120)
    return (done.stdout or "") + (f"\n(build_profile exited {done.returncode}: {done.stderr.strip()[-300:]})"
                                  if done.returncode else "")


def pr_head(pr_number: int) -> Optional[str]:
    done = subprocess.run(["gh", "pr", "view", str(pr_number), "-R", REPO, "--json", "headRefName", "--jq", ".headRefName"],
                          capture_output=True, text=True)
    return done.stdout.strip() or None if done.returncode == 0 else None


def cleanup(person: Person, run: dict, wf_id: Optional[str]) -> None:
    if run.get("decision_id"):
        status, _ = person.call("POST", f"/v1/decisions/{run['decision_id']}/reject",
                                {"actor": ACTOR, "note": "e2e gate: a release-gate build, closed after it passed"})
        print(f"  cleanup: merge question {run['decision_id']} rejected ({status})", flush=True)
    if wf_id:
        status, _ = person.call("POST", f"/v1/workflow-runs/{wf_id}/cancel",
                                {"project_id": PROJECT, "actor": ACTOR, "reason": "e2e gate: passed; not released"})
        print(f"  cleanup: workflow run {wf_id} cancelled ({status})", flush=True)
    num = run.get("pull_request_number")
    if num:
        head = pr_head(int(num))
        if head and head in (SCAFFOLD_BRANCH, "main", run.get("base_branch")):
            print(f"  cleanup: NOT deleting {head}: it is a base branch", flush=True)
            head = None
        done = subprocess.run(["gh", "pr", "close", str(num), "-R", REPO,
                               "--comment", "Closed by the e2e release gate after it passed."],
                              capture_output=True, text=True)
        print(f"  cleanup: PR #{num} {'closed' if done.returncode == 0 else 'NOT closed: ' + done.stderr.strip()[-200:]}", flush=True)
        if head:
            done = subprocess.run(["gh", "api", "-X", "DELETE", f"repos/{REPO}/git/refs/heads/{head}"],
                                  capture_output=True, text=True)
            print(f"  cleanup: branch {head} {'deleted' if done.returncode == 0 else 'NOT deleted: ' + done.stderr.strip()[-200:]}", flush=True)


# ---- the gate ----------------------------------------------------------------------------------

@dataclass
class Started:
    run_id: Optional[str]
    wf_id: Optional[str] = None
    title: str = ""
    notes: list[str] = field(default_factory=list)


def start(person: Person, brief: dict, stamp: str) -> Started:
    if brief.get("start") == "template_apply":
        status, out = person.call("GET", "/v1/templates?kind=connector")
        if status != 200:
            raise GateFail(f"GET /v1/templates answered {status}")
        key = brief["connector"]["key"]
        tpl = next((t for t in out.get("templates") or [] if t.get("template_key") == key and t.get("status") == "active"), None)
        if not tpl:
            raise GateFail(f"the {key} connector is not installed and active here")
        body = apply_body(brief, egress=list(tpl.get("egress") or []), stamp=stamp)
        out = person.start_with_consent(f"/v1/templates/{tpl['id']}/apply", body)
        wf_id = out.get("workflow_run_id") or (out.get("run") or {}).get("id")
        if not wf_id:
            raise GateFail(f"the apply started no workflow run: {str(out)[:300]}")
        return Started(None, wf_id, title=f"{body['new_app']['name']}: add")
    if brief.get("base") == "web_scaffold" and not branch_exists(SCAFFOLD_BRANCH):
        raise GateFail(f"the scaffold base {SCAFFOLD_BRANCH} is gone from {REPO}; set BOSS_E2E_BASE_BRANCH "
                       "to a branch POST /v1/web-app seeded")
    body = code_task_body(brief, base_branch=SCAFFOLD_BRANCH if brief.get("base") == "web_scaffold" else "main",
                          stamp=stamp)
    out = person.start_with_consent("/v1/code-tasks", body)
    return Started(out["run_id"], out.get("workflow_run_id"), title=body["title"])


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="scripts/e2e-gate.sh agent", description=__doc__.split("\n\n")[0])
    ap.add_argument("--brief", default="onboarding-path-builder")
    ap.add_argument("--with-connector", action="store_true",
                    help="use the brief's login-oauth variant (<brief>-login-oauth)")
    ap.add_argument("--budget-min", type=float, default=None, help="default: the brief's (25, or 30 with a connector)")
    ap.add_argument("--keep", action="store_true", help="keep the PR and branch even after a PASS")
    args = ap.parse_args(argv)

    t0 = time.time()
    started: Optional[Started] = None
    run: Optional[dict] = None

    def interrupted(*_: Any) -> None:
        raise GateFail("interrupted")

    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        name = args.brief + ("-login-oauth" if args.with_connector and not args.brief.endswith("-login-oauth") else "")
        brief = load_brief(name)
        budget = args.budget_min or float(brief.get("budget_min") or (30 if args.with_connector else 25))
        print(f"e2e-gate agent: {brief['title']} ({name}) on {API}, project {PROJECT}, repo {REPO}, budget {budget:g} min",
              flush=True)
        preflight()
        person = Person(load_owner(ACCOUNTS))
        person.sign_in()
        print("  signed in as the test owner", flush=True)
        stamp = time.strftime("%m%d-%H%M")
        t0 = time.time()
        started = start(person, brief, stamp)
        print(f"  started: {'run ' + started.run_id if started.run_id else ''}"
              f"{' workflow ' + started.wf_id if started.wf_id else ''}", flush=True)

        last_line = ""
        while True:
            now = time.time()
            wf = workflow_row(started.wf_id) if started.wf_id and not started.run_id else None
            if not started.run_id and started.wf_id:
                rows = sql_json("SELECT id FROM code_task_runs WHERE finalize_ctx->>'workflow_run_id' = "
                                f"{_q(started.wf_id)} ORDER BY created_at DESC LIMIT 1")
                if rows:
                    started.run_id = rows[0]["id"]
                    print(f"  the workflow's build: run {started.run_id}", flush=True)
            run = run_row(started.run_id) if started.run_id else None
            turn = last_turn_epoch(started.run_id) if started.run_id else None
            pending = pending_for(run, started.wf_id, started.title)
            reason = watch_verdict(run=run, now=now, started=t0, last_turn=turn, budget_min=budget,
                                   pending=pending, wf=wf)
            line = f"{run['status']}/{run.get('phase')}" if run else "waiting for the build to start"
            if line != last_line:
                print(f"  {(now - t0) / 60:5.1f} min  {line}", flush=True)
                last_line = line
            if reason:
                raise GateFail(reason)
            if run and run["status"] == "completed":
                break
            time.sleep(POLL_S)

        minutes = (time.time() - t0) / 60
        checks = final_checks(run, run_events(run["id"]))
        print("\nchecks:")
        for check, ok, detail in checks:
            print(f"  [{'ok' if ok else 'FAIL'}] {check}: {detail}")
        print("\nstage table:")
        print(stage_table(run["id"]))
        failed = [c for c, ok, _ in checks if not ok]
        if failed:
            raise GateFail("; ".join(failed))
        print(f"PASS e2e-gate agent: {brief['title']} — {run['id']} completed with {run['pull_request_url']} "
              f"in {minutes:.1f} min (budget {budget:g})", flush=True)
        if not args.keep:
            cleanup(person, run, started.wf_id)
        return 0
    except GateFail as exc:
        minutes = (time.time() - t0) / 60
        if run and run.get("id") and run["status"] != "completed":
            print("\nstage table (so far):")
            print(stage_table(run["id"]))
        print(f"FAIL e2e-gate agent after {minutes:.1f} min: {exc}", flush=True)
        if started and (started.run_id or started.wf_id):
            branch = f"boss/code/{started.run_id}" if started.run_id else "(none yet)"
            print(f"  kept for diagnosis: run {started.run_id or '-'}"
                  f"{', workflow ' + started.wf_id if started.wf_id else ''}, branch {branch} on {REPO}"
                  f"{', PR ' + run['pull_request_url'] if run and run.get('pull_request_url') else ''}. "
                  f"Console: {CONSOLE}/?area=builds", flush=True)
            if run and run["status"] not in ("completed",) + DEAD:
                print(f"  the build is still {run['status']}; cancel it with POST /v1/code-tasks/{run['id']}/cancel "
                      "or from the console", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
