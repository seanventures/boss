"""Community audit export must work without the Enterprise cluster table."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from boss_engine.services.audit import AuditTrailService


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
