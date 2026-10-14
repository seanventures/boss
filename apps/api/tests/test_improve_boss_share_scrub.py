"""Share this run: precision AND recall, measured on the independent tester's own corpus
(scratchpad/tmfix3-test/item4.py, 2026-10-05). Every known install name masked — zero leaks —
and at most 2 of the 28 harmless tokens touched. Before this, 6 leaked and 21 of 28 were masked."""
from __future__ import annotations

import json

from sqlalchemy import text

from apps.api.tests.improve_fixtures import project, reset_state, seed_build, seed_workflow_run

LEAKS = ["db-prod-07", "acmewidgets", "build-07", "intranet", "buildbox07", "kestrelbox", "fe80",
         "2001:db8", "8a2e", "0db8", "ledgerly", "acme-corp", "release-larkspur", "fix-ledger",
         "PayrollPilot", "payroll-pilot", "payroll_pilot", "Nightjar", "NIGHTJAR",
         "Larkspur Holdings", "QuokkaPay", "zephyrledger", "feat-quokka", "dogfood", "10.20.30.40",
         "admin@acmewidgets.io"]
BENIGN = ["self.settings.timeout", "os.path.join", "Response.json", "README.md", "TypeError",
          "pytest -q", "version 1.2.3", "e.g.", "release", "manager", "console", "system", "BOSS",
          "agents", "operator", "runtime", "Chief", "executive", "ELIFECYCLE", "node_modules",
          "localhost:3000", "async/await", "package.json", "useEffect", "Array.prototype.map",
          "user.name", "config.yaml", "api.v1"]
TEXT = ("connect to db-prod-07.internal.acmewidgets.io:5432 failed; also build-07.intranet; ssh buildbox07: Connection refused; "
        "deploy to kestrelbox failed; peer fe80::1ff:fe23:4567:890a and 2001:db8:85a3::8a2e:370:7334 and [2001:db8::1]:8443 "
        "and 2001:0db8:0000:0000:0000:ff00:0042:8329 and ::1; repo acme-corp/ledgerly (ledgerly, acme-corp, ledgerly_core); "
        "branch release-larkspur, boss/fix-ledger; app PayrollPilot aka payroll-pilot / payroll_pilot; codename Nightjar "
        "NIGHTJAR nightjar-sso for Larkspur Holdings; target app QuokkaPay; project dogfood; ip 10.20.30.40; mail admin@acmewidgets.io.\n"
        "BENIGN: self.settings.timeout os.path.join Response.json README.md TypeError pytest -q version 1.2.3 e.g. "
        "the release manager console system BOSS agents operator runtime Chief executive npm ERR! code ELIFECYCLE node_modules "
        "localhost:3000 async/await package.json useEffect Array.prototype.map user.name config.yaml api.v1")
LOG = """FAILED tests/test_invoice.py::test_total - AssertionError: assert 41 == 42
  File "/workspace/app/invoice.py", line 12, in total
    return sum(item.price for item in self.items)
Error: Cannot find module 'react-dom/client' imported from src/main.tsx
TypeError: response.data.map is not a function at fetchItems (api.ts:22)
Executive summary: the operator should check settings.json and requests.Session config."""


def _seed(engine, client):
    client.put("/v1/telemetry/sharing", json={"project_id": project(), "actor": "ceo",
                                              "mask_words": "Nightjar, Larkspur Holdings"})
    with engine.begin() as c:
        c.execute(text("UPDATE projects SET slug = 'dogfood' WHERE id = :p"), {"p": project()})
        rid = seed_build(c, status="failed", title="Ledger export",
                         spec={"role_key": "software_engineer", "repo": "acme-corp/ledgerly",
                               "base_branch": "release-larkspur"},
                         finalize={"app_name": "PayrollPilot", "branch": "boss/fix-ledger"})
        c.execute(text("UPDATE code_task_runs SET error = :e WHERE id = :i"), {"e": TEXT, "i": rid})
        c.execute(text("""INSERT INTO deployment_hosts (id, project_id, name, address, ssh_user, created_by)
                          VALUES ('dh_mask3', :p, 'kestrelbox', '10.20.30.40', 'boss', 'ceo')
                          ON CONFLICT DO NOTHING"""), {"p": project()})
        c.execute(text("""INSERT INTO deployment_targets (id, project_id, app_name, repo, environment,
                                                          target_kind, target_ref, created_by)
                          VALUES ('dt_mask3', :p, 'QuokkaPay', 'acme-corp/zephyrledger', 'prod',
                                  'ssh_host', 'dh_mask3', 'ceo') ON CONFLICT DO NOTHING"""),
                  {"p": project()})
        wf, steps = seed_workflow_run(c, status="failed")
        c.execute(text("UPDATE workflow_runs SET input = CAST(:i AS JSONB), error = :e WHERE id = :r"),
                  {"r": wf, "i": json.dumps({"repo": "acme-corp/zephyrledger", "branch": "feat-quokka",
                                             "app_name": "QuokkaPay"}),
                   "e": "push to zephyrledger branch feat-quokka for QuokkaPay failed"})
        c.execute(text("UPDATE workflow_step_runs SET last_error = :e, status = 'failed' WHERE id = :s"),
                  {"s": steps[0], "e": "clone acme-corp/zephyrledger: zephyrledger not found on kestrelbox"})
    return rid, wf


def _cleanup(engine):
    with engine.begin() as c:
        c.execute(text("DELETE FROM deployment_targets WHERE id = 'dt_mask3'"))
        c.execute(text("DELETE FROM deployment_hosts WHERE id = 'dh_mask3'"))
        c.execute(text("UPDATE projects SET slug = id WHERE id = :p AND slug = 'dogfood'"), {"p": project()})


def test_the_tester_corpus_leaks_nothing_and_keeps_the_harmless(engine, client):
    from boss_engine.services import improve_boss

    reset_state(engine)
    rid, wf = _seed(engine, client)
    try:
        with engine.begin() as c:
            build = improve_boss.share_bundle(c, project(), "build", rid)["bundle"]
            run = improve_boss.share_bundle(c, project(), "workflow_run", wf)["bundle"]
    finally:
        _cleanup(engine)
    for bundle in (build, run):
        leaked = [w for w in LEAKS if w.lower() in bundle.lower()]
        assert leaked == [], leaked
    benign = build.split("BENIGN:", 1)[1]
    lost = [w for w in BENIGN if w.lower() not in benign.lower()]
    print("benign masked:", len(lost), "of", len(BENIGN), lost)
    assert len(lost) <= 2, lost
    assert "manager" not in [w.lower() for w in lost]  # a role-word display name masks nothing


def test_code_and_log_lines_are_left_readable(engine, client):
    from boss_engine.services.share_scrub import install_names, scrub

    reset_state(engine)
    _seed(engine, client)
    try:
        with engine.begin() as c:
            out, _ = scrub(LOG, install_names(c, project()))
    finally:
        _cleanup(engine)
    for keep in ("self.items", "response.data.map", "react-dom/client", "settings.json",
                 "requests.Session", "Executive summary", "the operator", "api.ts:22"):
        assert keep in out, (keep, out)
    for gone in ("/workspace/app/invoice.py", "tests/test_invoice.py", "src/main.tsx"):
        assert gone not in out, gone


def test_variants_and_name_parts():
    from boss_engine.services.share_scrub import STOP_WORDS, variants

    assert {"payroll-pilot", "payroll_pilot", "payrollPilot", "PayrollPilot", "payrollpilot"} <= variants("PayrollPilot")
    assert {"executive", "console", "operator", "manager", "release", "boss", "system"} <= STOP_WORDS
