"""THE E2E DEV API: the worktree's real API, signed in as an owner, with the dev seams the
click-by-click console suites drive (tests/e2e/add_project, local_first, local_change_review).
Started by scripts/e2e/local-change-review.sh; NEVER for a real install, and it refuses any
database not named boss_test_*.

What is replaced, and nothing else:

  * auth: every request is the owner usr_pxdev (the identity seam the API tests use); the second
    factor is fresh or lapsed (POST /__dev/state {"mfa": "fresh"|"lapsed"}), and the step-up
    sheet's check (POST /v1/auth/step-up) takes "pxdev-pass" or code 000000;
  * the model: agent_decision._routed pinned to this machine's Claude CLI ("cli"), no model
    ("none"), or one that never answers ("slow");
  * GitHub: file trees from a fixture, reach through the plumbing gate's fake
    (tests/e2e/stubs/fakes.py on LF_FAKES), a borrowed connection for a project without one;
  * a host folder: the ssh probe and tar stream answered from fixtures (TEST-NET hosts);
  * readiness: POST /__dev/state {"refuse": true} refuses with the real checklist shape;
  * /__dev/lf/<seam>: build stages and worker jobs run with the pipeline's own functions (see
    the LOCAL-FIRST DEV SEAMS block below).

Environment: DATABASE_URL (boss_test_*), BOSS_LOCAL_REPO_ROOT, BOSS_LOCAL_IMPORT_ROOT, LF_FAKES,
BOSS_E2E_STATE (a folder for this API's disposable server key). Argument: the port.
"""
import base64, json, os, runpy, sys, threading, time
import re as _re
assert _re.search(r"/boss_test_\w+$", os.environ["DATABASE_URL"]), "the e2e dev API runs only on a boss_test_* database"
# A disposable server key for this dev API only, kept in the scratchpad (never printed).
_KEY = os.path.join(os.environ["BOSS_E2E_STATE"], ".dev-server-key")
if not os.path.exists(_KEY):
    with open(os.open(_KEY, os.O_WRONLY | os.O_CREAT, 0o600), "w") as fh:
        fh.write(base64.b64encode(os.urandom(32)).decode())
os.environ["BOSS_IDENTITY_SERVER_KEY"] = open(_KEY).read().strip()
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(_ROOT)
sys.path[:0] = ["engine", "apps/api", "apps/worker", "."]
from boss_engine.identity import vault
vault.boot = lambda *, db: ""

import faulthandler, signal
faulthandler.register(signal.SIGUSR1, all_threads=True)
STATE = {"mfa": "fresh", "model": os.environ.get("PX_MODEL", "cli"), "refuse": False, "calls": [], "github": "fake", "offline": False}

# ── the model ───────────────────────────────────────────────────────────────────────────────
from boss_engine.services import agent_decision
from boss_engine.services.claude_cli_adapter import ClaudeCliAdapter
from boss_engine.config import get_settings
from boss_engine.services.project_experience import add_project as _ap
_cli = ClaudeCliAdapter(get_settings())
_WAIT = _ap.WAIT_S


class _Never:
    def __getattr__(self, name):
        def call(*a, **k):
            time.sleep(_ap.WAIT_S + 14)   # past the route's wait (+10 s slack), then gone
            raise RuntimeError("slow model")
        return call


def _routed(connection, settings, project_id, ref, text):
    m = STATE["model"]
    if m == "none":
        raise RuntimeError("no model is connected")
    if m == "slow":
        return _Never(), _Never()
    return _cli, _cli


agent_decision._routed = _routed
agent_decision._jev_ready = lambda settings: False

# ── GitHub file trees and the host-folder probe ─────────────────────────────────────────────
from boss_engine.services import start_project
TREE = ["package.json", "README.md", "src/index.ts", "src/app.ts", "src/routes/tickets.ts",
        "src/routes/users.ts", "src/models/ticket.ts", "src/models/user.ts", "web/index.html",
        "web/app.tsx", "web/pages/inbox.tsx", "tests/tickets.test.ts", "tests/users.test.ts"]
start_project.github_paths = lambda connection, org_id, full_name, branch: list(TREE)

from boss_engine.services.project_experience import host_folder
_FOLDERS = {"/srv/apps/ledger-api": "ok"}


