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
from types import SimpleNamespace

from boss_engine.schemas import GitHubEasyConnectRequest
from boss_engine.services.setup_service import PROVIDERS, SetupError, verify_model_key


PAID_SETTINGS = SimpleNamespace(boss_edition="enterprise_self_hosted")


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


def test_the_wizard_picks_a_secret_store_that_works_here(monkeypatch):
    """A first-time user has not configured a vault — that is the whole point — and a wizard
    that cannot complete on the installation it exists to serve is worse than none."""
    from boss_engine.config import get_settings
    from boss_engine.services.secret_backends import default_secret_backend

    monkeypatch.setenv("BOSS_KMS_ALLOW_EPHEMERAL_MASTER", "true")

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
        verify_model_key(
            settings=PAID_SETTINGS, provider="custom", api_key="k", base_url="", model="m",
        )


def test_and_which_model_to_ask_for():
    with pytest.raises(SetupError):
        verify_model_key(
            settings=PAID_SETTINGS, provider="custom", api_key="k", base_url="http://x/v1",
            model="",
        )


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://127.0.0.1", "//localhost/v1"])
def test_model_key_probe_refuses_non_http_urls(url):
    with pytest.raises(SetupError, match="http or https"):
        verify_model_key(
            settings=PAID_SETTINGS, provider="custom", api_key="k", base_url=url, model="m",
        )


def test_model_key_probe_does_not_follow_redirects():
    from boss_engine.services.setup_service import _NoRedirect

    handler = _NoRedirect()
    assert handler.redirect_request(None, None, 302, "Moved", {}, "file:///etc/passwd") is None


def test_a_dead_host_is_reported_not_raised():
    """A wizard step that throws a stack trace at somebody on their first run has told them
    nothing. Port 1 is reserved and never listening."""
    result = verify_model_key(settings=PAID_SETTINGS, provider="custom", api_key="k",
                              base_url="http://127.0.0.1:1/v1", model="m", timeout=3)
    assert result["ok"] is False
    assert "could not reach" in result["error"]


@pytest.mark.parametrize("edition", ["community", "oss", "local", "unknown", ""])
def test_direct_model_probe_has_no_network_in_unmetered_editions(monkeypatch, edition):
    from boss_engine.services import setup_service

    calls = []
    monkeypatch.setattr(setup_service.urllib.request, "build_opener",
                        lambda *args: calls.append(args))
    with pytest.raises(SetupError, match="paused in Community"):
        verify_model_key(settings=SimpleNamespace(boss_edition=edition), provider="openai",
                         api_key="unused", model="test-model")
    assert calls == []


@pytest.mark.parametrize("edition", ["community", "oss", "local", "unknown", ""])
def test_environment_model_status_never_probes_in_unmetered_editions(monkeypatch, edition):
    from boss_engine.services import setup_service

    calls = []
    monkeypatch.setattr(setup_service, "_probe", lambda *args, **kwargs: calls.append(args))
    settings = SimpleNamespace(
        boss_edition=edition, openai_api_key="test-key-1234",
        openai_base_url="https://example.invalid/v1", chat_model="test-model",
    )

    result = setup_service.check_model(settings)

    assert result["configured"] is True
    assert result["verification_state"] == "unverified"
    assert result["ok"] is False
    assert result["key_last4"] == "1234"
    assert "error" not in result
    assert calls == []


def test_paid_environment_model_status_retains_models_check(monkeypatch):
    from boss_engine.services import setup_service

    calls = []

    def fake_probe(url, headers):
        calls.append((url, headers))
        return {"ok": True, "body": {"data": [{"id": "test-model"}]}}

    monkeypatch.setattr(setup_service, "_probe", fake_probe)
    settings = SimpleNamespace(
        boss_edition="enterprise_self_hosted", openai_api_key="test-key",
        openai_base_url="https://example.invalid/v1", chat_model="test-model",
    )

    result = setup_service.check_model(settings)

    assert result["ok"] is True
    assert result["model_listed"] is True
    assert calls == [
        ("https://example.invalid/v1/models", {"Authorization": "Bearer test-key"}),
    ]


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


def test_the_api_authorizes_before_probing_the_caller_url():
    from pathlib import Path

    api = Path(__file__).resolve().parents[2] / "apps" / "api" / "app" / "main.py"
    source = api.read_text(encoding="utf-8")
    setup = source[source.index('@app.post("/v1/setup/model"'):]
    setup = setup[: setup.index("@app.get(\"/v1/entitlements/sources\"")]
    assert setup.index("require_project_actor") < setup.index("verify_model_key(")


def test_a_subscription_is_redirected_rather_than_offered_a_key_box():
    from pathlib import Path

    api = Path(__file__).resolve().parents[2] / "apps" / "api" / "app" / "main.py"
    source = api.read_text(encoding="utf-8")
    assert 'is a subscription, not an API key' in source
    assert "/connect" in source


def test_an_install_configured_the_old_way_is_not_told_it_is_unconfigured():
    """Measured on the live deployment: no github_connections row, and it has been opening pull
    requests against example-user/boss-sandbox for weeks. A wizard that says "not connected" to that
    operator has told them a falsehood about their own system."""
    import inspect

    from boss_engine.services import setup_service

    source = inspect.getsource(setup_service.setup_status)
    assert "env_github" in source
    assert 'bool((connections and repos) or env_github)' in source
    assert '"from_environment": env_github' in source


def test_and_is_told_where_that_configuration_came_from():
    """Saying nothing about WHERE leaves them hunting for a setting the wizard never wrote."""
    import inspect

    from boss_engine.services import setup_service

    source = inspect.getsource(setup_service.setup_status)
    assert '"repository": f"{env_owner}/{env_repo}"' in source


def test_what_the_install_already_runs_on_is_checked_not_assumed():
    """"Connected" that means "an environment variable is not empty" is the status that stays
    green while the first build dies at push time."""
    import inspect

    from boss_engine.services import setup_service

    github = inspect.getsource(setup_service.check_github)
    assert "api.github.com/repos/" in github
    # The caller's own permissions, so BOSS can say `push: true` rather than `token: present`.
    assert 'permissions.get("push")' in github

    model = inspect.getsource(setup_service.check_model)
    assert '/models' in model


def test_a_read_only_token_is_not_reported_as_connected():
    """It looks connected and produces a pull request that never opens."""
    import inspect

    from boss_engine.services import setup_service

    source = inspect.getsource(setup_service.check_github)
    assert "cannot push to it" in source
    assert "Contents needs read and write" in source


def test_the_credential_itself_never_leaves():
    """Last four, so two installs can be told apart; never enough to use."""
    from boss_engine.services.setup_service import _last4, check_github, check_model
    import inspect

    assert _last4("ghp_abcdefgh") == "efgh"
    assert _last4("abc") == ""
    for fn in (check_github, check_model):
        source = inspect.getsource(fn)
        assert '"token": token' not in source
        assert '"api_key"' not in source


def test_the_status_carries_it_so_the_screen_can_show_it():
    import inspect

    from boss_engine.services import setup_service

    source = inspect.getsource(setup_service.setup_status)
    # The GitHub half asks the PROJECT now — the registry decides where a build writes, and a
    # card reporting the environment instead described an install nobody was running.
    assert '"github": check_github_for_project(connection, settings, project_id)' in source
    assert '"model": check_model(settings)' in source


def test_a_probe_never_raises_at_somebody_on_their_first_run():
    from boss_engine.services.setup_service import _probe

    result = _probe("http://127.0.0.1:1/nope", {}, timeout=3)
    assert result["ok"] is False
    assert result["error"]
