"""/v1/security/* on the real app under enforce (APP_TEMPLATES_AND_SECURITY_LOOP.md §4.3, §4.9 "API").

Authorization per role, scope refusals against the database (another project's preview, another
app's, a free URL), the worker-only run route, triage (a reason, an event, carried to the next scan)
and the before/after report — with the scan's tools replaced by their REAL recorded output
(engine/tests/fixtures/security_scan): the toolchain itself is exercised by the compose proof
(tests/e2e/security/scan_proof.py).
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api.app import main
from apps.api.tests.identity_fixtures import IdentityWorld
from boss_engine.identity.principal import AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus
from boss_engine.security_scan import parsers, service
from boss_engine.security_scan.runners import ToolResult
from boss_engine.services import toolchains

ROOT = Path(__file__).resolve().parents[3]
FIX = ROOT / "engine" / "tests" / "fixtures" / "security_scan"
APPS = {"before": ROOT / "tests/e2e/security/vulnapp", "after": ROOT / "tests/e2e/security/vulnapp-fixed"}
NAMES = {"before": "vulnapp", "after": "vulnapp-fixed"}
TOOLS = {"semgrep": "1.180.0", "semgrep_rules": "sha256:" + "a" * 64, "osv_scanner": "2.6.0", "zap": "2.17.0"}


@pytest.fixture(scope="module")
def world(engine) -> IdentityWorld:
    return IdentityWorld.create(engine)


@pytest.fixture(autouse=True)
def scanners_installed(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"java": {"jdk": "x"}, "security": TOOLS}))
    monkeypatch.setenv(toolchains.MANIFEST_ENV, str(manifest))
    return manifest


def _app(engine, project: str, app_key: str) -> str:
    run_id = f"ctr_{uuid.uuid4().hex[:12]}"
    with engine.begin() as c:
        c.execute(text(
            "INSERT INTO code_task_runs (id, project_id, title, status, spec, finalize_ctx) "
            "VALUES (:i, :p, 'sec app', 'completed', CAST(:s AS JSONB), CAST(:f AS JSONB))"),
            {"i": run_id, "p": project, "s": json.dumps({"owner": "acme", "repo": "shop"}),
             "f": json.dumps({"app_id": app_key})})
    return run_id


def _preview(engine, project: str, run_id: str, url: str = "http://127.0.0.1:41234", status: str = "running") -> str:
    pid = f"prev_{uuid.uuid4().hex[:10]}"
    with engine.begin() as c:
        c.execute(text("INSERT INTO app_previews (id, project_id, run_id, url, status) "
                       "VALUES (:i, :p, :r, :u, :s)"), {"i": pid, "p": project, "r": run_id, "u": url, "s": status})
    return pid


def _person(world, role, project=None):
    p = world.person(f"sec-{role}-{uuid.uuid4().hex[:6]}")
    world.bind(p, role, project=project or world.project_a)
    return p


def _client():
    return TestClient(main.app, raise_server_exceptions=False)


def _post_scan(client, world, app_key, **body):
    return client.post("/v1/security/scans", json={"project_id": world.project_a, "app_key": app_key, **body})


def _results(which: str, *, failed: tuple[str, ...] = (), rules: str = TOOLS["semgrep_rules"]):
    root = APPS[which]
    sem = parsers.parse_semgrep_sarif((FIX / f"{NAMES[which]}.semgrep.sarif").read_bytes(), root=root)[0]
    osv = parsers.parse_osv_json((FIX / f"{NAMES[which]}.osv.json").read_bytes(), root=Path("/src"))[0]
    zap = parsers.parse_zap_json((FIX / f"{NAMES[which]}.zap.json").read_bytes())[0]
    out = []
    for name, found in (("semgrep", sem), ("osv", osv), ("zap", zap)):
        if name in failed:
            out.append(ToolResult(name, status="failed", error="timed out after 600s"))
        else:
            out.append(ToolResult(name, version=TOOLS.get(name, TOOLS["osv_scanner"]),
                                  rules_digest=rules if name == "semgrep" else None, findings=found,
                                  routes=["/", "/robots.txt", "/sitemap.xml", "/token", "/users"] if name == "zap" else []))
    files = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
    return out, files


def _run(world, engine, app_key: str, which: str, **kw) -> dict:
    """Request a scan through the API as an operator, then execute it with the recorded output."""
    op = _person(world, "operator")
    with world.acting_as(world.ctx(op)):
        r = _post_scan(_client(), world, app_key)
    assert r.status_code == 202, r.text
    scan_id = r.json()["scan_id"]
    results, files = _results(which, **kw)
    out = service.execute_scan(engine, main.settings, scan_id,
                               do_checkout=lambda *a: ("c0ffee" if which == "before" else "beefed") * 6 + "abcd",
                               run_tools=lambda *a: (results, files))
    assert out["ok"] is (len(kw.get("failed", ())) < 3)
    return out["scan"]


# ---- authorization per role --------------------------------------------------------------------

@pytest.mark.parametrize("role,can_run,can_triage", [
    ("owner", True, True), ("admin", True, True), ("operator", True, True),
    ("viewer", False, False), ("auditor", False, False),
])
def test_roles(world, engine, role, can_run, can_triage):
    app_key = f"app-{uuid.uuid4().hex[:8]}"
    _app(engine, world.project_a, app_key)
    scan = _run(world, engine, app_key, "before")
    finding = service.list_findings(engine.connect(), scan["id"])[0]
    who = _person(world, role)
    client = _client()
    with world.acting_as(world.ctx(who)):
        assert (_post_scan(client, world, app_key).status_code == 202) is can_run
        reads = [client.get("/v1/security/scans", params={"project_id": world.project_a, "app_key": app_key}),
                 client.get(f"/v1/security/scans/{scan['id']}"),
                 client.get(f"/v1/security/scans/{scan['id']}/findings")]
        assert all(r.status_code == 200 for r in reads), [r.status_code for r in reads]
        t = client.post(f"/v1/security/findings/{finding['id']}/triage",
                        json={"status": "accepted", "reason": "only reachable by admins"})
        assert (t.status_code == 200) is can_triage, t.text
        if not can_triage:
            # The platform's no-existence-oracle rule (identity/authz._invisible): there is no
            # `security_finding:read`, so a denied object-scoped triage answers 404, not 403.
            assert t.status_code == 404


def test_a_person_of_another_project_reads_nothing_and_cannot_scan(world, engine):
    app_key = f"app-{uuid.uuid4().hex[:8]}"
    _app(engine, world.project_a, app_key)
    scan = _run(world, engine, app_key, "before")
    finding = service.list_findings(engine.connect(), scan["id"])[0]
    outsider = _person(world, "owner", project=world.project_b)
    client = _client()
    with world.acting_as(world.ctx(outsider)):
        assert _post_scan(client, world, app_key).status_code == 403
        # another project's scan is invisible: 404, never a 403 that confirms it exists
        assert client.get(f"/v1/security/scans/{scan['id']}").status_code == 404
        assert client.get(f"/v1/security/scans/{scan['id']}/findings").status_code == 404
        assert client.post(f"/v1/security/findings/{finding['id']}/triage",
                           json={"status": "accepted", "reason": "nope nope"}).status_code == 404
        assert client.get(f"/v1/security/reports/{app_key}", params={"project_id": world.project_a}).status_code == 403
    with world.acting_as(None):
        assert client.get(f"/v1/security/scans/{scan['id']}").status_code == 401


def test_only_the_worker_runs_a_scan(world, engine):
    owner = _person(world, "owner")
    client = _client()
    with world.acting_as(world.ctx(owner)):
        assert client.post("/v1/security/scans/scan_nope/run").status_code == 403
    worker = AuthContext(Principal("sys_worker", PrincipalKind.SYSTEM, world.org, PrincipalStatus.ACTIVE, "worker"),
                         AuthMethod.SYSTEM)
    with world.acting_as(worker):
        r = client.post("/v1/security/scans/scan_nope/run")
    assert r.status_code == 200 and r.json() == {"ok": False, "error": "no such scan: scan_nope"}


# ---- scope refusals ----------------------------------------------------------------------------

def test_scope_refusals(world, engine, scanners_installed):
    app_key = f"app-{uuid.uuid4().hex[:8]}"
    run_a = _app(engine, world.project_a, app_key)
    other_app_run = _app(engine, world.project_a, f"other-{uuid.uuid4().hex[:6]}")
    run_b = _app(engine, world.project_b, app_key)        # same app key, ANOTHER project
    mine = _preview(engine, world.project_a, run_a)
    theirs = _preview(engine, world.project_b, run_b)
    other_app = _preview(engine, world.project_a, other_app_run)
    remote = _preview(engine, world.project_a, run_a, url="https://shop.example.com")
    stopped = _preview(engine, world.project_a, run_a, status="stopped")
    op = _person(world, "operator")
    client = _client()
    cases = [
        ({"zap": {"target": "http://169.254.169.254/latest/meta-data"}}, 422, "target_out_of_scope"),
        ({"zap": {"target": "https://example.com"}}, 422, "target_out_of_scope"),
        ({"zap": {"target": f"preview:{theirs}"}}, 404, "preview_not_found"),
        ({"zap": {"target": f"preview:{other_app}"}}, 422, "target_out_of_scope"),
        ({"zap": {"target": f"preview:{remote}"}}, 409, "preview_not_reachable"),
        ({"zap": {"target": f"preview:{stopped}"}}, 409, "preview_not_running"),
        ({"ref": "main"}, 422, "ref_out_of_scope"),
        ({"ref": "https://github.com/evil/repo"}, 422, "ref_out_of_scope"),
        ({"tools": ["semgrep", "nmap"]}, 422, "unknown_tool"),
    ]
    with world.acting_as(world.ctx(op)):
        for body, status, code in cases:
            r = _post_scan(client, world, app_key, **body)
            assert (r.status_code, r.json().get("error")) == (status, code), (body, r.text)
        r = _post_scan(client, world, "no-such-app")
        assert (r.status_code, r.json()["error"]) == (404, "app_not_found")
        r = _post_scan(client, world, app_key, zap={"target": f"preview:{mine}", "mode": "api"})
        assert r.status_code == 202, r.text
        assert r.json()["scan"]["target"]["zap"] == {"mode": "api", "kind": "preview", "preview_id": mine,
                                                    "url": "http://127.0.0.1:41234"}
        assert _post_scan(client, world, app_key, ref="abc1234").status_code == 202
        # a body field the API does not know is refused, not ignored (e.g. a smuggled url)
        assert _post_scan(client, world, app_key, url="http://x").status_code == 422
        scanners_installed.write_text("{}")
        r = _post_scan(client, world, app_key)
        assert (r.status_code, r.json()["error"]) == (409, "scanners_not_installed")
        assert "boss toolchains enable security" in r.json()["message"]
    with engine.begin() as c:
        assert c.execute(text("SELECT count(*) FROM security_scans WHERE project_id = :p AND app_key = :a"),
                         {"p": world.project_b, "a": app_key}).scalar() == 0


def test_a_scan_request_queues_one_security_scan_job_with_the_request_authority(world, engine):
    app_key = f"app-{uuid.uuid4().hex[:8]}"
    _app(engine, world.project_a, app_key)
    op = _person(world, "operator")
    with world.acting_as(world.ctx(op)):
        r = _post_scan(_client(), world, app_key)
    scan_id = r.json()["scan_id"]
    with engine.begin() as c:
        job = c.execute(text("SELECT kind, payload, status, on_behalf_of FROM jobs WHERE project_id = :p "
                             "AND payload->>'scan_id' = :s"), {"p": world.project_a, "s": scan_id}).mappings().all()
    assert len(job) == 1 and job[0]["kind"] == "security.scan" and job[0]["status"] == "queued"
    assert job[0]["on_behalf_of"] == op.id
    assert r.json()["scan"]["status"] == "queued" and r.json()["scan"]["triggered_by"] == op.id


# ---- the scan, the findings, triage, the report ------------------------------------------------

def test_scan_fix_rescan_report(world, engine):
    app_key = f"app-{uuid.uuid4().hex[:8]}"
    _app(engine, world.project_a, app_key)
    before = _run(world, engine, app_key, "before")
    after = _run(world, engine, app_key, "after")
    assert before["status"] == after["status"] == "done"
    assert before["counts"]["by_tool"]["semgrep"]["high"] == 1 and before["counts"]["failed_tools"] == []
    assert {t["name"] for t in before["tools"]} == {"semgrep", "osv", "zap"}
    assert next(t for t in before["tools"] if t["name"] == "semgrep")["rules_digest"] == TOOLS["semgrep_rules"]
    assert "inventory" not in before["target"]
    viewer = _person(world, "viewer")
    client = _client()
    with world.acting_as(world.ctx(viewer)):
        found = client.get(f"/v1/security/scans/{before['id']}/findings").json()["findings"]
        assert found[0]["severity"] == "high"                                  # most severe first
        assert "s4-proof-hardcoded-signing-key" not in json.dumps(found)      # redacted at rest
        only_osv = client.get(f"/v1/security/scans/{before['id']}/findings", params={"tool": "osv"}).json()
        assert {f["tool"] for f in only_osv["findings"]} == {"osv"} and len(only_osv["findings"]) == 3
        rep = client.get(f"/v1/security/reports/{app_key}", params={"project_id": world.project_a}).json()
        md = client.get(f"/v1/security/reports/{app_key}",
                        params={"project_id": world.project_a, "format": "md"})
    fixed = {(f["tool"], f["rule_id"]) for f in rep["fixed"]}
    assert {("semgrep", "javascript.express.security.injection.tainted-sql-string"),
            ("semgrep", "javascript.jsonwebtoken.security.hardcoded-jwt-secret"),
            ("osv", "GHSA-35jh-r3h4-6jhm"), ("zap", "10038-1"), ("zap", "10020-1"),
            ("zap", "10021"), ("zap", "10037")} <= fixed
    still = {(f["tool"], f["rule_id"]) for f in rep["still_open"]}
    assert ("semgrep", "javascript.express.security.audit.express-check-csurf-middleware-usage") in still
    assert ("zap", "10112") in still
    # same fingerprints in both scans, first seen in the first one
    assert all(f["first_seen_scan_id"] == before["id"] for f in rep["still_open"])
    assert rep["new"] == [] and rep["summary"]["new_high_or_critical"] == 0
    assert rep["moved"] == rep["removed"] == rep["not_rescanned"] == [] and rep["rules_differ"] is False
    assert (rep["before"]["id"], rep["after"]["id"]) == (before["id"], after["id"])
    assert md.status_code == 200 and md.headers["content-type"].startswith("text/markdown")
    assert "## Fixed" in md.text and "0 new" in md.text


def test_triage_needs_a_reason_records_an_event_and_carries_to_the_next_scan(world, engine):
    app_key = f"app-{uuid.uuid4().hex[:8]}"
    _app(engine, world.project_a, app_key)
    first = _run(world, engine, app_key, "after")
    csurf = next(f for f in service.list_findings(engine.connect(), first["id"]) if "csurf" in f["rule_id"])
    op = _person(world, "operator")
    client = _client()
    with world.acting_as(world.ctx(op)):
        assert client.post(f"/v1/security/findings/{csurf['id']}/triage", json={"status": "accepted"}).status_code == 422
        assert client.post(f"/v1/security/findings/{csurf['id']}/triage",
                           json={"status": "accepted", "reason": "  "}).status_code == 422
        assert client.post(f"/v1/security/findings/{csurf['id']}/triage",
                           json={"status": "fixed", "reason": "it is fine"}).status_code == 422
        assert client.delete(f"/v1/security/findings/{csurf['id']}").status_code in (404, 405)
        r = client.post(f"/v1/security/findings/{csurf['id']}/triage",
                        json={"status": "false_positive", "reason": "JSON API with bearer tokens; no cookies"})
    assert r.status_code == 200 and r.json()["status"] == "false_positive" and r.json()["triaged_by"] == op.id
    with engine.begin() as c:
        ev = c.execute(text("SELECT actor, data FROM events WHERE event_type = 'security_finding.triaged' "
                            "AND entity_id = :i"), {"i": csurf["id"]}).mappings().all()
    assert len(ev) == 1 and ev[0]["actor"] == op.id and ev[0]["data"]["to"] == "false_positive"
    second = _run(world, engine, app_key, "after")
    again = next(f for f in service.list_findings(engine.connect(), second["id"]) if "csurf" in f["rule_id"])
    assert again["fingerprint"] == csurf["fingerprint"]
    assert (again["status"], again["triage_reason"]) == ("false_positive", "JSON API with bearer tokens; no cookies")
    assert again["first_seen_scan_id"] == first["id"]


def test_a_failed_tool_is_not_rescanned_never_fixed(world, engine):
    app_key = f"app-{uuid.uuid4().hex[:8]}"
    _app(engine, world.project_a, app_key)
    _run(world, engine, app_key, "before")
    after = _run(world, engine, app_key, "after", failed=("zap",))
    assert after["counts"]["failed_tools"] == ["zap"]
    assert next(t for t in after["tools"] if t["name"] == "zap") == {
        **next(t for t in after["tools"] if t["name"] == "zap"), "status": "failed", "findings": 0}
    with engine.begin() as c:
        rep = __import__("boss_engine.security_scan.report", fromlist=["x"]).build_report(c, world.project_a, app_key)
    assert {f["tool"] for f in rep["not_rescanned"]} == {"zap"} and not any(f["tool"] == "zap" for f in rep["fixed"])
    every = _run(world, engine, app_key, "after", failed=("semgrep", "osv", "zap"))
    assert every["status"] == "failed"


def test_a_report_across_different_rule_digests_is_flagged(world, engine):
    app_key = f"app-{uuid.uuid4().hex[:8]}"
    _app(engine, world.project_a, app_key)
    a = _run(world, engine, app_key, "before")
    b = _run(world, engine, app_key, "after", rules="sha256:" + "b" * 64)
    op = _person(world, "operator")
    with world.acting_as(world.ctx(op)):
        rep = _client().get(f"/v1/security/reports/{app_key}",
                            params={"project_id": world.project_a, "before": a["id"], "after": b["id"]}).json()
        same = _client().get(f"/v1/security/reports/{app_key}",
                             params={"project_id": world.project_a, "before": a["id"], "after": a["id"]})
        other = _client().get("/v1/security/reports/not-this-app",
                              params={"project_id": world.project_a, "before": a["id"], "after": b["id"]})
    assert rep["rules_differ"] is True
    assert {"tool": "semgrep", "field": "rules_digest", "before": "sha256:" + "a" * 64,
            "after": "sha256:" + "b" * 64} in rep["differences"]
    assert same.status_code == 422 and other.status_code == 404
