"""The first ten minutes of a self-hosted install.

"This is very essential setup for the open source, we want smooth onboarding without any setup
issues."

Every piece already had an endpoint — GitHub connections, entitlement sources, credential
upload, connection tests. What did not exist was an answer to "I just installed this, what now?",
and the honest answer was: set two environment variables on the API container, restart it, then
call four endpoints in the right order with the right ids. That is where an open-source
evaluation ends — not because something is missing, but because nothing told you the order.
"""
import inspect
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from boss_engine.schemas import GitHubEasyConnectRequest
from boss_engine.services import setup_actions, setup_checks, setup_view
from boss_engine.services.setup_checks import HttpAnswer
from boss_engine.services.setup_service import PROVIDERS, SetupError, model_key_details


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


def test_the_catalogue_covers_keys_compatibles_locals_and_subscriptions():
    assert PROVIDERS["openrouter"]["kind"] == "api_key"
    assert PROVIDERS["openai"]["kind"] == "api_key"
    assert PROVIDERS["anthropic"]["kind"] == "api_key"
    # The widest open-source coverage is not a provider at all.
    assert PROVIDERS["custom"]["api"] == "openai"
    for name in ("claude_code", "codex", "grok"):
        assert PROVIDERS[name]["kind"] == "subscription"
    assert "cursor" not in PROVIDERS        # not offered until its CLI ships (P1-8)
    assert PROVIDERS["ollama"]["kind"] == "local"
    # Add a model offers three kinds, never the decision service.
    kinds = {e["provider"]: e["kind"] for e in setup_view.CATALOGUE}
    assert set(kinds.values()) == {"subscription", "api_key", "local"}
    assert "typesafe" not in kinds


def test_a_custom_provider_must_say_where_it_lives():
    with pytest.raises(SetupError):
        model_key_details(provider="custom", base_url="", model="m")


def test_and_which_model_to_ask_for():
    with pytest.raises(SetupError):
        model_key_details(provider="custom", base_url="http://x/v1", model="")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://127.0.0.1", "//localhost/v1"])
def test_model_key_probe_refuses_non_http_urls(url):
    with pytest.raises(SetupError, match="http or https"):
        model_key_details(provider="custom", base_url=url, model="m")


def test_model_key_probe_does_not_follow_redirects():
    assert "follow_redirects=False" in inspect.getsource(setup_checks.default_http)


def test_a_dead_host_is_reported_not_raised():
    """A wizard step that throws a stack trace at somebody on their first run has told them
    nothing. Port 1 is reserved and never listening."""
    result = setup_checks.complete_once(api="openai", endpoint="http://127.0.0.1:1/v1/chat/completions",
                                        api_key="k", model="m")
    assert result["ok"] is False
    assert result["state"] == "unreachable"


def test_a_key_is_proved_only_through_the_governed_path():
    """Every edition: one completion, reserved against the budget and receipted. There is no
    ungoverned probe left to call."""
    source = inspect.getsource(setup_actions._key_proof)
    assert "governed_proof(" in source
    assert "complete_once(" in source
    import boss_engine.services.setup_service as service

    assert not hasattr(service, "verify_model_key")
    assert not hasattr(service, "live_model_probe_allowed")


def test_a_key_that_fails_its_check_is_not_stored():
    """A source recorded against a key that does not work is worse than no source: the first
    real evidence arrives an hour later when a build fails."""
    source = inspect.getsource(setup_actions._connect_key)
    assert source.index("_key_proof(") < source.index("upload_credential(")
    assert source.index("raise _failure(") < source.index("upload_credential(")


def test_the_api_authorizes_before_probing_the_caller_url():
    from pathlib import Path

    router = (Path(__file__).resolve().parents[2] / "apps" / "api" / "app" / "routers"
              / "setup.py").read_text(encoding="utf-8")
    models = router[router.index('@router.post("/v1/setup/models"'):]
    assert models.index("require_actor(") < models.index("connect_model(")


def test_a_subscription_is_redirected_rather_than_offered_a_key_box():
    from pathlib import Path

    api = Path(__file__).resolve().parents[2] / "apps" / "api" / "app" / "main.py"
    assert "is a subscription, not an API key" in api.read_text(encoding="utf-8")


# ── An install configured from its settings file ─────────────────────────────────────────────

@pytest.fixture()
def conn(entitlement_tables, setup_tables):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as c:
        entitlement_tables(c)
        setup_tables(c)
    with engine.begin() as c:
        yield c


def _env_settings(**extra):
    return SimpleNamespace(target_github_owner="example-user", target_github_repo="boss-sandbox",
                           github_token="ghp_" + "x" * 36, openai_api_key="", **extra)


