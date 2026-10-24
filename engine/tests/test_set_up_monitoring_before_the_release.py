"""SET UP MONITORING before the release (BOSSAPP-601).

1. Placement: every catalogue workflow that deploys twice runs "Set up monitoring" (Beacon,
   the SRE) after pass 2 and right before its second deploy; the release names it.
2. The pack is the model's: SEV-rated PromQL rules from the code and the staging series, a
   runbook per rule, dashboard panels; BOSS checks it (PromQL, SEV, bounds, the manifest still
   parses), previews each rule over the last hour, and the model retunes a noisy one.
3. Written as code: boss.deploy.yaml `alerts:` / `dashboard:` and runbooks/<name>.md on the
   build's branch; the release's manifest makes them active and repo-managed.
4. Add a feature extends the pack and never duplicates a rule.
5. The gate: `block` (the default) refuses a release with no pack; `warn` lets it go and says so.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import yaml
from sqlalchemy import text

from ._obs_seed import pg_engine, project
from boss_engine.services import app_alerts as aa
from boss_engine.services import app_alerts_view as view
from boss_engine.services import monitoring as mon
from boss_engine.services import monitoring_pack as mp
from boss_engine.services import workflow_capability_runner as runner
from boss_engine.services import workflow_catalogue as wc
from boss_engine.services.app_deploy_manifest import parse_manifest

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc)
BRANCH_ROOT = "crun_mon_root"

MANIFEST = """version: 1
name: order-api
kind: compose
compose: {file: docker-compose.yml, project: order-api}
# how BOSS reads it
metrics: {path: /metrics}
health:
  - {url: "http://127.0.0.1:8080/health", expect: 200}
