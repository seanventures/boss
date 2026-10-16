"""AN APP CALLED WHAT PEOPLE CALL IT CAN DEPLOY ITS DATABASE.

HelpDesk AI's app key is "helpdesk ai". Its first stack deploy (16 Oct) failed before anything
shipped: "db: provision failed: ValidationError … SecretRef … input_value='app_secret://deploy/
helpdesk ai/DATABASE_URL'". The contract's reference pattern refuses a space, so every app named
with two words could never get a provisioned database.
"""
import pytest

from boss_engine.platform.bindings import make_ref, parse_ref
from boss_engine.platform.contract import ContractError


@pytest.mark.parametrize("app_key", ["helpdesk ai", "helpdesk", "my_app.v2", "Café App",
                                     "a:b/c", "x y z"])
def test_a_reference_holds_any_app_key_and_gives_it_back(app_key):
    ref = make_ref("deploy", app_key, "DATABASE_URL")  # validated by SecretRef's pattern
    assert parse_ref(ref) == ("deploy", app_key, "DATABASE_URL")
    assert parse_ref(ref.secretRef) == ("deploy", app_key, "DATABASE_URL")


def test_a_plain_key_reads_exactly_as_before():
    """References already stored for a one-word app are unchanged."""
    assert make_ref("deploy", "helpdesk", "DATABASE_URL").secretRef == \
        "app_secret://deploy/helpdesk/DATABASE_URL"


def test_the_helpdesk_reference_is_the_one_that_failed_now_valid():
    assert make_ref("deploy", "helpdesk ai", "DATABASE_URL").secretRef == \
        "app_secret://deploy/helpdesk:20ai/DATABASE_URL"


def test_something_else_is_still_not_a_reference():
    with pytest.raises(ContractError):
        parse_ref("vault://deploy/helpdesk/DATABASE_URL")
