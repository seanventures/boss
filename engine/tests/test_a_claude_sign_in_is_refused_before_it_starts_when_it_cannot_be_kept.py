"""BOSSAPP-897: with the database store's key inside Postgres, Setup refused to store a Claude
token only AFTER the person signed in on claude.ai, wasting the sign-in and its one-time code (and
the retry then reported the code as refused). The sign-in now refuses before it starts."""
from boss_engine.services import subscription_sign_in as ssi


def test_no_sign_in_starts_when_the_token_could_not_be_stored(monkeypatch):
    from boss_engine.services import secret_backends
    monkeypatch.setattr(secret_backends, "default_secret_backend", lambda settings=None: "database")
    monkeypatch.setattr(secret_backends, "database_key_is_external", lambda: False)
    started = []
    monkeypatch.setattr(ssi.shutil, "which", lambda cmd: started.append(cmd) or "/usr/bin/claude")
    out = ssi.start("claude_code", project_id="p", actor="ceo", settings=object(), fresh=True,
                    signed_in=lambda: False, store=lambda *a, **k: None)
    assert out["state"] == "failed"
    said = f"{out['headline']} {out['fix']}"
    assert "BOSS_KMS_MASTER_KEY" in said and "Nothing was signed in" in said
    assert started == []                       # the CLI was never looked up, let alone run


def test_with_the_key_outside_the_database_nothing_is_refused(monkeypatch):
    from boss_engine.services import secret_backends
    monkeypatch.setattr(secret_backends, "default_secret_backend", lambda settings=None: "database")
    monkeypatch.setattr(secret_backends, "database_key_is_external", lambda: True)
    assert ssi._cannot_keep_token("claude_code", object()) is None
    assert ssi._cannot_keep_token("codex", object()) is None
