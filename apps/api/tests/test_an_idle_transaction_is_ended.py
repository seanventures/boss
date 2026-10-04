"""An API request cannot hold a transaction open for hours.

A connection sat "idle in transaction" on entitlement_sources for ~2 h during a walk and every
later request queued behind it. The API's engine has Postgres end any session that idles inside
an open transaction (BOSS_API_DB_IDLE_IN_TRANSACTION_SECONDS, default 300).
"""
from __future__ import annotations

import time

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


def test_the_api_engine_sets_the_limit():
    from apps.api.app import main

    assert main.settings.api_db_idle_in_transaction_seconds > 0
    with main.engine.connect() as c:
        shown = c.execute(text("SHOW idle_in_transaction_session_timeout")).scalar()
    assert shown not in ("0", "0ms"), shown


def test_a_session_idle_inside_a_transaction_is_ended(engine):
    from boss_engine.config import get_settings
    from boss_engine.db import get_engine

    settings = get_settings().model_copy(update={"database_url": str(engine.url.render_as_string(
        hide_password=False))})
    limited = get_engine(settings, idle_in_transaction_seconds=1)
    try:
        with pytest.raises(DBAPIError):
            with limited.begin() as c:
                c.execute(text("SELECT 1"))
                time.sleep(2.5)  # something hung between two statements
                c.execute(text("SELECT 1"))
    finally:
        limited.dispose()
