"""A credential the console writes and a worker reads must survive a redeploy.

WHAT IT COST TO LEARN THIS. `default_secret_backend` answers "what is the most protected store
available here", and on a containerised install the answer was `local_vault` — AES-GCM files in
a directory inside the container. The compose mounted a volume for artifacts and none for
secrets, so every redeploy deleted the lot.

  * it destroyed the generated SSH host credentials. `DatabaseBackend` was written in response
    and left BEHIND local_vault in the preference order, so nothing used it.
  * on 2026-09-27 it destroyed a customer's Google OAuth client minutes after they pasted it in.
    They pasted it again. It went to the same place, and was reported as unreadable again while
    the console's own summary still read "Set".

Protection and durability are different questions, and a store that silently loses what it was
given protects nothing. Channel credentials now ask the second question.
"""
from boss_engine.config import Settings
from boss_engine.services import secret_backends as sb


class _Backend:
    def __init__(self, kind, ok=True, durable=None):
        self.kind, self._ok, self._durable = kind, ok, durable

    def health(self):
        out = {"ok": self._ok, "kind": self.kind}
        if self._durable is not None:
            out["durable"] = self._durable
        return out


def _with_backends(monkeypatch, mapping):
    def fake(kind, settings):
        if kind not in mapping:
            raise sb.SecretUnavailable(f"{kind} not configured here")
        return mapping[kind]
    monkeypatch.setattr(sb, "backend_for", fake)


def test_a_store_that_does_not_claim_durability_is_not_used(monkeypatch):
    """local_vault reports `writable: true` and never claims `durable`. That is the whole
    difference — it can be written to, and what is written does not survive."""
    _with_backends(monkeypatch, {
        "local_vault": _Backend("local_vault"),          # healthy, says nothing about durability
        "database": _Backend("database", durable=True),
    })
    assert sb.durable_secret_backend(Settings()) == "database"


def test_the_most_protected_store_still_wins_when_it_is_durable(monkeypatch):
    """A managed store is both, and this must not demote it."""
    _with_backends(monkeypatch, {
        "infisical": _Backend("infisical", durable=True),
        "database": _Backend("database", durable=True),
    })
    assert sb.durable_secret_backend(Settings()) == "infisical"


def test_an_unhealthy_durable_store_is_skipped(monkeypatch):
    _with_backends(monkeypatch, {
        "infisical": _Backend("infisical", ok=False, durable=True),
        "database": _Backend("database", durable=True),
    })
    assert sb.durable_secret_backend(Settings()) == "database"


def test_with_nothing_durable_it_falls_back_to_the_database_not_the_vault(monkeypatch):
    """THE LINE THAT MATTERS MOST. The default chooser would land on local_vault here, which is
    exactly how the operator's client was destroyed twice. If nothing claims durability, the
    database is still the only store on a containerised install that survives the container."""
    _with_backends(monkeypatch, {"local_vault": _Backend("local_vault")})
    assert sb.durable_secret_backend(Settings()) == "database"


def test_a_backend_that_raises_is_not_fatal(monkeypatch):
    """Selection runs on every save; one broken optional backend must not break the save."""
    class Exploding:
        kind = "vault"

        def health(self):
            raise RuntimeError("boom")

    _with_backends(monkeypatch, {
        "vault": Exploding(), "database": _Backend("database", durable=True)})
    assert sb.durable_secret_backend(Settings()) == "database"


def test_the_real_managed_stores_say_they_survive_a_redeploy(tmp_path):
    """THE SELECTOR ONLY TRUSTS THE FLAG. The unit above passes a mock that already says
    `durable: True`, and the three stores an operator actually configures never did — their
    health answered `ok` and stopped there. `durable_secret_backend` then skipped a working
    Infisical, Vault, or platform store and wrote the Google client secret and refresh token
    into Postgres. On an install with no separate master key, that key sits in the same
    table, so a database copy is the credential.

    local_vault stays silent on purpose: its files live in the container, and claiming
    otherwise is how the client was destroyed the first two times.
    """
    infisical = sb.InfisicalBackend(
        "https://infisical.invalid", "id", "sec", "proj", token="t")
    infisical.list_keys = lambda: []
    assert infisical.health().get("durable") is True

    platform = sb.PlatformSecretStoreBackend("https://ops.invalid", "token")
    platform.list_keys = lambda: []
    assert platform.health().get("durable") is True

    vault = sb.VaultKvBackend("https://vault.invalid", "token")
    vault._call = lambda *args, **kwargs: {}
    assert vault.health().get("durable") is True

    local = sb.LocalVaultBackend(root=str(tmp_path / "vault"), master_key=b"k" * 32)
    assert local.health().get("ok") is True
    assert local.health().get("durable") is not True


def test_channel_credentials_ask_for_durability_rather_than_protection():
    """Both places channel_oauth stores a secret — the OAuth client and the refresh token."""
    import inspect

    from boss_engine.services import channel_oauth

    source = inspect.getsource(channel_oauth)
    assert source.count("durable_secret_backend(settings)") == 2
    assert "default_secret_backend" not in source, (
        "a channel credential picked the most PROTECTED store; it must pick a durable one")
