"""An agent step runs on the model the project connected — through the real start and tick.

THE INCIDENT (2026-10-03). The five bundled examples on a live install: Claude Code and Codex
subscriptions connected, no OpenAI key. Readiness said a model was connected. "Video edit and
publish" started, and its `edit` step — an agent step run by `video_editor` — DEADLETTERED with

    IntegrationConfigurationError: OPENAI_API_KEY is not configured

The agent loop built its model from the deployment settings (`llm_engine` = "auto" = the
OpenAI adapter) instead of the project's connected sources. See services/model_routing.py.

Nothing about model choice is patched here. Only the Claude CLI's subprocess is replaced, so the
call can be seen without a real subscription, and the OpenAI adapter is made to fail loudly.
"""
from __future__ import annotations

from sqlalchemy import text

PROJECT = "boss-dogfood"


def _no_openai_key(monkeypatch):
    from apps.api.app.config import get_settings

    settings = get_settings()
    for attr, value in (("openai_api_key", None), ("azure_openai_key", None),
                        ("llm_engine", "auto"), ("chat_llm_engine", ""), ("chat_model", "")):
        monkeypatch.setattr(settings, attr, value, raising=False)


def _subscription_only(engine, *, connected: bool) -> None:
    with engine.begin() as c:
        c.execute(text("DELETE FROM project_settings WHERE project_id=:p "
                       "AND key IN ('chat_llm_engine', 'llm_engine')"), {"p": PROJECT})
        c.execute(text("UPDATE entitlement_sources SET connection_status='not_connected', "
                       "credential_secret_ref_id=NULL WHERE project_id=:p"), {"p": PROJECT})
        if connected:
            updated = c.execute(text(
                "UPDATE entitlement_sources SET connection_status='connected' "
                "WHERE project_id=:p AND source_id='anthropic_claude_code'"),
                {"p": PROJECT}).rowcount
            assert updated, "the seed no longer creates the Claude Code source row"


def _agent(client, role_key: str, skill: str) -> None:
    made = client.post("/v1/roles", json={"project_id": PROJECT, "role_key": role_key,
                                          "display_name": role_key.replace("_", " ").title()})
    assert made.status_code == 201, made.text
    granted = client.post("/v1/role-skills", json={
        "project_id": PROJECT, "role_key": role_key, "skill_id": skill, "actor": "ceo"})
    assert granted.status_code == 201, granted.text


def _start_edit_step(client, name: str, *, start_anyway=None) -> dict:
    made = client.post("/v1/delivery-workflows/create",
                       json={"project_id": PROJECT, "name": name, "actor": "ceo"})
    assert made.status_code == 200, made.text
    definition_id = made.json()["definition"]["id"]
    saved = client.post("/v1/delivery-workflows/save", json={
        "project_id": PROJECT, "definition_id": definition_id, "actor": "ceo",
        "spec": {"entry_step": "edit",
                 "input_schema": {"required": ["instructions"],
                                  "properties": {"instructions": {"title": "What to do"}}},
                 "steps": [{"id": "edit", "kind": "capability", "next": [],
                            "captain": "video_editor_test",
                            "approval": {"mode": "auto"},
                            "binding": {"agent_work": True, "inputs": {
                                "instructions": {"from_input": "instructions"}}}}]}})
    assert saved.status_code == 200, saved.text
    listed = client.get("/v1/delivery-workflows", params={"project_id": PROJECT}).json()
    workflow_key = next(w["workflow_key"] for w in listed["items"] if w["id"] == definition_id)
    body = {"project_id": PROJECT, "actor": "ceo", "workflow_key": workflow_key,
            "input": {"instructions": "Trim the first five seconds."}}
    if start_anyway:
        body["start_anyway"] = start_anyway
    started = client.post("/v1/delivery-workflows/start", json=body)
    assert started.status_code == 200, started.text
    ticked = client.post("/v1/delivery-workflows/run-capabilities",
                         json={"project_id": PROJECT})
    assert ticked.status_code == 200, ticked.text
    runs = client.get("/v1/delivery-workflows/runs", params={"project_id": PROJECT}).json()
    return next(s for r in runs["runs"] for s in r["steps"] if s["step_id"] == "edit")


