"""Tune a rule (P1-C): a noisy rule's threshold is changed where the rule lives.

  * A rule the repo owns (boss.deploy.yaml `alerts:`) is PROPOSED as a pull request that changes
    only that rule's lines — every other rule, section and comment of the file intact — and the
    rule's row is not overwritten (the merged change arrives with the next deploy).
  * The Rules list says where such a rule lives: the manifest's path and its line.
  * A console rule is saved directly, and the change is audited (before, after, who, why).
  * Before saving, the 24 h preview runs the NEW threshold over the app's own series.
"""
from __future__ import annotations

import difflib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import yaml
from sqlalchemy import text

from ._obs_seed import pg_engine, project
from boss_engine.services import app_alerts as aa
from boss_engine.services import app_alerts_view as view
from boss_engine.services import rule_tuning as rt

T0 = datetime(2026, 10, 18, 7, 0, tzinfo=timezone.utc)

MANIFEST = """version: 1
name: order-api
kind: compose
compose: {file: docker-compose.yml, project: order-api}
# how BOSS reads it
metrics: {path: /metrics}
alerts:
  # Monitoring pack: hand-tuned
  error_rate:
    threshold: 5   # percent of requests
    window_minutes: 5
  latency_p95:
    threshold: 500
    severity: sev3
  rules:
    - alert: CheckoutSlow
      expr: sum(rate(checkout_failed_total[5m])) > 1
      for: 1m
      labels:
        severity: sev3
    - alert: PaymentsDown
      expr: sum(rate(payments_failed_total[5m])) > 0.5
health:
  - {url: "http://127.0.0.1:8080/health", expect: 200}
dashboard:
  panels:
    - {title: Requests, expr: "sum(rate(http_requests_total[5m]))"}
"""


@pytest.fixture()
def pg():
    eng = pg_engine()
    with eng.begin() as c:
        pid = project(c)
    return eng, pid


def _app(c, pid: str) -> dict:
    tid = f"t_tune_{uuid4().hex[:10]}"
    c.execute(text(
        "INSERT INTO deployment_targets (id, project_id, app_name, repo, environment, target_kind, "
        " target_ref, status, created_by, domains, public_url) VALUES (:t, :p, 'Order API', "
        " 'acme/shop', 'staging', 'stack', 'order-api', 'active', 'test', '[]'::jsonb, "
        " 'http://203.0.113.10:8000/')"), {"t": tid, "p": pid})
    manifest = {**yaml.safe_load(MANIFEST), "manifest_path": "deploy/boss.deploy.yaml"}
    c.execute(text(
        "INSERT INTO stack_deployments (id, project_id, target_id, repository, ref, commit_sha, "
        " manifest, manifest_text, status, created_by, created_at, finished_at) VALUES (:i, :p, "
        " :t, 'acme/shop', 'main', :c, CAST(:m AS jsonb), :mt, 'healthy', 'usr_op', :at, :at)"),
        {"i": f"sd_{uuid4().hex[:10]}", "p": pid, "t": tid, "c": "6bf2673" + "0" * 33,
         "m": json.dumps(manifest), "mt": MANIFEST, "at": T0 - timedelta(hours=3)})
    t = dict(c.execute(text("SELECT * FROM deployment_targets WHERE id = :t"),
                       {"t": tid}).mappings().one())
    aa.ensure_rules(c, t)
    return t


def _rules(c, t) -> dict[str, dict]:
    return {(r["alert"] if r["kind"] == "custom" else r["kind"]): r for r in aa.rules(c, t["id"])}


class _PR:
    def __init__(self, url: str, number: int):
        self.url, self.number = url, number


class _FakeGitHub:
    """The repo as GitHub would hold it: one file on main; records what BOSS writes."""

    def __init__(self, files: dict[str, str]):
        self.files = {("main", p): v for p, v in files.items()}
        self.branches: list[tuple[str, str]] = []
        self.prs: list[dict] = []

    def default_branch_sha(self):
        return "main", "0" * 40

    def read_text_file(self, branch, path):
        return self.files.get((branch, path))

    def ensure_branch(self, branch, base=None):
        self.branches.append((branch, base))
        for (b, p), v in list(self.files.items()):
            if b == base:
                self.files[(branch, p)] = v
        return "1" * 40

    def upsert_file(self, branch, path, content, message):
        self.files[(branch, path)] = content
        return "2" * 40

    def create_pull_request(self, branch, title, body, draft=None, base=None):
        self.prs.append({"branch": branch, "title": title, "body": body, "base": base})
        return _PR("https://github.example/acme/shop/pull/7", 7)


