from pathlib import Path

from boss_engine.config import Settings


def test_relocated_engine_resolves_monorepo_assets():
    settings = Settings()
    expected_root = Path(__file__).resolve().parents[2]

    assert settings.repo_root == expected_root
    assert settings.schema_path == expected_root / "db" / "schema.sql"
    assert settings.schema_path.is_file()
    assert settings.specpack_root == expected_root / "packages" / "specpack"
    assert settings.specpack_root.is_dir()


def test_specpack_root_env_override(tmp_path, monkeypatch):
    # Standalone/pip installs point at a spec pack directly (Plane #299).
    monkeypatch.setenv("BOSS_SPECPACK_ROOT", str(tmp_path))
    settings = Settings()
    assert settings.specpack_root == tmp_path.resolve()


def test_the_platform_secret_store_can_actually_be_selected(monkeypatch):
    """The most protected backend was unreachable, and failed as "not configured".

    `backend_for("platform_secret_store")` reads settings.platform_secret_store_url, but
    Settings defined no such field and ignores unknown env vars. So the attribute was always
    absent, the backend always raised SecretUnavailable, and `default_secret_backend` — which
    tries it FIRST — silently fell through to a lesser store on every installation. The
    operator's evidence was an error saying the store was not configured, right after they
    configured it.
    """
    from boss_engine.services.secret_backends import backend_for

    monkeypatch.setenv("BOSS_PLATFORM_SECRET_STORE_URL", "https://ops.example/api")
    monkeypatch.setenv("BOSS_PLATFORM_SECRET_STORE_TOKEN", "ops-token-value")
    settings = Settings()

    assert settings.platform_secret_store_url == "https://ops.example/api"
    assert backend_for("platform_secret_store", settings).kind == "platform_secret_store"


def test_the_store_token_can_stay_out_of_the_environment(tmp_path, monkeypatch):
    """A token in a file is not in env, so nothing that dumps env carries it along."""
    from boss_engine.services.secret_backends import backend_for

    token_file = tmp_path / "ops-token"
    token_file.write_text("ops-token-value\n")
    monkeypatch.setenv("BOSS_PLATFORM_SECRET_STORE_URL", "https://ops.example/api")
    monkeypatch.delenv("BOSS_PLATFORM_SECRET_STORE_TOKEN", raising=False)
    monkeypatch.setenv("BOSS_PLATFORM_SECRET_STORE_TOKEN_FILE", str(token_file))

    assert backend_for("platform_secret_store", Settings()).kind == "platform_secret_store"
