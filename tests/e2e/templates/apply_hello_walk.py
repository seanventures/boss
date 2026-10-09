"""F2 END-TO-END PROOF: apply the `hello` built-in template to an app, through the API, on a real
compose stack in enforce mode, every outside service faked (APP_TEMPLATES_AND_SECURITY_LOOP.md
§2.12; stream S2).

  1. the console build walk's first half (tests/e2e/identity/build_walk.py): GitHub App connect,
     repository, a Fast fix that builds the app (`e2e_feature`, Python) and opens its PR;
  2. the gallery lists `hello` (GET /v1/templates, as the Owner);
  3. POST /v1/templates/hello/apply {extends_run_id: <that build>, stack: python}: a governed
     build through the composer's start path, onto the app's branch;
  4. the build's code agent (the e2e stub, scripted here to ADAPT the reference: hello.py + the
     template's tests) runs, and the build's quality gate records `template_tests` (the
     template's own pytest command, ≥ 2 passing in its JUnit report) and `no_committed_secrets`
     as passed;
  5. a PR for it is opened on the fake GitHub, carrying the template's test file;
  6. GET /v1/apps/{app_key}/templates shows the application past `building`.

The stack runs tests/e2e/compose/build-override.yml plus an override that mounts templates/ (see
the S2 section of docs/product/APP_PLATFORM_PROGRESS.md for the exact commands).

    cd tests && python3 -m e2e.templates.apply_hello_walk --stack-dir <dir> --project <p> \\
        --console <url> --api <url> --fake <url> --shots <dir>

Exit 0 when no check failed. Never prints a secret.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from e2e.harness import identity as I  # noqa: E402
from e2e.identity import build_walk as BW  # noqa: E402
from e2e.identity import kit as K  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
HELLO = REPO_ROOT / "templates" / "apps" / "hello" / "stacks" / "python"
MARK = 'Apply the template "Hello endpoint"'
UNIT = ("import unittest\n\nimport hello\n\n\nclass HelloGreetingTest(unittest.TestCase):\n"
        "    def test_default_greeting(self):\n        self.assertEqual(hello.greeting(), 'hello from BOSS')\n\n\n"
        "if __name__ == '__main__':\n    unittest.main()\n")


def scripted_rules() -> list[dict[str, Any]]:
    """The stub agent adapting the template: the reference module at the app's root, the
    template's tests at the same relative path, and a unittest of its own for the app's suite."""
    hello = (HELLO / "hello.py").read_text()
    tests = (HELLO / "tests" / "template_hello" / "test_hello.py").read_text()
    t_files = {"tests/template_hello/test_hello.py": tests, "test_e2e_hello.py": UNIT}
    return [
        {"id": "s2.solo", "when": ["one agent does both", MARK], "files": {"hello.py": hello, **t_files},
         "reply": "Added GET /hello (hello.py) and the template's tests; they pass."},
        {"id": "s2.write-tests", "when": ["author tests", MARK], "files": t_files,
         "reply": "Copied the template's tests and wrote test_e2e_hello.py."},
        {"id": "s2.implement", "when": ["QA has ALREADY written the tests", MARK], "files": {"hello.py": hello},
         "reply": "Implemented hello.py from the reference; the suite passes."},
        {"id": "s2.mutation", "when": ["Mutation testing is a standard QA practice", "hello.py"],
         "files": {"hello.py": hello.replace('or "hello from BOSS"', 'or "goodbye"')},
         "reply": "Changed the default greeting."},
    ]


class TemplateWalk(BW.Walk):
    def consent_click(self, page: Any) -> None:
        """A grant that outlives one run: the apply's build is a second run under it."""
        buttons = page.locator("[data-testid=consent-durations] button")
        texts = [b.inner_text() for b in buttons.all()]
        pick = next((i for i, t in enumerate(texts) if "30 days" in t or "7 days" in t), 0)
        self.j.note(f"consent: chose {texts[pick] if texts else '?'!r}")
        buttons.nth(pick).click()
        page.wait_for_timeout(2500)
        if K.code_box(page).count():
            K.answer_step_up_sheet(page, self.owner)
        page.wait_for_timeout(5000)
        K.settle(page)

    def post_rules(self) -> None:
        body = json.dumps({"rules": scripted_rules()})
        code = ("import sys,urllib.request;r=urllib.request.Request('http://127.0.0.1:58700/_override',"
                "data=sys.stdin.buffer.read(),headers={'Content-Type':'application/json'});"
                "print(urllib.request.urlopen(r,timeout=10).status)")
        d = BW.subprocess.run(["docker", "exec", "-i", "-u", "boss", f"{self.args.project}-api-1", "python3", "-c", code],
                              input=body, capture_output=True, text=True, timeout=60)
        self.j.check("the stub agent is scripted to adapt the template", d.stdout.strip() == "200",
                     expect="200 from the api container's model stub", actual=(d.stdout + d.stderr)[-200:], fatal=True)

    def wait_first_build(self) -> Optional[str]:
        crun = self.state.get("code_task_run") or ""
        final = K.wait_for(lambda: (lambda s: s if s in ("completed", "failed", "cancelled") else None)(
            self.psql("SELECT status FROM code_task_runs WHERE id = :'c'", c=crun)), timeout_s=600, every_s=4)
        self.j.check("the app's first build completed (the app to apply the template to)", final == "completed",
                     expect="completed", actual=final)
        return crun if final == "completed" else None

    def apply_hello(self, page: Any, app_run: str) -> None:
        j = self.j
        project = self.psql("SELECT project_id FROM code_task_runs WHERE id = :'c'", c=app_run)
        self.state["project_id"] = project
        listed = I.api_in_page(page, "GET", "/v1/templates")
        cards = {c.get("template_key"): c for c in ((listed.get("body") or {}).get("templates") or [])}
        j.check("the gallery lists the hello built-in", listed["status"] == 200 and "hello" in cards,
                expect="200 with hello", actual=(listed["status"], sorted(cards)))
        self.post_rules()
        prs_before = len([p for p in self.fake_get("/_world").get("prs") or [] if p.get("repo") == BW.REPO])
        r = I.api_in_page(page, "POST", "/v1/templates/hello/apply",
                          {"project_id": project, "extends_run_id": app_run, "stack": "python",
                           "note": "e2e: add the hello endpoint"})
        body = r.get("body") or {}
        j.check("POST /v1/templates/hello/apply starts a governed build (201)", r["status"] == 201,
                expect="201 {workflow_run_id, application_id}", actual=(r["status"], K.short(body, 400)))
        if r["status"] != 201:
            return
        wf, app_key = body["workflow_run_id"], body["app_key"]
        self.state.update({"apply": {k: body.get(k) for k in ("workflow_run_id", "application_id", "app_key")},
                           "template": body.get("template")})
        j.check("the apply builds onto the app (extends its build)", (body.get("extends") or {}).get("run_id") == app_run,
                expect=app_run, actual=body.get("extends"))
        crun = K.wait_for(lambda: self.psql("SELECT id FROM code_task_runs WHERE finalize_ctx->>'workflow_run_id' = :'w' "
                                            "ORDER BY created_at DESC LIMIT 1", w=wf) or None, timeout_s=300, every_s=3)
        j.check("the apply's workflow run started a code task", bool(crun), expect="a code task", actual=crun)
        if not crun:
            wfs = self.psql("SELECT status || ' ' || left(coalesce(blocked_reason,''),200) FROM workflow_runs WHERE id = :'w'", w=wf)
            j.note(f"workflow run: {wfs}")
            return
        self.state["template_build"] = crun
        final = K.wait_for(lambda: (lambda s: s if s.split("|")[0] in ("completed", "failed", "cancelled", "blocked") else None)(
            self.psql("SELECT status || '|' || left(coalesce(error::text,''),300) FROM code_task_runs WHERE id = :'c'", c=crun)),
            timeout_s=self.args.build_timeout, every_s=4) or "timeout"
        self.state["template_build_final"] = final
        gates = self.psql("SELECT status || '|' || gates_passed::text || '|' || gates_failed::text || '|' || "
                          "coalesce((evidence->'template')::text, '') FROM quality_gates WHERE code_task_run_id = :'c' "
                          "ORDER BY created_at DESC LIMIT 1", c=crun)
        status, passed, failed, evidence = (gates.split("|", 3) + ["", "", "", ""])[:4]
        self.state["gates"] = {"status": status, "passed": passed, "failed": failed}
        self.state["template_evidence"] = K.short(evidence, 1500)
        j.check("the build's template_tests gate passed (the template's own tests, run by BOSS)",
                '"template_tests"' in passed, expect="template_tests in gates_passed", actual=(passed, failed, K.short(evidence, 600)))
        j.check("the build's no_committed_secrets gate passed", '"no_committed_secrets"' in passed,
                expect="no_committed_secrets in gates_passed", actual=(passed, failed))
        try:
            tt = json.loads(evidence).get("template_tests") or {}
        except ValueError:
            tt = {}
        j.check("the gate counted ≥ 2 passing template tests from a JUnit report", int(tt.get("passed") or 0) >= 2,
                expect="passed >= 2", actual={k: tt.get(k) for k in ("command", "passed", "tests", "reports", "error")})
        j.check("the template build finished (completed)", final.startswith("completed"), expect="completed", actual=final)
        prs = [p for p in self.fake_get("/_world").get("prs") or [] if p.get("repo") == BW.REPO]
        new = prs[prs_before:] if len(prs) > prs_before else []
        pr = new[-1] if new else None
        self.state["template_pr"] = pr and {k: pr.get(k) for k in ("number", "head", "base", "title", "state")}
        j.check("a PR for the template build was opened on the fake GitHub", bool(pr), expect="a new PR",
                actual=self.state["template_pr"])
        if pr:
            files = self.in_container("fakegh", ["git", "--git-dir", f"/srv/fakegh/git/{BW.REPO}.git", "diff",
                                                 "--name-only", f"main...{pr['head']}"]).stdout.split()
            self.state["template_pr_files"] = files
            j.check("the PR carries the template's tests and the adapted module",
                    "tests/template_hello/test_hello.py" in files and "hello.py" in files,
                    expect="tests/template_hello/test_hello.py, hello.py", actual=files)
            j.check("no JUnit report from the gate reached the PR", "boss-template-junit.xml" not in files,
                    expect="absent", actual=files)
        apps = I.api_in_page(page, "GET", f"/v1/apps/{app_key}/templates?project_id={project}")
        rows = (apps.get("body") or {}).get("templates") or []
        self.state["application"] = rows[:1]
        j.check("the app's templates show hello, past `building`",
                bool(rows) and rows[0].get("template_key") == "hello" and rows[0].get("status") in ("pr_open", "applied"),
                expect="hello: pr_open", actual=K.short(rows, 400))

    def run_all(self) -> None:
        j = self.j
        ctx, page = self.page()
        try:
            self.preflight(page)
            with j.tolerate("connect the GitHub App from Setup"):
                self.connect_app(page)
            with j.tolerate("register the repository from Setup"):
                self.register_repo(page)
            self.watch_start()
            with j.tolerate("start a Fast fix and consent"):
                self.fast_fix(page)
            with j.tolerate("the first build"):
                self.run_build(page)
            app_run = self.wait_first_build()
            if app_run:
                with j.tolerate("apply hello"):
                    self.apply_hello(page, app_run)
        except Exception as exc:  # noqa: BLE001
            j.error = K.fmt_exc(exc)
            j.note(f"ABORTED: {j.error}")
        finally:
            j.finished = time.time()
            ctx.close()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stack-dir", required=True)
    ap.add_argument("--project", required=True)
    ap.add_argument("--console", required=True)
    ap.add_argument("--api", required=True)
    ap.add_argument("--fake", required=True)
    ap.add_argument("--shots", required=True)
    ap.add_argument("--report", default=None)
    ap.add_argument("--build-timeout", type=int, default=900)
    ap.add_argument("--guard-wait", type=int, default=960)
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args(argv)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed, proxy={"server": args.fake, "bypass": "localhost,127.0.0.1"})
        walk = TemplateWalk(args, browser)
        walk.j.title = "F2: apply the hello template through the API"
        walk.run_all()
        browser.close()
    result = {**walk.j.as_dict(), "state": json.loads(K.scrub(json.dumps(walk.state, default=str)))}
    report = Path(args.report) if args.report else walk.j.shots / "apply_hello_walk.json"
    K.write_private(report, json.dumps(result, indent=1))
    print(f"[templates] {result['status']} — report {report}")
    return 1 if result["status"] == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