def _changed_lines(before: str, after: str) -> set[int]:
    """1-based line numbers of `before` that the change replaced, or after which it inserted."""
    out: set[int] = set()
    sm = difflib.SequenceMatcher(a=before.splitlines(), b=after.splitlines(), autojunk=False)
    for op, i1, i2, _j1, _j2 in sm.get_opcodes():
        if op == "equal":
            continue
        out.update(range(i1 + 1, i2 + 1) if i2 > i1 else {i1})
    return out


# ── a repo rule: a pull request touching only its lines; its row is not overwritten ──────────
def test_a_repo_rule_is_proposed_as_a_pull_request_that_changes_only_its_lines(pg):
    eng, pid = pg
    gh = _FakeGitHub({"deploy/boss.deploy.yaml": MANIFEST})
    with eng.begin() as c:
        t = _app(c, pid)
        lat = _rules(c, t)["latency_p95"]
        assert rt.via(lat) == "pull_request"
        got = rt.propose(c, None, t, lat["id"], {"params": {"threshold": 1200,
                                                           "window_minutes": 10}},
                         actor="usr_param", reason="p95 is noisy at night", github=gh,
                         now=T0)
        after_row = _rules(c, t)["latency_p95"]
        audit = c.execute(text(
            "SELECT change, after, reason, changed_by FROM monitoring_changes "
            "WHERE rule_id = :r ORDER BY changed_at"), {"r": lat["id"]}).mappings().all()

    # the pull request: on its own branch from the serving ref, against that ref
    assert gh.branches == [(got["branch"], "main")] and got["branch"].startswith("boss/tune-")
    assert gh.prs[0]["base"] == "main" and got["pull_request"]["number"] == 7
    assert "1200" in gh.prs[0]["title"] and "p95 is noisy at night" in gh.prs[0]["body"]
    assert got["path"] == "deploy/boss.deploy.yaml" and got["line"] == 12

    new = gh.files[(got["branch"], "deploy/boss.deploy.yaml")]
    # only the latency rule's lines (12-14, and the line added under it) changed
    assert _changed_lines(MANIFEST, new) <= set(range(12, 15)), (MANIFEST, new)
    assert "    threshold: 1200\n    severity: sev3\n    window_minutes: 10\n" in new
    old_doc, new_doc = yaml.safe_load(MANIFEST), yaml.safe_load(new)
    assert new_doc["alerts"]["latency_p95"] == {"threshold": 1200, "severity": "sev3",
                                               "window_minutes": 10}
    for key in ("version", "name", "kind", "compose", "metrics", "health", "dashboard"):
        assert new_doc[key] == old_doc[key]
    assert new_doc["alerts"]["error_rate"] == old_doc["alerts"]["error_rate"]
    assert new_doc["alerts"]["rules"] == old_doc["alerts"]["rules"]
    assert "# Monitoring pack: hand-tuned" in new and "# percent of requests" in new

    # the rule's row is NOT overwritten: the merged change arrives with the next deploy
    assert after_row["params"] == lat["params"] and after_row["expr"] == lat["expr"]
    assert after_row["source"] == "yaml" and after_row.get("changed_by") is None
    # ... and the proposal is audited, with the pull request
    assert [a["change"] for a in audit] == ["proposed"]
    assert audit[0]["after"]["pull_request"]["url"].endswith("/pull/7")
    assert audit[0]["changed_by"] == "usr_param"


def test_a_custom_repo_rule_changes_only_its_own_item_in_the_rules_list(pg):
    eng, pid = pg
    gh = _FakeGitHub({"deploy/boss.deploy.yaml": MANIFEST})
    with eng.begin() as c:
        t = _app(c, pid)
        slow = _rules(c, t)["CheckoutSlow"]
        got = rt.propose(c, None, t, slow["id"],
                         {"expr": "sum(rate(checkout_failed_total[5m])) > 4", "for": "5m"},
                         actor="usr_param", github=gh, now=T0)
    new = gh.files[(got["branch"], "deploy/boss.deploy.yaml")]
    assert got["line"] == 16
    assert _changed_lines(MANIFEST, new) <= {17, 18}
    doc = yaml.safe_load(new)
    assert doc["alerts"]["rules"][0]["expr"] == "sum(rate(checkout_failed_total[5m])) > 4"
    assert doc["alerts"]["rules"][0]["for"] == "5m"
    assert doc["alerts"]["rules"][1] == yaml.safe_load(MANIFEST)["alerts"]["rules"][1]


