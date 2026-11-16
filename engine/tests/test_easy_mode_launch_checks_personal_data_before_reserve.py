"""Easy Mode checks the launch prompt before it reserves a budget.

The text is the spec blurb and the spec prompt. A card number or a national id
blocks: no reservation and no run, even when the caller says it is allowed.
An email or a phone number asks: nothing is reserved until allowed is true.
A normal app prompt still reserves once, and the same body is reused.
An empty prompt stays on the lucky mode. Nothing here says GitHub or Drive
are connected.
"""
from __future__ import annotations

import inspect
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

URL = "postgresql+psycopg://boss:boss@127.0.0.1:55920/boss_test_launch_gate"

_BLURB_CARD = """\
id: blurb-card
title: Checkout notes
blurb: The customer's card is 4111 1111 1111 1111.
category: app
prompt: Build a small notes app with folders and search.
stack: vite-react
resources: []
workflows:
  - name: Build the notes
    steps:
      - Draft the page
"""

_PROMPT_ID = """\
id: prompt-id
title: Signer notes
blurb: A list and an editor.
category: app
prompt: The signer national id is 123-45-6789.
stack: vite-react
resources: []
workflows:
  - name: Build the notes
    steps:
      - Draft the page
"""


@pytest.fixture(scope="module")
def database():
    os.environ["DATABASE_URL"] = URL
    os.environ["BOSS_TEST_DATABASE_URL"] = URL
    from boss_engine.config import get_settings
    from boss_engine.db import ensure_schema

    settings = get_settings()
    eng = create_engine(URL, future=True)
    ensure_schema(eng, settings)
    with eng.begin() as connection:
        connection.execute(
            text("INSERT INTO orgs (id, name) VALUES (:id, 'BOSS') ON CONFLICT (id) DO NOTHING"),
            {"id": settings.default_org_id},
        )
        _wipe_launches(connection)
    yield eng, settings
    eng.dispose()


def _wipe_launches(connection) -> None:
    """Drop projects this file launched earlier, so a second run is not a reuse."""
    ids = [row[0] for row in connection.execute(text(
        "SELECT DISTINCT project_id FROM receipts WHERE effect_kind = 'factory.launch'"))]
    if not ids:
        return
    connection.execute(text("SET LOCAL session_replication_role = replica"))
    tables = [row[0] for row in connection.execute(text(
        """
        SELECT c.table_name
        FROM information_schema.columns AS c
        JOIN information_schema.tables AS t
          ON t.table_schema = c.table_schema AND t.table_name = c.table_name
        WHERE c.column_name = 'project_id' AND c.table_schema = 'public'
          AND t.table_type = 'BASE TABLE'
        """))]
    for project_id in ids:
        for table in tables:
            connection.execute(
                text(f'DELETE FROM "{table}" WHERE project_id = :p'), {"p": project_id})
        connection.execute(text("DELETE FROM projects WHERE id = :p"), {"p": project_id})


def _no_connection_claim(blob: str) -> None:
    lowered = blob.lower()
    for phrase in (
        "github is connected",
        "drive is connected",
        "github connected",
        "drive connected",
    ):
        assert phrase not in lowered, phrase


def _watch(monkeypatch, order: list, seen: list) -> None:
    from boss_engine.services.budget import BudgetGovernor
    import boss_engine.services.factory_launch as launch_mod

    original = BudgetGovernor.reserve

    def reserve(self, *args, **kwargs):
        order.append("reserve")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(BudgetGovernor, "reserve", reserve)
    if not hasattr(launch_mod, "before_builder_reserve"):
        return
    real = launch_mod.before_builder_reserve

    def spy(message, *, allowed=False, reserve=None):
        seen.append({"message": message, "allowed": bool(allowed)})
        return real(message, allowed=allowed, reserve=reserve)

    monkeypatch.setattr(launch_mod, "before_builder_reserve", spy)


def _call(engine, settings, prompt, *, allowed=False, template_dir=None, template_id=None,
          order: list, runs: list):
    from boss_engine.services.factory_launch import launch

    def build(connection, *, project_id, spec, actor, settings, jobs, reservation_id):
        order.append("run")
        runs.append({
            "project_id": project_id,
            "reservation_id": reservation_id,
            "mode": spec.get("mode"),
        })
        return {"project_id": project_id, "run_id": "run_gate_1"}

    kwargs = {
        "org_id": settings.default_org_id,
        "actor": "operator",
        "prompt": prompt,
        "template_id": template_id,
        "template_dir": template_dir,
        "builder": build,
    }
    if "allowed" in inspect.signature(launch).parameters:
        kwargs["allowed"] = allowed
    with engine.begin() as connection:
        return launch(connection, settings, **kwargs)


