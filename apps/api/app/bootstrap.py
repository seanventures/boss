from sqlalchemy import text

from .config import get_settings
from .db import ensure_schema, get_engine
from .services.entitlements import EntitlementBrokerService
from .services.specpack import SpecPackService


def seed() -> None:
    settings = get_settings()
    settings.artifact_root.mkdir(parents=True, exist_ok=True)
    engine = get_engine(settings)
    entitlements = EntitlementBrokerService(settings)
    specpack = SpecPackService(settings)
    ensure_schema(engine, settings)

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO orgs (id, name)
                VALUES (:id, 'BOSS')
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"id": settings.default_org_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO workspaces (id, org_id, name)
                VALUES (:id, :org_id, 'Dogfood')
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"id": settings.default_workspace_id, "org_id": settings.default_org_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO projects (id, workspace_id, name, slug)
                VALUES (:id, :workspace_id, 'BOSS dogfood bootstrap', 'boss-dogfood')
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": settings.default_project_id,
                "workspace_id": settings.default_workspace_id,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO budget_policies (
                    id, scope_type, scope_id, daily_cap_usd_micros, monthly_cap_usd_micros,
                    burst_cap_usd_micros_per_min, max_concurrent_jobs, max_workers_total,
                    allowed_model_tiers, soft_cap_pct, auto_lite_mode_pct,
                    freeze_provisioning_pct, freeze_tier2_pct
                )
                VALUES (
                    'bp_boss_default', 'project', :scope_id, :daily_cap, :monthly_cap, :burst_cap,
                    3, 2, '["gpt-5.2-codex"]'::jsonb, 0.70, 0.85, 0.90, 0.95
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "scope_id": settings.default_project_id,
                "daily_cap": settings.daily_cap_usd_micros,
                "monthly_cap": settings.monthly_cap_usd_micros,
                "burst_cap": settings.burst_cap_usd_micros_per_min,
            },
        )
        specpack.install_pack(
            connection,
            settings.default_project_id,
            settings.core_pack_id,
            "bootstrap",
        )
        entitlements.seed_default_sources(
            connection,
            settings.default_project_id,
            "bootstrap",
        )


def main() -> None:
    seed()
    print("BOSS bootstrap seed complete.")


if __name__ == "__main__":
    main()
