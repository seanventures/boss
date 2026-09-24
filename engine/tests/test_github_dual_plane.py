"""GitHub dual-plane honesty: factory prefers MC primary binding when present (P0-6)."""

from __future__ import annotations

from unittest.mock import MagicMock

from boss_engine.config import Settings
from boss_engine.services.github_adapter import GitHubAdapter
from boss_engine.services.software_factory import SoftwareFactoryService


def _settings() -> Settings:
    return Settings(
        BOSS_TARGET_GITHUB_OWNER="default-owner",
        BOSS_TARGET_GITHUB_REPO="default-repo",
        BOSS_GITHUB_TOKEN="legacy-token",
    )


def test_factory_defaults_to_legacy_env_plane():
    sf = SoftwareFactoryService(_settings(), owner="acme", repo="app")
    assert sf.github_plane == "legacy_env"
    assert sf.github_binding_id is None
    assert sf._github_plane_fields()["github_plane"] == "legacy_env"


def test_bind_project_github_legacy_when_no_registry(monkeypatch):
    sf = SoftwareFactoryService(_settings(), owner="acme", repo="app")

    def fake_resolve(*_a, **_k):
        return {
            "owner": "acme",
            "repo": "app",
            "full_name": "acme/app",
            "binding_id": None,
            "connection_id": None,
            "default_branch": "main",
            "role": None,
            "source": "legacy_settings",
        }

    monkeypatch.setattr(
        "boss_engine.services.repo_registry.resolve_repository_target",
        fake_resolve,
    )
    plane = sf.bind_project_github(MagicMock(), "boss-dogfood", owner="acme", repo="app")
    assert plane["github_plane"] == "legacy_env"
    assert sf.github_plane == "legacy_env"
    assert sf.github.repository_full_name == "acme/app"


def test_bind_project_github_prefers_primary_binding(monkeypatch):
    sf = SoftwareFactoryService(_settings(), owner="default-owner", repo="default-repo")

    def fake_resolve(*_a, **_k):
        return {
            "owner": "example-org",
            "repo": "boss",
            "full_name": "example-org/boss",
            "binding_id": "bind_primary_1",
            "connection_id": "conn_1",
            "default_branch": "main",
            "role": "primary",
            "source": "registry:primary",
        }

    class FakeConnService:
        def __init__(self, *_a, **_k):
            pass

        def adapter_for_binding(self, connection, *, project_id, binding_id):
            adapter = GitHubAdapter(
                _settings(), owner="example-org", repo="boss", token="mc-token"
            )
            binding = {
                "id": binding_id,
                "owner": "example-org",
                "name": "boss",
                "full_name": "example-org/boss",
            }
            connection_row = {"id": "conn_1", "status": "active"}
            return adapter, connection_row, binding

    monkeypatch.setattr(
        "boss_engine.services.repo_registry.resolve_repository_target",
        fake_resolve,
    )
    monkeypatch.setattr(
        "boss_engine.services.github_connections.GitHubConnectionService",
        FakeConnService,
    )

    plane = sf.bind_project_github(MagicMock(), "boss-dogfood")
    assert plane["github_plane"] == "mission_control_binding"
    assert plane["github_binding_id"] == "bind_primary_1"
    assert plane["github_connection_id"] == "conn_1"
    assert sf.github_plane == "mission_control_binding"
    assert sf.github.repository_full_name == "example-org/boss"
    assert sf.github._token_override == "mc-token"