def _hold(engine, reservation_id: str):
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT status FROM budget_reservations WHERE id = :id"),
            {"id": reservation_id},
        ).mappings().first()


def _catalogue(directory: Path, body: str) -> None:
    directory.mkdir()
    (directory / "one.yaml").write_text(body, encoding="utf-8")


def test_a_card_number_blocks_before_reserve_even_when_allowed(database, monkeypatch):
    """A card in the prompt blocks. allowed does not open a reservation or a run."""
    engine, settings = database
    from boss_engine.services.factory_launch import FactoryLaunchError

    order: list = []
    seen: list = []
    runs: list = []
    _watch(monkeypatch, order, seen)
    prompt = "Charge 4111111111111111 now."
    with pytest.raises(FactoryLaunchError) as caught:
        _call(engine, settings, prompt, allowed=True, order=order, runs=runs)
    assert caught.value.status == 409
    assert caught.value.code == "personal_data_blocked"
    assert "4111111111111111" not in str(caught.value)
    _no_connection_claim(str(caught.value))
    assert order == []
    assert runs == []
    assert seen and prompt in seen[0]["message"]
    assert seen[0]["allowed"] is True


def test_a_card_in_the_blurb_blocks_when_the_prompt_is_clean(database, monkeypatch, tmp_path):
    """Empty caller prompt, card only in the spec blurb: still no reservation and no run."""
    engine, settings = database
    from boss_engine.services.factory_launch import FactoryLaunchError

    catalogue = tmp_path / "templates"
    _catalogue(catalogue, _BLURB_CARD)
    order: list = []
    seen: list = []
    runs: list = []
    _watch(monkeypatch, order, seen)
    with pytest.raises(FactoryLaunchError) as caught:
        _call(
            engine, settings, "", template_dir=catalogue, template_id="blurb-card",
            order=order, runs=runs,
        )
    assert caught.value.status == 409
    assert caught.value.code == "personal_data_blocked"
    assert "4111 1111 1111 1111" not in str(caught.value)
    _no_connection_claim(str(caught.value))
    assert order == []
    assert runs == []
    assert seen and "4111 1111 1111 1111" in seen[0]["message"]


def test_a_national_id_in_the_prompt_blocks_when_the_blurb_is_clean(
        database, monkeypatch, tmp_path):
    """A clean blurb does not hide a national id in the prompt. No reservation, no run."""
    engine, settings = database
    from boss_engine.services.factory_launch import FactoryLaunchError

    catalogue = tmp_path / "templates"
    _catalogue(catalogue, _PROMPT_ID)
    order: list = []
    seen: list = []
    runs: list = []
    _watch(monkeypatch, order, seen)
    with pytest.raises(FactoryLaunchError) as caught:
        _call(
            engine, settings, "", allowed=True, template_dir=catalogue,
            template_id="prompt-id", order=order, runs=runs,
        )
    assert caught.value.status == 409
    assert caught.value.code == "personal_data_blocked"
    assert "123-45-6789" not in str(caught.value)
    _no_connection_claim(str(caught.value))
    assert order == []
    assert runs == []
    assert seen and "123-45-6789" in seen[0]["message"]
    assert seen[0]["allowed"] is True


def test_an_email_asks_and_does_not_reserve_until_allowed(database, monkeypatch):
    """An email is an ask. The same body reserves once after allowed, then reuses."""
    engine, settings = database
    from boss_engine.services.factory_launch import FactoryLaunchError

    order: list = []
    seen: list = []
    runs: list = []
    _watch(monkeypatch, order, seen)
    prompt = "Send the build log to ada@example.com."
    with pytest.raises(FactoryLaunchError) as caught:
        _call(engine, settings, prompt, order=order, runs=runs)
    assert caught.value.status == 409
    assert caught.value.code == "personal_data_ask"
    assert "ada@example.com" not in str(caught.value)
    _no_connection_claim(str(caught.value))
    assert order == []
    assert runs == []
    assert seen and prompt in seen[0]["message"]
    assert seen[0]["allowed"] is False

    allowed = _call(engine, settings, prompt, allowed=True, order=order, runs=runs)
    assert allowed["reused"] is False
    assert allowed["run_id"] == "run_gate_1"
    assert allowed["reservation_id"]
    assert _hold(engine, allowed["reservation_id"])["status"] == "active"
    assert order == ["reserve", "run"]
    assert seen[-1]["allowed"] is True
    _no_connection_claim(json.dumps(allowed))

    again = _call(engine, settings, prompt, order=order, runs=runs)
    assert again["reused"] is True
    assert again["project_id"] == allowed["project_id"]
    assert again["run_id"] == allowed["run_id"]
    assert again["receipt_id"] == allowed["receipt_id"]
    assert order == ["reserve", "run"]
    assert len(runs) == 1


