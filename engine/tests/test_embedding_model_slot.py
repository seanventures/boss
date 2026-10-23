"""The embedding model is its own slot, checked through the gateway (operator decision 2026-10-22).

Breaks covered:
  * BOSS_KNOWLEDGE_EMBEDDING_MODEL named a model that nothing could call ("this build has no
    governed embedding call"), so meaning-based search could never be on;
  * a slot that does not save or read back; a key that comes back in the answer, in any part;
  * a check that skips the gateway: no reservation in the budget category `knowledge`, no
    receipt, or a failure that is not shown in the model's own words;
  * no model, or a model with no embeddings, failing a search instead of turning meaning-based
    search off and saying why;
  * a new model or dimension mixing two vector spaces instead of re-embedding in the background;
  * an embedding call built anywhere but the router.

The scripted engine plays the model (BOSS_SCRIPTED_ENGINE=1): no paid call is made. The hosted
provider's client is faked at the adapter's one SDK seam.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from boss_engine.config import get_settings
from boss_engine.db import ensure_schema
from boss_engine.services import embedding_adapter
from boss_engine.services.knowledge import embedding_slot
from boss_engine.services.knowledge.embeddings import embed_due
from boss_engine.services.knowledge.service import KnowledgeService
from boss_engine.services.knowledge.store import PostgresKnowledgeStore
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

ROOT = Path(__file__).resolve().parents[2]
ENGINE_PKG = ROOT / "engine" / "boss_engine"
STORE = PostgresKnowledgeStore()
SERVICE = KnowledgeService(STORE)
PROJECT = "proj_em"
#: A made-up key of a real key's shape. It must never come back, in whole or in part.
FAKE_KEY = "sk-proj-EMBEDDINGTESTKEY0123456789abcdefXYZ"


# ── the router owns every embedding call ─────────────────────────────────────────────────────

def _files(root: Path):
    for path in sorted(root.rglob("*.py")):
        yield path.relative_to(ENGINE_PKG).as_posix(), ast.parse(path.read_text())


def test_only_the_router_builds_an_embedding_adapter():
    builders = []
    for rel, tree in _files(ENGINE_PKG):
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                if name == "EmbeddingAdapter":
                    builders.append(rel)
    assert builders == ["services/model_routing.py"], builders


def test_the_knowledge_package_calls_no_provider_sdk():
    for rel, tree in _files(ENGINE_PKG / "services" / "knowledge"):
        imported = {alias.name.split(".")[0] for node in ast.walk(tree)
                    if isinstance(node, (ast.Import, ast.ImportFrom))
                    for alias in (node.names if isinstance(node, ast.Import) else [SimpleNamespace(name=node.module or "")])}
        assert not imported & {"openai", "ollama", "httpx", "requests", "anthropic"}, (rel, imported)


def test_the_environment_variable_is_gone():
    assert not hasattr(get_settings(), "knowledge_embedding_model")


# ── against Postgres ─────────────────────────────────────────────────────────────────────────

def _database_url() -> str:
    return os.environ.get("BOSS_KG_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


@pytest.fixture(scope="module")
def pg_engine():
    url = _database_url()
    name = url.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    if not url or not any(marker in name for marker in ("test", "testing", "ci", "scratch", "tmp")):
        pytest.skip("BOSS_KG_TEST_DATABASE_URL must name a disposable database")
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        pytest.skip(f"disposable postgres is not reachable: {exc.__class__.__name__}")
    ensure_schema(engine)
    return engine


@pytest.fixture()
def connection(pg_engine, monkeypatch):
    monkeypatch.setenv("BOSS_SCRIPTED_ENGINE", "1")
    with pg_engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text("INSERT INTO orgs (id, name) VALUES ('org_em', 'EM')"))
            conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES ('ws_em', 'org_em', 'EM')"))
            conn.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, 'ws_em', 'EM', 'em')"),
                         {"p": PROJECT})
            yield conn
        finally:
            transaction.rollback()


def _save(connection, **kw):
    args = {"provider": "scripted", "model": "scripted-embed", "dimension": 8, "actor": "ceo"}
    args.update(kw)
    return embedding_slot.save(connection, get_settings(), PROJECT, **args)


def _receipts(connection, skill: str):
    return connection.execute(text(
        "SELECT status, effect_kind FROM receipts WHERE project_id = :p AND effect_kind = :s"),
        {"p": PROJECT, "s": skill}).mappings().all()


def _reservations(connection):
    return connection.execute(text(
        "SELECT category, status FROM budget_reservations WHERE project_id = :p"), {"p": PROJECT}).mappings().all()


def test_with_no_model_meaning_search_is_off_and_says_why(connection):
    status = embedding_slot.status(connection, get_settings(), PROJECT)
    assert status["configured"] is False and status["search"] is False
    assert status["reason"] == embedding_slot.NO_MODEL and "Setup" in status["reason"]
    assert [p["provider"] for p in status["providers"]] == ["azure", "openai", "ollama"]
    found = SERVICE.search(connection, project_id=PROJECT, query="anything")
    assert found["retrieval"]["mode"] == "text+graph" and found["retrieval"]["reason"] == embedding_slot.NO_MODEL
    assert SERVICE.status(connection, project_id=PROJECT)["retrieval"] == {
        "mode": "text+graph", "reason": embedding_slot.NO_MODEL}


def test_the_slot_saves_reads_and_passes_its_check_through_the_gateway(connection):
    status = _save(connection)
    assert status["configured"] and status["provider"] == "scripted" and status["dimension"] == 8
    assert status["model_id"] == "scripted:scripted-embed:8"
    assert status["check"]["state"] == "ok" and status["check"]["reply"] == "8 numbers from scripted-embed"
    assert status["search"] is True and status["reason"] == ""
    stored = embedding_slot.read(connection, PROJECT)
    assert stored["model"] == "scripted-embed" and stored["check"]["state"] == "ok"
    assert [dict(r) for r in _receipts(connection, "knowledge.embedding_check")] == [
        {"status": "succeeded", "effect_kind": "knowledge.embedding_check"}]
    assert {r["category"] for r in _reservations(connection)} == {"knowledge"}
    found = SERVICE.search(connection, project_id=PROJECT, query="anything")
    assert found["retrieval"]["mode"] == "hybrid"
    assert SERVICE.status(connection, project_id=PROJECT)["embedding_model"]["model_id"] == "scripted:scripted-embed:8"


def test_a_model_without_embeddings_fails_its_check_in_its_own_words_and_search_goes_on(connection):
    status = _save(connection, model=embedding_adapter.SCRIPTED_CHAT_ONLY)
    assert status["check"]["state"] == "failed"
    assert "does not support embeddings" in status["check"]["error"]
    assert status["search"] is False and "check failed" in status["reason"]
    assert "does not support embeddings" in status["reason"]
    assert [r["status"] for r in _receipts(connection, "knowledge.embedding_check")] == ["failed"]
    found = SERVICE.search(connection, project_id=PROJECT, query="anything")
    assert found["retrieval"]["mode"] == "text+graph" and "check failed" in found["retrieval"]["reason"]


class _FakeHosted:
    """The OpenAI SDK at the adapter's one seam: answers 1536 numbers, or the provider's 401,
    which (as the real one does) echoes part of the key."""

    def __init__(self, fail: bool):
        self.fail = fail
        self.calls = 0

    def __call__(self, adapter):
        fake = self

        class _Embeddings:
            def create(self, model, input):
                fake.calls += 1
                if fake.fail:
                    raise RuntimeError(f"Error code: 401 - Incorrect API key provided: {adapter._api_key[:12]}****. ")
                return SimpleNamespace(data=[SimpleNamespace(index=i, embedding=[0.1] * 1536) for i, _ in enumerate(input)],
                                       usage=SimpleNamespace(prompt_tokens=4))

        return SimpleNamespace(embeddings=_Embeddings()), adapter.model


@pytest.mark.parametrize("fail", [False, True])
def test_the_key_goes_to_the_vault_and_never_comes_back(connection, monkeypatch, fail):
    fake = _FakeHosted(fail)
    monkeypatch.setattr(embedding_adapter.EmbeddingAdapter, "_client", lambda self: fake(self))
    status = _save(connection, provider="openai", model="text-embedding-3-small", dimension=1536,
                   api_key=FAKE_KEY, accept_database_fallback=True)
    assert fake.calls == 1
    assert status["has_key"] is True
    said = json.dumps(status)
    for piece in (FAKE_KEY, FAKE_KEY[:12], FAKE_KEY[-4:], FAKE_KEY[8:20]):
        assert piece not in said, "a part of the key came back"
    stored = connection.execute(text(
        "SELECT value::text FROM project_settings WHERE project_id = :p AND key = :k"),
        {"p": PROJECT, "k": embedding_slot.SETTING_KEY}).scalar_one()
    assert FAKE_KEY not in stored and FAKE_KEY[:12] not in stored
    if fail:
        assert status["check"]["state"] == "failed" and "401" in status["check"]["error"]
        assert status["search"] is False
    else:
        assert status["check"] == {**status["check"], "state": "ok", "reply": "1536 numbers from text-embedding-3-small"}
        receipt = connection.execute(text(
            "SELECT result FROM receipts WHERE project_id = :p AND effect_kind = 'knowledge.embedding_check'"),
            {"p": PROJECT}).scalar_one()
        assert receipt["usage"]["billing"] == "metered" and receipt["actual_usd_micros"] >= 1


def test_a_wrong_dimension_fails_the_check_and_says_the_right_one(connection, monkeypatch):
    fake = _FakeHosted(False)                               # the model answers 1536 numbers
    monkeypatch.setattr(embedding_adapter.EmbeddingAdapter, "_client", lambda self: fake(self))
    status = _save(connection, provider="openai", model="text-embedding-3-small", dimension=768,
                   api_key=FAKE_KEY, accept_database_fallback=True)
    assert status["check"]["state"] == "failed" and status["search"] is False
    assert "Set the dimension to 1536" in status["check"]["error"]
    with pytest.raises(embedding_slot.EmbeddingSlotError, match="between"):
        _save(connection, dimension=3)


def test_a_hosted_provider_needs_its_key(connection):
    with pytest.raises(embedding_slot.EmbeddingSlotError, match="needs its API key"):
        _save(connection, provider="azure", endpoint="https://example.openai.azure.com",
              deployment="embed", dimension=1536)


def _fact(connection, key: str, body: str) -> str:
    return STORE.upsert_node(connection, project_id=PROJECT, kind="fact", node_key=f"fact:{key}",
                             name=key, body=body, trust_tier="observed", actor_kind="system")


class _Same:
    """engine.begin() on the test's own connection, inside its transaction."""

    def __init__(self, connection):
        self.connection = connection

    def begin(self):
        from contextlib import contextmanager

        @contextmanager
        def nested():
            with self.connection.begin_nested():
                yield self.connection
        return nested()


