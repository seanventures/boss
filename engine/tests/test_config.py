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
