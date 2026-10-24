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
  * an embedding call built anywhere but the router;
  * the slot set per project (three projects, three times) instead of once for the org; an
    older install's project row not read, or not promoted to the org with its key;
  * an Azure Target URI that had to be taken apart by hand, and a dimension the operator had to
    know (text-embedding-3-small answers 1536; a guessed 256 failed the check);
  * a new org slot not re-embedding EVERY project of the org.

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
    args = {"provider": "scripted", "model": "scripted-embed", "actor": "ceo"}
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

    def __init__(self, fail: bool, honours_dimensions: bool = False):
        self.fail = fail
        self.honours = honours_dimensions
        self.calls = 0
        self.asked: list = []

    def __call__(self, adapter):
        fake = self

        class _Embeddings:
            def create(self, model, input, **kw):
                fake.calls += 1
                fake.asked.append(kw.get("dimensions"))
                if fake.fail:
                    raise RuntimeError(f"Error code: 401 - Incorrect API key provided: {adapter._api_key[:12]}****. ")
                size = kw.get("dimensions") if fake.honours and kw.get("dimensions") else 1536
                return SimpleNamespace(data=[SimpleNamespace(index=i, embedding=[0.1] * size) for i, _ in enumerate(input)],
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
        "SELECT value::text FROM org_settings WHERE org_id = 'org_em' AND key = :k"),
        {"k": embedding_slot.SETTING_KEY}).scalar_one()
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


def test_a_size_the_model_ignores_fails_the_check_and_says_the_right_one(connection, monkeypatch):
    fake = _FakeHosted(False)                     # answers 1536 numbers, whatever is asked
    monkeypatch.setattr(embedding_adapter.EmbeddingAdapter, "_client", lambda self: fake(self))
    status = _save(connection, provider="openai", model="text-embedding-3-small", dimension=768,
                   api_key=FAKE_KEY, accept_database_fallback=True)
    assert fake.asked == [768], "a size the operator set is asked of the model"
    assert status["check"]["state"] == "failed" and status["search"] is False
    assert "Leave the dimension empty to use the model's own (1536)" in status["check"]["error"]
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


# ── one org, one model ───────────────────────────────────────────────────────────────────────

OTHER = "proj_em_b"
AZURE_URI = ("https://boss-kg.cognitiveservices.azure.com/openai/deployments/text-embedding-3-small/"
             "embeddings?api-version=2023-05-15")


@pytest.fixture()
def two(connection):
    connection.execute(text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, 'ws_em', 'EM B', 'em-b')"),
                       {"p": OTHER})
    return connection


def test_the_azure_target_uri_is_taken_apart_and_old_api_versions_replaced():
    got = embedding_slot.parse_url("azure", AZURE_URI)
    assert got == {"endpoint": "https://boss-kg.cognitiveservices.azure.com",
                   "deployment": "text-embedding-3-small", "api_version": "2024-10-21"}
    for base in ("https://boss-kg.openai.azure.com/", "https://boss-kg.cognitiveservices.azure.com/",
                 "https://boss-kg.openai.azure.com"):
        parsed = embedding_slot.parse_url("azure", base)
        assert parsed == {"endpoint": base.rstrip("/"), "api_version": "2024-10-21"}, base
    newer = embedding_slot.parse_url("azure", AZURE_URI.replace("2023-05-15", "2024-06-01"))
    assert newer["api_version"] == "2024-06-01"
    assert embedding_slot.parse_url("openai", "https://api.openai.com/v1/embeddings") == {
        "base_url": "https://api.openai.com/v1"}
    assert embedding_slot.parse_url("ollama", "http://localhost:11434/v1/embeddings") == {
        "base_url": "http://localhost:11434"}
    clean = embedding_slot._validate("azure", model="", dimension=None, base_url="", endpoint="",
                                     deployment="", api_version="", url=AZURE_URI)
    assert (clean["endpoint"], clean["deployment"], clean["api_version"], clean["dimension_set"]) == (
        "https://boss-kg.cognitiveservices.azure.com", "text-embedding-3-small", "2024-10-21", False)
    # A deployment typed into the form wins over the URL's; a base URL needs one.
    typed = embedding_slot._validate("azure", model="", dimension=None, base_url="", endpoint="",
                                     deployment="embed-large", api_version="", url=AZURE_URI)
    assert typed["deployment"] == "embed-large"
    with pytest.raises(embedding_slot.EmbeddingSlotError, match="deployment"):
        embedding_slot._validate("azure", model="", dimension=None, base_url="", endpoint="",
                                 deployment="", api_version="", url="https://boss-kg.openai.azure.com/")


def test_one_save_serves_every_project_of_the_org_with_one_org_key(two, monkeypatch):
    fake = _FakeHosted(False)
    monkeypatch.setattr(embedding_adapter.EmbeddingAdapter, "_client", lambda self: fake(self))
    saved = _save(two, provider="azure", url=AZURE_URI, api_key=FAKE_KEY, accept_database_fallback=True)
    assert saved["check"]["state"] == "ok" and saved["scope"] == "org" and saved["projects"] == 2
    # THE DIMENSION IS LEARNED from the model's reply, not asked: nothing asked of the model.
    assert saved["dimension"] == 1536 and saved["dimension_set"] is False and fake.asked == [None]
    assert saved["deployment"] == "text-embedding-3-small" and saved["api_version"] == "2024-10-21"
    other = embedding_slot.status(two, get_settings(), OTHER)
    assert other["configured"] and other["search"] and other["model_id"] == saved["model_id"]
    assert two.execute(text("SELECT count(*) FROM project_settings WHERE key = :k"),
                       {"k": embedding_slot.SETTING_KEY}).scalar_one() == 0
    ref = two.execute(text(
        "SELECT scope_type, scope_id, provider, metadata FROM secret_refs WHERE status = 'active' "
        "AND id = :id"), {"id": embedding_slot.read(two, OTHER)["secret_ref_id"]}).mappings().one()
    assert (ref["scope_type"], ref["scope_id"], ref["provider"]) == ("org", "org_em", "azure")
    assert ref["metadata"]["allowed_hosts"] == ["boss-kg.cognitiveservices.azure.com"]
    # The other project reaches the ORG key and embeds with it.
    rechecked = embedding_slot.check(two, get_settings(), OTHER, actor="ceo")
    assert rechecked["check"]["state"] == "ok" and fake.calls == 2


