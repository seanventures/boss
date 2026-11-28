"""BOSSAPP-572: One rules book — registry, role-scoped loading within token budget, and
honest code-enforcement guard.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from boss_engine.services import rules_book
from boss_engine.services.core import make_id


def _test_db_url() -> str:
    return os.environ.get("BOSS_TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""


class TestRulesBookRegistry:
    def test_every_code_enforced_rule_has_real_setting_or_gate(self):
        """Guard test: no prompt-only rule claims code enforcement, and every setting/gate/policy
        ref resolves to a real knob in project_settings.KNOBS / config.Settings or a real symbol."""
        violations = rules_book.verify_code_enforcement()
        assert violations == [], f"Rules book enforcement violations: {violations}"

    def test_required_schema_fields_and_standing_rules_present(self):
        data = rules_book.list_rules()
        assert data["read_only"] is True
        assert data["total"] >= 20
        by_id = {r["id"]: r for r in data["rules"]}

        # Every entry carries id, text, why, category, roles, enforcement, source.
        for rule in data["rules"]:
            assert rule["id"]
            assert rule["title"]
            assert rule["text"]
            assert rule["why"]
            assert rule["category"] in rules_book.CATEGORIES
            assert isinstance(rule["roles"], list) and rule["roles"]
            assert rule["enforcement"]["kind"] in rules_book.ENFORCEMENT_KINDS
            assert rule["enforcement"]["where"]
            assert rule["source"] in rules_book.SOURCES

        # Key operator & platform rules required by BOSSAPP-572 + 2026-11-24 decision:
        assert "autonomy.dev_act" in by_id
        assert "autonomy.never_ask_mid_build" in by_id
        assert "autonomy.no_community_gating" in by_id
        assert "autonomy.boss_side_full_control" in by_id
        assert "build.open_pr_when_checks_fail" in by_id
        assert by_id["build.open_pr_when_checks_fail"]["enforcement"]["kind"] == "setting"
        assert "security.never_print_credential" in by_id
        assert by_id["security.never_print_credential"]["enforcement"]["kind"] == "gate"
        assert "ops.fix_class_not_instance" in by_id
        assert by_id["ops.fix_class_not_instance"]["enforcement"]["kind"] == "prompt_only"
        assert by_id["ops.fix_class_not_instance"]["enforcement"]["ref"] == ""

    def test_search_and_category_filtering(self):
        build_only = rules_book.list_rules(category="build")
        assert build_only["total"] > 0
        assert all(r["category"] == "build" for r in build_only["rules"])

        found = rules_book.list_rules(q="prometheus")
        assert any(r["id"] == "build.observability_metrics" for r in found["rules"])

        settings_only = rules_book.list_rules(enforcement_kind="setting")
        assert settings_only["total"] > 0
        assert all(r["enforcement"]["kind"] == "setting" for r in settings_only["rules"])

    def test_role_scoped_loading_filters_categories_and_respects_token_budget(self):
        ux_slice = rules_book.rules_for_role("ux", max_tokens=1200)
        ux_ids = {r["id"] for r in ux_slice["rules"]}
        # Universal rules are included for every role.
        assert "security.never_print_credential" in ux_ids
        assert "autonomy.dev_act" in ux_ids
        # UX gets UX rules, not FinOps-only call_classes or deploy_fix_attempts.
        assert "ux.tokens_and_copy_budget" in ux_ids
        assert "cost.call_classes" not in ux_ids
        assert "deploy.fix_attempts" not in ux_ids
        assert ux_slice["tokens_used"] <= ux_slice["tokens_budget"]

        finops_slice = rules_book.rules_for_role("finops", max_tokens=1600)
        finops_ids = {r["id"] for r in finops_slice["rules"]}
        assert "cost.budget_at_limit" in finops_ids
        assert "cost.call_classes" in finops_ids
        assert "ux.tokens_and_copy_budget" not in finops_ids

        # Tight token budget truncates cleanly without exceeding budget.
        tight = rules_book.rules_for_role("engineering_captain", max_tokens=180)
        assert tight["tokens_used"] <= 180
        assert tight["truncated"] is True
        assert tight["omitted_count"] > 0
        # Highest priority universal rule (never_print_credential) is always kept first.
        assert tight["rules"][0]["id"] == "security.never_print_credential"

        block = rules_book.format_role_rulebook("coder", max_tokens=600)
        assert "=== BOSS RULES BOOK (role: coder) ===" in block
        assert "=== END BOSS RULES BOOK ===" in block


class TestRulesBookDBIntegration:
    def test_mandated_standards_and_project_settings_fold_into_rules_book(self):
        url = _test_db_url()
        if not url:
            pytest.skip("BOSS_TEST_DATABASE_URL / DATABASE_URL not set")
        eng = create_engine(url, future=True)
        org_id, ws_id, proj_id, std_id = make_id("org"), make_id("ws"), make_id("prj"), make_id("std")
        try:
            with eng.begin() as c:
                c.execute(text("INSERT INTO orgs (id, name) VALUES (:id, 'rb')"), {"id": org_id})
                c.execute(
                    text("INSERT INTO workspaces (id, org_id, name) VALUES (:id, :o, 'rb')"),
                    {"id": ws_id, "o": org_id},
                )
                c.execute(
                    text("INSERT INTO projects (id, workspace_id, name, slug) VALUES (:id, :w, 'rb', :s)"),
                    {"id": proj_id, "w": ws_id, "s": f"rb-{proj_id[-6:]}"},
                )
                c.execute(
                    text(
                        "INSERT INTO engineering_standards "
                        "(id, project_id, standard_key, title, body, scope, mandated_by_role, rationale, status, created_by) "
                        "VALUES (:id, :p, 'license_header', 'Apache license header', "
                        "'Include Apache 2.0 header in every source file.', 'all', 'security', "
                        "'Required by legal policy.', 'active', 'test')"
                    ),
                    {"id": std_id, "p": proj_id},
                )

                book = rules_book.list_rules(connection=c, project_id=proj_id)
                by_id = {r["id"]: r for r in book["rules"]}
                assert "mandated.license_header" in by_id
                mand = by_id["mandated.license_header"]
                assert mand["source"] == "mandated_standard"
                assert mand["enforcement"]["kind"] == "prompt_only"
                assert mand["universal"] is True
                assert "autonomy.dev_act" in by_id
                assert by_id["autonomy.dev_act"].get("effective_value")
        finally:
            with eng.begin() as c:
                c.execute(text("DELETE FROM projects WHERE id = :p"), {"p": proj_id})
                c.execute(text("DELETE FROM workspaces WHERE id = :w"), {"w": ws_id})
                c.execute(text("DELETE FROM orgs WHERE id = :o"), {"o": org_id})
            eng.dispose()