def _runner(settings):
    def run(host, command):
        exists = any(p in command for p in _FOLDERS)
        if not exists:
            return 0, "NODIR\n", ""
        out = ("DIR\nGIT\nREMOTE https://github.com/acme/ledger-api.git\nBRANCH main\nFILES 42\n"
               "TOP pyproject.toml\nTOP src\nTOP tests\nTOP README.md\n")
        return 0, out, ""
    return run


host_folder._ssh_runner = _runner

# ── a new project's GitHub binding: the disposable DB has no working GitHub App or token, so a
#    project that has no connection of its own borrows a copy of an existing (fake) one ───────
from boss_engine.multirepo import project_repos
from boss_engine.multirepo.common import MultiRepoError
from sqlalchemy import text as _t
_conn_for = project_repos._connection_for


def _connection_for(db, settings, project_id, actor, **kw):
    try:
        return _conn_for(db, settings, project_id, actor, **kw)
    except MultiRepoError as e:
        said = str(getattr(e, "code", "")) + str(e.body() if hasattr(e, "body") else "")
        if "github_not_connected" not in said and "github_repo_unreachable" not in said:
            raise
    have = db.execute(_t("SELECT id FROM github_connections WHERE project_id = :p AND status = 'active' LIMIT 1"),
                      {"p": project_id}).scalar()
    if have:
        return have    # its (dev) connection reaches the fake GitHub: no second copy
    cols = [r[0] for r in db.execute(_t("SELECT column_name FROM information_schema.columns WHERE table_name = 'github_connections'"))]
    new_id = "ghc_dev" + os.urandom(6).hex()
    sel = ", ".join(":id" if c == "id" else ":p" if c == "project_id" else c for c in cols)
    db.execute(_t(f"INSERT INTO github_connections ({', '.join(cols)}) SELECT {sel} FROM github_connections "
                  "WHERE status = 'active' ORDER BY created_at LIMIT 1"), {"id": new_id, "p": project_id})
    return new_id


project_repos._connection_for = _connection_for

# ── app secrets: the disposable DB's vault was sealed with a key this machine does not hold, so a
#    connector's generated/test secret is kept as "no value" here ─────────────────────────────
vault.store_app_secret = lambda **kw: None
vault.read_app_secret = lambda item_id, **kw: "dev-secret"

# ── readiness: in "refuse" mode the per-need check says the deployment target is missing ────
from boss_engine.services import workflow_readiness as _wr
_pre = _wr.preflight


def _preflight(connection, settings, project_id, spec, input_data=None):
    out = _pre(connection, settings, project_id, spec, input_data)
    if not STATE["refuse"]:
        return out
    need = _wr.need_for("deployment_target")
    item = {"key": need.key, "label": need.label, "fix": need.fix, "area": need.area, "href": need.href,
            "needed_by": [{"step_id": "deploy", "label": "Deploy"}], "first_step": "deploy",
            "first_step_label": "Deploy", "after_a_person": True, "ok": False,
            "detail": "no verified deployment target for this project (dev seam)"}
    checked = [c for c in out["checked"] if c["key"] != need.key] + [item]
    missing = [c for c in checked if not c["ok"]]
    return {**out, "checked": checked, "missing": missing, "ready": False,
            "summary": _wr.summary_sentence(missing)}


_wr.preflight = _preflight

# The model is this machine's Claude CLI (pinned above) and GitHub is stubbed: the readiness of
# those two needs answers as the seams do, not as the disposable DB's empty settings would.
_wr_check = _wr.check


def _check(connection, settings, project_id, key):
    if key == "model":
        return True, "the dev API pins the decision model to this machine's Claude CLI (dev seam)"
    if key == "repository":
        return True, "GitHub is stubbed on the dev API (dev seam)"
    return _wr_check(connection, settings, project_id, key)


_wr.check = _check

# ── auth ────────────────────────────────────────────────────────────────────────────────────
import base64
os.environ.setdefault("BOSS_IDENTITY_SERVER_KEY", base64.b64encode(os.urandom(32)).decode())
from sqlalchemy import create_engine, text
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity.principal import Principal, PrincipalKind, PrincipalStatus  # noqa
eng = create_engine(os.environ["DATABASE_URL"], future=True)
w = IdentityWorld(eng, "org_boss", "ws_default", "helpdesk-ai-demo", "helpdesk-ai-demo")
with eng.begin() as db:
    have = db.execute(text("SELECT 1 FROM identity_principals WHERE id='usr_pxdev'")).first()
