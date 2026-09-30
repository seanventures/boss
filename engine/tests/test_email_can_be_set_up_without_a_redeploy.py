"""Configuring how a project sends email.

It was environment-variable only: six settings on the container, changed by redeploying it. An
operator who wants a newsletter emailed should not have to edit a compose file to say which
mail server to use.

Exercised against a real database — the password path in particular, because "the secret is not
in the row" is the kind of claim that is true in the source and false in the table.
"""
import os

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import email_config

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="no database configured")
PROJECT = "boss-dogfood"


@pytest.fixture()
def db():
    engine = create_engine(DATABASE_URL)
    yield engine
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM project_configurations WHERE project_id=:p AND config_key=:k"),
            {"p": PROJECT, "k": email_config.CONFIG_KEY})


def _save(connection, **kw):
    return email_config.save(
        connection, Settings(), project_id=PROJECT, actor="test",
        host=kw.pop("host", "smtp.example.com"),
        from_address=kw.pop("from_address", "boss@example.com"), **kw)


def test_what_is_configured_can_be_read_back(db):
    with db.begin() as connection:
        _save(connection, port=2525, username="mailer", from_name="BOSS")
        shown = email_config.describe(connection, PROJECT)
    assert shown["configured"] is True
    assert (shown["host"], shown["port"], shown["username"]) == \
        ("smtp.example.com", 2525, "mailer")


def test_the_password_is_never_in_the_row_or_the_response(db):
    """THE CLAIM WORTH CHECKING IN THE TABLE, not the source."""
    with db.begin() as connection:
        shown = _save(connection, password="hunter2-not-a-real-password")
        stored = connection.execute(
            text("SELECT body::text FROM project_configurations "
                 "WHERE project_id=:p AND config_key=:k"),
            {"p": PROJECT, "k": email_config.CONFIG_KEY}).scalar()

    assert "hunter2" not in str(stored), "the password is sitting in the configuration row"
    assert "hunter2" not in str(shown), "the password came back in the API response"
    # Whether, not what.
    assert shown["has_password"] is True
    assert "password" not in {k for k in shown if k != "has_password"}


def test_editing_a_port_does_not_require_retyping_the_password(db):
    """Friction that gets solved by writing the password in a text file somewhere."""
    with db.begin() as connection:
        _save(connection, password="hunter2-not-a-real-password")
        again = _save(connection, port=465, use_tls=False, use_ssl=True)
    assert again["has_password"] is True
    assert again["port"] == 465


def test_the_settings_handed_to_the_sender_carry_this_project_s_server(db):
    with db.begin() as connection:
        _save(connection, host="mail.acme.test", port=2525, from_name="Acme")
        settings = email_config.settings_for(connection, Settings(), PROJECT)
    assert settings.email_smtp_host == "mail.acme.test"
    assert settings.email_smtp_port == 2525
    assert settings.email_from_name == "Acme"
    # Configuring IS consenting: somebody who has just typed their mail server in has said to
    # send, and a second environment switch for the same decision is a trap.
    assert settings.email_real_send_allowed is True


def test_an_install_with_no_configuration_is_left_exactly_as_it_was(db):
    """The environment remains the fallback, so this adds a way to configure rather than
    replacing one."""
    base = Settings()
    with db.begin() as connection:
        same = email_config.settings_for(connection, base, PROJECT)
    assert same is base


@pytest.mark.parametrize("kwargs,expect", [
    ({"host": ""}, "SMTP host"),
    ({"from_address": "not-an-address"}, "sent from"),
    ({"port": 0}, "between 1 and 65535"),
    ({"port": "abc"}, "must be a number"),
    ({"use_tls": True, "use_ssl": True}, "not both"),
])
def test_a_bad_setting_says_which_one(db, kwargs, expect):
    with db.begin() as connection:
        with pytest.raises(email_config.EmailConfigError, match=expect):
            _save(connection, **kwargs)
