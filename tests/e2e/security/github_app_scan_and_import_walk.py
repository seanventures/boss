"""SCAN A GITHUB-BUILT APP, AND IMPORT A TEMPLATE FROM GITHUB, on a real compose stack.

The two P0s the app-platform tester found (2026-10-09):
  * App ▸ Security ▸ Scan now failed for every app built from GitHub: the scan clones the app as
    BOSS inside its bound sandbox, and through egress the clone's credential helper asked the
    run's shim socket, which answers only the sandbox uid: no credential, 401 from egress;
  * Template import from GitHub always failed: the importer used the egress CONNECT proxy, which
    refuses github.com by design.

The walk (every outside service faked: tests/e2e/compose/build-override.yml + an override that
adds `fxd-org/tpl-good` to the fake GitHub App's installation):
  1. the console build walk's first half (tests/e2e/identity/build_walk.py): GitHub App connect,
     repository, a Fast fix that builds the app on GitHub and opens its PR;
  2. POST /v1/security/scans for that app (semgrep + osv): the worker runs it, the API clones the
     app's branch through egress as BOSS's system repo reader and runs the tools as a sandbox uid.
     Expected: status `done`, the commit scanned, the tools ran isolated, no error;
  3. a template is pushed to the fake GitHub as fxd-org/tpl-good (in the App's installation) and
     fxd-org/tpl-public (NOT in it: a public repository), and each is imported with
     POST /v1/templates/import {git_url: https://github.com/fxd-org/<repo>}. Expected: 201
     pending_review, pinned to the pushed commit, and egress's log shows the /github path (the
     CONNECT proxy never carried GitHub);
  4. POST /v1/delivery-workflows/start security_review: started, or a STRUCTURED consent request
     (the build's contract), never a bare refusal.

    cd tests && python3 -m e2e.security.github_app_scan_and_import_walk --stack-dir <dir> \\
        --project <p> --console <url> --api <url> --fake <url> --shots <dir>

Exit 0 when no check failed. Never prints a secret.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from e2e.harness import identity as I  # noqa: E402
from e2e.identity import build_walk as BW  # noqa: E402
from e2e.identity import kit as K  # noqa: E402
from e2e.templates.apply_hello_walk import TemplateWalk  # noqa: E402

TEMPLATES = {"tpl-good": "health-endpoint", "tpl-public": "health-endpoint-public"}


class ScanImportWalk(TemplateWalk):
    # ---- 2. scan the GitHub-built app ---------------------------------------------------------
    def scan(self, page: Any, app_run: str) -> None:
        j = self.j
        project = self.psql("SELECT project_id FROM code_task_runs WHERE id = :'c'", c=app_run)
        app_key = self.psql("SELECT coalesce(finalize_ctx->>'app_id','') FROM code_task_runs WHERE id = :'c'", c=app_run)
        pr = self.psql("SELECT coalesce(pull_request_url,'') FROM code_task_runs WHERE id = :'c'", c=app_run)
        self.state["app"] = {"project": project, "app_key": app_key, "pr": pr}
        j.check("the app was built on GitHub (it has a PR)", bool(pr) and bool(app_key),
                expect="a PR url and an app id", actual=self.state["app"])
        r = I.api_in_page(page, "POST", "/v1/security/scans",
                          {"project_id": project, "app_key": app_key, "tools": ["semgrep", "osv"]})
        body = r.get("body") or {}
        j.check("POST /v1/security/scans queues the scan (202)", r["status"] == 202,
                expect="202 {scan_id}", actual=(r["status"], K.short(body, 300)))
        if r["status"] != 202:
            return
        scan_id = body["scan_id"]
        final = K.wait_for(lambda: (lambda s: s if s and s not in ("queued", "running") else None)(
            self.psql("SELECT status FROM security_scans WHERE id = :'s'", s=scan_id)),
            timeout_s=self.args.scan_timeout, every_s=5) or "timeout"
        got = I.api_in_page(page, "GET", f"/v1/security/scans/{scan_id}")
        scan = got.get("body") or {}
        self.state["scan"] = {k: scan.get(k) for k in ("status", "commit_sha", "counts", "tools")}
        sandbox = json.loads(self.psql("SELECT coalesce(target->'sandbox','{}')::text FROM security_scans "
                                       "WHERE id = :'s'", s=scan_id) or "{}")
        self.state["scan_sandbox"] = sandbox
        err = (scan.get("counts") or {}).get("error")
        j.check("the scan of the GitHub-built app finished `done`", final == "done",
                expect="done", actual=(final, err))
        j.check("the scan says no error", not err, expect="no error", actual=err)
        j.check("the scanned commit is the app's", bool(scan.get("commit_sha")), expect="a commit sha",
                actual=scan.get("commit_sha"))
        tools = {t.get("name"): t.get("status") for t in scan.get("tools") or []}
        j.check("semgrep and osv ran", tools.get("semgrep") == "done" and tools.get("osv") == "done",
                expect="semgrep: done, osv: done", actual=tools)
        j.check("the tools ran as a sandbox uid", bool(sandbox.get("isolated")) and bool(sandbox.get("uid")),
                expect="isolated, with a uid", actual=sandbox)
        rows = self.psql("SELECT run_id || ' ' || method || ' ' || path_template || ' ' || decision "
                         "FROM identity_egress_log WHERE provider = 'github' AND run_id LIKE 'run_sys:git:%' "
                         "AND ts > now() - interval '30 minutes' ORDER BY ts DESC LIMIT 6")
        self.state["scan_egress"] = rows.splitlines()
        j.check("the scan's clone went through egress's GitHub path as BOSS's system repo reader",
                any(" allowed" in r for r in rows.splitlines()), expect="run_sys:git:… allowed",
                actual=rows.splitlines()[:6])

    # ---- 3. import from the fake GitHub -------------------------------------------------------
    def push_template(self, repo: str, template_id: str) -> str:
        from engine.tests.templates._fixtures import MANIFEST, write_template

        import copy

        m = copy.deepcopy(MANIFEST)
        m["id"] = template_id
        work = Path(tempfile.mkdtemp(prefix="fxsec-tpl-"))
        write_template(work / repo, manifest=m)
        dest = f"/tmp/{repo}-src"
        self.in_container("fakegh", ["rm", "-rf", dest])
        subprocess.run(["docker", "cp", str(work / repo), f"{self.args.project}-fakegh-1:{dest}"], check=True,
                       capture_output=True, timeout=60)
        bare = f"/srv/fakegh/git/fxd-org/{repo}.git"
        script = (f"set -e; cd {dest}; git init -q -b main; git -c user.name=e2e -c user.email=e2e@x.test add -A; "
                  f"git -c user.name=e2e -c user.email=e2e@x.test commit -qm template; "
                  f"if [ ! -d {bare} ]; then mkdir -p $(dirname {bare}); git init -q --bare -b main {bare}; fi; "
                  f"git push -q -f {bare} main:main; git rev-parse HEAD")
        done = self.in_container("fakegh", ["sh", "-c", script])
        sha = done.stdout.strip().splitlines()[-1] if done.returncode == 0 and done.stdout.strip() else ""
        self.j.check(f"the template is on the fake GitHub as fxd-org/{repo}", len(sha) == 40,
                     expect="a pushed commit", actual=(sha, done.stderr[-300:]))
        return sha

    def import_templates(self, page: Any) -> None:
        j = self.j
        for repo, tid in TEMPLATES.items():
            sha = self.push_template(repo, tid)
            if not sha:
                continue
            url = f"https://github.com/fxd-org/{repo}"
            r = I.api_in_page(page, "POST", "/v1/templates/import", {"git_url": url, "ref": "main"})
            body = r.get("body") or {}
            self.state[f"import_{repo}"] = {"status": r["status"], **{k: body.get(k) for k in
                                                                      ("status", "template_key", "commit_sha", "error",
                                                                       "message")}}
            j.check(f"import from {url} is stored for review (201 pending_review)",
                    r["status"] == 201 and body.get("status") == "pending_review",
                    expect="201 pending_review", actual=(r["status"], K.short(body, 400)))
            j.check(f"the {repo} import is pinned to the pushed commit", body.get("commit_sha") == sha,
                    expect=sha, actual=body.get("commit_sha"))
        log = (self.fake_get("/_log") or {}).get("entries") or []
        blocked = [(e.get("egress"), e.get("path")) for e in log if e.get("blocked")]
        self.state["blocked_egress"] = blocked[-5:]
        j.check("nothing reached an unfaked host", not blocked, expect="no blocked egress", actual=blocked[-3:])
        for repo in TEMPLATES:
            git = [(e.get("method"), str(e.get("path"))[:60], str(e.get("auth", ""))[:10], e.get("status"))
                   for e in log if str(e.get("egress", "")).startswith("github.com")
                   and f"/fxd-org/{repo}" in str(e.get("path"))]
            self.state[f"github_requests_{repo}"] = git[-6:]
            j.note(f"github.com requests for {repo}: {git[-6:]}")
        good = [a for _, _, a, _ in self.state.get("github_requests_tpl-good") or []]
        j.check("tpl-good (in the App's installation) was read with an installation token egress injected",
                any(a.startswith("basic:ghs") for a in good), expect="basic:ghs_…", actual=good)
        public = [a for _, _, a, _ in self.state.get("github_requests_tpl-public") or []]
        j.check("tpl-public (no org credential covers it) was read anonymously, nothing more",
                bool(public) and not any(a.startswith(("basic:", "bearer")) for a in public),
                expect="no credential", actual=public)

    # ---- 4. the security review asks for consent the console can render ------------------------
    def review_consent(self, page: Any) -> None:
        j = self.j
        app = self.state.get("app") or {}
        if not app.get("app_key"):
            return
        r = I.api_in_page(page, "POST", "/v1/delivery-workflows/start",
                          {"project_id": app["project"], "workflow_key": "security_review",
                           "input": {"app_key": app["app_key"]}})
        body = r.get("body") or {}
        self.state["review_start"] = {"status": r["status"], "error": body.get("error"),
                                      "keys": sorted(body)[:20]}
        if r["status"] == 409 and body.get("error") == "consent_required":
            ok = bool(body.get("agents")) and bool(body.get("durations")) and "permissions" in body \
                and body.get("scope") == "workflow_run"
            j.check("the security review asks for consent with the build's structured contract", ok,
                    expect="{error: consent_required, scope: workflow_run, agents, permissions, durations}",
                    actual=K.short(body, 500))
        else:
            j.check("the security review starts (a covering grant exists)", r["status"] in (200, 201, 202),
                    expect="2xx, or a structured consent request", actual=(r["status"], K.short(body, 400)))

    def run_all(self) -> None:
        j = self.j
        ctx, page = self.page()
        try:
            app_run = None
            if self.args.resume:
                app_run = self.psql("SELECT id FROM code_task_runs WHERE status = 'completed' "
                                    "AND coalesce(pull_request_url,'') <> '' ORDER BY created_at ASC LIMIT 1")
                j.check("an app built by an earlier walk exists", bool(app_run), expect="a completed build",
                        actual=app_run, fatal=True)
            else:
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
                with j.tolerate("scan the GitHub-built app"):
                    self.scan(page, app_run)
            with j.tolerate("import templates from the fake GitHub"):
                self.import_templates(page)
            with j.tolerate("security review consent"):
                self.review_consent(page)
        except Exception as exc:  # noqa: BLE001
            j.error = K.fmt_exc(exc)
            j.note(f"ABORTED: {j.error}")
        finally:
            j.finished = time.time()
            ctx.close()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for a in ("--stack-dir", "--project", "--console", "--api", "--fake", "--shots"):
        ap.add_argument(a, required=True)
    ap.add_argument("--report", default=None)
    ap.add_argument("--build-timeout", type=int, default=900)
    ap.add_argument("--scan-timeout", type=int, default=900)
    ap.add_argument("--guard-wait", type=int, default=960)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--resume", action="store_true", help="scan the app an earlier walk built on this stack")
    args = ap.parse_args(argv)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed, proxy={"server": args.fake, "bypass": "localhost,127.0.0.1"})
        walk = ScanImportWalk(args, browser)
        walk.j.title = "Scan a GitHub-built app; import a template from GitHub"
        walk.run_all()
        browser.close()
    result = {**walk.j.as_dict(), "state": json.loads(K.scrub(json.dumps(walk.state, default=str)))}
    report = Path(args.report) if args.report else walk.j.shots / "scan_and_import_walk.json"
    K.write_private(report, json.dumps(result, indent=1))
    print(f"[scan+import] {result['status']} — report {report}")
    return 1 if result["status"] == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
