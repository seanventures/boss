"""Setup › Embedding model over HTTP (operator decision 2026-10-22).

Save, check and remove answer with the Repo and models view; the view carries the slot without
its key; a model without embeddings is stored with its failed check, not refused; meaning-based
search is then off and the knowledge status says why. The scripted engine plays the model.
"""
from __future__ import annotations

import json

from apps.api.tests.setup_helpers import AUTH, PROJECT, as_ceo, fakes, view

KEY = "sk-proj-SETUPEMBEDDINGKEY0123456789abcdXYZ"


def _post(client, path: str, **body):
    return client.post(path, headers=AUTH, json={"project_id": PROJECT, **body})


def test_save_check_and_remove_the_embedding_model(client, monkeypatch):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    monkeypatch.setenv("BOSS_SCRIPTED_ENGINE", "1")
    before = view(client)["embedding_model"]
    assert before["configured"] is False and "no embedding model" in before["reason"]

    saved = _post(client, "/v1/setup/embedding-model", provider="scripted", model="scripted-embed",
                  dimension=8, api_key=KEY)
    assert saved.status_code == 200, saved.text
    slot = saved.json()["embedding_model"]
    assert slot["check"]["state"] == "ok" and slot["search"] is True and slot["has_key"] is True
    assert KEY not in saved.text and KEY[-4:] not in json.dumps(slot)
    assert view(client)["embedding_model"]["model_id"] == "scripted:scripted-embed:8"

    checked = _post(client, "/v1/setup/embedding-model/check")
    assert checked.status_code == 200 and checked.json()["embedding_model"]["check"]["state"] == "ok"

    failed = _post(client, "/v1/setup/embedding-model", provider="scripted", model="scripted-chat-only",
                   dimension=8)
    assert failed.status_code == 200, failed.text
    slot = failed.json()["embedding_model"]
    assert slot["check"]["state"] == "failed" and "does not support embeddings" in slot["reason"]

    removed = client.delete("/v1/setup/embedding-model", headers=AUTH, params={"project_id": PROJECT})
    assert removed.status_code == 200 and removed.json()["embedding_model"]["configured"] is False


def test_a_slot_it_will_not_store_is_refused_in_words(client, monkeypatch):
    as_ceo(monkeypatch)
    fakes(monkeypatch)
    refused = _post(client, "/v1/setup/embedding-model", provider="openai", model="text-embedding-3-small",
                    dimension="many")
    assert refused.status_code == 422 and "dimension" in refused.text
    nothing = _post(client, "/v1/setup/embedding-model/check")
    assert nothing.status_code == 422 and "no embedding model" in nothing.text
