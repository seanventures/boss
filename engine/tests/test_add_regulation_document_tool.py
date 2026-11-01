"""KG P7a: the agent tools for setting up a regulation case (add_regulation_document,
set_ontology_pack), exercised through the real registry.invoke path — not a bare call into
services/knowledge/documents.py — with only the identity decision (authz.check) faked, exactly
as test_an_agent_reaches_every_project_it_may.py does for a project-scoped tool.

Breaks covered: the tool inventing clause text instead of refusing when none was given; the
pack-change tool being pressable by the model itself rather than only offered to an operator.
"""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from boss_engine.context import EngineContext
from boss_engine.identity import authz
from boss_engine.identity.authz import Decision, DenyReason
from boss_engine.identity.principal import AuthContext, AuthMethod, Principal, PrincipalKind, PrincipalStatus
from boss_engine.tools import build_default_registry

ALICE = AuthContext(Principal("usr_reg", PrincipalKind.USER, "org_reg", PrincipalStatus.ACTIVE, "A"),
                    AuthMethod.SESSION)


def _database_url() -> str:
    return os.environ.get("DATABASE_URL") or ""


@pytest.fixture(scope="module")
def pg_engine():
    url = _database_url()
    if not url:
        pytest.skip("DATABASE_URL must be set to a reachable postgres for these tests")
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        pytest.skip(f"postgres is not reachable: {exc.__class__.__name__}")
    return engine


@pytest.fixture()
def project(pg_engine):
    project_id = f"proj_reg_{uuid.uuid4().hex[:10]}"
    with pg_engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO orgs (id, name) VALUES (:o, 'Reg Org') ON CONFLICT DO NOTHING"),
            {"o": "org_reg_doc"})
        connection.execute(text(
            "INSERT INTO workspaces (id, org_id, name) VALUES (:w, :o, 'Reg Workspace') "
            "ON CONFLICT DO NOTHING"), {"w": "ws_reg_doc", "o": "org_reg_doc"})
        connection.execute(text(
            "INSERT INTO projects (id, workspace_id, name, slug) VALUES (:p, :w, :p, :p)"),
            {"p": project_id, "w": "ws_reg_doc"})
    try:
        yield project_id
    finally:
        with pg_engine.begin() as connection:
            # Setting the banking-regulatory pack installs the workflow it unlocks
            # (workflow_catalogue.seed_unlocked_by_pack); those rows go first.
            for table in ("workflow_versions", "workflow_definitions", "events"):
                connection.execute(text(f"DELETE FROM {table} WHERE project_id = :p"),
                                   {"p": project_id})
            connection.execute(text("DELETE FROM projects WHERE id = :p"), {"p": project_id})


@pytest.fixture(autouse=True)
def allow(monkeypatch, project):
    def check(ctx, permission, scope, **_):
        if scope.label() == f"project:{project}":
            return Decision.allow(permission, scope)
        return Decision.deny(permission, scope, DenyReason.MISSING_PERMISSION)
    monkeypatch.setattr(authz, "check", check)


def test_set_ontology_pack_is_never_pressed_by_the_model_itself():
    # P1 -> criterion 4 (operator_only), via the registry invariant the write gate reads
    reg = build_default_registry()
    spec = reg.get("set_ontology_pack")
    assert spec.operator_only is True, "set_ontology_pack must be operator_only"
    assert spec.self_executable is False, (
        "an operator_only tool must never be self_executable: the model proposes it, a person presses it")


def test_add_regulation_document_adds_through_the_real_registry_path(project, monkeypatch):
    # P1 -> criterion 4
    monkeypatch.setenv("DATABASE_URL", _database_url())
    ctx = EngineContext.create(ensure_schema=True)
    reg = build_default_registry()

    pack = reg.invoke("set_ontology_pack", ctx, project_id=project, actor="ceo",
                      args={"pack_id": "banking-regulatory"}, auth=ALICE, operator_confirmed=True)
    assert pack.get("ok") is True, pack

    out = reg.invoke("add_regulation_document", ctx, project_id=project, actor="ceo", args={
        "title": "Reg Z", "source_url": "https://example.test/regz", "citation": "12 CFR 1026",
        "clauses": [{"section": "1026.36(c)(1)(i)",
                    "text": "A loan originator shall not finance, directly or indirectly."}],
    }, auth=ALICE)

    assert out.get("ok") is True, out
    assert out.get("document_id") and len(out.get("clause_ids") or []) == 1


def test_add_regulation_document_refuses_empty_clauses_instead_of_inventing_text(project, monkeypatch):
    # P1 -> criterion 4
    monkeypatch.setenv("DATABASE_URL", _database_url())
    ctx = EngineContext.create(ensure_schema=True)
    reg = build_default_registry()

    reg.invoke("set_ontology_pack", ctx, project_id=project, actor="ceo",
              args={"pack_id": "banking-regulatory"}, auth=ALICE, operator_confirmed=True)

    out = reg.invoke("add_regulation_document", ctx, project_id=project, actor="ceo", args={
        "title": "Reg Z", "source_url": "https://example.test/regz", "citation": "12 CFR 1026",
        "clauses": [],
    }, auth=ALICE)

    assert out.get("ok") is False, (
        "an empty clause list must be refused in words, never filled in by generating clause text: " + repr(out))