if not have:
    owner = w.person("PX Dev", principal_id="usr_pxdev")
    w.bind(owner, "owner")
    w.claimed()
else:
    owner = Principal("usr_pxdev", PrincipalKind.USER, "org_boss", PrincipalStatus.ACTIVE, "PX Dev", False)
from apps.api.app import identity_guard
st = get_settings()


def _auth(creds, conn=None):
    c = w.ctx(owner, mfa=STATE["mfa"] == "fresh")
    if conn is not None:
        conn.state.identity = c
    return c


identity_guard._authenticate = _auth
st.authz_mode, st.authz_router_modes = "enforce", ""

# LOCAL-FIRST DEV SEAMS (exec'd into devapi_lf.py; never for a real install).
#
#   GitHub      STATE["github"] "fake": BOSS reaches GitHub through the plumbing gate's fake
#               (tests/e2e/stubs/fakes.py on LF_FAKES): local_publish.default_reach and the
#               push-pending job's GitHub use it, with an App-like create token. "none": the real
#               default_reach (this dev box has no GitHub App: "no credential" words).
#   offline     STATE["offline"]: the machine checks' network probe answers offline.
#   /__dev/lf/stage        {project_id, title?}  a build stage of a LOCAL project, driven with the
#                          pipeline's own functions (SoftwareFactoryService on the project's
#                          binding: ensure_branch + upsert_file commits, propose_pull_request opens
#                          the change set and its review decision, a review comment) — the agent's
#                          coding is skipped (a full agent build is too slow for a UI suite).
#   /__dev/lf/run-merges   {project_id}  the worker's governed-merge job(s) Merge queued, run as the
#                          worker runs them (merge_pull_request on the local adapter).
#   /__dev/lf/preview      {project_id, branch?}  the preview's clone of the local repository
#                          (identity.runs.repo_access → file://), the files it got.
#   /__dev/lf/outage-run   {project_id}  a GitHub project's build through an outage: mirror refreshed,
#                          GitHub switched off (fake 502s), the stage commits land on the mirror, the
#                          "PR" is a pending-push change set, the run ends waiting_for_github.
#   /__dev/lf/push-pending {project_id}  the worker's push-pending job, once per waiting push.
#   /__dev/lf/github       {outage?: bool, owners?: [...]}  the fake's switches.
import json as _json
import urllib.request as _ur
import uuid as _uuid

_FAKES = os.environ.get("LF_FAKES", "http://127.0.0.1:8919").rstrip("/")

from boss_engine.services import local_publish as _lp, local_projects as _lpr, github_outage as _go, local_repo as _lr
from boss_engine.services.repo_host import GitAccess as _GitAccess
from boss_engine.services.github_adapter import GitHubAdapter as _GHA

_real_reach = _lp.default_reach


def _fake_adapter(settings, owner, repo):
    return _GHA(settings, owner=owner, repo=repo, token="e2e-token", api_base_url=f"{_FAKES}/github")


def _fake_access(owner, repo):
    return _GitAccess(f"{_FAKES}/gh-web/{owner}/{repo}.git", via="token")


def _reach(c, settings, project_id, *, connection_id=None, actor="operator", owner="", repo=""):
    if STATE.get("github") != "fake":
        return _real_reach(c, settings, project_id, connection_id=connection_id, actor=actor, owner=owner, repo=repo)
    conn = connection_id
    if conn is None:
        try:
            # Its own transaction: the dev borrow runs after a refused lookup, which would leave the
            # caller's transaction aborted.
            with eng.begin() as c2:
                conn = project_repos._connection_for(c2, settings, project_id, actor, owner=owner, repo=repo)
        except Exception:  # noqa: BLE001
            conn = None
    return _lp.GitHubReach(adapter_for=lambda o, r: _fake_adapter(settings, o, r),
                           access_for=_fake_access, create_token="e2e-token", create_token_kind="app",
                           api_base_url=f"{_FAKES}/github", connection_id=conn)


_lp.default_reach = _reach

# A server's folder copied here (Bring existing code › a server › Copy it here): the demo hosts are
# TEST-NET addresses, so the ssh tar stream is a tar of the fixture folder named after the path's
# last part in the import directory's ".hosts/" (never an ssh session).
def _fake_ssh_tar(settings):
    import io as _io, tarfile as _tf, pathlib as _pl
    def fetch(host, path):
        root = _pl.Path(os.environ["BOSS_LOCAL_IMPORT_ROOT"]).parent / "host-fixtures" / path.rstrip("/").rsplit("/", 1)[-1]
        buf = _io.BytesIO()
        with _tf.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(str(root), arcname=".")
        return buf.getvalue()
    return fetch