def test_a_phone_number_asks_and_does_not_reserve_until_allowed(database, monkeypatch):
    """A phone number is an ask. Nothing is reserved until allowed is true."""
    engine, settings = database
    from boss_engine.services.factory_launch import FactoryLaunchError

    order: list = []
    seen: list = []
    runs: list = []
    _watch(monkeypatch, order, seen)
    prompt = "Call the on-call line at 415-555-0134 before launch."
    with pytest.raises(FactoryLaunchError) as caught:
        _call(engine, settings, prompt, order=order, runs=runs)
    assert caught.value.status == 409
    assert caught.value.code == "personal_data_ask"
    assert "415-555-0134" not in str(caught.value)
    _no_connection_claim(str(caught.value))
    assert order == []
    assert runs == []
    assert seen and "415-555-0134" in seen[0]["message"]

    allowed = _call(engine, settings, prompt, allowed=True, order=order, runs=runs)
    assert allowed["reused"] is False
    assert allowed["reservation_id"]
    assert _hold(engine, allowed["reservation_id"])["status"] == "active"
    assert order == ["reserve", "run"]
    assert len(runs) == 1
    _no_connection_claim(json.dumps(allowed))


def test_a_normal_app_prompt_reserves_once_and_a_repeat_is_reused(database, monkeypatch):
    """A prompt with no personal data reserves once. The same body is reused."""
    engine, settings = database
    order: list = []
    seen: list = []
    runs: list = []
    _watch(monkeypatch, order, seen)
    prompt = "Build a notes app with folders, search, and a share link."
    first = _call(engine, settings, prompt, order=order, runs=runs)
    assert first["reused"] is False
    assert first["mode"] == "prompt"
    assert first["run_id"] == "run_gate_1"
    assert first["reservation_id"]
    assert _hold(engine, first["reservation_id"])["status"] == "active"
    assert order == ["reserve", "run"]
    assert seen and prompt in seen[0]["message"]
    assert seen[0]["allowed"] is False
    _no_connection_claim(json.dumps(first))

    second = _call(engine, settings, prompt, order=order, runs=runs)
    assert second["reused"] is True
    assert second["project_id"] == first["project_id"]
    assert second["run_id"] == first["run_id"]
    assert second["receipt_id"] == first["receipt_id"]
    assert order == ["reserve", "run"]
    assert len(runs) == 1


def test_an_empty_launch_stays_lucky_and_does_not_claim_a_connection(
        database, monkeypatch, tmp_path):
    """No prompt stays mode lucky, reserves once, and reuses. It does not say a
    GitHub or Drive account is connected."""
    engine, settings = database
    order: list = []
    seen: list = []
    runs: list = []
    _watch(monkeypatch, order, seen)
    absent = tmp_path / "absent"
    first = _call(engine, settings, "", template_dir=absent, order=order, runs=runs)
    assert first["mode"] == "lucky"
    assert first["template_id"] == "lucky-notes"
    assert first["reused"] is False
    assert first["reservation_id"]
    assert _hold(engine, first["reservation_id"])["status"] == "active"
    assert order == ["reserve", "run"]
    assert seen
    message = seen[0]["message"]
    assert (
        "A list of notes and an editor, on one page." in message
        or "Build a small notes app" in message
    )
    _no_connection_claim(json.dumps(first))
    _no_connection_claim(message)

    second = _call(engine, settings, "", template_dir=absent, order=order, runs=runs)
    assert second["reused"] is True
    assert second["mode"] == "lucky"
    assert second["project_id"] == first["project_id"]
    assert second["run_id"] == first["run_id"]
    assert order == ["reserve", "run"]
    assert len(runs) == 1
