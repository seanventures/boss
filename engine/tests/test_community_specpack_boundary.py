"""Agent module entitlement checks use the shared edition contract."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from boss_engine.config import Settings
from boss_engine.services.specpack import SpecPackService


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


class _LocalEntitlements:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    def execute(self, statement, params):
        query = str(statement)
        self.statements.append(query)
        assert params == {"project_id": "project-1"}
        if "FROM account_connect_orgs" in query:
            return _Rows([])
        assert "FROM license_entitlements" in query
        return _Rows(self.rows)


class _Edition:
    def __init__(self, *, capabilities, active_org_id=None):
        self.capabilities = capabilities
        self.active_org_id = active_org_id

    def edition_status(self, connection, project_id):
        assert project_id == "project-1"
        return {
            "status": "active",
            "included_capabilities": self.capabilities,
            "account_connect_org": {"hosted_org_id": self.active_org_id}
            if self.active_org_id
            else None,
        }


def test_community_pack_and_local_entitlements_do_not_use_paid_tables():
    connection = _LocalEntitlements(
        [
            {
                "capability_key": "local_custom",
                "capabilities": ["local_extra"],
                "source": "local",
                "account_connect_org_id": None,
            },
            {
                "capability_key": "stale_hosted",
                "capabilities": ["hosted_extra"],
                "source": "account_connect_hosted",
                "account_connect_org_id": "unlinked-org",
            },
        ]
    )
    settings = Settings(
        BOSS_EDITION="community",
        BOSS_SPECPACK_ROOT=str(
            Path(__file__).resolve().parents[2] / "packages" / "specpack"
        ),
    )
    service = SpecPackService(settings)
    capabilities = service._project_capabilities(connection, "project-1")
    assert {"local_custom", "local_extra"} <= capabilities
    assert "stale_hosted" not in capabilities
    assert len(connection.statements) == 2
    assert any("FROM license_entitlements" in sql for sql in connection.statements)


def test_paid_edition_service_can_be_injected_without_changing_entitlement_rules():
    connection = _LocalEntitlements(
        [
            {
                "capability_key": "private_agent_marketplace",
                "capabilities": ["paid_tools"],
                "source": "account_connect_hosted",
                "account_connect_org_id": "active-org",
            }
        ]
    )
    service = SpecPackService(
        None,
        edition_service=_Edition(
            capabilities=["community_agents", "enterprise_agents"],
            active_org_id="active-org",
        ),
    )
    assert service._project_capabilities(connection, "project-1") == {
        "community_agents",
        "enterprise_agents",
        "private_agent_marketplace",
        "paid_tools",
    }


def test_importing_specpack_does_not_import_commercial_service():
    engine_dir = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from boss_engine.services.specpack import SpecPackService; "
            "assert 'boss_engine.services.commercial' not in sys.modules",
        ],
        env={**os.environ, "PYTHONPATH": str(engine_dir)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
