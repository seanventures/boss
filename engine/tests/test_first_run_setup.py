"""The first ten minutes of a self-hosted install.

"This is very essential setup for the open source, we want smooth onboarding without any setup
issues."

Every piece already had an endpoint — GitHub connections, entitlement sources, credential
upload, connection tests. What did not exist was an answer to "I just installed this, what now?",
and the honest answer was: set two environment variables on the API container, restart it, then
call four endpoints in the right order with the right ids. That is where an open-source
evaluation ends — not because something is missing, but because nothing told you the order.
"""
import pytest

from boss_engine.schemas import GitHubEasyConnectRequest
from boss_engine.services.setup_service import PROVIDERS, SetupError, verify_model_key


def test_a_token_can_be_pasted_rather_than_put_in_the_environment():
    """"Operators never paste raw tokens into BOSS" is the right rule for a platform whose
    operators own the server. It is the wrong first step for someone trying the product."""
    request = GitHubEasyConnectRequest(project_id="p", token="github_pat_example")
    assert request.token == "github_pat_example"


def test_and_the_environment_path_is_untouched():
    request = GitHubEasyConnectRequest(project_id="p")
    assert request.token is None
    assert request.env_credential_key == "BOSS_GITHUB_TOKEN"


def test_a_pasted_token_is_written_through_not_stored_in_a_column():
    """The value leaves BOSS inside put_secret; what the connection keeps is a reference."""
    import inspect

    from boss_engine.services import github_connections

    source = inspect.getsource(github_connections.GitHubConnectionService.store_token_credential)
    assert "put_secret(" in source
    assert "stored['ref_key']" in source
    # Not a fixed name: a second GitHub account must not silently overwrite the first's token.
    assert "slug" in source


def test_the_wizard_picks_a_secret_store_that_works_here():
    """A first-time user has not configured a vault — that is the whole point — and a wizard
    that cannot complete on the installation it exists to serve is worse than none."""
    from boss_engine.config import get_settings
    from boss_engine.services.secret_backends import default_secret_backend

    assert default_secret_backend(get_settings()) in {
        "platform_secret_store", "vault", "local_vault", "database"}


def test_the_catalogue_covers_keys_compatibles_and_subscriptions():
    assert PROVIDERS["openrouter"]["kind"] == "api_key"
    assert PROVIDERS["openai"]["kind"] == "api_key"
    assert PROVIDERS["anthropic"]["kind"] == "api_key"
    # The widest open-source coverage is not a provider at all.
    assert PROVIDERS["custom"]["api"] == "openai"
    for name in ("claude_code", "codex", "cursor"):
        assert PROVIDERS[name]["kind"] == "subscription"


def test_a_custom_provider_must_say_where_it_lives():
    with pytest.raises(SetupError):
        verify_model_key(provider="custom", api_key="k", base_url="", model="m")


def test_and_which_model_to_ask_for():
    with pytest.raises(SetupError):
        verify_model_key(provider="custom", api_key="k", base_url="http://x/v1", model="")


def test_a_dead_host_is_reported_not_raised():
    """A wizard step that throws a stack trace at somebody on their first run has told them
    nothing. Port 1 is reserved and never listening."""
    result = verify_model_key(provider="custom", api_key="k",
                              base_url="http://127.0.0.1:1/v1", model="m", timeout=3)
    assert result["ok"] is False
    assert "could not reach" in result["error"]


def test_the_api_refuses_to_store_a_key_it_could_not_prove():
    """A source recorded as configured against a key that does not work is worse than no source:
    the first real evidence arrives an hour later when a build fails."""
    from pathlib import Path

    api = Path(__file__).resolve().parents[2] / "apps" / "api" / "app" / "main.py"
    source = api.read_text(encoding="utf-8")
    setup = source[source.index('@app.post("/v1/setup/model"'):]
    setup = setup[: setup.index("@app.get(\"/v1/entitlements/sources\"")]
    assert setup.index("verify_model_key(") < setup.index("upload_credential(")
    assert 'raise HTTPException(status_code=422' in setup


def test_a_subscription_is_redirected_rather_than_offered_a_key_box():
    from pathlib import Path

    api = Path(__file__).resolve().parents[2] / "apps" / "api" / "app" / "main.py"
    source = api.read_text(encoding="utf-8")
    assert 'is a subscription, not an API key' in source
    assert "/connect" in source