def test_a_tuning_that_changes_nothing_or_breaks_bounds_is_refused_in_words(pg):
    eng, pid = pg
    gh = _FakeGitHub({"deploy/boss.deploy.yaml": MANIFEST})
    with eng.begin() as c:
        t = _app(c, pid)
        lat = _rules(c, t)["latency_p95"]
        with pytest.raises(rt.TuneRefused, match="between"):
            rt.propose(c, None, t, lat["id"], {"params": {"threshold": 0}}, actor="u",
                       github=gh, now=T0)
        with pytest.raises(rt.TuneRefused, match="Nothing to change"):
            rt.propose(c, None, t, lat["id"], {"params": {"threshold": 500}}, actor="u",
                       github=gh, now=T0)
    assert gh.prs == []


# ── where it lives: the manifest's path and the rule's line, on the Rules list ───────────────
def test_the_rules_list_says_where_a_repo_rule_lives_and_how_it_is_tuned(pg):
    eng, pid = pg
    with eng.begin() as c:
        t = _app(c, pid)
        tab = view.app_alerts_tab(c, None, pid, t["id"], state="firing")
    by = {(r["alert"] if r["kind"] == "custom" else r["kind"]): r for r in tab["rules"]}
    assert by["latency_p95"]["tune_via"] == "pull_request"
    assert by["latency_p95"]["where"]["path"] == "deploy/boss.deploy.yaml"
    assert by["latency_p95"]["where"]["line"] == 12
    assert by["latency_p95"]["where"]["href"].endswith(
        "/acme/shop/blob/6bf2673" + "0" * 33 + "/deploy/boss.deploy.yaml#L12-L14")
    assert by["CheckoutSlow"]["where"]["line"] == 16
    assert by["PaymentsDown"]["where"]["line"] == 21
    assert by["error_rate"]["where"]["line"] == 9
    # BOSS's own default (not in the yaml) is tuned in the console
    assert by["health_check"]["tune_via"] == "console" and by["health_check"]["where"] is None


# ── a console rule: saved directly, audited ──────────────────────────────────────────────────
def test_a_console_rule_is_saved_directly_and_audited_and_never_proposed(pg):
    eng, pid = pg
    with eng.begin() as c:
        t = _app(c, pid)
        hc = _rules(c, t)["health_check"]
        assert rt.via(hc) == "console"
        with pytest.raises(rt.TuneRefused, match="not declared in the repo"):
            rt.propose(c, None, t, hc["id"], {"params": {"min_failures": 5}}, actor="u",
                       github=_FakeGitHub({}), now=T0)
        saved = aa.update_rule(c, t, hc["id"], {"params": {"min_failures": 5}},
                               actor="usr_param", reason="flaps on deploys", now=T0)
        audit = c.execute(text(
            "SELECT change, before, after, reason, changed_by FROM monitoring_changes "
            "WHERE rule_id = :r"), {"r": hc["id"]}).mappings().all()
        events = c.execute(text(
            "SELECT event_type FROM events WHERE entity_id = :r"), {"r": hc["id"]}).scalars().all()
    assert saved["params"]["min_failures"] == 5 and saved["source"] == "console"
    assert [a["change"] for a in audit] == ["edited"]
    assert audit[0]["before"]["params"]["min_failures"] == 3
    assert audit[0]["after"]["params"]["min_failures"] == 5
    assert audit[0]["reason"] == "flaps on deploys" and audit[0]["changed_by"] == "usr_param"
    assert "alert_rule.edited" in events


# ── the 24 h preview runs the NEW threshold ─────────────────────────────────────────────────
def _scrape(c, t, at, requests, errors):
    snap = {"requests": requests, "errors": errors, "routes": {}, "buckets": {},
            "hist_count": None, "hist_sum": None}
    c.execute(text(
        "INSERT INTO app_metrics_scrapes (id, project_id, deployment_target_id, scraped_at, ok, "
        " word, snapshot) VALUES (:i, :p, :t, :at, true, 'Scraped', CAST(:s AS jsonb))"),
        {"i": f"ams_{uuid4().hex}", "p": t["project_id"], "t": t["id"], "at": at,
         "s": json.dumps(snap)})


def test_the_preview_replays_the_new_threshold_over_the_last_24_hours(pg):
    eng, pid = pg
    with eng.begin() as c:
        t = _app(c, pid)
        # 8% of requests fail for the last 40 minutes: above 5%, below 10%
        req = err = 0.0
        for i in range(41):
            _scrape(c, t, T0 - timedelta(minutes=40 - i), req, err)
            req, err = req + 50, err + 4
        er = _rules(c, t)["error_rate"]
        got = rt.preview_change(c, None, t, er, {"params": {"threshold": 10}}, now=T0)
    assert got["window"] == "24h"
    assert got["now"]["fired"] >= 1 and "last 24 h" in got["now"]["word"]
    assert got["tuned"]["fired"] == 0 and "last 24 h" in got["tuned"]["word"]
    assert "> 10 and" in got["tuned"]["expr"] and got["tuned"]["condition"].startswith("> 10%")
