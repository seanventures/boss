"""The Skills page's History, Import and Tests tabs, through the HTTP surface (BOSSAPP-841..844)."""
from __future__ import annotations

import uuid

PROJECT = "boss-dogfood"
V1 = "# Tone\nWrite short sentences.\n"
V2 = "# Tone\nWrite short sentences.\nNever use jargon.\n"


def _key() -> str:
    # "zz_": test_a_skill_from_a_file picks the first catalogue id as "a built-in".
    return f"zz_tone_{uuid.uuid4().hex[:8]}"


def _save(client, key, knowledge, **extra):
    return client.post("/v1/skill-definitions", json={
        "project_id": PROJECT, "actor": "ceo", "skill_key": key, "label": "Tone",
        "behaviour_kind": "guide", "summary": "How we write", "knowledge": knowledge,
        "risk_tier": 0, **extra})


def test_history_lists_versions_diffs_and_restores_as_a_new_version(client):
    key = _key()
    assert _save(client, key, V1, mode="create").status_code == 201
    saved = _save(client, key, V2, mode="update")
    assert saved.status_code == 201 and saved.json()["skill"]["version_no"] == 2
    listed = client.get(f"/v1/skill-definitions/{key}/versions",
                        params={"project_id": PROJECT}).json()
    assert [v["version_no"] for v in listed["versions"]] == [2, 1]
    diff = client.get(f"/v1/skill-definitions/{key}/diff",
                      params={"project_id": PROJECT, "from_version": "1", "to_version": "2"})
    assert diff.status_code == 200 and "Never use jargon" in diff.json()["diff"]
    restored = client.post(f"/v1/skill-definitions/{key}/restore",
                           json={"project_id": PROJECT, "actor": "ceo", "version": 1})
    assert restored.status_code == 200, restored.text
    again = client.get(f"/v1/skill-definitions/{key}/versions",
                       params={"project_id": PROJECT}).json()["versions"]
    assert again[0]["version_no"] == 3 and again[0]["current"]
    assert "jargon" not in again[0]["content"]["knowledge"]


def test_an_agent_principal_cannot_save_a_skill_directly(client):
    r = client.post("/v1/skill-definitions", json={
        "project_id": PROJECT, "actor": "agent:writer", "skill_key": _key(), "label": "Tone",
        "behaviour_kind": "guide", "summary": "How we write", "knowledge": V1})
    # Refused either by the auth layer (an agent principal is not a member) or by the gate.
    assert r.status_code in (403, 422), r.text


def test_tests_tab_runs_grades_and_gates_publish(client):
    key = _key()
    _save(client, key, V1, mode="create")
    case = client.post(f"/v1/skill-definitions/{key}/tests/cases",
                       json={"project_id": PROJECT, "actor": "ceo", "name": "greets",
                             "input": {}})
    assert case.status_code == 201, case.text
    run = client.post(f"/v1/skill-definitions/{key}/tests/run",
                      json={"project_id": PROJECT, "actor": "ceo"})
    assert run.status_code == 200, run.text
    run_id = run.json()["runs"][0]["id"]
    graded = client.post(f"/v1/skill-test-runs/{run_id}/grade",
                         json={"project_id": PROJECT, "actor": "ceo", "grade": "fail",
                               "notes": "too long"})
    assert graded.status_code == 200 and graded.json()["run"]["grade"] == "fail"
    view = client.get(f"/v1/skill-definitions/{key}/tests",
                      params={"project_id": PROJECT}).json()
    assert view["gate"]["allowed"] is False
    assert view["cases"][0]["current"]["grade"] == "fail"
    blocked = client.post(f"/v1/skill-definitions/{key}/publish",
                          json={"project_id": PROJECT, "actor": "ceo", "version": 1})
    assert blocked.status_code == 409


def test_import_previews_then_creates_a_pinned_skill(client, monkeypatch):
    from boss_engine.services import skill_sources

    def fake_fetcher(_engine, _settings, _project):
        def fetch(loc, ref):
            return skill_sources.Fetched(
                sha="a" * 40, text="---\nname: release-notes\ndescription: Release notes\n"
                                  "---\nLead with what changed.\n")
        return fetch

    monkeypatch.setattr(skill_sources, "default_fetcher", fake_fetcher)
    key = f"zz_notes_{uuid.uuid4().hex[:8]}"
    body = {"project_id": PROJECT, "actor": "ceo", "kind": "github",
            "locator": "acme/playbooks/skills/release-notes", "ref": "main", "skill_key": key}
    shown = client.post("/v1/skill-sources/preview", json=body)
    assert shown.status_code == 200, shown.text
    assert shown.json()["sha"] == "a" * 40 and "Lead with" in shown.json()["skill"]["knowledge"]
    made = client.post("/v1/skill-sources", json={**body, "pin": "a" * 40})
    assert made.status_code == 201, made.text
    assert made.json()["source"]["pinned_sha"] == "a" * 40
    sources = client.get("/v1/skill-sources", params={"project_id": PROJECT}).json()["sources"]
    assert any(s["skill_key"] == key for s in sources)
