"""Choosing codex as the reviewer offers codex's models beside it, not Claude's.

The lists were drawn from the DEPLOYMENT settings, so after the operator set the reviewer to
codex on this project, the reviewer's model picker still followed the deployment's harness and
offered opus, sonnet, fable and haiku. Reported from the console: "after selecting codex I'm
not seeing codex models but still Claude ones".
"""
from boss_engine.services.project_settings import CLAUDE_MODELS, _models_of

PROJECT_ID = "boss-dogfood"


def _knob(client, key):
    body = client.get(f"/v1/settings?project_id={PROJECT_ID}").json()
    return next(k for k in body["knobs"] if k["key"] == key)


def test_the_reviewer_model_follows_the_reviewer_chosen_here(client):
    saved = client.put("/v1/settings/code_review_engine",
                       json={"project_id": PROJECT_ID, "actor": "ceo", "value": "codex"})
    assert saved.status_code == 200, saved.text
    model = _knob(client, "code_review_model")
    offered = [c for c in model["choices"] if c]
    assert set(offered) <= set(_models_of("codex")) | {""}, offered
    assert not set(offered) & set(m for m in CLAUDE_MODELS if m), offered


def test_handing_the_reviewer_back_restores_the_implementers_list(client):
    client.put("/v1/settings/code_review_engine",
               json={"project_id": PROJECT_ID, "actor": "ceo", "value": "codex"})
    client.put("/v1/settings/code_review_engine",
               json={"project_id": PROJECT_ID, "actor": "ceo", "value": None})
    engine = _knob(client, "code_agent_engine")["value"] or ""
    offered = [c for c in _knob(client, "code_review_model")["choices"] if c]
    assert set(offered) <= set(_models_of(engine)) | {""}, (engine, offered)


def test_every_lane_model_follows_its_own_lane(client):
    """Hard problems and Routine work were hard-wired to Claude's four aliases, so the same bug
    survived the reviewer fix in two more rows."""
    for engine_key, model_key in (("code_agent_strong_engine", "code_agent_strong_model"),
                                  ("code_agent_low_cost_engine", "code_agent_low_cost_model")):
        client.put(f"/v1/settings/{engine_key}",
                   json={"project_id": PROJECT_ID, "actor": "ceo", "value": "codex"})
        knob = _knob(client, model_key)
        # The current value always stays in the list (a picker must not drop what it shows),
        # so it is left out of the comparison.
        offered = [c for c in knob["choices"] if c and c != knob["value"]]
        assert offered and set(offered) <= set(_models_of("codex")), (model_key, offered)
        client.put(f"/v1/settings/{engine_key}",
                   json={"project_id": PROJECT_ID, "actor": "ceo", "value": None})


def test_changing_a_lanes_engine_clears_a_model_chosen_for_the_old_one(client):
    client.put("/v1/settings/code_agent_strong_engine",
               json={"project_id": PROJECT_ID, "actor": "ceo", "value": "claude_code"})
    client.put("/v1/settings/code_agent_strong_model",
               json={"project_id": PROJECT_ID, "actor": "ceo", "value": "opus"})
    moved = client.put("/v1/settings/code_agent_strong_engine",
                       json={"project_id": PROJECT_ID, "actor": "ceo", "value": "codex"}).json()
    assert moved["cleared"] == "code_agent_strong_model"
    assert _knob(client, "code_agent_strong_model")["source"] == "deployment default"
    client.put("/v1/settings/code_agent_strong_engine",
               json={"project_id": PROJECT_ID, "actor": "ceo", "value": None})
