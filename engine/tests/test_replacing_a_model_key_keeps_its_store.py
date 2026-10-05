"""Replacing a model key keeps it in the store it already lives in (operator decision 2026-10-04).

Repo and models' Capacity row lets a first-time key go to a chosen secret backend (Vault, the
platform store, a local runner). The Model row's `Replace` asks no such question — so it must
not quietly move a Vault key into the default store either. Replace reads the backend of the key
it replaces; only a source with no key yet falls back to the default.
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text

from ._obs_seed import pg_engine, project
from boss_engine.services.setup_actions import _backend_it_lives_in


def test_replace_reads_the_backend_the_key_lives_in():
    eng = pg_engine()
    with eng.begin() as c:
        pid = project(c)
        ref = f"sr_{uuid4().hex[:10]}"
        c.execute(text(
            "INSERT INTO secret_refs (id, project_id, scope_id, ref_kind, ref_key, storage_backend, created_by) "
            "VALUES (:id, :p, :p, 'vault', 'model_x', 'vault', 'test')"), {"id": ref, "p": pid})
        for sid, cred in (("anthropic_api", ref), ("openai_api", None)):
            c.execute(text(
                "INSERT INTO entitlement_sources (id, project_id, provider, source_id, source_kind, "
                " entitlement_kind, owner_type, owner_id, credential_secret_ref_id) "
                "VALUES (:id, :p, 'x', :s, 'api', 'api_key', 'project', :p, :r)"),
                {"id": f"es_{uuid4().hex[:10]}", "p": pid, "s": sid, "r": cred})
        assert _backend_it_lives_in(c, pid, "anthropic_api") == "vault"
        assert _backend_it_lives_in(c, pid, "openai_api") is None, "no key yet: the default store"
        assert _backend_it_lives_in(c, pid, "missing") is None
