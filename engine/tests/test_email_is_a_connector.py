"""Email is a connector: one connection URL or separate fields, the same stored config either way,
a selection order that says why in words, and Mailpit only when the operator asks for it.

BOSS is open source: it ships the templates and an SMTP sender, and every install brings its own
mail service and domain (operator decision 2026-10-22).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from boss_engine.config import Settings
from boss_engine.services import email_config, mail_transport

ROOT = Path(__file__).resolve().parents[2]
SECRET = "p@ss/w:rd#?%&+ key"
ENCODED = "p%40ss%2Fw%3Ard%23%3F%25%26%2B%20key"


# ---- one connection URL ----------------------------------------------------------------------------

@pytest.mark.parametrize("url,expect", [
    ("smtp://login:key@smtp.example.com:587",
     {"host": "smtp.example.com", "port": 587, "username": "login", "password": "key",
      "security": "starttls"}),
    ("smtps://login:key@smtp.example.com:465",
     {"host": "smtp.example.com", "port": 465, "username": "login", "password": "key",
      "security": "ssl"}),
    # The scheme says the port when the URL does not.
    ("smtp://login:key@smtp.example.com", {"port": 587, "security": "starttls"}),
    ("smtps://login:key@smtp.example.com", {"port": 465, "security": "ssl"}),
    # A relay that takes no login.
    ("smtp://relay.example.com:25", {"host": "relay.example.com", "port": 25, "username": "",
                                     "password": ""}),
    # Case and surrounding space are not the operator's problem.
    ("  SMTPS://Login:key@SMTP.Example.com:465 ", {"host": "smtp.example.com", "username": "Login"}),
])
def test_a_good_url_gives_host_port_tls_and_login(url, expect):
    parsed = email_config.parse_url(url)
    for key, value in expect.items():
        assert parsed[key] == value, key


def test_percent_encoded_user_and_password_are_decoded():
    parsed = email_config.parse_url(
        f"smtps://apikey%40example.com:{ENCODED}@email-smtp.example.com:465")
    assert parsed["username"] == "apikey@example.com"
    assert parsed["password"] == SECRET


@pytest.mark.parametrize("url,words", [
    ("", "connection URL"),
    ("https://login:key@smtp.example.com", "smtp://"),
    ("smtp+tls://login:key@smtp.example.com", "smtp://"),
    ("smtp://login:key@:587", "host"),
    ("smtp://", "host"),
    ("smtp://login:key@smtp.example.com:99999", "port"),
    ("smtp://login:key@smtp.example.com:abc", "port"),
    ("smtp://:key@smtp.example.com:587", "user"),
    ("smtp://login:k#ey@smtp.example.com:587", "Percent-encode"),
    ("smtp://login:k/ey@smtp.example.com:587", "Percent-encode"),
    ("smtp://login:key@smtp.example.com:587/path", "path"),
])
def test_a_bad_url_says_what_is_wrong(url, words):
    with pytest.raises(email_config.EmailConfigError) as caught:
        email_config.parse_url(url)
    assert words.lower() in str(caught.value).lower()
    assert caught.value.field == "url"


def test_a_bad_url_never_repeats_the_password():
    for url in (f"http://login:{ENCODED}@smtp.example.com", f"smtp://login:{ENCODED}@:587",
                f"smtp://login:{ENCODED}@smtp.example.com:99999",
                "smtp://login:hunter2#x@smtp.example.com"):
        with pytest.raises(email_config.EmailConfigError) as caught:
            email_config.parse_url(url)
        said = str(caught.value)
        for leak in (ENCODED, SECRET, "hunter2"):
            assert leak not in said, (url, said)


def test_a_connector_needs_a_from_address():
    with pytest.raises(email_config.EmailConfigError) as caught:
        email_config.connector_values(url="smtps://login:key@smtp.example.com:465")
    assert "From" in str(caught.value) and caught.value.field == "from_address"
    with pytest.raises(email_config.EmailConfigError) as caught:
        email_config.connector_values(url="smtps://login:key@smtp.example.com:465",
                                      from_address="not-an-address")
    assert caught.value.field == "from_address"


def test_a_connector_needs_a_url_or_a_host():
    with pytest.raises(email_config.EmailConfigError) as caught:
        email_config.connector_values(from_address="boss@example.com")
    assert caught.value.field == "url" and "host" in str(caught.value)


def test_the_url_and_the_fields_give_the_same_config():
    by_url = email_config.connector_values(
        url=f"smtps://apikey%40example.com:{ENCODED}@email-smtp.example.com:465",
        from_address="boss@example.com", from_name="Example Ops")
    by_fields = email_config.connector_values(
        host="email-smtp.example.com", port="465", security="ssl",
        username="apikey@example.com", password=SECRET,
        from_address="boss@example.com", from_name="Example Ops")
    assert by_url == by_fields
    assert by_url["use_ssl"] is True and by_url["use_tls"] is False


def test_the_shown_url_has_no_password():
    shown = email_config.display_url({"host": "smtp.example.com", "port": 465,
                                      "username": "apikey@example.com", "use_ssl": True})
    assert shown == "smtps://apikey%40example.com@smtp.example.com:465"
    assert email_config.parse_url(shown)["password"] == ""


# ---- the order mail goes out in --------------------------------------------------------------------

def _base(**extra):
    values = {"email_smtp_host": None, "email_from_address": None,
              "email_real_send_allowed": False, "email_dev_inbox": "", "email_dev_inbox_url": ""}
    values.update(extra)
    return Settings().model_copy(update=values)


def _saved(monkeypatch, row):
    """The connector row as the transport reads it (no database needed for the order)."""
    monkeypatch.setattr(mail_transport, "_with_connection",
                        lambda fn, settings: fn(None))
    monkeypatch.setattr(email_config, "_row", lambda connection, project_id: row)
    monkeypatch.setattr(email_config, "_password", lambda connection, settings, row: "key")


def test_the_connector_wins_then_env_then_dev_inbox_then_none(monkeypatch):
    env_on = _base(email_smtp_host="env-smtp.example.com", email_from_address="env@example.com",
                   email_real_send_allowed=True, email_dev_inbox="mailpit:1025")

    _saved(monkeypatch, {"host": "smtp.example.com", "port": 465, "username": "login",
                         "use_ssl": True, "from_address": "boss@example.com",
                         "secret_ref_id": "sec_x"})
    chosen = mail_transport.route(None, env_on)
    assert chosen.source == "connector" and chosen.real
    assert chosen.settings.email_smtp_host == "smtp.example.com"

    _saved(monkeypatch, None)
    assert mail_transport.route(None, env_on).source == "env"
    env_off = env_on.model_copy(update={"email_real_send_allowed": False})
    assert mail_transport.route(None, env_off).source == "dev_inbox"
    none = mail_transport.route(None, env_off.model_copy(update={"email_dev_inbox": ""}))
    assert none.source == "none" and not none.ready
    assert "BOSS_EMAIL_REAL_SEND_ALLOWED" in none.detail
    nothing = mail_transport.route(None, _base())
    assert "Email (SMTP)" in nothing.detail and "Skills" in nothing.detail


def test_the_connector_can_choose_the_local_test_inbox_only_when_it_is_on(monkeypatch):
    _saved(monkeypatch, {"mode": "dev_inbox"})
    on = mail_transport.route(None, _base(email_dev_inbox="1"))
    assert on.source == "dev_inbox" and not on.real
    assert (on.settings.email_smtp_host, on.settings.email_smtp_port) == ("mailpit", 1025)
    off = mail_transport.route(None, _base())
    assert off.source == "none" and "local test inbox" in off.detail.lower()


@pytest.mark.parametrize("raw,expect", [
    ("", None), ("0", None), ("false", None), ("off", None),
    ("1", ("mailpit", 1025)), ("true", ("mailpit", 1025)), ("mailpit:1025", ("mailpit", 1025)),
    ("127.0.0.1:2525", ("127.0.0.1", 2525)),
])
def test_the_dev_inbox_switch(raw, expect):
    dev = mail_transport._dev_inbox(_base(email_dev_inbox=raw))
    assert (None if dev is None else (dev.email_smtp_host, dev.email_smtp_port)) == expect


# ---- Mailpit is opt-in -----------------------------------------------------------------------------

def test_mailpit_is_off_on_a_default_install_config():
    compose = yaml.safe_load((ROOT / "scripts/install/community-compose.yml").read_text())
    profiles = compose["services"]["mailpit"].get("profiles") or []
    assert profiles == ["mail"], "Mailpit must not come up with the default or web profile"
    assert Settings().email_dev_inbox == ""

    install = (ROOT / "install.sh").read_text()
    assert re.search(r"^WITH_MAILPIT=\"?\$\{BOSS_EMAIL_DEV_INBOX:-0\}\"?", install, re.M), \
        "the dev inbox defaults to off unless BOSS_EMAIL_DEV_INBOX=1"
    assert "--with-mailpit) WITH_MAILPIT=1" in install
    # The only place the dev inbox is pointed at Mailpit is inside the opt-in.
    block = install[install.index('if [[ "$WITH_MAILPIT" -eq 1 ]]'):]
    block = block[:block.index("\nfi\n")]
    assert 'MAIL_DEV_INBOX="mailpit:1025"' in block and "--profile mail" in block
    assert install.count('MAIL_DEV_INBOX="mailpit:1025"') == 1
    # Day-two commands keep the profile only when the install opted in.
    boss = (ROOT / "scripts/install/boss.sh").read_text()
    assert "BOSS_EMAIL_DEV_INBOX" in boss and "--profile mail" in boss