_lpr._ssh_tar = _fake_ssh_tar
_real_online = _lpr._online
_lpr._online = lambda timeout=3.0: (not STATE.get("offline")) and _real_online(timeout)


def _fake_call(path, body=None):
    req = _ur.Request(f"{_FAKES}{path}", method="POST" if body is not None else "GET",
                      data=_json.dumps(body).encode() if body is not None else None,
                      headers={"content-type": "application/json"})
    with _ur.urlopen(req, timeout=20) as r:
        return _json.loads(r.read() or b"{}")


def _seam_github(body):
    out = {}
    if "owners" in body:
        out["owners"] = _fake_call("/github/_owners", {"owners": body["owners"]})
    if "outage" in body:
        out["outage"] = _fake_call("/github/_outage", {"on": bool(body["outage"])})
    out["created"] = _fake_call("/github/_created")
    return out


def _sf():
    from boss_engine.services.software_factory import SoftwareFactoryService
    return SoftwareFactoryService(get_settings())


def _seam_stage(body):
    """A build stage on a LOCAL project: commits, the change set, its review decision, a comment."""
    from boss_engine.services.software_factory import FileChange
    pid = body["project_id"]
    settings = get_settings()
    sf = _sf()
    with eng.begin() as db:
        plane = sf.bind_project_github(db, pid)
    owner, _, repo = sf.github.repository_full_name.partition("/")
    run_id = f"crun_lf{_uuid.uuid4().hex[:10]}"
    branch = f"boss/code/{run_id}"
    title = body.get("title") or "Expense list page"
    with eng.begin() as db:
        db.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, phase, spec, finalize_ctx) "
                        "VALUES (:id, :p, :t, 'running', 'coding', CAST(:s AS JSONB), CAST(:f AS JSONB))"),
                   {"id": run_id, "p": pid, "t": title, "s": _json.dumps({"owner": owner, "repo": repo}),
                    "f": _json.dumps({"work_branch": branch})})
    files = [FileChange(path="index.html", content="<!doctype html>\n<html>\n  <head><title>Expenses</title></head>\n"
                                                  "  <body>\n    <h1>Expenses</h1>\n    <ul id=\"list\"></ul>\n"
                                                  "    <script src=\"app.js\"></script>\n  </body>\n</html>\n"),
             FileChange(path="app.js", content="// The team's expenses, newest first.\nconst items = [];\n"
                                               "export function add(amount, vendor) {\n  items.unshift({ amount, vendor });\n"
                                               "  return items.length;\n}\n"),
             FileChange(path="app.test.js", content="import { add } from './app.js';\n"
                                                    "test('adds one', () => { expect(add(12, 'Cafe')).toBe(1); });\n")]
    sf.github.ensure_branch(branch, "main")
    for fc in files:
        sf.github.upsert_file(branch, fc.path, fc.content, "governed: implement")
    with eng.begin() as db:
        reservation = sf.budget.reserve(db, pid, category="code_task", amount_usd_micros=1000,
                                        reason="local-first ui dev seam", expires_in_minutes=30)
    with eng.begin() as db:
        proposal = sf.propose_pull_request(
            db, pid, None, reservation_id=reservation["reservation_id"],
            dedupe_key=f"code:task:{run_id}", title=title, branch_name=branch,
            summary="The expense list: a page, its script and a test.", files=files, run_id=run_id, pr_base="main")
    url = proposal["pull_request_url"]
    with eng.begin() as db:
        db.execute(text("UPDATE code_task_runs SET pull_request_url = :u, status = 'completed', phase = 'done', "
                        "state = COALESCE(state, '{}'::jsonb) || jsonb_build_object('tests_passed', 'true') WHERE id = :r"),
                   {"u": url, "r": run_id})
    number = int(url.rstrip("/").rsplit("/", 1)[-1])
    sf.github.create_review(number, "Review panel: reads well; one note.", "COMMENT",
                            [{"path": "app.js", "line": 3, "body": "Validate the amount is a number."}])
    return {"run_id": run_id, "number": number, "url": url, "plane": plane.get("github_plane"),
            "repository": f"{owner}/{repo}", "branch": branch}