"""

CODE = {
    "src/routes/orders.ts": "router.post('/orders', async (req, res) => { const pay = await "
                            "payments.charge(req.body); ordersCreated.inc(); ... })",
    "src/payments.ts": "export async function charge(o) { return fetch(PAYMENTS_URL, ...); "
                       "paymentFailures.inc() }",
}


# --------------------------------------------------------------------------- #
# 1. Placement
# --------------------------------------------------------------------------- #
def _cap(step):
    return (step.get("binding") or {}).get("capability")


def test_every_workflow_that_deploys_twice_sets_up_monitoring_right_before_its_release():
    with_step = []
    for entry in wc.CATALOGUE:
        by_id = {s["id"]: s for s in entry["steps"]}
        releases = [s for s in entry["steps"] if _cap(s) == "deploy"
                    and (s["binding"]["inputs"] or {}).get("pass_2_of")]
        packs = [s for s in entry["steps"] if _cap(s) == mp.CAPABILITY]
        assert len(packs) == len(releases), entry["key"]
        for release in releases:
            pack_id = release["binding"]["inputs"]["monitoring_step"]
            pack = by_id[pack_id]
            assert pack["next"] == [release["id"]]
            assert pack["captain"] == "sre" and pack["default_captain"] == "sre"
            assert pack["title"].endswith("Set up monitoring")
            # after pass 2 (the join) and only then: it never races pass 2 on the branch
            assert [s["id"] for s in entry["steps"] if pack_id in (s.get("next") or [])] == [
                [s for s in entry["steps"] if s.get("kind") == "join"
                 and pack_id in (s.get("next") or [])][0]["id"]]
            assert pack["binding"]["inputs"]["run_id"] == release["binding"]["inputs"]["run_id"]
            assert pack["binding"]["inputs"]["first_deploy"] == \
                release["binding"]["inputs"]["pass_2_of"]
        if packs:
            with_step.append(entry["key"])
    assert "standard_product_delivery" in with_step and "minor_feature" in with_step
    assert "incident_response" not in with_step       # one deploy: nothing to set up before
    # idempotent: applying the rule again adds nothing
    entry = wc.CATALOGUE[0]
    again = mp.with_monitoring_step({"steps": entry["steps"]})
    assert again["steps"] == entry["steps"]


def test_the_step_is_the_sres_and_says_set_up_monitoring():
    from boss_engine.services.workflow_capabilities import CAPABILITIES

    cap = CAPABILITIES[mp.CAPABILITY]
    assert cap.label == "Set up monitoring" and cap.captain == "sre" and cap.needs_model
    assert "monitoring_step" in CAPABILITIES["deploy"].optional_inputs
    # the old after-deploy probe step no longer shares the name
    assert CAPABILITIES["monitor"].label != "Set up monitoring"


def test_monitoring_gate_defaults_to_block_and_warn_is_honoured():
    assert mp.gate_mode({}) == "block"
    assert mp.gate_mode({"monitoring_gate": "warn"}) == "warn"
    assert mp.gate_mode({"monitoring_gate": "nonsense"}) == "block"
    entry = {**wc.CATALOGUE[0], "monitoring_gate": "warn"}
    assert wc.entry_spec(entry)["monitoring_gate"] == "warn"


# --------------------------------------------------------------------------- #
# Fakes: a repository and a model
# --------------------------------------------------------------------------- #
class FakeRepo:
    def __init__(self, files: dict[str, str]):
        self.files = dict(files)
        self.commits: list[tuple[str, str, str]] = []

    def read_text_file(self, branch, path):
        return self.files.get(path)

    def upsert_file(self, branch, path, content, message):
        self.files[path] = content
        self.commits.append((branch, path, message))
        return f"{len(self.commits):040x}"

    def pull_request_files(self, number):
        return [{"path": p, "status": "added", "additions": len(t.splitlines()) + 10}
                for p, t in self.files.items() if p.startswith("src/")]


class Model:
    """Answers each call with the next pack; records what it was asked."""

    def __init__(self, *packs):
        self.packs = list(packs)
        self.prompts: list[str] = []

    def create_text_response(self, prompt, system_prompt=None):
        self.prompts.append(prompt)
        return None, "Here it is:\n" + json.dumps(self.packs.pop(0)), {"model": "stub-model"}


def _pack(error_threshold: float, *, rules=None) -> dict:
    return {
        "summary": "Watches order creation and the payments upstream on the Order API.",
        "builtins": {
            "error_rate": {"threshold": error_threshold, "window_minutes": 5,
                           "min_requests": 20, "severity": "sev2", "runbook": "orders-failing",
                           "why": "POST /orders is the money path"},
            "latency_p95": {"threshold": 900, "window_minutes": 5, "min_requests": 20,
                            "severity": "sev3", "runbook": "orders-slow"},
            "health_check": {"min_failures": 3, "window_minutes": 10, "severity": "sev1",
                             "runbook": "order-api-down"},
            "no_traffic": {"enabled": True, "window_minutes": 30, "expected_hours": 2,
                           "severity": "sev3", "runbook": "orders-stopped"},
        },
        "rules": rules if rules is not None else [
            {"alert": "OrderPaymentFailures",
             "expr": "sum(rate(payment_failures_total{SELECTOR}[5m])) > 0.2", "for": "5m",
             "severity": "sev2", "threshold": "> 0.2 failed charges/s for 5 min",
             "summary": "Payments are failing", "runbook": "payments-failing",
             "why": "src/payments.ts calls the payments upstream"},
        ],
        "runbooks": [
            {"name": "orders-failing", "title": "Orders are failing",
             "markdown": "# Orders are failing\n\n1. Open POST /orders in src/routes/orders.ts"},
            {"name": "orders-slow", "title": "Orders are slow",
             "markdown": "# Orders are slow\n\n1. Check the payments upstream latency"},
            {"name": "order-api-down", "title": "The Order API is down",
             "markdown": "# The Order API is down\n\n1. Check the container"},
            {"name": "orders-stopped", "title": "No orders are arriving",
             "markdown": "# No orders are arriving\n\n1. Check the load balancer"},
            {"name": "payments-failing", "title": "Payments are failing",
             "markdown": "# Payments are failing\n\n1. src/payments.ts: is PAYMENTS_URL up?"},
        ],
        "panels": [{"title": "Orders created / s",
                    "expr": "sum(rate(orders_created_total{SELECTOR}[5m]))", "unit": "short"}],
    }


# --------------------------------------------------------------------------- #
# 2-4. The pack, against a real database
# --------------------------------------------------------------------------- #
@pytest.fixture()
def pg():
    eng = pg_engine()
    with eng.begin() as c:
        pid = project(c)
    return eng, pid


def _seed(c, pid) -> tuple[dict, str]:
    tid = f"t_mp_{uuid4().hex[:10]}"
    c.execute(text(
        "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, "
        " target_ref, status, created_by, domains) VALUES (:t, :p, 'Order API', 'acme/shop', "
        " 'staging', 'stack', 'order-api', 'active', 'test', '[]'::jsonb)"), {"t": tid, "p": pid})
    mon.ensure_target_services(c, pid)
    # an hour of healthy traffic: 100 requests a minute, 1 of them a 5xx (1%)
    for i in range(62):
        at = NOW - timedelta(minutes=61 - i)
        snap = {"requests": 100.0 * i, "errors": 1.0 * i, "routes": {
            "POST /orders": {"requests": 60.0 * i, "errors": 1.0 * i},
            "GET /orders": {"requests": 40.0 * i, "errors": 0.0}}, "buckets": {},
            "hist_count": None, "hist_sum": None}
        c.execute(text(
            "INSERT INTO app_metrics_scrapes (id, project_id, deployment_target_id, scraped_at, "
            " ok, word, snapshot) VALUES (:i, :p, :t, :at, true, 'Scraped', CAST(:s AS jsonb))"),
            {"i": f"ams_{uuid4().hex}", "p": pid, "t": tid, "at": at, "s": json.dumps(snap)})
        c.execute(text(
            "INSERT INTO deployment_health_samples (id, project_id, deployment_target_id, "
            " checked_at, ok, status_code, latency_ms) VALUES (:i, :p, :t, :at, true, 200, 30)"),
            {"i": f"dhs_{uuid4().hex}", "p": pid, "t": tid, "at": at})
    crun = f"crun_mp_{uuid4().hex[:8]}"
    c.execute(text(
        "INSERT INTO code_task_runs (id, project_id, title, spec, state, finalize_ctx, "
        " pull_request_number, status) VALUES (:i, :p, 'Order API', CAST(:s AS jsonb), "
        " CAST(:st AS jsonb), CAST(:f AS jsonb), 41, 'completed')"),
        {"i": crun, "p": pid, "s": json.dumps({"owner": "acme", "repo": "shop"}),
         "st": json.dumps({"impl_files": ["boss.deploy.yaml", *CODE]}),
         "f": json.dumps({"root_run_id": BRANCH_ROOT})})
    return dict(c.execute(text("SELECT * FROM deployment_targets WHERE id = :t"),
                          {"t": tid}).mappings().one()), crun


def _run(c, pid, crun, tid, repo, model, monkeypatch):
    monkeypatch.setattr(mp, "_step_output", lambda _c, _r, s: (
        {"status": "succeeded", "stack_target_id": tid} if s == "deploy" else {}))
    return mp.run_step(c, None, pid, run_id="wfr_mp", step_id="monitoring",
                       inputs={"run_id": crun, "first_deploy": "deploy"}, adapter=model,
                       github=repo, now=NOW)


def test_beacon_writes_a_previewed_tuned_pack_as_code_and_the_release_makes_it_live(
        pg, monkeypatch):
    eng, pid = pg
    repo = FakeRepo({"boss.deploy.yaml": MANIFEST, **CODE})
    # First answer: a 0.5% error threshold, which the healthy 1% would trip — noise. Second:
    # retuned to 3%.
    model = Model(_pack(0.5), _pack(3))
    with eng.begin() as c:
        target, crun = _seed(c, pid)
        out = _run(c, pid, crun, target["id"], repo, model, monkeypatch)

    # The model read the code, the series it emitted and the routes it served.
    first = model.prompts[0]
    assert "src/payments.ts" in first and "PAYMENTS_URL" in first
    assert "POST /orders" in first
    assert "PREVIEW over the last hour" in model.prompts[1]          # it was shown the noise

    card = out["pack"]
    assert out["status"] == "succeeded" and card["gate"] == "block"
    by = {r["alert"]: r for r in card["rules"]}
    assert set(by) >= {"AppErrorRate", "AppLatencyP95", "AppHealthCheckFailing", "AppNoTraffic",
                       "OrderPaymentFailures"}
    assert by["AppErrorRate"]["severity"] == "sev2" and by["AppHealthCheckFailing"][
        "severity"] == "sev1"
    assert by["AppErrorRate"]["threshold"].startswith("> 3%")
    assert by["AppErrorRate"]["preview"]["fired"] == 0
    assert "last hour" in by["AppErrorRate"]["preview"]["word"]
    assert card["tuned"] and card["tuned"][0]["alert"] == "AppErrorRate"
    assert card["tuned"][0]["fired_before"] > 0
    pay = by["OrderPaymentFailures"]
    assert 'app="Order API",environment="staging"' in pay["expr"]
    # Keyed by the rule's own stable identity (its alert name), not by the model's free-text
    # runbook title — which it words differently call to call (BOSSAPP-603: 18 runbook files
    # for ~15 rules, before this) — so a retry overwrites the same file.
    assert pay["runbook"] == "runbooks/orderpaymentfailures.md"
    assert pay["runbook_title"] == "Payments are failing"
    assert card["rules_href"].endswith(f"app={target['id']}&atab=alerts")
    assert card["by"]["role_key"] == "sre"

    # Written as code, on the build's branch: the manifest and a runbook per rule.
    assert {b for b, _p, _m in repo.commits} == {f"boss/code/{BRANCH_ROOT}"}
    written = repo.files["boss.deploy.yaml"]
    assert written.startswith(MANIFEST.split("# how")[0])          # the rest is untouched
    assert "# how BOSS reads it" in written
    doc = yaml.safe_load(written)
    assert doc["alerts"]["error_rate"]["threshold"] == 3
    assert doc["alerts"]["rules"][0]["alert"] == "OrderPaymentFailures"
    assert doc["dashboard"]["panels"][0]["title"] == "Orders created / s"
    assert "PAYMENTS_URL" in repo.files["runbooks/orderpaymentfailures.md"]
    manifest = parse_manifest(doc, source_text=written)

    # The release ships that commit: its manifest makes the rules active and repo-managed.
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO stack_deployments (id, project_id, target_id, repository, ref, "
            " commit_sha, manifest, status, created_by, created_at, finished_at) VALUES "
            " (:i, :p, :t, 'acme/shop', 'pull/41', :c, CAST(:m AS jsonb), 'healthy', 'usr_op', "
            " :at, :at)"),
            {"i": f"sd_{uuid4().hex[:10]}", "p": pid, "t": target["id"], "c": "a" * 40,
             "m": json.dumps(manifest.summary()), "at": NOW})
        rules = {r["alert"]: r for r in aa.ensure_rules(c, aa.subject(c, target["id"]))}
        assert rules["OrderPaymentFailures"]["managed_by"] == "code"
        assert rules["AppErrorRate"]["managed_by"] == "code"
        assert rules["AppErrorRate"]["params"]["threshold"] == 3
        assert rules["OrderPaymentFailures"]["labels"]["severity"] == "sev2"
        wire = view._rule_wire(c, rules["OrderPaymentFailures"], target, {})
        assert wire["managed_by"] == "code" and wire["source_word"] == "from boss.deploy.yaml"
        assert wire["runbook"]["kind"] == "repo"


def test_add_a_feature_extends_the_pack_and_never_duplicates(pg, monkeypatch):
    eng, pid = pg
    repo = FakeRepo({"boss.deploy.yaml": MANIFEST, **CODE})
    with eng.begin() as c:
        target, crun = _seed(c, pid)
        _run(c, pid, crun, target["id"], repo, Model(_pack(3)), monkeypatch)
        before = yaml.safe_load(repo.files["boss.deploy.yaml"])["alerts"]["rules"]
        # The feature adds refunds. The model repeats the payments rule (same name) and a copy
        # of it under a new name (same query), and adds one rule for the new endpoint.
        same = dict(_pack(3)["rules"][0])
        copy = {**same, "alert": "PaymentsBroken"}
        refunds = {"alert": "RefundFailures", "severity": "sev3", "for": "10m",
                   "expr": "sum(rate(refund_failures_total{SELECTOR}[10m])) > 0.1",
                   "summary": "Refunds are failing", "runbook": "refunds-failing"}
        feature = {**_pack(3, rules=[same, copy, refunds]), "builtins": {},
                   "runbooks": [{"name": "refunds-failing", "title": "Refunds are failing",
                                 "markdown": "# Refunds are failing\n\n1. POST /refunds"}],
                   "panels": []}
        model = Model(feature)
        out = _run(c, pid, crun, target["id"], repo, model, monkeypatch)
    assert "ADDS A FEATURE" in model.prompts[0] and "OrderPaymentFailures" in model.prompts[0]
    after = yaml.safe_load(repo.files["boss.deploy.yaml"])["alerts"]["rules"]
    assert [r["alert"] for r in after] == [r["alert"] for r in before] + ["RefundFailures"]
    card = out["pack"]
    assert card["adding"] is True
    assert sorted(r["alert"] for r in card["rules"] if r["new"] and r["kind"] == "custom") == [
        "RefundFailures"]
    assert any("already in the pack" in why for why in card["refused"])
    # the built-ins keep the values the first pack set
    assert yaml.safe_load(repo.files["boss.deploy.yaml"])["alerts"]["error_rate"]["threshold"] == 3


def test_a_retry_keeps_the_same_runbook_file_even_when_the_model_rewords_it():
    """BOSSAPP-603: the runbook's path is keyed on the rule's own stable identity (its alert
    name, or its built-in kind) — never on the model's free-text summary or title, which it
    words differently call to call. Before this, a retry's reworded pack wrote a SECOND
    runbook file for the same rule instead of overwriting the first (18 runbooks for ~15
    rules)."""
    from boss_engine.services import monitoring_pack as mp

    rule = {"alert": "OrderPaymentFailures",
           "expr": "sum(rate(payment_failures_total{SELECTOR}[5m])) > 0.2", "for": "5m",
           "severity": "sev2", "description": "src/payments.ts: is PAYMENTS_URL up?"}
    first = mp.validate({"rules": [{**rule, "summary": "Payments are failing"}]},
                        app="Order API", env="staging")
    retried = mp.validate({"rules": [{**rule, "summary": "Payment processing is broken now"}]},
                          app="Order API", env="staging")
    path1 = first["rules"][0]["annotations"]["runbook_url"]
    path2 = retried["rules"][0]["annotations"]["runbook_url"]
    assert path1 == path2 == "runbooks/orderpaymentfailures.md"
    assert set(first["runbooks"]) == set(retried["runbooks"]) == {"orderpaymentfailures"}


def test_a_pack_that_breaks_the_apps_own_tests_is_repaired_or_refused_with_the_output(
        pg, monkeypatch):
    """BOSSAPP-603: a pack that breaks the app's own test suite (it broke test_deploy_manifest
    by adding `alerts:`) is fixed in the same step when the model's repair fixes it, or the
    step fails carrying the test output — never ships silently as 'succeeded'."""
    class CIRepo(FakeRepo):
        def __init__(self, files, verdicts):
            super().__init__(files)
            self._verdicts = list(verdicts)
            self.checked = 0

        def commit_checks(self, commit_sha):
            self.checked += 1
            conclusion = self._verdicts.pop(0) if self._verdicts else "success"
            return [{"name": "test_deploy_manifest", "status": "completed",
                    "conclusion": conclusion}]

    eng, pid = pg
    # The repair's pack passes CI.
    repo = CIRepo({"boss.deploy.yaml": MANIFEST, **CODE}, verdicts=["failure", "success"])
    with eng.begin() as c:
        target, crun = _seed(c, pid)
        out = _run(c, pid, crun, target["id"], repo, Model(_pack(3), _pack(3)), monkeypatch)
    assert out["status"] == "succeeded" and repo.checked == 2

    # The repair's pack ALSO fails CI: the step fails, carrying the test output.
    repo2 = CIRepo({"boss.deploy.yaml": MANIFEST, **CODE}, verdicts=["failure", "failure"])
    with eng.begin() as c:
        pid2 = project(c)
        target2, crun2 = _seed(c, pid2)
        with pytest.raises(mp.PackRefused, match="test_deploy_manifest"):
            _run(c, pid2, crun2, target2["id"], repo2, Model(_pack(3), _pack(3)), monkeypatch)
    assert repo2.checked == 2


def test_a_pack_that_would_break_the_manifest_or_has_no_model_is_refused_in_words(
        pg, monkeypatch):
    eng, pid = pg
    repo = FakeRepo({"boss.deploy.yaml": MANIFEST, **CODE})
    bad = _pack(3, rules=[{"alert": "Broken", "expr": "sum(rate(x{SELECTOR}[5m]) >", "severity":
                           "sev2"}])
    with eng.begin() as c:
        target, crun = _seed(c, pid)
        out = _run(c, pid, crun, target["id"], repo, Model(bad), monkeypatch)
        assert any(why.startswith("Broken:") for why in out["pack"]["refused"])
        assert "Broken" not in repo.files["boss.deploy.yaml"]
        # nothing BOSS could accept at all: no pack, said in words
        nothing = {**bad, "builtins": {}}
        with pytest.raises(mp.PackRefused, match="no rule BOSS could accept"):
            _run(c, pid, crun, target["id"], FakeRepo({"boss.deploy.yaml": MANIFEST, **CODE}),
                 Model(nothing), monkeypatch)

        class NoModel:
            def create_text_response(self, *_a, **_k):
                return None, "I would rather not.", {}

        with pytest.raises(mp.PackRefused, match="JSON"):
            _run(c, pid, crun, target["id"], repo, NoModel(), monkeypatch)


# --------------------------------------------------------------------------- #
# 5. The gate, and the release card
# --------------------------------------------------------------------------- #
RELEASE = {"id": "deploy_again", "binding": {"capability": "deploy", "inputs": {
    "pass_2_of": "deploy", "monitoring_step": "monitoring"}}}


def test_block_refuses_a_release_without_a_pack_and_warn_lets_it_go(monkeypatch):
    rows = {"monitoring": {"status": "failed", "note": "no model is connected"}}
    monkeypatch.setattr(mp, "_step_output", lambda _c, _r, s: rows.get(s, {}))
    refused = mp.release_refusal(None, "wfr_1", RELEASE, "block")
    assert refused and "monitoring_gate: block" in refused and "no model" in refused
    assert mp.release_refusal(None, "wfr_1", RELEASE, "warn") is None
    card = mp.card_for(None, "wfr_1", RELEASE)
    assert card["missing"] is True and "no model" in card["why"]
    rows["monitoring"] = {"status": "succeeded", "pack": {"rules": [{"alert": "AppErrorRate"}],
                                                          "files_written": ["boss.deploy.yaml"]}}
    assert mp.release_refusal(None, "wfr_1", RELEASE, "block") is None
    assert mp.card_for(None, "wfr_1", RELEASE)["rules"][0]["alert"] == "AppErrorRate"
    assert mp.landed(None, "wfr_1", RELEASE) is True
    # a deploy that follows no Set up monitoring step is untouched
    assert mp.release_refusal(None, "wfr_1", {"binding": {"inputs": {}}}, "block") is None


def test_the_step_fails_under_block_and_goes_on_under_warn(monkeypatch):
    def refuse(*_a, **_k):
        raise mp.PackRefused("no model is connected")

    monkeypatch.setattr(mp, "run_step", refuse)
    ctx = type("Ctx", (), {"settings": object()})()
    monkeypatch.setattr(mp, "run_gate", lambda _c, _r: "block")
    with pytest.raises(runner.SettledCapabilityError, match="cannot ship without one"):
        runner._set_up_monitoring(None, ctx, "p", {"run_id": "wfr_1"}, {"id": "monitoring"}, {})
    monkeypatch.setattr(mp, "run_gate", lambda _c, _r: "warn")
    out = runner._set_up_monitoring(None, ctx, "p", {"run_id": "wfr_1"}, {"id": "monitoring"},
                                    {})
    assert out["status"] == "not_applicable" and "goes on without one" in out["note"]


def test_a_written_pack_is_a_succeeded_step_and_no_pack_is_not():
    """The step's own result is judged by workflow_outcomes (a missing verdict was a KeyError
    on every real run: Order API, 2026-10-19, wfr_723213d8 deadlettered at Set up monitoring)."""
    from boss_engine.services import workflow_outcomes as wo

    ok = {"capability": "monitoring_pack", "status": "succeeded", "commit_sha": "abc123",
          "pack": {"rules": [{"alert": "OrdersPostFailing"}]}}
    assert wo.VERDICTS["monitoring_pack"](ok, {}).outcome == wo.SUCCEEDED
    warn = {"capability": "monitoring_pack", "status": "not_applicable", "note": "No pack: warn"}
    assert wo.VERDICTS["monitoring_pack"](warn, {}).outcome == wo.SUCCEEDED
    empty = {"capability": "monitoring_pack", "status": "succeeded", "pack": {"rules": []}}
    assert wo.VERDICTS["monitoring_pack"](empty, {}).outcome == wo.FAILED
    # A pack already on the branch (a retry, or a feature whose pack covers it) wrote nothing.
    kept = {"capability": "monitoring_pack", "status": "succeeded", "commit_sha": None,
            "pack": {"rules": [{"alert": "OrdersPostFailing", "new": False}], "adding": True}}
    assert wo.VERDICTS["monitoring_pack"](kept, {}).outcome == wo.SUCCEEDED


def test_set_up_monitoring_holds_its_lease_while_it_thinks():
    """Its completion was refused 'lease has expired' three times: it thinks for minutes inside
    the call and held the two-minute lease every step holds."""
    from boss_engine.services import workflow_capability_runner as runner

    step = {"binding": {"capability": "monitoring_pack"}}
    assert runner.lease_for(step) >= 3 * 60 * 3
    assert runner.lease_for(step) <= runner.MAX_INLINE_LEASE_SECONDS
    # a tool call, or work that starts and parks, keeps the ordinary lease
    assert runner.lease_for({"binding": {"capability": "deploy"}}) == runner.LEASE_SECONDS
    assert runner.lease_for({"binding": {"capability": "build"}}) == runner.LEASE_SECONDS
    assert runner.lease_for({}) == runner.LEASE_SECONDS