def _spaces(connection):
    return connection.execute(text(
        "SELECT model_id, dimensions, count(*) FROM kg_embeddings WHERE project_id = :p "
        "GROUP BY model_id, dimensions ORDER BY model_id"), {"p": PROJECT}).all()


def test_a_dimension_change_re_embeds_in_the_background_and_spaces_never_mix(connection):
    _fact(connection, "a", "Invoice totals use bankers rounding")
    _fact(connection, "b", "Release only after a sign-off")
    _save(connection)
    assert embedding_slot.status(connection, get_settings(), PROJECT)["reembed_pending"] == 2
    first = embed_due(_Same(connection), project_id=PROJECT)
    assert first["embedded"] == 2 and first["failed"] == 0
    assert [tuple(r) for r in _spaces(connection)] == [("scripted:scripted-embed:8", 8, 2)]
    assert embedding_slot.status(connection, get_settings(), PROJECT)["reembed_pending"] == 0

    _save(connection, dimension=16)                        # a new space
    assert embedding_slot.status(connection, get_settings(), PROJECT)["reembed_pending"] == 2
    again = embed_due(_Same(connection), project_id=PROJECT)
    assert again["dropped"] == 2 and again["embedded"] == 2
    assert [tuple(r) for r in _spaces(connection)] == [("scripted:scripted-embed:16", 16, 2)]
    found = SERVICE.search(connection, project_id=PROJECT, query="bankers rounding")
    assert found["retrieval"]["mode"] == "hybrid" and found["retrieval"]["lists"]["vector"] >= 1


def test_a_failed_slot_embeds_nothing_and_removing_it_turns_search_off(connection):
    _fact(connection, "a", "Invoice totals use bankers rounding")
    _save(connection, model=embedding_adapter.SCRIPTED_CHAT_ONLY)
    assert embed_due(_Same(connection), project_id=PROJECT)["embedded"] == 0
    _save(connection)
    cleared = embedding_slot.clear(connection, get_settings(), PROJECT, actor="ceo")
    assert cleared["configured"] is False and cleared["reason"] == embedding_slot.NO_MODEL
    assert embedding_slot.read(connection, PROJECT) is None


def test_setup_view_carries_the_slot(connection):
    from boss_engine.services import setup_view

    _save(connection)
    view = setup_view.embedding_model(connection, get_settings(), PROJECT)
    assert view["configured"] and view["check"]["state"] == "ok" and "secret_ref_id" not in view