def _seam_run_merges(body):
    """The governed merge job(s) a Merge queued, as the worker runs them."""
    pid = body["project_id"]
    with eng.begin() as db:
        jobs = db.execute(text("SELECT id, payload FROM jobs WHERE project_id = :p AND kind = "
                               "'github.pull_request.merge' AND status IN ('queued','pending','ready') "
                               "ORDER BY created_at"), {"p": pid}).mappings().all()
    done = []
    for j in jobs:
        payload = j["payload"] if isinstance(j["payload"], dict) else _json.loads(j["payload"])
        merger = _sf()
        with eng.begin() as db:
            out = merger.merge_pull_request(db, pid, None, payload["decision_id"], payload["dedupe_key"],
                                            payload["pull_request_number"], payload["pull_request_url"],
                                            payload["title"], payload.get("branch_name"))
            db.execute(text("UPDATE jobs SET status = 'succeeded', updated_at = now() WHERE id = :i"), {"i": j["id"]})
        done.append({"job": j["id"], "status": out.get("status"), "merged_sha": out.get("merged_sha")})
    return {"merged": done}


def _seam_preview(body):
    """The preview's clone of the project's local repository (file://, never GitHub)."""
    import subprocess, tempfile, shutil
    from boss_engine.identity import runs as identity_runs
    pid = body["project_id"]
    with eng.connect() as db:
        b = _lpr.local_binding(db, pid, include_mirror=True)
    with identity_runs.repo_access(eng, host="github", owner=b["owner"], repo=b["name"], project_id=pid,
                                   token="never-used") as access:
        dest = tempfile.mkdtemp(prefix="lf-preview-")
        try:
            subprocess.run(["git", "clone", "--depth", "1", "--branch", body.get("branch") or "main", access.url,
                            dest + "/app"], check=True, capture_output=True, env=access.boss_environ())
            files = sorted(p.name for p in __import__("pathlib").Path(dest, "app").iterdir() if p.name != ".git")
        finally:
            shutil.rmtree(dest, ignore_errors=True)
        return {"url": access.url, "via": access.via, "files": files}


def _seam_outage_run(body):
    """A GitHub project's build through an outage, ending waiting_for_github (see the top)."""
    from boss_engine.services.local_repo_host import LocalGitAdapter
    pid = body["project_id"]
    settings = get_settings()
    with eng.connect() as db:
        row = db.execute(text("SELECT owner, name FROM github_repository_bindings WHERE project_id = :p AND "
                              "host = 'github' AND role = 'primary' AND status = 'active' LIMIT 1"),
                         {"p": pid}).mappings().first()
    owner, repo = row["owner"], row["name"]
    _fake_call("/github/_outage", {"on": False})
    _lr.ensure_mirror(owner, repo, _fake_access(owner, repo).url, settings=settings)
    _fake_call("/github/_outage", {"on": True})
    run_id = f"crun_lf{_uuid.uuid4().hex[:10]}"
    branch = f"boss/code/{run_id}"
    with eng.begin() as db:
        db.execute(text("INSERT INTO code_task_runs (id, project_id, title, status, phase, spec, finalize_ctx) "
                        "VALUES (:id, :p, 'Receipt upload', 'running', 'coding', CAST(:s AS JSONB), CAST(:f AS JSONB))"),
                   {"id": run_id, "p": pid, "s": _json.dumps({"owner": owner, "repo": repo}),
                    "f": _json.dumps({"work_branch": branch})})
    # GitHub answers 502: the stage's write goes to the mirror (what ResilientGitHub does).
    try:
        _fake_adapter(settings, owner, repo).ensure_branch(branch, "main")
        reached = True
    except Exception:  # noqa: BLE001 — the outage
        reached = False
    mirror = LocalGitAdapter(settings, owner=owner, repo=repo, project_id=pid,
                             git_dir=_lr.mirror_path(owner, repo, settings), engine=eng, pending_push=True)
    mirror.ensure_branch(branch, "main")
    mirror.upsert_file(branch, "upload.html", "<!doctype html><h1>Upload a receipt</h1>\n", "governed: implement")
    pr = mirror.create_pull_request(branch, "Receipt upload", "Snap a receipt; BOSS reads it.", base="main")
    with eng.begin() as db:
        cs = db.execute(text("SELECT id FROM local_change_sets WHERE project_id = :p AND branch = :b ORDER BY created_at DESC LIMIT 1"),
                        {"p": pid, "b": branch}).scalar()
        pending = _go.record_pending(db, project_id=pid, owner=owner, repo=repo, branch=branch, base_branch="main",
                                     run_id=run_id, change_set_id=cs, reason="GitHub answered 502 (fake outage)",
                                     enqueue=False)
        db.execute(text("UPDATE repo_push_pending SET next_attempt_at = now() + interval '2 minutes', attempts = 1, "
                        "last_error = 'HTTP 502 from GitHub' WHERE id = :i"), {"i": pending})
        db.execute(text("UPDATE code_task_runs SET pull_request_url = :u, status = 'completed' WHERE id = :r"),
                   {"u": pr.url, "r": run_id})
        _go.mark_run_waiting(db, run_id, reason="GitHub answered 502 (fake outage)", pending_id=pending)
        # The build an agent's work item started (Agents at work lists the item, with what its
        # build waits for): an always-on item linked to this run, as a squad's build links it.
        item = f"wi_lf{_uuid.uuid4().hex[:12]}"
        db.execute(text("INSERT INTO work_items (id, project_id, subject_type, subject_id, title, description, ambient, "
                        "fingerprint, lifecycle, task_class, source, created_by) VALUES (:i, :p, 'roadmap', :s, "
                        "'Receipt upload', 'Snap a receipt; BOSS reads it.', true, :s, 'in_progress', 'roadmap_item', "
                        "'onboarding', 'agent:chief_of_staff')"), {"i": item, "p": pid, "s": f"lf:{run_id}"})
        db.execute(text("INSERT INTO work_item_links (work_item_id, kind, ref, project_id) VALUES (:i, 'code_task', :r, :p)"),
                   {"i": item, "r": run_id, "p": pid})
    return {"work_item_id": item,"run_id": run_id, "pending_id": pending, "branch": branch, "reached_github": reached,
            "local_url": pr.url}


