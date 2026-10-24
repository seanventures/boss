"""Tune an alert rule over HTTP (P1-C): where a repo rule lives, the 24 h preview with the new
values, a repo rule proposed as a pull request (its row untouched), a console rule refused there
and saved with the PUT instead."""
from __future__ import annotations

import json
from uuid import uuid4

import yaml
from sqlalchemy import text

from apps.api.app.db import begin
from apps.api.tests._obs_api_seed import PROJECT_ID, deployed_target

MANIFEST = """version: 1
name: order-api
kind: compose
compose: {file: docker-compose.yml, project: order-api}
metrics: {path: /metrics}
alerts:
  latency_p95:
    threshold: 500
  rules:
    - alert: CheckoutSlow
      expr: sum(rate(checkout_failed_total[5m])) > 1
health:
  - {url: "http://127.0.0.1:8080/health", expect: 200}
"""


class _PR:
    url, number = "https://github.example/acme/shop/pull/9", 9


class _FakeGitHub:
    def __init__(self):
        self.files = {("main", "boss.deploy.yaml"): MANIFEST}
        self.prs: list[dict] = []

    def default_branch_sha(self):
        return "main", "0" * 40

    def read_text_file(self, branch, path):
        return self.files.get((branch, path))

    def ensure_branch(self, branch, base=None):
        self.files[(branch, "boss.deploy.yaml")] = self.files[(base, "boss.deploy.yaml")]
        return "1" * 40

    def upsert_file(self, branch, path, content, message):
        self.files[(branch, path)] = content
        return "2" * 40

    def create_pull_request(self, branch, title, body, draft=None, base=None):
        self.prs.append({"branch": branch, "base": base, "title": title})
        return _PR()


def _app(engine) -> str:
    tid = f"t_tune_{uuid4().hex[:8]}"
    with begin(engine) as c:
        deployed_target(c, PROJECT_ID, tid, "Order API", "staging")
        c.execute(text(
            "INSERT INTO stack_deployments (id, project_id, target_id, repository, ref, "
            " commit_sha, manifest, manifest_text, status, created_by) VALUES (:i, :p, :t, "
            " 'acme/shop', 'main', :c, CAST(:m AS jsonb), :mt, 'healthy', 'usr_op')"),
            {"i": f"sd_{uuid4().hex[:10]}", "p": PROJECT_ID, "t": tid, "c": "a" * 40,
             "m": json.dumps({**yaml.safe_load(MANIFEST), "manifest_path": "boss.deploy.yaml"}),
             "mt": MANIFEST})
    return tid


def test_a_repo_rule_is_tuned_by_a_pull_request_and_a_console_rule_by_the_put(
        client, engine, monkeypatch):
    from boss_engine.services import monitoring_pack

    gh = _FakeGitHub()
    monkeypatch.setattr(monitoring_pack, "_github_for", lambda *a, **k: gh)
    tid = _app(engine)
    rules = client.get(f"/v1/apps/{tid}/alert-rules", params={"project_id": PROJECT_ID})
    assert rules.status_code == 200, rules.text
    by = {(r["alert"] if r["kind"] == "custom" else r["kind"]): r for r in rules.json()["rules"]}
    lat, hc = by["latency_p95"], by["health_check"]
    assert lat["tune_via"] == "pull_request"
    assert lat["where"]["path"] == "boss.deploy.yaml" and lat["where"]["line"] == 7
    assert by["CheckoutSlow"]["where"]["line"] == 10
    assert hc["tune_via"] == "console" and hc["where"] is None

    preview = client.post(f"/v1/apps/{tid}/alert-rules/{lat['id']}/tune-preview", json={
        "project_id": PROJECT_ID, "change": {"params": {"threshold": 1500}}})
    assert preview.status_code == 200, preview.text
    assert preview.json()["window"] == "24h"
    assert "last 24 h" in preview.json()["tuned"]["word"]
    assert preview.json()["tuned"]["condition"].startswith("> 1500 ms")
    bad = client.post(f"/v1/apps/{tid}/alert-rules/{lat['id']}/tune-preview", json={
        "project_id": PROJECT_ID, "change": {"params": {"threshold": -1}}})
    assert bad.status_code == 422 and "between" in bad.json()["detail"]

    proposed = client.post(f"/v1/apps/{tid}/alert-rules/{lat['id']}/propose", json={
        "project_id": PROJECT_ID, "actor": "ceo", "reason": "noisy",
        "change": {"params": {"threshold": 1500}}})
    assert proposed.status_code == 200, proposed.text
    got = proposed.json()
    assert got["pull_request"]["url"].endswith("/pull/9") and got["line"] == 7
    new = gh.files[(got["branch"], "boss.deploy.yaml")]
    assert new == MANIFEST.replace("threshold: 500", "threshold: 1500")
    after = client.get(f"/v1/apps/{tid}/alert-rules", params={"project_id": PROJECT_ID}).json()
    still = next(r for r in after["rules"] if r["kind"] == "latency_p95")
    assert still["params"]["threshold"] == 500 and still["source"] == "yaml"
    assert after["changes"][0]["change"] == "proposed"

    console = client.post(f"/v1/apps/{tid}/alert-rules/{hc['id']}/propose", json={
        "project_id": PROJECT_ID, "actor": "ceo", "change": {"params": {"min_failures": 5}}})
    assert console.status_code == 422 and "console" in console.json()["detail"]
    saved = client.put(f"/v1/apps/{tid}/alert-rules", json={
        "project_id": PROJECT_ID, "actor": "ceo", "reason": "flaps",
        "rules": [{"id": hc["id"], "params": {"min_failures": 5}}]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["changes"][0] == {**saved.json()["changes"][0], "change": "edited",
                                          "reason": "flaps"}