def test_bind_project_github_falls_back_honestly_when_binding_unusable(monkeypatch):
    sf = SoftwareFactoryService(_settings(), owner="acme", repo="app")

    def fake_resolve(*_a, **_k):
        return {
            "owner": "example-org",
            "repo": "boss",
            "full_name": "example-org/boss",
            "binding_id": "bind_broken",
            "connection_id": "conn_broken",
            "default_branch": "main",
            "role": "primary",
            "source": "registry:primary",
        }

    class BrokenConnService:
        def __init__(self, *_a, **_k):
            pass

        def adapter_for_binding(self, *_a, **_k):
            raise RuntimeError("SecretRef could not be resolved")

    monkeypatch.setattr(
        "boss_engine.services.repo_registry.resolve_repository_target",
        fake_resolve,
    )
    monkeypatch.setattr(
        "boss_engine.services.github_connections.GitHubConnectionService",
        BrokenConnService,
    )

    plane = sf.bind_project_github(MagicMock(), "boss-dogfood", owner="acme", repo="app")
    assert plane["github_plane"] == "legacy_env"
    assert plane["source"] == "binding_fallback_legacy_env"
    assert plane["github_binding_id"] == "bind_broken"
    assert sf.github_plane == "legacy_env"


def test_describe_github_plane_is_read_only_and_prefers_binding(monkeypatch):
    sf = SoftwareFactoryService(_settings(), owner="default-owner", repo="default-repo")
    original_repo = sf.github.repository_full_name

    def fake_resolve(*_a, **_k):
        return {
            "owner": "example-org",
            "repo": "boss",
            "full_name": "example-org/boss",
            "binding_id": "bind_primary_1",
            "connection_id": "conn_1",
            "default_branch": "main",
            "role": "primary",
            "source": "registry:primary",
        }

    monkeypatch.setattr(
        "boss_engine.services.repo_registry.resolve_repository_target",
        fake_resolve,
    )
    plane = sf.describe_github_plane(MagicMock(), "boss-dogfood")
    assert plane["github_plane"] == "mission_control_binding"
    assert plane["github_binding_id"] == "bind_primary_1"
    assert plane["github_connection_id"] == "conn_1"
    assert plane["full_name"] == "example-org/boss"
    # GET-safe: adapters stay on the legacy settings plane.
    assert sf.github_plane == "legacy_env"
    assert sf.github.repository_full_name == original_repo


def test_describe_github_plane_legacy_when_no_binding(monkeypatch):
    sf = SoftwareFactoryService(_settings(), owner="acme", repo="app")

    def fake_resolve(*_a, **_k):
        return {
            "owner": "acme",
            "repo": "app",
            "full_name": "acme/app",
            "binding_id": None,
            "connection_id": None,
            "source": "legacy_settings",
        }

    monkeypatch.setattr(
        "boss_engine.services.repo_registry.resolve_repository_target",
        fake_resolve,
    )
    plane = sf.describe_github_plane(MagicMock(), "boss-dogfood")
    assert plane["github_plane"] == "legacy_env"
    assert plane["source"] == "legacy_settings"
    assert plane["github_binding_id"] is None


def test_propose_pull_request_surfaces_github_plane(monkeypatch):
    """propose_pull_request attaches github_plane even on early blocked paths."""
    sf = SoftwareFactoryService(_settings(), owner="acme", repo="app")

    def fake_bind(self, connection, project_id, **kwargs):
        self.github_plane = "mission_control_binding"
        self.github_binding_id = "bind_x"
        self.github_connection_id = "conn_x"
        return {
            "github_plane": "mission_control_binding",
            "github_binding_id": "bind_x",
            "github_connection_id": "conn_x",
        }

    monkeypatch.setattr(SoftwareFactoryService, "bind_project_github", fake_bind)

    class Budget:
        def assert_active_reservation(self, *_a, **_k):
            return None  # missing reservation → blocked

    class Decisions:
        def create_blocking(self, *_a, **_k):
            return "dec_blocked_1"

    sf.budget = Budget()
    sf.decisions = Decisions()

    result = sf.propose_pull_request(
        MagicMock(),
        "boss-dogfood",
        None,
        "res_missing",
        "dedupe:test",
        "Title",
        "boss/test",
        "summary",
        [],
    )
    assert result["status"] == "blocked"
    assert result["github_plane"] == "mission_control_binding"
    assert result["github_binding_id"] == "bind_x"
    assert result["github_connection_id"] == "conn_x"