def test_the_video_edit_step_runs_on_the_connected_subscription(client, engine, monkeypatch):
    from boss_engine.services.claude_cli_adapter import ClaudeCliAdapter
    from boss_engine.services.openai_adapter import OpenAIAdapter

    _no_openai_key(monkeypatch)
    _subscription_only(engine, connected=True)
    _agent(client, "video_editor_test", "video.trim")

    asked = []

    def subscription_answers(self, prompt, **_kw):
        asked.append(type(self).__name__)
        return {"result": '{"final": "Trimmed as asked."}',
                "usage": {"input_tokens": 3, "output_tokens": 2}}

    def openai_must_not_be_asked(*_a, **_k):
        raise AssertionError("the OpenAI adapter was asked on a subscription-only project")

    monkeypatch.setattr(ClaudeCliAdapter, "_invoke", subscription_answers)
    monkeypatch.setattr(OpenAIAdapter, "create_chat_completion", openai_must_not_be_asked)

    # No start_anyway: a connected subscription IS a connected model, and readiness agrees.
    step = _start_edit_step(client, "Video edit on a subscription")
    assert step["status"] == "succeeded", step.get("error")
    assert asked and set(asked) == {"ClaudeCliAdapter"}, asked
    assert step["output"]["ran_as"]["agent"] == "video_editor_test"


def test_with_no_model_at_all_the_step_fails_with_the_fix(client, engine, monkeypatch):
    """The other side: nothing connected. The step stops at its FIRST attempt (settled — a retry
    cannot connect a model) and its error says what to connect, instead of retrying on a
    schedule and ending on the bare "OPENAI_API_KEY is not configured"."""
    _no_openai_key(monkeypatch)
    _subscription_only(engine, connected=False)
    monkeypatch.setattr("boss_engine.services.setup_service.connected_engines",
                        lambda *_a, **_k: {})
    _agent(client, "video_editor_test", "video.trim")

    step = _start_edit_step(client, "Video edit with no model", start_anyway=["model"])
    assert step["status"] in ("failed", "deadlettered"), step
    assert step["attempt"] == 1, step
    assert str(step["error"]).startswith("No connected model can run this work. "
                                         "Connect a model"), step["error"]


# ── A key stored through the console, not a subscription ─────────────────────────────────────

def _store_key(client, monkeypatch, provider: str, key: str) -> None:
    """The console's own route: POST /v1/setup/model (Community stores it unverified)."""
    from boss_engine.services import secret_backends

    monkeypatch.setattr(secret_backends, "default_secret_backend", lambda _s: "database")
    stored = client.post("/v1/setup/model", json={
        "project_id": PROJECT, "actor": "ceo", "provider": provider, "api_key": key,
        "model": f"{provider}-test-model"})
    assert stored.status_code == 201, stored.text


def _openai_compatible_answers(monkeypatch) -> list:
    from boss_engine.services.openai_adapter import OpenAIAdapter

    asked = []

    def answer(self, messages, tools=None, **_kw):
        asked.append({"key": self._resolved_api_key(), "base": self._resolved_base_url(),
                      "model": self.settings.openai_model})
        return ({"role": "assistant", "content": "Trimmed as asked.", "tool_calls": [],
                 "raw": {"role": "assistant", "content": "Trimmed as asked."}},
                {"model": "gpt-4o", "prompt_tokens": 10, "completion_tokens": 5,
                 "total_tokens": 15})

    monkeypatch.setattr(OpenAIAdapter, "create_chat_completion", answer)
    return asked


