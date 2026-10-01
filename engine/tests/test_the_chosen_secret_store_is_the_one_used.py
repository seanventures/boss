"""The secret store chosen at the installer's prompt is the one BOSS uses.

The installer wrote the answer to BOSS_COMMUNITY_SECRET_STORE and nothing read it: choosing
"database" on a machine where Infisical was still running stored the next credential in
Infisical anyway, because the default takes "the most protected store that answers"
(reported 2026-09-30: "I said database, why is it storing in Infisical?").
"""
from __future__ import annotations

from boss_engine.config import Settings
from boss_engine.services import secret_backends as sb


class _Healthy:
    def health(self):
        return {"ok": True, "durable": True}


def _everything_answers(monkeypatch):
    monkeypatch.setattr(sb, "backend_for", lambda kind, settings: _Healthy())


def test_database_chosen_means_database_even_when_infisical_answers(monkeypatch):
    _everything_answers(monkeypatch)
    monkeypatch.setenv("BOSS_COMMUNITY_SECRET_STORE", "database")
    assert sb.default_secret_backend(Settings()) == "database"
    assert sb.durable_secret_backend(Settings()) == "database"


def test_infisical_chosen_still_prefers_infisical(monkeypatch):
    _everything_answers(monkeypatch)
    monkeypatch.setenv("BOSS_COMMUNITY_SECRET_STORE", "infisical")
    assert sb.default_secret_backend(Settings()) == "infisical"


def test_no_choice_keeps_the_old_preference(monkeypatch):
    _everything_answers(monkeypatch)
    monkeypatch.delenv("BOSS_COMMUNITY_SECRET_STORE", raising=False)
    assert sb.default_secret_backend(Settings()) == "infisical"


def test_the_key_is_external_only_when_the_environment_holds_it(monkeypatch):
    for name in ("BOSS_KMS_MASTER_KEY", "BOSS_KMS_MASTER_KEY_FILE", "BOSS_KMS_ALLOW_EPHEMERAL_MASTER"):
        monkeypatch.delenv(name, raising=False)
    assert sb.database_key_is_external() is False
    monkeypatch.setenv("BOSS_KMS_MASTER_KEY", "a" * 44)
    assert sb.database_key_is_external() is True


def test_choosing_the_database_is_not_asked_again_when_a_codex_login_is_stored(monkeypatch):
    """The 'store it in the database deliberately?' question is for when BOSS FELL BACK to the
    database. An operator who chose it at the installer prompt has already answered."""
    import json

    from boss_engine.services.setup_service import store_coding_agent_credential

    monkeypatch.setenv("BOSS_COMMUNITY_SECRET_STORE", "database")
    stored = {}
    monkeypatch.setattr(
        "boss_engine.services.secrets_service.put_secret",
        lambda *a, **k: stored.update(k) or {"secret_ref_id": "s", "ref_key": "k",
                                            "backend": k["backend"], "fingerprint": "f"})
    monkeypatch.setattr("boss_engine.services.core.emit_event", lambda *a, **k: "evt")

    class _C:
        def execute(self, *a, **k):
            return self

    result = store_coding_agent_credential(
        _C(), Settings(), "boss-dogfood", "codex", json.dumps({"tokens": {"a": "b"}}), actor="ceo")
    assert result["backend"] == "database" and stored["backend"] == "database"
