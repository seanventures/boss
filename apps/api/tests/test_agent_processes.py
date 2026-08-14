"""Agent process HTTP — PRD handoff chain completes in simulated mode."""

import pytest

RICH_PRD = """
# ApproveIt PRD
Users (requesters and managers) submit expenses.
Acceptance: must submit, must verify approval, shall reject negatives.
Data model and API for requests. Security: no self-approval.
Risks: terminal immutability. Success metric: one approved request.
Deploy with npm run verify.
"""

CANONICAL_PROCESS_STORY = (
    "PRD delivered, architect, CTO, and VP product review, improve, "
    "captain builds, QA, UI/UX, captain verifies, panel certifies, captain accepts"
)


def test_start_simulated_prd_process_reaches_captain_accept(client):
    started = client.post(
        "/v1/agent-processes/runs",
        json={
            "project_id": "boss-dogfood",
            "actor": "ceo",
            "title": "ApproveIt process",
            "prd": RICH_PRD,
            "simulated": True,
            "auto_build": False,
        },
    )
    assert started.status_code == 201, started.text
    body = started.json()
    assert body["status"] == "completed"
    keys = [step["step_key"] for step in body.get("steps") or []]
    assert "intake_prd" in keys
    assert "review_architect" in keys
    assert "review_cto" in keys
    assert "review_vp" in keys
    assert "improve_prd" in keys
    assert "captain_build" in keys
    assert "qa_check" in keys
    assert "ux_review" in keys
    assert "captain_verify" in keys
    assert "panel_certify" in keys
    assert "captain_accept" in keys
    accept = next(step for step in body["steps"] if step["step_key"] == "captain_accept")
    assert accept["status"] == "succeeded"
    panel = next(step for step in body["steps"] if step["step_key"] == "panel_certify")
    assert (panel.get("output") or {}).get("verdict") in {"certified", "certified_planned"}


def test_empty_prd_is_rejected(client):
    missing = client.post(
        "/v1/agent-processes/runs",
        json={"project_id": "boss-dogfood", "actor": "ceo", "prd": "   "},
    )
    assert missing.status_code == 422


def test_templates_include_default_prd_process(client):
    listed = client.get("/v1/agent-processes/templates", params={"project_id": "boss-dogfood"})
    assert listed.status_code == 200, listed.text
    keys = [item["process_key"] for item in listed.json()["templates"]]
    assert "prd_to_accepted_work" in keys


def _compiler_missing() -> bool:
    try:
        from boss_engine.services.process_compiler import compile_process_from_text  # noqa: F401
    except ImportError:
        return True
    return False


def _detail(response) -> str:
    body = response.json()
    detail = body.get("detail", body)
    return detail if isinstance(detail, str) else str(detail)


def _step_keys(spec_or_run: dict) -> list[str]:
    steps = spec_or_run.get("steps") or []
    if not steps and isinstance(spec_or_run.get("spec"), dict):
        steps = spec_or_run["spec"].get("steps") or []
    keys = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        keys.append(str(step.get("step_key") or step.get("id") or ""))
    return [key for key in keys if key]


def _step_actions(spec_or_run: dict) -> list[str]:
    steps = spec_or_run.get("steps") or []
    if not steps and isinstance(spec_or_run.get("spec"), dict):
        steps = spec_or_run["spec"].get("steps") or []
    return [str(step.get("action") or "") for step in steps if isinstance(step, dict)]


def test_compile_canonical_prd_story_returns_review_and_accept(client):
    before = client.get("/v1/agent-processes/templates", params={"project_id": "boss-dogfood"})
    before_keys = {item["process_key"] for item in before.json()["templates"]}

    compiled = client.post(
        "/v1/agent-processes/compile",
        json={"project_id": "boss-dogfood", "actor": "ceo", "text": CANONICAL_PROCESS_STORY},
    )
    assert compiled.status_code != 500, compiled.text
    if _compiler_missing():
        assert compiled.status_code == 422
        assert "compiler unavailable" in _detail(compiled).lower()
        after = client.get("/v1/agent-processes/templates", params={"project_id": "boss-dogfood"})
        after_keys = {item["process_key"] for item in after.json()["templates"]}
        assert after_keys == before_keys
        return

    assert compiled.status_code == 200, compiled.text
    body = compiled.json()
    spec = body.get("spec") or {}
    keys = _step_keys(spec)
    actions = _step_actions(spec)
    assert "review_architect" in keys or any("architect" in key for key in keys)
    assert "accept_work" in actions or any(key.endswith("accept") or "accept" in key for key in keys)
    assert "warnings" in body
    after = client.get("/v1/agent-processes/templates", params={"project_id": "boss-dogfood"})
    after_keys = {item["process_key"] for item in after.json()["templates"]}
    assert after_keys == before_keys


def test_compile_empty_text_is_rejected(client):
    missing = client.post(
        "/v1/agent-processes/compile",
        json={"project_id": "boss-dogfood", "actor": "ceo", "text": "   "},
    )
    assert missing.status_code == 422
    assert missing.status_code != 500


