"""Community audit export must work without the Enterprise cluster table."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from boss_engine.services.audit import AuditTrailService
from boss_engine.services.enterprise_audit import EnterpriseAuditTrailService


class _Rows:
    def __init__(self, row):
        self.row = row

    def mappings(self):
        return self

    def first(self):
        return self.row


class _PaidConnection:
    def __init__(self, row):
        self.row = row
        self.statements = []

    def execute(self, statement, params):
        self.statements.append(str(statement))
        assert params == {"project_id": "project-1"}
        return _Rows(self.row)


class _NoEnterpriseTable:
    def execute(self, statement, params):
        raise AssertionError(f"Community audit touched an Enterprise table: {statement}")


def test_community_governance_does_not_query_omitted_enterprise_table():
    governance = AuditTrailService(None)._audit_governance(
        _NoEnterpriseTable(), "project-1"
    )
    assert governance["edition_scope"] == "community"
    assert governance["export_allowed"] is True


def test_importing_community_audit_does_not_import_paid_extension():
    engine_dir = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(engine_dir)}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from boss_engine.services.audit import AuditTrailService; "
            "assert 'boss_engine.services.enterprise_audit' not in sys.modules",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_paid_audit_controls_still_deny_and_expose_retention():
    connection = _PaidConnection(
        {
            "id": "cluster-1",
            "deployment_mode": "self_hosted",
            "account_connect_mode": "detached",
            "status": "active",
            "audit_controls": {
                "audit_exports_enabled": False,
                "retention_days": 365,
                "audit_export_mode": "scheduled",
            },
            "metadata": {"blocking_gaps": ["audit_exports_disabled"]},
        }
    )
    governance = EnterpriseAuditTrailService(None)._audit_governance(
        connection, "project-1"
    )
    assert len(connection.statements) == 1
    assert "FROM enterprise_clusters" in connection.statements[0]
    assert governance["edition_scope"] == "enterprise"
    assert governance["export_allowed"] is False
    assert governance["retention_days"] == 365
    assert governance["blocking_gaps"] == ["audit_exports_disabled"]


def test_paid_audit_without_cluster_preserves_existing_scope():
    connection = _PaidConnection(None)
    governance = EnterpriseAuditTrailService(None)._audit_governance(
        connection, "project-1"
    )
    assert governance["edition_scope"] == "community_or_unconfigured_enterprise"
    assert governance["export_allowed"] is True