def test_a_size_the_operator_sets_is_asked_of_the_model(connection, monkeypatch):
    fake = _FakeHosted(False, honours_dimensions=True)
    monkeypatch.setattr(embedding_adapter.EmbeddingAdapter, "_client", lambda self: fake(self))
    status = _save(connection, provider="openai", model="text-embedding-3-small", dimension=256,
                   api_key=FAKE_KEY, accept_database_fallback=True)
    assert fake.asked == [256] and status["check"]["state"] == "ok"
    assert status["dimension"] == 256 and status["dimension_set"] is True
    assert status["model_id"] == "openai:text-embedding-3-small:256"


def _legacy(connection, project_id: str, slot: dict) -> None:
    connection.execute(text(
        "INSERT INTO project_settings (project_id, key, value) VALUES (:p, :k, CAST(:v AS JSONB))"),
        {"p": project_id, "k": embedding_slot.SETTING_KEY, "v": json.dumps(slot)})


def test_an_older_installs_project_row_is_read_then_promoted_to_the_org_with_its_key(two, monkeypatch):
    from boss_engine.services.secrets_service import put_secret

    fake = _FakeHosted(False)
    monkeypatch.setattr(embedding_adapter.EmbeddingAdapter, "_client", lambda self: fake(self))
    old = put_secret(two, get_settings(), project_id=PROJECT, name=embedding_slot.SECRET_NAME,
                     value=FAKE_KEY, backend="database", actor="ceo")["secret_ref_id"]
    # The operator's guessed 256, which text-embedding-3-small never answered with.
    _legacy(two, PROJECT, {"provider": "openai", "model": "text-embedding-3-small", "dimension": 256,
                           "base_url": "", "secret_ref_id": old,
                           "check": {"state": "failed", "error": "answered with 1536"}})
    seen = embedding_slot.status(two, get_settings(), PROJECT)
    assert seen["configured"] and seen["scope"] == "project" and seen["has_key"]
    assert embedding_slot.status(two, get_settings(), OTHER)["configured"] is False

    promoted = embedding_slot.check(two, get_settings(), PROJECT, actor="ceo")
    assert promoted["scope"] == "org" and promoted["check"]["state"] == "ok"
    assert promoted["dimension"] == 1536, "the size is learned, so the old guess no longer fails"
    assert embedding_slot.status(two, get_settings(), OTHER)["search"] is True
    assert embedding_slot._read_project(two, PROJECT) is None
    refs = {r["id"]: (r["scope_type"], r["status"]) for r in two.execute(text(
        "SELECT id, scope_type, status FROM secret_refs WHERE project_id = :p"), {"p": PROJECT}).mappings()}
    assert refs[old] == ("project", "revoked")
    new = embedding_slot.read(two, OTHER)["secret_ref_id"]
    assert refs[new] == ("org", "active")


def test_saving_the_org_slot_removes_every_project_row_of_the_org(two):
    _legacy(two, OTHER, {"provider": "scripted", "model": "scripted-embed", "dimension": 8,
                         "check": {"state": "ok"}})
    _save(two)
    assert embedding_slot._read_project(two, OTHER) is None
    assert embedding_slot.read(two, OTHER)["scope"] == "org"
    cleared = embedding_slot.clear(two, get_settings(), OTHER, actor="ceo")
    assert cleared["configured"] is False
    assert embedding_slot.read(two, PROJECT) is None


def test_a_new_org_slot_re_embeds_every_project_of_the_org(two):
    for pid in (PROJECT, OTHER):
        STORE.upsert_node(two, project_id=pid, kind="fact", node_key="fact:a", name="a",
                          body="Invoice totals use bankers rounding", trust_tier="observed", actor_kind="system")
    _save(two)
    first = embed_due(_Same(two))
    assert first["projects"] >= 2 and first["embedded"] >= 2
    _save(two, dimension=16)
    again = embed_due(_Same(two))
    assert again["dropped"] >= 2 and again["embedded"] >= 2
    for pid in (PROJECT, OTHER):
        rows = two.execute(text("SELECT DISTINCT model_id FROM kg_embeddings WHERE project_id = :p"),
                           {"p": pid}).scalars().all()
        assert rows == ["scripted:scripted-embed:16"], (pid, rows)


def test_changing_it_inside_a_request_needs_the_org(connection, monkeypatch):
    from boss_engine.identity import lineage

    monkeypatch.setattr(lineage, "request_enforced", lambda: True)
    monkeypatch.setattr(lineage, "current_context", lambda: None)
    with pytest.raises(embedding_slot.EmbeddingSlotForbidden, match="whole organisation"):
        _save(connection)
