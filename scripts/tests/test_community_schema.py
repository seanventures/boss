"""Boundary checks for the reviewed Community database bootstrap artifact."""

from __future__ import annotations

import importlib.util
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "build_community_schema", ROOT / "scripts/build_community_schema.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CommunitySchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = (ROOT / "db/schema.sql").read_bytes()
        self.community = (ROOT / "db/schema.community.sql").read_text(encoding="utf-8")

    def test_generated_schema_is_reviewed_and_current(self) -> None:
        self.assertEqual(self.community, MODULE.render(self.source))
        with self.assertRaisesRegex(ValueError, "source schema changed"):
            MODULE.render(self.source + b"-- new migration\n")

    def test_only_paid_tables_are_removed(self) -> None:
        source_tables = set(MODULE.TABLE_START.findall(self.source.decode("utf-8")))
        community_tables = set(MODULE.TABLE_START.findall(self.community))
        self.assertEqual(source_tables - community_tables, MODULE.OMITTED_TABLES)
        self.assertNotIn("goldeneye", self.community.lower())
        self.assertTrue(
            {
                "orgs",
                "workspaces",
                "projects",
                "jobs",
                "receipts",
                "budget_reservations",
                "events",
                "agent_runs",
                "agent_definitions",
                "agent_wakeups",
                "mcp_servers",
                "project_configurations",
                "workflow_schedules",
                "account_connect_orgs",
                "account_connect_auth_sessions",
                "account_connect_health_checks",
                "cloud_worker_enrollments",
                "license_entitlements",
                "enterprise_identity_providers",
                "enterprise_saml_sessions",
                "enterprise_scim_users",
            }
            <= community_tables
        )
        self.assertFalse(any(marker in self.community for marker in MODULE.PAID_MARKERS))

    def test_all_foreign_keys_resolve_to_selected_tables(self) -> None:
        community_tables = set(MODULE.TABLE_START.findall(self.community))
        referenced = set(re.findall(r"\bREFERENCES ([a-z][a-z0-9_]*)\(", self.community))
        self.assertFalse(referenced - community_tables)


if __name__ == "__main__":
    unittest.main()
