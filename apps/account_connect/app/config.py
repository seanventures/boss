from __future__ import annotations

import os
from functools import lru_cache

from pydantic import BaseModel, Field


class Settings(BaseModel):
    database_url: str = Field(
        default_factory=lambda: os.getenv(
            "AC_DATABASE_URL",
            "postgresql+psycopg://boss:boss@localhost:5432/boss_account_connect",
        )
    )
    service_token: str = Field(default_factory=lambda: os.getenv("AC_SERVICE_TOKEN", ""))
    public_base_url: str = Field(
        default_factory=lambda: os.getenv("AC_PUBLIC_BASE_URL", "http://localhost:8010")
    )
    default_org_id: str = Field(
        default_factory=lambda: os.getenv("AC_DEFAULT_ORG_ID", "org_boss_cloud")
    )
    default_org_name: str = Field(
        default_factory=lambda: os.getenv("AC_DEFAULT_ORG_NAME", "BOSS Cloud")
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
