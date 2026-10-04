"""A model key is proved before it is kept — in Community too, through the governed path.

Community used to save keys `unverified` because a live check had no budget reservation or
receipt, while subscriptions were proved; a bad key then failed inside the first workflow. Now
every edition asks one tiny question through `governed_proof`: reserved against the budget,
receipted, settled at its real cost. (Replaces test_setup_model_probe_gate: the intent — no
UNGOVERNED provider call in Community — stands; the call is now governed.)
"""
from sqlalchemy import text

from apps.api.tests.setup_helpers import AUTH, PROJECT, as_ceo, connect_key, fakes


def _source(engine, source_id="setup-openrouter"):
    with engine.begin() as c:
        return c.execute(text(
            "SELECT credential_secret_ref_id, supported_models, connection_metadata "
            "FROM entitlement_sources WHERE project_id = :p AND source_id = :s"),
            {"p": PROJECT, "s": source_id}).mappings().first()


def test_a_key_is_asked_one_question_reserved_and_receipted(client, engine, monkeypatch):
    as_ceo(monkeypatch)
    calls: list = []
    fakes(monkeypatch, model_calls=calls)
    response = connect_key(client)
    assert response.status_code == 200, response.text
    [model] = response.json()["models"]
    assert (model["label"], model["pill"]["label"], model["kind"]) == (
        "OpenRouter", "Working", "metered")
    assert [m for m, _ in calls] == ["POST"]                   # exactly one completion
    saved = _source(engine)
    assert saved["credential_secret_ref_id"] and saved["supported_models"] == ["stub/small"]
    receipt_id = saved["connection_metadata"]["last_check"]["receipt_id"]
    with engine.begin() as c:
        receipt = c.execute(text("SELECT effect_kind, status FROM receipts WHERE id = :r"),
                            {"r": receipt_id}).mappings().one()
        held = c.execute(text("SELECT count(*) FROM budget_reservations WHERE project_id = :p "
                              "AND status = 'active'"), {"p": PROJECT}).scalar()
    assert dict(receipt) == {"effect_kind": "model.connection_proof", "status": "succeeded"}
    assert held == 0                                           # settled, not left holding


def test_a_rejected_key_is_not_kept(client, engine, monkeypatch):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    response = connect_key(client, key="sk-stub-401")
    assert response.status_code == 422
    assert response.json()["detail"]["state"] == "key_rejected"
    assert response.json()["detail"]["headline"] == "Key rejected"
    saved = _source(engine)
    assert saved is None or saved["credential_secret_ref_id"] is None


def test_a_busy_provider_keeps_the_key_and_says_until_when(client, engine, monkeypatch):
    """Rate-limited is not wrong: the key is kept, the row says Limited and when it resets."""
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    response = connect_key(client, key="sk-stub-429")
    assert response.status_code == 200, response.text
    [model] = response.json()["models"]
    assert model["pill"]["label"].startswith("Limited · ")
    assert model["retry_at"]
    assert _source(engine)["credential_secret_ref_id"]


def test_no_credit_and_unknown_model_name_their_fix(client, monkeypatch):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    no_credit = connect_key(client, key="sk-stub-402")
    assert no_credit.json()["detail"]["headline"] == "No credit"
    unknown = connect_key(client, key="sk-stub-404", model="nope")
    assert unknown.json()["detail"]["headline"] == "Model not found"
    assert "nope" in unknown.json()["detail"]["fix"]


def test_one_press_is_one_check(client, monkeypatch):
    as_ceo(monkeypatch)
    calls: list = []
    fakes(monkeypatch, model_calls=calls)
    for _ in range(2):
        assert connect_key(client, idempotency_key="press-1").status_code == 200
    assert len(calls) == 1


def test_a_second_key_is_a_second_model_and_a_busy_key_never_replaces_a_working_one(
        client, engine, monkeypatch):
    """One source per connection: a second OpenRouter key was written over the first, and a
    rate-limited key replaced a working one."""
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    assert connect_key(client, model="stub/small").status_code == 200
    second = connect_key(client, model="stub/large")
    assert second.status_code == 200, second.text
    rows = {m["source_id"]: m for m in second.json()["models"]}
    assert set(rows) == {"setup-openrouter", "setup-openrouter-2"}
    assert {m["model"] for m in rows.values()} == {"stub/small", "stub/large"}
    # Replace the first with a key the provider says is busy: refused, the working one stays.
    replaced = client.post("/v1/setup/models", headers=AUTH, json={
        "project_id": PROJECT, "provider": "openrouter", "kind": "api_key",
        "api_key": "sk-stub-429", "model": "stub/small", "source_id": "setup-openrouter"})
    assert replaced.status_code == 422
    assert replaced.json()["detail"]["state"] == "rate_limited"
    with engine.begin() as c:
        meta = c.execute(text("SELECT connection_metadata FROM entitlement_sources "
                              "WHERE project_id = :p AND source_id = 'setup-openrouter'"),
                         {"p": PROJECT}).scalar()
    assert meta["last_check"]["state"] == "working"