def test_a_stored_openrouter_key_runs_the_step_on_openrouter(client, engine, monkeypatch):
    """The review's repro, end to end: a project whose only model is a key stored in the
    console. Readiness counted it; the step got `OpenAIAdapter(settings)` with no project and
    died with "OPENAI_API_KEY is not configured"."""
    _no_openai_key(monkeypatch)
    _subscription_only(engine, connected=False)
    _store_key(client, monkeypatch, "openrouter", "sk-or-test-not-real")
    _agent(client, "video_editor_test", "video.trim")
    asked = _openai_compatible_answers(monkeypatch)

    # No start_anyway: readiness and the router agree the stored key is a model.
    step = _start_edit_step(client, "Video edit on a stored OpenRouter key")
    assert step["status"] == "succeeded", step.get("error")
    assert asked and asked[0] == {"key": "sk-or-test-not-real",
                                  "base": "https://openrouter.ai/api/v1",
                                  "model": "openrouter-test-model"}, asked

    # AND THE TURN WAS SPENT THROUGH THE BUDGET: reserved, receipted, settled.
    with engine.begin() as c:
        settled = c.execute(text(
            "SELECT count(*) FROM cost_receipts WHERE project_id=:p "
            "AND skill_or_model_id='model.agent_turn' AND actual_usd_micros IS NOT NULL"),
            {"p": PROJECT}).scalar()
    assert settled >= 1


def test_a_stored_openai_key_with_no_env_key_runs_the_step(client, engine, monkeypatch):
    _no_openai_key(monkeypatch)
    _subscription_only(engine, connected=False)
    _store_key(client, monkeypatch, "openai", "sk-stored-test-not-real")
    _agent(client, "video_editor_test", "video.trim")
    asked = _openai_compatible_answers(monkeypatch)

    step = _start_edit_step(client, "Video edit on a stored OpenAI key")
    assert step["status"] == "succeeded", step.get("error")
    assert asked and asked[0]["key"] == "sk-stored-test-not-real", asked


def test_an_agent_step_on_a_capped_project_is_refused_by_the_budget(client, engine, monkeypatch):
    """The agent loop's turns spent with no reservation: a capped project ran past its cap and
    the cost was recorded nowhere. Refused before the provider is asked, settled at once."""
    _no_openai_key(monkeypatch)
    _subscription_only(engine, connected=False)
    _store_key(client, monkeypatch, "openai", "sk-stored-test-not-real")
    _agent(client, "video_editor_test", "video.trim")
    asked = _openai_compatible_answers(monkeypatch)
    with engine.begin() as c:
        c.execute(text("UPDATE budget_policies SET daily_cap_usd_micros = 1 "
                       "WHERE scope_type='project' AND scope_id=:p"), {"p": PROJECT})

    step = _start_edit_step(client, "Video edit past the cap")
    assert asked == [], "the provider was asked past the budget cap"
    assert step["status"] in ("failed", "deadlettered"), step
    assert step["attempt"] == 1, step
    assert str(step["error"]).startswith("The budget refused this work."), step["error"]


# ── Every step kind's failure is classified where it is recorded ─────────────────────────────

def test_a_non_agent_step_whose_model_is_missing_is_settled_with_the_fix(
        client, engine, monkeypatch):
    """`typed_decisions` wraps a model failure as "typed decision failed: …" and the runner's
    catch-all records anything else raw — both retried on a schedule and deadlettered on the bare
    sentence, because the settled+explained mapping lived only on the agent_work path. The
    classification is now in fail_step, so the step kind does not matter."""
    from boss_engine.services import workflow_capability_runner as runner

    _no_openai_key(monkeypatch)
    _subscription_only(engine, connected=True)
    _agent(client, "video_editor_test", "video.trim")

    def typed_decision_without_a_model(*_a, **_k):
        raise runner.CapabilityExecutionError(
            "typed decision failed: OPENAI_API_KEY is not configured.")

    monkeypatch.setattr(runner, "_execute_step", typed_decision_without_a_model)
    step = _start_edit_step(client, "A decision with no model")
    assert step["status"] in ("failed", "deadlettered"), step
    assert step["attempt"] == 1, step
    assert str(step["error"]).startswith("No connected model can run this work. "
                                         "Connect a model"), step["error"]