def _seam_push_pending(body):
    pid = body["project_id"]
    with eng.connect() as db:
        ids = [r[0] for r in db.execute(text("SELECT id FROM repo_push_pending WHERE project_id = :p AND status IN "
                                             "('waiting','pushing')"), {"p": pid})]
    settings = get_settings()
    out = [_go.attempt(eng, settings, i, github=lambda c, s, p, o, r: (_fake_adapter(s, o, r), _fake_access(o, r)))
           for i in ids]
    return {"attempts": out}


def _seam_onboard(body):
    """The worker's onboarding job, one pass (the dev stack runs no worker)."""
    from boss_engine.services.project_experience import onboarding as _ob
    pid = body["project_id"]
    with eng.connect() as db:
        run_id = db.execute(text("SELECT id FROM project_onboarding_runs WHERE project_id = :p ORDER BY started_at DESC NULLS LAST, updated_at DESC LIMIT 1"),
                            {"p": pid}).scalar()
    if not run_id:
        return {"error": "no onboarding run"}
    return _ob.advance(eng, get_settings(), run_id, requeue=False)


def _seam_conn(body):
    import traceback as _tb
    try:
        with eng.begin() as c2:
            return {"conn": project_repos._connection_for(c2, get_settings(), body["project_id"], "usr_pxdev",
                                                          owner=body.get("owner", "acme"), repo=body.get("repo", "x"))}
    except Exception as e:  # noqa: BLE001
        return {"error": _tb.format_exc()[-1500:]}