def test_get_template_by_process_key(client):
    listed = client.get("/v1/agent-processes/templates", params={"project_id": "boss-dogfood"})
    assert listed.status_code == 200, listed.text
    got = client.get(
        "/v1/agent-processes/templates/prd_to_accepted_work",
        params={"project_id": "boss-dogfood"},
    )
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["process_key"] == "prd_to_accepted_work"
    keys = _step_keys(body.get("spec") or body)
    actions = _step_actions(body.get("spec") or body)
    assert "review_architect" in keys
    assert "accept_work" in actions
    missing = client.get(
        "/v1/agent-processes/templates/does_not_exist",
        params={"project_id": "boss-dogfood"},
    )
    assert missing.status_code == 404


def test_list_and_get_runs_include_spec_summary_and_steps(client):
    started = client.post(
        "/v1/agent-processes/runs",
        json={
            "project_id": "boss-dogfood",
            "actor": "ceo",
            "title": "Summary run",
            "prd": RICH_PRD,
            "simulated": True,
            "auto_build": False,
        },
    )
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]

    got = client.get(f"/v1/agent-processes/runs/{run_id}", params={"project_id": "boss-dogfood"})
    assert got.status_code == 200, got.text
    body = got.json()
    assert body.get("steps")
    assert "review_architect" in _step_keys(body)
    summary = body.get("spec_summary") or {}
    assert summary.get("step_count", 0) >= 1
    assert "review_architect" in (summary.get("step_keys") or [])
    assert "accept_work" in (summary.get("actions") or [])

    listed = client.get("/v1/agent-processes/runs", params={"project_id": "boss-dogfood"})
    assert listed.status_code == 200, listed.text
    match = next(item for item in listed.json()["runs"] if item["id"] == run_id)
    assert match.get("steps")
    listed_summary = match.get("spec_summary") or {}
    assert listed_summary.get("step_count", 0) >= 1
    assert "review_architect" in (listed_summary.get("step_keys") or _step_keys(match))


def test_start_run_uses_custom_process_key(client):
    spec = {
        "key": "custom_review_chain",
        "name": "Custom review chain",
        "entry": "intake_prd",
        "steps": [
            {
                "id": "intake_prd",
                "kind": "role",
                "role": "product_manager",
                "action": "accept_prd",
                "next": ["review_architect"],
            },
            {
                "id": "review_architect",
                "kind": "role",
                "role": "chief_architect",
                "action": "review_prd",
                "next": [],
            },
        ],
    }
    created = client.post(
        "/v1/agent-processes/templates",
        json={
            "project_id": "boss-dogfood",
            "actor": "ceo",
            "process_key": "custom_review_chain",
            "name": "Custom review chain",
            "description": "intake then architect review",
            "spec": spec,
        },
    )
    assert created.status_code == 201, created.text
    started = client.post(
        "/v1/agent-processes/runs",
        json={
            "project_id": "boss-dogfood",
            "actor": "ceo",
            "title": "Custom template run",
            "prd": RICH_PRD,
            "process_key": "custom_review_chain",
            "simulated": True,
            "auto_build": False,
        },
    )
    assert started.status_code == 201, started.text
    body = started.json()
    keys = _step_keys(body)
    assert "intake_prd" in keys
    assert "review_architect" in keys
    assert "captain_build" not in keys
    unknown = client.post(
        "/v1/agent-processes/runs",
        json={
            "project_id": "boss-dogfood",
            "actor": "ceo",
            "prd": RICH_PRD,
            "process_key": "missing_custom_template",
            "simulated": True,
        },
    )
    assert unknown.status_code == 422


@pytest.mark.skipif(
    _compiler_missing(),
    reason="process_compiler module not present — cannot persist a from_text template",
)
def test_create_template_from_text_then_start_simulated_run(client):
    created = client.post(
        "/v1/agent-processes/templates",
        json={
            "project_id": "boss-dogfood",
            "actor": "ceo",
            "process_key": "from_text_prd",
            "name": "From text PRD process",
            "from_text": CANONICAL_PROCESS_STORY,
        },
    )
    assert created.status_code == 201, created.text
    template = created.json()
    assert template["process_key"] == "from_text_prd"
    spec = template.get("spec") or {}
    keys = _step_keys(spec)
    actions = _step_actions(spec)
    assert "review_architect" in keys or any("architect" in key for key in keys)
    assert "accept_work" in actions
    started = client.post(
        "/v1/agent-processes/runs",
        json={
            "project_id": "boss-dogfood",
            "actor": "ceo",
            "title": "From-text simulated",
            "prd": RICH_PRD,
            "process_key": "from_text_prd",
            "simulated": True,
            "auto_build": False,
        },
    )
    assert started.status_code == 201, started.text
    run = started.json()
    assert run.get("steps")
    assert "review_architect" in _step_keys(run) or any(
        "architect" in key for key in _step_keys(run)
    )