def test_an_install_configured_the_old_way_is_not_told_it_is_unconfigured(conn):
    """Measured on the live deployment: no github_connections row, and it has been opening pull
    requests against example-user/boss-sandbox for weeks. Repo and models shows that repository
    as a row with the chip `From install` — never an empty form."""
    view = setup_view.build_basics(conn, _env_settings(), "p")
    [repo] = view["repository"]["repos"]
    assert repo["full_name"] == "example-user/boss-sandbox"
    assert view["repository"]["connection"]["source"] == "install"
    # Not checked yet: it worked yesterday, so it still counts while the background check runs.
    assert repo["pill"]["state"] == "checking"
    assert setup_view.basics(view)["github"] is True


def test_and_its_last_check_is_what_the_row_says(conn):
    conn.execute(text(
        "INSERT INTO events (id, project_id, event_type, actor, entity_type, entity_id, data,"
        " created_at) VALUES ('e1', 'p', 'setup.repository_checked', 'ceo', 'repository',"
        " 'install', :d, :at)"),
        {"d": json.dumps({"state": "cant_write", "checks": {
            "read": {"ok": True, "state": "working"},
            "write": {"ok": False, "state": "cant_write"}}}),
         "at": datetime.now(timezone.utc)})
    view = setup_view.build_basics(conn, _env_settings(), "p")
    [repo] = view["repository"]["repos"]
    assert repo["pill"]["label"] == "Can't write"
    assert repo["pill"]["tip"] == "Token needs Contents: Read and write."
    assert setup_view.basics(view)["github"] is False


# ── What the install runs on is checked, not assumed ─────────────────────────────────────────

class FakeGitHub:
    """A fine-grained token's answers, per permission profile (fixtures from GitHub's REST docs:
    blobs need Contents: write, pulls need Pull requests: write)."""

    def __init__(self, *, contents_write=True, pull_requests=True, status=200):
        self.contents_write = contents_write
        self.pull_requests = pull_requests
        self.status = status
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, url, *, headers, json_body=None, timeout=15.0):
        path = url.split("api.github.com", 1)[1]
        self.calls.append((method, path))
        assert "/git/refs" not in path, "a setup check must never create a branch"
        if self.status != 200:
            return HttpAnswer(self.status, {}, {"message": "Bad credentials"})
        if method == "GET" and path == "/repos/o/r":
            return HttpAnswer(200, {"github-authentication-token-expiration":
                                    "2099-01-02 10:00:00 UTC"},
                              {"full_name": "o/r", "default_branch": "main", "private": True,
                               "permissions": {"push": True}})
        denied = HttpAnswer(403, {}, {"message": "Resource not accessible by personal access token"})
        if path.endswith("/git/blobs"):
            return HttpAnswer(201, {}, {"sha": "abc"}) if self.contents_write else denied
        if path.endswith("/pulls"):
            return HttpAnswer(422, {}, {"message": "Validation Failed"}) \
                if self.pull_requests else denied
        raise AssertionError(path)


def test_what_the_install_already_runs_on_is_checked_not_assumed():
    """`permissions.push` is the account's role, not what a fine-grained token may do — a
    green "Can push" could still fail at push or PR time. So the check WRITES (a blob nobody
    sees) and ASKS TO OPEN A PR (with a head that does not exist)."""
    fake = FakeGitHub()
    found = setup_checks.probe_repository("t", "o/r", http=fake)
    assert found["state"] == "working"
    assert [m for m, _ in fake.calls] == ["GET", "POST", "POST"]
    assert found["expires_at"].startswith("2099-01-02")


def test_a_read_only_token_is_not_reported_as_connected():
    """It looks connected and produces a pull request that never opens."""
    found = setup_checks.probe_repository("t", "o/r", http=FakeGitHub(contents_write=False))
    assert found["state"] == "cant_write"
    assert found["checks"]["read"]["ok"] is True and found["checks"]["write"]["ok"] is False


def test_nor_one_that_cannot_open_pull_requests():
    found = setup_checks.probe_repository("t", "o/r", http=FakeGitHub(pull_requests=False))
    assert found["state"] == "cant_open_prs"


def test_a_revoked_token_says_so():
    found = setup_checks.probe_repository("t", "o/r", http=FakeGitHub(status=401))
    assert found["state"] == "token_rejected"


def test_the_credential_itself_never_leaves():
    """No part of a token or key in anything a check returns."""
    token = "github_pat_" + "Z" * 40
    fake = FakeGitHub()
    found = setup_checks.probe_repository(token, "o/r", http=fake)
    blob = json.dumps(found)
    for size in range(4, 12):
        assert token[-size:] not in blob and token[:size] not in blob


def test_a_probe_never_raises_at_somebody_on_their_first_run():
    result = setup_checks.default_http("GET", "http://127.0.0.1:1/nope", headers={}, timeout=3)
    assert result.status == 0
    assert result.error