def _seam_answer(body):
    """BOSSAPP-948: the fix round's builder answering the person's comments, through the
    pipeline's own functions: its RESPONSE lines (_record_comment_responses, numbered as the brief
    numbered them), a commit on the branch, and the change set refreshed (on_change_proposed)."""
    from boss_engine.services import local_review as _lrv, review_comments as _rc
    from boss_engine.services.governed_code_task_service import _record_comment_responses
    from boss_engine.services.local_repo_host import LocalGitAdapter
    pid, n = body["project_id"], int(body["number"])
    settings = get_settings()
    with eng.begin() as db:
        cs = _lrv.change_row(db, pid, n)
        fctx = db.execute(text("SELECT finalize_ctx FROM code_task_runs WHERE id = :r"), {"r": cs["run_id"]}).scalar() or {}
    shown = _rc.implementer_order([_rc.ReviewComment.from_dict(e) for e in (fctx.get("review_comments") or [])
                                   if str(e.get("id") or "").startswith("lcc_")])
    words = body.get("words") or "checks the amount is a number before adding it"
    reply = "\n".join(f"RESPONSE: {i} FIXED — {words}" for i in range(1, len(shown) + 1))
    _record_comment_responses(eng, cs["run_id"], pid, shown, reply, round_i=1, changed_files=["app.js"])
    owner, _, repo = cs["repository"].partition("/")
    ad = LocalGitAdapter(settings, owner=owner, repo=repo, project_id=pid, engine=eng)
    now = _lr.read_file(ad.git_dir, cs["branch"], "app.js") or ""
    ad.commit_files(cs["branch"], {"app.js": now + "export const isAmount = (x) => typeof x === 'number' && x >= 0;\n"},
                    "fix round: validate the amount")
    with eng.begin() as db:
        out = _lrv.on_change_proposed(db, settings, run_id=cs["run_id"], repository=cs["repository"], number=n)
    return {"answered": len(shown), **out}


LF_SEAMS = {"answer": _seam_answer,"conn": _seam_conn, "onboard": _seam_onboard,"github": _seam_github, "stage": _seam_stage, "run-merges": _seam_run_merges,
            "preview": _seam_preview, "outage-run": _seam_outage_run, "push-pending": _seam_push_pending}

# ── Add a GitHub account (BOSSAPP-945/1015/1016): the accounts fake + its /__dev/gh seam ─────
# The fake GitHub the API tests use (apps/api/tests/github_accounts_fake.py) answers every read
# the sheet makes; POST /__dev/gh does what a person does on github.com (make the app public,
# install it on an account), exactly as tests/e2e/add_github_account expects.
from urllib.parse import urlsplit as _urlsplit  # noqa: E402

from apps.api.tests.github_accounts_fake import demo_world as _gh_world  # noqa: E402
from boss_engine.identity import github_app as _gh_app, github_setup as _gh_setup  # noqa: E402
from boss_engine.identity.authn_support import default_org_id as _gh_org  # noqa: E402
from boss_engine.services import setup_checks as _gh_checks  # noqa: E402

GH_SLUG = "boss-localhost-ab12"
_GH = _gh_world(slug=GH_SLUG)
_gh_app.TRANSPORT = _GH
_gh_app.app_jwt = lambda db, now=None: "eyJfake.app.jwt"


def _gh_ready(db, org_id=None):
    cfg = _gh_setup.app_config(db, org_id)
    return bool(cfg["app_id"] and cfg["installation_id"])


_gh_setup.app_ready = _gh_ready

# Only GitHub's own hosts go to the accounts fake; every other probe keeps the real client
# (the plumbing-gate fakes answer their own URLs).
_gh_http0 = _gh_checks.default_http


def _gh_http(method, url, *, headers, json_body=None, timeout=15.0):
    if _urlsplit(url).netloc in ("api.github.com", "github.com"):
        return _GH.http(method, url, headers=headers, json_body=json_body, timeout=timeout)
    return _gh_http0(method, url, headers=headers, json_body=json_body, timeout=timeout)


_gh_checks.default_http = _gh_http


def _gh_project_ready(org: str) -> None:
    """The guided sheet lives on the blocks layout, which only shows once the project is proven
    (a working repository AND a checked model). Prove the repo through the product's own ops —
    the checks run against this fake — and keep one checked model source."""
    from boss_engine.services import setup_actions as _sa

    pid = os.environ.get("BOSS_DEFAULT_PROJECT_ID", "helpdesk-ai-demo")
    with eng.begin() as db:
        db.execute(_t(
            "INSERT INTO entitlement_sources (id, project_id, provider, source_id, source_kind, "
            "entitlement_kind, owner_type, owner_id, status, auth_method, connection_status, "
            "connection_metadata, supported_models) VALUES ('es_dev_model', :p, 'claude_code', "
            "'claude_code', 'subscription', 'subscription', 'org', :o, 'connected', 'oauth', "
            "'connected', :m, '[\"claude\"]'::jsonb) ON CONFLICT (project_id, source_id) DO NOTHING"),
            {"p": pid, "o": org,
             "m": _json.dumps({"last_check": {"ok": True, "state": "working",
                                              "at": "2026-01-01T00:00:00Z"}})})
        bound = db.execute(_t("SELECT 1 FROM github_repository_bindings WHERE project_id = :p "
                              "AND status = 'active' LIMIT 1"), {"p": pid}).first()
    if bound:
        return
    settings = get_settings()
    with eng.begin() as db:
        out = _sa.repository_from_app(db, settings, pid, actor="dev")
    with eng.begin() as db:
        _sa.select_repository(db, settings, pid, out["connection_id"], f"paramg/{pid}",
                              make_default=True, actor="dev")


