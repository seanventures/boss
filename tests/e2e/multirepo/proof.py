"""THE MULTI-REPO FEATURE, END TO END, ON ONE DISPOSABLE COMPOSE STACK — every outside service faked.

One change across three repositories of the organisation `acme` (acme/contracts, acme/api,
acme/web), through the real API, the real worker and the real build path, in enforce mode:

  1. the stack (stack.py): api, worker, egress, web, db and the fake GitHub (`fakegh`, the org
     `acme` with three repositories carrying language/description/id); the Owner claims it and
     signs in through the real sign-in page (TOTP), as the other compose walks do;
  2. GitHub App connected from Setup through the fake (build_walk.connect_app); the registry
     refreshed, the three repositories ticked (the three checks run against the fake), added to
     the project with role hints, components Contracts -> Backend -> Frontend saved (depends_on),
     ship targets: Contracts as a package (released by the merge), Backend and Frontend to the
     project's verified machine (the stub ssh host);
  3. ONE change started with POST /v1/apps/{app}/change-sets: the first start answers 409
     consent_required whose `multirepo.repos` are exactly the three repositories; the grant is
     created as the consent sheet does and the start is retried;
  4. lanes build in waves (the coding agent is the scripted stub: each lane writes a small real
     module + a passing unittest in ITS repository); every push used a token scoped to its ONE
     repository (the fake's log); three PRs open and cross-referenced; ONE approve_change_set
     decision pinned to the three head SHAs; a push to one PR after the question withdraws it and
     asks again; the Owner approves (step-up) -> the change_set.merge job merges all three, in
     order, at the approved SHAs -> the deploy plan runs -> the set ends `live` (or the exact
     blocker is recorded);
  5. a second change where the acme/api lane's tests fail: the set is `blocked` with the reason,
     no decision is raised, acme/web never starts.

    cd tests && python3 -m e2e.multirepo.proof all --dir <scratch dir> [--build] [--keep]
    cd tests && python3 -m e2e.multirepo.proof up|run|shots|down --dir <scratch dir>

Writes <dir>/proof.json (the report) and <dir>/shots/. Exit 0 when no check failed. Never prints
a secret.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from e2e.harness import identity as I  # noqa: E402
from e2e.identity import build_walk as BW  # noqa: E402
from e2e.identity import kit as K  # noqa: E402
from e2e.multirepo.stack import Stack  # noqa: E402

ORG = "acme"
REPOS = ["acme/contracts", "acme/api", "acme/web"]
APP_NAME = "Acme Shop"
MARK = "order notes"                      # in the change's title and directive: in every stage's prompt
FAIL_MARK = "gift wrap"                   # the forced-failure change
LANE_FILES = {
    "acme/contracts": ("orders_contract",
                       "FIELDS = ('id', 'total_cents', 'note')\nVERSION = '1.1.0'\n\n\ndef has_note(order):\n"
                       "    return bool(order.get('note'))\n",
                       "self.assertIn('note', orders_contract.FIELDS)\n        self.assertTrue(orders_contract.has_note({'note': 'x'}))\n"
                       "        self.assertFalse(orders_contract.has_note({}))"),
    "acme/api": ("orders_api",
                 "CONTRACT_VERSION = '1.1.0'\n\n\ndef create_order(total_cents, note=''):\n"
                 "    return {'id': 1, 'total_cents': int(total_cents), 'note': note.strip()[:280]}\n",
                 "self.assertEqual(orders_api.create_order(5, ' hi ')['note'], 'hi')\n"
                 "        self.assertEqual(orders_api.CONTRACT_VERSION, '1.1.0')"),
    "acme/web": ("orders_view",
                 "def note_line(order):\n    note = order.get('note') or ''\n    return f'Note: {note}' if note else ''\n",
                 "self.assertEqual(orders_view.note_line({'note': 'hi'}), 'Note: hi')\n"
                 "        self.assertEqual(orders_view.note_line({}), '')"),
}


def _unit(mod: str, body: str) -> str:
    return (f"import unittest\n\nimport {mod}\n\n\nclass {mod.title().replace('_', '')}Test(unittest.TestCase):\n"
            f"    def test_it(self):\n        {body}\n\n\nif __name__ == '__main__':\n    unittest.main()\n")


def lane_rules() -> list[dict[str, Any]]:
    """The scripted coding agent and the two set-level prompts (planner, cross-repo reviewer)."""
    rules: list[dict[str, Any]] = []
    # The forced failure first (narrowest): acme/api's tests can never pass for the gift-wrap change.
    bad_test = ("import unittest\n\nimport orders_api\n\n\nclass GiftWrapTest(unittest.TestCase):\n"
                "    def test_gift_wrap(self):\n        self.assertEqual(orders_api.gift_wrap_cents(), 499)\n\n\n"
                "if __name__ == '__main__':\n    unittest.main()\n")
    bad_impl = "def gift_wrap_cents():\n    return 0\n"
    for stage, when in (("solo", "one agent does both"), ("tests", "author tests"),
                        ("impl", "QA has ALREADY written the tests")):
        rules.append({"id": f"mr.fail.api.{stage}", "when": ["You work ONLY in acme/api", FAIL_MARK, when],
                      "files": {"orders_api.py": bad_impl, "test_gift_wrap.py": bad_test},
                      "reply": "Added gift_wrap_cents()."})
    for full, (mod, impl, assertion) in LANE_FILES.items():
        lane = f"You work ONLY in {full}"
        files = {f"{mod}.py": impl, f"test_{mod}.py": _unit(mod, assertion)}
        rules += [
            {"id": f"mr.{mod}.solo", "when": [lane, "one agent does both"], "files": files,
             "reply": f"Added {mod}.py with its unittest; it passes."},
            {"id": f"mr.{mod}.tests", "when": [lane, "author tests"], "files": {f"test_{mod}.py": files[f'test_{mod}.py']},
             "reply": f"Wrote test_{mod}.py."},
            {"id": f"mr.{mod}.impl", "when": [lane, "QA has ALREADY written the tests"], "files": {f"{mod}.py": impl},
             "reply": f"Implemented {mod}.py; the suite passes."},
            {"id": f"mr.{mod}.mutation", "when": ["Mutation testing is a standard QA practice", f"{mod}.py"],
             "files": {f"{mod}.py": impl.replace("note", "nope", 1) if mod != "orders_view" else
                       impl.replace("'Note: ", "'Nope: ")},
             "reply": "Mutated the module."},
        ]
    plan = {"summary": "Orders get an optional note: the contract adds the field, the API stores it, the web shows it.",
            "components": [
                {"name": "Contracts", "change": "Add the optional `note` field to Order (v1.1.0).",
                 "interface": "Order.note (string, optional, <= 280 chars)", "unchanged": False},
                {"name": "Backend", "change": "Accept and store the note on create_order, against contracts v1.1.0.",
                 "interface": "create_order(total_cents, note)", "unchanged": False},
                {"name": "Frontend", "change": "Show the order's note on the order view.", "interface": "",
                 "unchanged": False}]}
    checks = {"checks": [
        {"name": "Contract field implemented by the API", "detail": "orders_api.create_order returns note; contracts FIELDS has note.", "pass": True},
        {"name": "Callers use only the contract", "detail": "orders_view reads order['note'] only.", "pass": True},
        {"name": "Pinned to the same contract build", "detail": "orders_api.CONTRACT_VERSION == contracts VERSION 1.1.0.", "pass": True},
        {"name": "Safe rollout order", "detail": "note is optional: old callers keep working while waves roll out.", "pass": True}]}
    rules += [
        {"id": "mr.plan", "when": ["BOSS MULTI-REPO PLAN"], "reply": json.dumps(plan)},
        {"id": "mr.cross-check", "when": ["BOSS CROSS-REPO CHECK"], "reply": json.dumps(checks)},
    ]
    return rules


class Proof(BW.Walk):
    """build_walk's sign-in, plumbing and GitHub App connect, pointed at the `acme` org."""

    def __init__(self, args: argparse.Namespace, browser: Any, stack: Stack):
        BW.APP_OWNER = ORG                # the connect flow fills the App's owner with this org
        BW.REPO = REPOS[1]
        super().__init__(args, browser)
        self.st = stack
        self.j.id, self.j.title = "multirepo", "One change across acme/contracts, acme/api, acme/web"
        self.state.update({"repos": REPOS})

    # ---- plumbing ----------------------------------------------------------------------------
    def call(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        """A console call as the Owner; a step-up when the API asks for one."""
        return K.stepped(self.pg, self.owner, method, path, body, self.j)

    def tick(self) -> Any:
        """What the worker does every few seconds (POST /v1/delivery-workflows/run-capabilities,
        as the worker): nudged here so the proof does not wait out the worker's interval."""
        status, body = K.http("POST", f"{self.st.api}/v1/delivery-workflows/run-capabilities",
                              data=json.dumps({"project_id": self.project, "worker_id": "mr-proof"}).encode(),
                              headers={"Authorization": f"Bearer {self.st.value('BOSS_WORKER_API_KEY')}",
                                       "Content-Type": "application/json"}, timeout=300)
        return (status, (body or {}).get("change_sets") if isinstance(body, dict) else body)

    def view(self, cs_id: str) -> dict[str, Any]:
        r = self.call("GET", f"/v1/change-sets/{cs_id}?project_id={self.project}")
        return r.get("body") or {} if r["status"] == 200 else {"_status": r["status"], "_body": r.get("body")}

    def wait_set(self, cs_id: str, done: Callable[[dict[str, Any]], bool], timeout_s: int,
                 label: str) -> dict[str, Any]:
        deadline = time.time() + timeout_s
        last_state = ""
        v: dict[str, Any] = {}
        while time.time() < deadline:
            self.tick()
            v = self.view(cs_id)
            lanes = " ".join(f"{(lane.get('full_name') or '').split('/')[-1]}:{lane.get('lane_state')}/"
                             f"{lane.get('phase') or '-'}" for lane in v.get("lanes") or [])
            now = f"{v.get('state')} [{lanes}]"
            if now != last_state:
                self.j.note(f"{label}: {now}")
                last_state = now
            if done(v):
                return v
            time.sleep(12)
        self.j.note(f"{label}: timed out after {timeout_s}s at {last_state}")
        return v

    def stub_rules(self) -> None:
        """Script BOTH stub models: the api container's (the builds, the planner and reviewer on
        the API's tick) and fakegh's (the worker's)."""
        body = json.dumps({"rules": lane_rules()})
        code = ("import sys,urllib.request;r=urllib.request.Request('http://127.0.0.1:58700/_override',"
                "data=sys.stdin.buffer.read(),headers={'Content-Type':'application/json'});"
                "print(urllib.request.urlopen(r,timeout=10).status)")
        a = self.st.exec("api", ["python3", "-c", code], input_text=body)
        f = self.st.exec("fakegh", ["python3", "-c", code], user="root", input_text=body)
        self.j.check("both scripted models take the multi-repo rules", a.stdout.strip() == f.stdout.strip() == "200",
                     expect="200 and 200", actual=(a.stdout + a.stderr)[-160:] + " | " + (f.stdout + f.stderr)[-160:],
                     fatal=True)

    def fake_log(self) -> list[dict[str, Any]]:
        return self.fake_get("/_log")["entries"]

    def fake_prs(self) -> list[dict[str, Any]]:
        return [p for p in self.fake_get("/_world").get("prs") or [] if p.get("repo") in REPOS]

    def epoch(self, sql: str, **kw: str) -> float:
        out = self.psql(sql, **kw)
        return float(out) if out else 0.0

    # ---- 2. registry, project repositories, components ---------------------------------------
    def setup_repos(self) -> None:
        j = self.j
        r = self.call("POST", "/v1/github/repos/refresh", {"project_id": self.project})
        items = {i["full_name"]: i for i in (r.get("body") or {}).get("items") or []}
        j.check("Refresh lists the App's three repositories with GitHub's metadata (language, description)",
                r["status"] == 200 and set(REPOS) <= set(items)
                and [items[x].get("language") for x in REPOS] == ["Protobuf", "Go", "TypeScript"],
                expect="200; acme/contracts Protobuf, acme/api Go, acme/web TypeScript",
                actual=(r["status"], {k: (v.get("language"), v.get("state")) for k, v in items.items()}))
        ids = [items[x]["id"] for x in REPOS if x in items]
        r = self.call("PUT", "/v1/github/repos/allowed", {"project_id": self.project, "allowed_ids": ids})
        got = {i["full_name"]: i for i in (r.get("body") or {}).get("items") or []}
        self.state["registry"] = {k: {"state": v.get("state"), "checks": v.get("checks")} for k, v in got.items()}
        j.check("Ticking the three runs the checks against GitHub: Read, Write and PRs all pass (Working)",
                r["status"] == 200 and all(got.get(x, {}).get("state") == "working" for x in REPOS)
                and all(all((got[x]["checks"].get(c) or {}).get("ok") for c in ("read", "write", "pull_requests"))
                        for x in REPOS if x in got),
                expect="state working, checks read/write/pull_requests ok", actual=self.state["registry"])
        self.reg_ids = {x: items[x]["id"] for x in REPOS if x in items}
        for full, hint in zip(REPOS, ("contracts", "backend", "frontend")):
            r = self.call("PUT", f"/v1/projects/{self.project}/repos/{self.reg_ids[full]}", {"role_hint": hint})
            j.check(f"{full} added to the project as {hint}", r["status"] == 200,
                    expect="200", actual=(r["status"], K.short(r.get("body"), 300)))
        r = self.call("PUT", f"/v1/projects/{self.project}/repo-mode", {"repo_mode": "multi"})
        listed = r.get("body") or {}
        j.check("the project is set to Several repos with the three repositories",
                r["status"] == 200 and set(REPOS) <= {x.get("full_name") for x in listed.get("items") or listed.get("repos") or []},
                expect="200, three repositories", actual=(r["status"], K.short(listed, 500)))
        comps = [
            {"name": "Contracts", "repository": "acme/contracts", "depends_on": [],
             "ship_target": {"kind": "package", "package": "@acme/contracts"}},
            {"name": "Backend", "repository": "acme/api", "depends_on": ["Contracts"], "ship_target": {"kind": "host"}},
            {"name": "Frontend", "repository": "acme/web", "depends_on": ["Backend"], "ship_target": {"kind": "host"}},
        ]
        r = self.call("PUT", "/v1/apps/acme-shop/components",
                      {"project_id": self.project, "app_name": APP_NAME, "components": comps})
        body = r.get("body") or {}
        self.app_key = body.get("app_key") or "acme-shop"
        self.state["app_key"] = self.app_key
        j.check("components saved: Contracts -> Backend -> Frontend, three waves",
                r["status"] == 200 and body.get("waves") == [["Contracts"], ["Backend"], ["Frontend"]],
                expect="200, waves [[Contracts],[Backend],[Frontend]]", actual=(r["status"], K.short(body, 500)))

    def approval_seat(self) -> None:
        """A tier-2 decision is approved from the ceo seat (identity/approvals.py). The signed-in
        Owner takes the seat as an Owner would in Settings › Approvals (PUT /v1/identity/seats/ceo),
        keeping its current holders."""
        held = [x for x in self.psql(
            "SELECT s.principal_id FROM identity_approval_seats s JOIN identity_principals p ON p.id = s.principal_id "
            "WHERE s.seat_key = 'ceo' AND p.kind = 'user' AND p.status = 'active' AND NOT p.network_bound").split() if x]
        if self.owner.principal_id in held:
            return
        r = self.call("PUT", "/v1/identity/seats/ceo", {"principal_ids": held + [self.owner.principal_id]})
        self.j.check("the Owner holds the ceo seat (tier-2 approvals)", r["status"] == 200, expect="200",
                     actual=(r["status"], K.short(r.get("body"), 200)))

    def deploy_host(self) -> None:
        """A verified machine for the project (the stub ssh host in api and worker)."""
        j = self.j
        hosts = self.call("GET", f"/v1/deployment-hosts?project_id={self.project}").get("body") or {}
        items = hosts.get("items") if isinstance(hosts, dict) else hosts
        host = next((h for h in items or [] if h.get("name") == "acme-prod"), None)
        if host is None:
            r = self.call("POST", "/v1/deployment-hosts", {
                "project_id": self.project, "name": "acme-prod", "address": "deploy.e2e.test", "ssh_user": "deploy",
                "ssh_port": 22, "actor": self.owner.principal_id,
                "notes": "the proof's stub host (tests/e2e/stubs/bin/ssh)"})
            host = (r.get("body") or {}).get("host") or r.get("body") or {}
            j.note(f"host create: {r['status']}")
        hid = host.get("id")
        if host.get("status") != "verified" and hid:
            k = self.call("POST", f"/v1/deployment-hosts/{hid}/key", {"backend": "database", "actor": self.owner.principal_id})
            v = self.call("POST", f"/v1/deployment-hosts/{hid}/verify", {"actor": self.owner.principal_id})
            j.note(f"host key {k['status']}, verify {v['status']} {K.short(v.get('body'), 300)}")
            host = {**host, "status": (v.get("body") or {}).get("status")}
        self.state["host"] = {"id": hid, "status": host.get("status")}
        j.check("the project has a verified machine to deploy to (stub ssh host)", host.get("status") == "verified",
                expect="verified", actual=self.state["host"])

    # ---- 3. start ONE change: consent asks for exactly the three ------------------------------
    def start_change(self, title: str, directive: str, key: str) -> Optional[str]:
        j = self.j
        path = f"/v1/apps/{self.app_key}/change-sets"
        body = {"project_id": self.project, "title": title, "instructions": directive,
                "acceptance_criteria": ["Each repository's unittest suite passes"]}
        r = K.api(self.pg, "POST", path, body)
        if r["status"] == 401:
            r = self.call("POST", path, body)
        asked = r.get("body") or {}
        if isinstance(asked.get("detail"), dict):
            asked = asked["detail"]
        if r["status"] == 409 and asked.get("error") == "consent_required":
            mr = asked.get("multirepo") or {}
            repos = sorted(x.get("full_name") for x in mr.get("repos") or [])
            self.state[f"{key}_consent"] = {"multirepo_repos": repos, "github_repos": sorted((asked.get("github") or {}).get("repos") or []),
                                            "access": [x.get("access") for x in mr.get("repos") or []]}
            j.check(f"[{key}] the first start answers 409 consent_required whose multirepo.repos are exactly the three",
                    repos == sorted(REPOS) and sorted((asked.get("github") or {}).get("repos") or []) == sorted(REPOS),
                    expect=sorted(REPOS), actual=self.state[f"{key}_consent"])
            grant = {"agents": [a["id"] for a in asked.get("agents") or []], "permissions": asked.get("permissions") or [],
                     "projects": asked.get("projects") or [], "purpose": asked.get("task") or title,
                     "github": {"repos": sorted((asked.get("github") or {}).get("repos") or []),
                                **((asked.get("github") or {}).get("permissions") or {})}}
            if asked.get("task_run_id"):
                grant.update(kind="task", task_run_id=asked["task_run_id"])
            else:
                grant.update(kind="standing", days=7)
            g = self.call("POST", "/v1/identity/me/delegations", grant)
            if g["status"] == 403 and "github_access_lacking" in json.dumps(g.get("body") or {}):
                j.note("the grant needs the Owner's own GitHub (D28): connecting it through the fake, as build_walk does")
                if not self.my_github_connected():
                    self.connect_my_github(self.pg.context)
                g = self.call("POST", "/v1/identity/me/delegations", grant)
            j.check(f"[{key}] the grant (the consent sheet's choice, kind {grant['kind']}) is created", g["status"] in (200, 201),
                    expect="201", actual=(g["status"], K.short(g.get("body"), 300)))
            r = K.api(self.pg, "POST", path, body)
        out = r.get("body") or {}
        j.check(f"[{key}] the change starts (201) after consent", r["status"] == 201 and bool(out.get("id")),
                expect="201 {id, lanes}", actual=(r["status"], K.short(out, 400)))
        if r["status"] != 201:
            return None
        grant_repos = sorted(x.get("full_name") for x in (out.get("grant") or {}).get("repos") or [])
        j.check(f"[{key}] the set's recorded grant names exactly the three repositories", grant_repos == sorted(REPOS),
                expect=sorted(REPOS), actual=grant_repos)
        return out["id"]

    # ---- 4. the happy change ------------------------------------------------------------------
    def happy(self) -> None:
        j = self.j
        log0 = len(self.fake_log())
        title = f"Add {MARK} to orders"
        directive = (f"Customers can leave {MARK} on an order: the contract gains an optional note field, the "
                     "API accepts and stores it, the web order view shows it.")
        cs = self.start_change(title, directive, "happy")
        if not cs:
            return
        self.state["happy_id"] = cs
        v = self.wait_set(cs, lambda x: x.get("state") in ("in_review", "blocked", "failed", "cancelled"),
                          self.args.build_timeout, "happy")
        self.state["happy_review"] = {"state": v.get("state"), "blocked_reason": v.get("blocked_reason"),
                                      "plan_by": (v.get("plan") or {}).get("by"),
                                      "lanes": [{k: lane.get(k) for k in ("full_name", "wave", "lane_state", "pr_number",
                                                                          "head_sha", "code_task_run_id", "error")}
                                                for lane in v.get("lanes") or []]}
        j.check("the planner's plan (BOSS MULTI-REPO PLAN) was used for all three components",
                (v.get("plan") or {}).get("by") == "model" and len((v.get("plan") or {}).get("components") or []) == 3,
                expect="plan.by model, 3 components", actual=K.short(v.get("plan"), 300))
        j.check("all three lanes built and the set asks for approval (in_review)", v.get("state") == "in_review",
                expect="in_review", actual=self.state["happy_review"])
        lanes = {lane["full_name"]: lane for lane in v.get("lanes") or []}
        if v.get("state") != "in_review":
            self.lane_errors(lanes)
            return
        self.wave_checks(lanes)
        self.pr_checks(lanes)
        self.token_checks(log0)
        decision = self.decision_checks(cs, lanes, "happy")
        if not decision:
            return
        decision = self.push_reasks(cs, lanes, decision)
        if decision:
            self.approve_and_merge(cs, decision)

    def lane_errors(self, lanes: dict[str, Any]) -> None:
        for full, lane in lanes.items():
            if lane.get("code_task_run_id"):
                row = self.psql("SELECT status || '|' || coalesce(phase,'') || '|' || left(coalesce(error::text,''),300) "
                                "FROM code_task_runs WHERE id = :'c'", c=lane["code_task_run_id"])
                self.j.note(f"{full}: build {lane['code_task_run_id']} {row}")

    def wave_checks(self, lanes: dict[str, Any]) -> None:
        j = self.j
        prs = {p["repo"]: p for p in self.fake_prs()}

        def started(full: str) -> float:
            return self.epoch("SELECT extract(epoch from created_at) FROM code_task_runs WHERE id = :'c'",
                              c=lanes[full]["code_task_run_id"])

        def opened(full: str) -> float:
            p = prs.get(full) or {}
            return time.mktime(time.strptime(p["created_at"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone if p else 0.0

        t = {f: (started(f), opened(f)) for f in REPOS}
        self.state["waves"] = {f: {"build_started": round(a, 1), "pr_opened": round(b, 1), "wave": lanes[f]["wave"]}
                               for f, (a, b) in t.items()}
        j.check("lanes were built in waves: acme/api's build started only after acme/contracts' PR opened",
                t["acme/api"][0] >= t["acme/contracts"][1] - 1 > 0, expect="api build start >= contracts PR open",
                actual=self.state["waves"])
        j.check("…and acme/web's build started only after acme/api's PR opened",
                t["acme/web"][0] >= t["acme/api"][1] - 1 > 0, expect="web build start >= api PR open",
                actual=self.state["waves"])
        brief = self.psql("SELECT left(coalesce(spec::text, ''), 4000) FROM code_task_runs WHERE id = :'c'",
                          c=lanes["acme/api"]["code_task_run_id"])
        j.check("acme/api's brief names the contracts PR, branch and head SHA it builds against",
                f"acme/contracts PR #{lanes['acme/contracts']['pr_number']}" in brief
                and (lanes["acme/contracts"].get("head_sha") or "x")[:12] in brief,
                expect=f"acme/contracts PR #{lanes['acme/contracts']['pr_number']} … {str(lanes['acme/contracts'].get('head_sha'))[:12]}",
                actual=brief[:600])

    def pr_checks(self, lanes: dict[str, Any]) -> None:
        j = self.j
        prs = {p["repo"]: p for p in self.fake_prs() if p.get("state") == "open"}
        self.state["prs"] = {r: {k: p.get(k) for k in ("number", "head", "base", "state")} for r, p in prs.items()}
        j.check("three PRs are open on GitHub, one per repository", set(REPOS) <= set(prs),
                expect=REPOS, actual=self.state["prs"])
        linked = []
        for full in REPOS:
            body = str((prs.get(full) or {}).get("body") or "")
            others = [f"{o}#{lanes[o]['pr_number']}" for o in REPOS if o != full]
            if "<!-- boss:change-set -->" in body and all(o in body for o in others):
                linked.append(full)
        j.check("each PR's description lists the whole set (cross-referenced)", linked == REPOS,
                expect="all three list the other two", actual={"linked": linked,
                                                                "web_body": str((prs.get("acme/web") or {}).get("body"))[-400:]})
        for full in REPOS:
            mod = LANE_FILES[full][0]
            files = self.in_container("fakegh", ["git", "--git-dir", f"/srv/fakegh/git/{full}.git", "diff", "--name-only",
                                                 f"main...{(prs.get(full) or {}).get('head', 'main')}"]).stdout.split()
            self.state.setdefault("pr_files", {})[full] = files
            j.check(f"{full}'s PR carries its own small change and passing test ({mod}.py, test_{mod}.py)",
                    f"{mod}.py" in files and f"test_{mod}.py" in files and not any(
                        LANE_FILES[o][0] + ".py" in files for o in REPOS if o != full),
                    expect=f"{mod}.py + test_{mod}.py, nothing of the other repos", actual=files)

    def token_checks(self, since: int) -> None:
        """Every request that touched one of the three repositories with an installation token
        used a token scoped to THAT ONE repository; every push did."""
        j = self.j
        toks = {t["id"]: t for t in self.fake_get("/_github_app").get("tokens") or []}
        entries = self.fake_log()[since:]
        pushes, bad, used = [], [], 0
        for e in entries:
            path = str(e.get("path") or "")
            repo = next((r for r in REPOS if f"/{r}.git/" in path or f"/{r}/" in path or path.endswith(f"/{r}")
                         or f"/repos/{r}/" in path or path == f"/repos/{r}"), None)
            if not repo or not e.get("token_id"):
                continue
            used += 1
            scope = toks.get(e["token_id"], {}).get("repositories")
            if e.get("method") in ("POST", "PUT", "PATCH") and ("git-receive-pack" in path or any(
                    w in path for w in ("/contents/", "/git/refs", "/git/commits", "/git/trees", "/git/blobs", "/pulls"))):
                pushes.append((repo, scope, e.get("method"), path.split(repo, 1)[-1][:40]))
            if scope != [repo]:
                bad.append((e.get("method"), path[:80], scope))
        self.state["tokens"] = {"requests_with_token": used, "pushes": pushes, "violations": bad[:10],
                                "minted": len(toks)}
        self.state["tokens"]["writes"] = len(pushes)
        j.check("every write to a lane's repository (its branch's commits, refs, PR) used an installation token "
                "scoped to that ONE repository",
                bool(pushes) and {p[0] for p in pushes} == set(REPOS) and all(p[1] == [p[0]] for p in pushes),
                expect="writes on each repo, token.repositories == [that repo]",
                actual=[p for p in pushes if p[1] != [p[0]]][:8] or f"{len(pushes)} writes, all one-repo")
        j.check("no request touching a repository carried a token reaching any other repository", used > 0 and not bad,
                expect="0 violations", actual=self.state["tokens"])

    def decision_checks(self, cs: str, lanes: dict[str, Any], key: str) -> Optional[dict[str, Any]]:
        j = self.j
        rows = self.psql("SELECT id || '|' || status || '|' || (recommendation->'head_shas')::text FROM decisions WHERE "
                         "recommendation->>'change_set_id' = :'c' AND recommendation->>'action' = 'approve_change_set' "
                         "ORDER BY created_at", c=cs).splitlines()
        pending = [r for r in rows if r.split("|")[1] == "pending"]
        heads = {p["repo"]: self.in_container("fakegh", ["git", "--git-dir", f"/srv/fakegh/git/{p['repo']}.git",
                                                         "rev-parse", f"refs/heads/{p['head']}"]).stdout.strip()
                 for p in self.fake_prs() if p.get("state") == "open"}
        got = json.loads(pending[0].split("|", 2)[2]) if pending else {}
        self.state[f"{key}_decisions"] = [r.split("|")[:2] for r in rows]
        j.check(f"[{key}] exactly ONE approve_change_set decision is open, pinned to the three PRs' head SHAs on GitHub",
                len(pending) == 1 and got == {r: heads.get(r) for r in REPOS},
                expect="1 pending; head_shas == the fake's PR heads", actual={"decisions": rows, "fake_heads": heads})
        if len(pending) != 1:
            return None
        did = pending[0].split("|")[0]
        rec = self.psql("SELECT recommendation::text FROM decisions WHERE id = :'d'", d=did)
        return {"id": did, "recommendation": json.loads(rec)}

    def push_reasks(self, cs: str, lanes: dict[str, Any], decision: dict[str, Any]) -> Optional[dict[str, Any]]:
        """A push to one PR after the question was asked withdraws it and asks again."""
        j = self.j
        pr = next(p for p in self.fake_prs() if p["repo"] == "acme/web" and p["state"] == "open")
        script = ("set -e; t=$(mktemp -d); git clone -q -b \"$1\" /srv/fakegh/git/acme/web.git \"$t\"; cd \"$t\"; "
                  "printf '\\n<!-- reviewed copy -->\\n' >> README.md; git -c user.name=reviewer -c user.email=r@x.test "
                  "commit -qam 'docs: a reviewer pushes to the PR'; git push -q origin \"$1\"; git rev-parse HEAD")
        done = self.in_container("fakegh", ["sh", "-c", script, "sh", pr["head"]])
        new_sha = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ""
        j.note(f"pushed {new_sha[:12]} to acme/web {pr['head']} (rc {done.returncode} {done.stderr[-200:]})")
        v = self.wait_set(cs, lambda x: x.get("state") == "in_review" and (x.get("decision") or {}).get("id")
                          not in (None, decision["id"]), 300, "after push")
        old = self.psql("SELECT status FROM decisions WHERE id = :'d'", d=decision["id"])
        new = self.decision_checks(cs, {lane["full_name"]: lane for lane in v.get("lanes") or []}, "re-asked")
        self.state["push_reask"] = {"pushed": new_sha[:12], "old_decision": decision["id"], "old_status": old,
                                    "new_decision": (new or {}).get("id")}
        j.check("a push to a PR after the question withdraws it (expired) and asks again pinned to the new head",
                old == "expired" and bool(new) and (new or {}).get("recommendation", {}).get("head_shas", {}).get("acme/web") == new_sha,
                expect="old expired; new decision with acme/web @ the pushed sha", actual=self.state["push_reask"])
        return new

    def approve_and_merge(self, cs: str, decision: dict[str, Any]) -> None:
        j = self.j
        rec = decision["recommendation"]
        approved = rec.get("head_shas") or {}
        r = self.call("POST", f"/v1/decisions/{decision['id']}/approve",
                      {"note": "Approve and ship all", "content_hash": (rec.get("action_preview") or {}).get("content_hash")})
        j.check("the Owner approves the one decision (step-up as asked)", r["status"] == 200,
                expect="200", actual=(r["status"], K.short(r.get("body"), 300)))
        v = self.wait_set(cs, lambda x: x.get("state") in ("merged", "deploying", "live", "failed", "rolled_back")
                          or ((x.get("state") == "merged") and (x.get("release") or {}).get("status") == "needs_target"),
                          600, "merge")
        job = self.psql("SELECT status || '|' || coalesce(left(last_error,200),'') FROM jobs WHERE kind = 'change_set.merge' "
                        "AND payload->>'change_set_id' = :'c' ORDER BY created_at DESC LIMIT 1", c=cs)
        self.state["merge_job"] = job
        prs = {p["repo"]: p for p in self.fake_prs()}
        merged = {r: prs.get(r, {}).get("merged") for r in REPOS}
        at = {r: prs.get(r, {}).get("merged_at_sha") for r in REPOS}
        order = sorted(REPOS, key=lambda r: prs.get(r, {}).get("merged_at") or 9e12)
        self.state["merge"] = {"job": job, "merged": merged, "at_sha": {k: (s or "")[:12] for k, s in at.items()},
                               "approved": {k: (s or "")[:12] for k, s in approved.items()}, "order": order}
        j.check("the change_set.merge job ran (worker -> POST /v1/change-sets/{id}/merge)", job.startswith(("completed", "succeeded", "done")),
                expect="completed", actual=job)
        j.check("all three PRs merged on GitHub, at exactly the approved SHAs", all(merged.values()) and at == approved,
                expect="merged, merged_at_sha == approved head_shas", actual=self.state["merge"])
        j.check("…in dependency order: contracts, then api, then web", order == REPOS, expect=REPOS, actual=order)
        mains = {r: self.in_container("fakegh", ["git", "--git-dir", f"/srv/fakegh/git/{r}.git", "ls-tree", "--name-only",
                                                 "main"]).stdout.split() for r in REPOS}
        j.check("each repository's main now carries its lane's module", all(LANE_FILES[r][0] + ".py" in mains[r] for r in REPOS),
                expect="orders_contract.py / orders_api.py / orders_view.py on main", actual=mains)
        self.deploy(cs)

    def deploy(self, cs: str) -> None:
        j = self.j
        v = self.wait_set(cs, lambda x: x.get("state") in ("live", "rolled_back", "failed")
                          or (x.get("release") or {}).get("status") in ("needs_target", "rollback_failed"),
                          self.args.deploy_timeout, "deploy")
        plan = self.call("GET", f"/v1/change-sets/{cs}/deploy-plan?project_id={self.project}").get("body") or {}
        self.state["deploy_plan"] = {"status": plan.get("status"), "ready": plan.get("ready"), "problems": plan.get("problems"),
                                     "waves": [{k: w.get(k) for k in ("order", "repository", "kind", "status", "to", "from",
                                                                      "error", "deployment_run_id")} for w in plan.get("waves") or []]}
        self.state["final"] = {"state": v.get("state"), "blocked_reason": v.get("blocked_reason"), "error": v.get("error"),
                               "release": v.get("release")}
        j.check("the deploy plan has three waves: contracts released by the merge, api and web to the machine",
                [w.get("kind") for w in plan.get("waves") or []] == ["merge", "host", "host"] and plan.get("ready") is True,
                expect="kinds [merge, host, host], ready", actual=self.state["deploy_plan"])
        ssh = [d for d in self.fake_get("/_world").get("deploys") or [] if d.get("path") == "/deploy/ssh"]
        self.state["ssh_commands"] = {"n": len(ssh), "docker_run": sum(1 for d in ssh if "docker run" in
                                                                       str((d.get("body") or {}).get("command")))}
        j.check("the set ends live (merged, deployed wave by wave, health checks passed)", v.get("state") == "live",
                expect="live", actual=self.state["final"])
        if v.get("state") != "live":
            runs = self.psql("SELECT string_agg(id || ' ' || status || ' ' || left(coalesce(failure_reason,''),160), ' ; ') "
                             "FROM deployment_runs WHERE created_at > now() - interval '2 hours'")
            j.note(f"deployment runs: {runs}")

    # ---- 5. the forced failure ----------------------------------------------------------------
    def failing(self) -> None:
        j = self.j
        running = self.psql("SELECT count(*) FROM repo_change_sets WHERE project_id = :'p' AND app_key = :'a' AND state IN "
                            "('planned','building','blocked','in_review','approved','merging','deploying')",
                            p=self.project, a=self.app_key)
        if running not in ("", "0"):
            j.note(f"the first change is still open ({running}); stopping it so the app takes a new change")
            first = self.state.get("happy_id")
            if first:
                self.call("POST", f"/v1/change-sets/{first}/stop", {"project_id": self.project})
        title = f"Offer {FAIL_MARK} at checkout"
        cs = self.start_change(title, f"Customers can add {FAIL_MARK} for 4.99 at checkout, end to end.", "failing")
        if not cs:
            return
        self.state["failing_id"] = cs
        v = self.wait_set(cs, lambda x: x.get("state") in ("blocked", "in_review", "failed", "cancelled"),
                          self.args.build_timeout, "failing")
        lanes = {lane["full_name"]: lane for lane in v.get("lanes") or []}
        self.state["failing_final"] = {"state": v.get("state"), "blocked_reason": v.get("blocked_reason"),
                                       "lanes": {k: {"state": x.get("lane_state"), "run": x.get("code_task_run_id"),
                                                     "error": str(x.get("error") or "")[:200]} for k, x in lanes.items()}}
        j.check("the set is blocked, and says acme/api failed", v.get("state") == "blocked"
                and "acme/api" in str(v.get("blocked_reason") or ""), expect="blocked: … acme/api: …",
                actual=self.state["failing_final"])
        n = self.psql("SELECT count(*) FROM decisions WHERE recommendation->>'change_set_id' = :'c'", c=cs)
        j.check("no approval decision was raised for the blocked set", n == "0" and not v.get("decision"),
                expect="0 decisions", actual=(n, v.get("decision")))
        web = lanes.get("acme/web") or {}
        j.check("acme/web never started (its lane is still waiting, no build)",
                web.get("lane_state") == "waiting" and not web.get("code_task_run_id"),
                expect="waiting, no code task", actual=web)
        time.sleep(15)
        self.tick()
        again = self.view(cs)
        web2 = next((x for x in again.get("lanes") or [] if x.get("full_name") == "acme/web"), {})
        j.check("…and stays so on the next tick (the whole set waits)", again.get("state") == "blocked"
                and not web2.get("code_task_run_id"), expect="still blocked, web not started",
                actual=(again.get("state"), web2.get("lane_state")))

    # ---- the whole proof ----------------------------------------------------------------------
    def run_all(self) -> None:
        j = self.j
        ctx, page = self.page()
        self.pg = page
        try:
            self.project = self.psql("SELECT id FROM projects ORDER BY created_at LIMIT 1")
            self.state["project_id"] = self.project
            self.preflight(page)
            with j.tolerate("connect the GitHub App from Setup (fake GitHub, org acme)"):
                self.connect_app(page)
            with j.tolerate("registry, project repositories, components"):
                self.setup_repos()
            with j.tolerate("the approval seat"):
                self.approval_seat()
            with j.tolerate("a verified machine"):
                self.deploy_host()
            self.stub_rules()
            with j.tolerate("the happy change"):
                self.happy()
            with j.tolerate("the forced failure"):
                self.failing()
            unmatched = [e for e in self._stub_log() if e.get("unmatched")]
            self.state["stub_unmatched"] = [str(e.get("prompt_head") or e.get("prompt") or "")[:160] for e in unmatched][-8:]
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

    def _stub_log(self) -> list[dict[str, Any]]:
        code = ("import urllib.request,json;print(json.dumps(json.loads(urllib.request.urlopen("
                "'http://127.0.0.1:58700/_log',timeout=10).read()).get('entries', [])[-400:]))")
        try:
            return json.loads(self.st.exec("api", ["python3", "-c", code]).stdout or "[]")
        except ValueError:
            return []


# ---- commands ---------------------------------------------------------------------------------------

def _args_for_walk(args: argparse.Namespace, st: Stack) -> argparse.Namespace:
    return argparse.Namespace(stack_dir=str(st.dir), project=st.project, console=st.web, api=st.api, fake=st.fake,
                              shots=str(st.dir / "shots"), report=None, build_timeout=args.build_timeout,
                              deploy_timeout=args.deploy_timeout, guard_wait=0, headed=args.headed)


def run_proof(args: argparse.Namespace, st: Stack) -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed, proxy={"server": st.fake, "bypass": "localhost,127.0.0.1"})
        proof = Proof(_args_for_walk(args, st), browser, st)
        proof.run_all()
        browser.close()
    result = {**proof.j.as_dict(), "state": json.loads(K.scrub(json.dumps(proof.state, default=str)))}
    report = st.dir / "proof.json"
    K.write_private(report, json.dumps(result, indent=1))
    fails = [c["name"] for c in result["checks"] if c["status"] == "fail"]
    print(f"[multirepo] {result['status']} — {len(result['checks'])} checks, {len(fails)} failed — report {report}")
    return 1 if result["status"] == "fail" else 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["up", "run", "shots", "down", "all"])
    ap.add_argument("--dir", required=True, help="scratch dir: stack.env, accounts.json (0600), proof.json, shots/")
    ap.add_argument("--project", default="bossmr")
    ap.add_argument("--tag", default="mr")
    ap.add_argument("--build", action="store_true", help="build the images from this checkout first")
    ap.add_argument("--keep", action="store_true", help="`all` leaves the stack up")
    ap.add_argument("--build-timeout", type=int, default=1500)
    ap.add_argument("--deploy-timeout", type=int, default=600)
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args(argv)
    st = Stack(Path(args.dir), project=args.project, tag=args.tag)
    if args.command == "down":
        st.down()
        print("down; left:", st.left_running())
        return 0
    if args.command in ("up", "all"):
        if not st.env_file.exists() or args.command == "all":
            st.write_env()
        if args.build:
            st.build()
    if args.command == "up":
        st.up()
        st.seed_and_claim()
        print("up, claimed, seeded")
        return 0
    if args.command == "run":
        return run_proof(args, st)
    if args.command == "shots":
        from e2e.multirepo import shots

        return shots.capture(args, st)
    rc = 1
    try:
        st.up()
        st.seed_and_claim()
        rc = run_proof(args, st)
        try:
            from e2e.multirepo import shots

            shots.capture(args, st)
        except Exception as exc:  # noqa: BLE001
            print(f"[multirepo] screenshots failed: {type(exc).__name__}: {str(exc)[:300]}")
    finally:
        if not args.keep:
            st.down()
            print("down; left:", st.left_running())
    return rc


if __name__ == "__main__":
    sys.exit(main())
