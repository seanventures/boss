"""A connection proof that names a host variable (an env reference) is created through the ONE
checked path, which needs integration:manage at the org (rc6 N-1). Without it the person is told
plainly what is needed — never a raw exception (BOSSAPP-1286)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_an_env_ref_without_org_authority_is_refused_in_plain_words(monkeypatch):
    from boss_engine.services import connection_spend, entitlements, secret_refs
    from boss_engine.services.env_refs import HostCredentialAuthorityRequired

    def refuse(*a, **k):
        raise HostCredentialAuthorityRequired("needs integration:manage at the org")

    monkeypatch.setattr(secret_refs.SecretRefService, "create_secret_ref", refuse)
    monkeypatch.setattr(entitlements.EntitlementBrokerService, "upsert_source",
                        lambda self, c, record, actor=None: None)
    spend = connection_spend.ConnectionSpend.__new__(connection_spend.ConnectionSpend)
    spend.project_id, spend.actor, spend.capacity_group = "p_1", "usr_admin", "g"
    spend.candidate = {"provider": "github", "credential_env_name": "BOSS_GITHUB_TOKEN"}
    monkeypatch.setattr(spend, "_existing", lambda *a, **k: None, raising=False)
    db = SimpleNamespace(execute=lambda *a, **k: None)
    with pytest.raises(connection_spend.TypedDecisionSpendError) as refused:
        from boss_engine.config import get_settings

        spend._prepare(db, get_settings(), None, None, "identity", None)  # noqa: SLF001
    assert "integration:manage at the org" in str(refused.value)
    assert "BOSS_GITHUB_TOKEN" in str(refused.value)