def _seam_gh(body):
    from sqlalchemy.exc import DBAPIError
    try:
        with eng.begin() as db:
            org = _gh_org(db)
    except DBAPIError:
        # A pooled connection Postgres had already terminated: it is invalidated by the error,
        # so one retry gets a fresh connection (seen once as a flaky 500 in the e2e fixture).
        with eng.begin() as db:
            org = _gh_org(db)
    if body.get("reset"):
        fresh = _gh_world(slug=GH_SLUG)
        _GH.public, _GH.users, _GH.repos = False, fresh.users, fresh.repos
        _GH.installations, _GH.read_only, _GH.tokens, _GH.calls = {}, set(), {}, []
        default = _GH.install("paramg")
        with eng.begin() as db:
            db.execute(_t("DELETE FROM identity_github_installations WHERE org_id = :o"), {"o": org})
            db.execute(_t("DELETE FROM identity_github_app WHERE org_id = :o"), {"o": org})
            db.execute(_t("DELETE FROM github_repos WHERE org_id = :o"), {"o": org})
            db.execute(_t(
                "INSERT INTO identity_github_app (org_id, app_id, slug, client_id, installation_id, "
                "account_login, account_type, owner_login, owner_type, created_by) VALUES "
                "(:o, '777', :s, 'Iv1.x', :i, 'paramg', 'User', 'paramg', 'User', 'dev')"),
                {"o": org, "s": GH_SLUG, "i": default})
        _gh_project_ready(org)
    if "public" in body:
        _GH.make_public(bool(body["public"]))
    if body.get("install"):
        _GH.install(str(body["install"]))
    if body.get("uninstall"):
        _GH.uninstall(str(body["uninstall"]))
    if "read_only" in body:
        _GH.read_only = {str(r).lower() for r in (body["read_only"] or [])}
    return {"public": _GH.public, "installations": dict(_GH.installations)}

from apps.api.app.main import app  # noqa: E402
from apps.api.app import main as _main  # noqa: E402
# Merge = a person approving the review decision queues the governed merge (as the API test does).
_main.decisions.settings.enable_sandbox_merge = True
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402


@app.middleware("http")
async def _dev(request: Request, call_next):
    path = request.url.path
    if path == "/__dev/state":
        if request.method == "POST":
            body = await request.json()
            for k in ("mfa", "model", "refuse", "github", "offline"):
                if k in body:
                    STATE[k] = body[k]
            _ap.WAIT_S = 8 if STATE["model"] == "slow" else _WAIT
            if body.get("clear_calls"):
                STATE["calls"] = []
        return JSONResponse({k: v for k, v in STATE.items()})
    if path == "/__dev/gh" and request.method == "POST":
        from starlette.concurrency import run_in_threadpool
        try:
            return JSONResponse(await run_in_threadpool(_seam_gh, await request.json()))
        except Exception as e:  # noqa: BLE001
            import traceback; traceback.print_exc()
            return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    if path.startswith("/__dev/lf/") and request.method == "POST":
        from starlette.concurrency import run_in_threadpool
        body = await request.json()
        fn = LF_SEAMS.get(path[len("/__dev/lf/"):])
        if fn is None:
            return JSONResponse({"error": "no such seam"}, status_code=404)
        try:
            return JSONResponse(await run_in_threadpool(fn, body))
        except Exception as e:  # noqa: BLE001
            import traceback; traceback.print_exc()
            return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    if path == "/v1/auth/step-up" and request.method == "POST":
        body = await request.json()
        if body.get("password") == "pxdev-pass" or body.get("code") == "000000":
            STATE["mfa"] = "fresh"
            return JSONResponse({"ok": True, "step_up_fresh_until": None})
        return JSONResponse({"error": "invalid_credentials"}, status_code=401)
    resp = await call_next(request)
    if path.startswith("/v1/"):
        STATE["calls"] = (STATE["calls"] + [f"{request.method} {path} {resp.status_code}"])[-400:]
    return resp


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
