"""Provider agnosticism, as a property you can check rather than a claim in a README.

Two things were missing. First, pointing BOSS at one provider took SIX settings in THREE
vocabularies — `BOSS_LLM_ENGINE` and `BOSS_CHAT_LLM_ENGINE` speak `claude_cli`, the coding
lanes speak harness names, the strong/low-cost pair speak a third — so an operator who missed
one was indistinguishable from one who set them all. Second, there was no way to check the
result: config says what was ASKED FOR, and only the llm_calls ledger says what HAPPENED.

The distinction that makes the audit honest: routing `anthropic/claude-opus-4.5` through
OpenRouter means the transport is OpenRouter and the weights are Anthropic's. Your prompt
still reached Anthropic. A report that collapsed those would let someone claim "no Anthropic"
while sending Anthropic every token.
"""

from __future__ import annotations

import pytest

from boss_engine.config import Settings
from boss_engine.services import provider_audit as pa
from boss_engine.services.harness_registry import available_harnesses, get_harness


# --- the one switch -----------------------------------------------------------

def test_nothing_changes_when_the_lock_is_unset() -> None:
    """An installation that already tuned its per-surface settings must not be silently
    re-pointed by an upgrade."""
    assert Settings().provider_lock == ""
    assert Settings(BOSS_CODE_AGENT_ENGINE="groq").code_agent_engine == "groq"


@pytest.mark.parametrize("lock", ["openrouter", "groq", "deepseek", "ollama", "vllm"])
def test_the_lock_points_every_coding_surface_at_one_provider(lock: str) -> None:
    s = Settings(BOSS_PROVIDER_LOCK=lock)
    assert s.code_agent_engine == lock
    assert s.code_review_engine == lock
    assert s.code_agent_strong_engine == lock
    assert s.code_agent_low_cost_engine == lock


def test_the_lock_also_moves_chat_meetings_and_discovery() -> None:
    """They all resolve through make_chat_adapter, which stays on Claude for the exact
    string 'claude_cli'. A lock that left those behind would be a half-move nobody could see."""
    s = Settings(BOSS_PROVIDER_LOCK="openrouter", OPENROUTER_API_KEY="k")
    assert s.llm_engine == "openrouter" and s.chat_llm_engine == "openrouter"
    assert s.llm_engine != "claude_cli"


def test_locking_to_an_api_lane_binds_the_credentials_chat_actually_reads() -> None:
    """Chat goes through OpenAIAdapter, which reads openai_api_key/openai_base_url. Without
    binding them a 'lock' would leave those surfaces calling whatever OPENAI_* happened to be
    set — the exact silent mis-routing this feature exists to end."""
    s = Settings(BOSS_PROVIDER_LOCK="openrouter", OPENROUTER_API_KEY="or-key")
    assert s.openai_api_key == "or-key"
    assert s.openai_base_url == "https://openrouter.ai/api/v1"


def test_locking_away_from_azure_clears_azure() -> None:
    """OpenAIAdapter dispatches to Azure whenever AZURE_OPENAI_* are present, which would
    beat the lock silently."""
    s = Settings(BOSS_PROVIDER_LOCK="deepseek", DEEPSEEK_API_KEY="d",
                 AZURE_OPENAI_KEY="a", AZURE_OPENAI_ENDPOINT="https://e",
                 AZURE_OPENAI_DEPLOYMENT="dep")
    assert s.azure_openai_key is None and s.azure_openai_endpoint is None


def test_locking_to_claude_puts_chat_back_on_the_claude_cli() -> None:
    s = Settings(BOSS_PROVIDER_LOCK="claude_code")
    assert s.code_agent_engine == "claude_code"
    assert s.llm_engine == "claude_cli" and s.chat_llm_engine == "claude_cli"


# --- transport is not vendor --------------------------------------------------

@pytest.mark.parametrize(
    "model,vendor",
    [("anthropic/claude-opus-4.5", "anthropic"), ("claude-3-haiku", "anthropic"),
     ("gpt-5.2-codex", "openai"), ("openai/gpt-4o", "openai"),
     ("deepseek/deepseek-chat", "deepseek"), ("google/gemini-3.1-pro", "google"),
     ("z-ai/glm-4.5", "zhipu"), ("moonshotai/kimi-k2", "moonshot"),
     ("qwen/qwen3-32b", "alibaba"), ("", "unknown"), ("something-unheard-of", "unknown")],
)
def test_the_vendor_is_read_from_the_model_not_the_endpoint(model: str, vendor: str) -> None:
    assert pa.vendor_of(model) == vendor


def test_claude_over_openrouter_is_still_anthropic() -> None:
    """THE point of the audit. Anything else lets an operator believe they left Anthropic
    while sending Anthropic every token."""
    [bucket] = pa.summarise([
        {"engine": "openrouter", "model": "anthropic/claude-opus-4.5",
         "stage": "review_code", "ok": True, "cost_usd_micros": 2000},
    ])
    assert bucket["transport"] == "openrouter"
    assert bucket["vendor"] == "anthropic"


def test_a_claim_of_no_anthropic_is_refuted_by_the_ledger() -> None:
    summary = pa.summarise([
        {"engine": "openrouter", "model": "deepseek/deepseek-chat", "ok": True,
         "cost_usd_micros": 500},
        {"engine": "openrouter", "model": "anthropic/claude-opus-4.5", "ok": True,
         "cost_usd_micros": 2000},
    ])
    bad = pa.violations(summary, expect="deepseek", dimension="vendor")
    assert [b["vendor"] for b in bad] == ["anthropic"]


def test_a_clean_run_reports_no_violations() -> None:
    summary = pa.summarise([
        {"engine": "openrouter", "model": "deepseek/deepseek-chat", "ok": True,
         "cost_usd_micros": 500},
    ])
    assert pa.violations(summary, expect="deepseek") == []
    assert pa.violations(summary, expect=None) == []


def test_failures_are_counted_separately_from_successes() -> None:
    """A provider that was CALLED and refused still received the prompt."""
    [bucket] = pa.summarise([
        {"engine": "claude_code", "model": "claude-opus-4.5", "ok": False,
         "cost_usd_micros": 0},
    ])
    assert bucket["calls"] == 1 and bucket["failed"] == 1 and bucket["ok"] == 0


# --- CLI harnesses are declared, not coded ------------------------------------

@pytest.mark.parametrize("name", ["codex", "cursor", "aider"])
def test_the_popular_cli_agents_are_registered(name: str) -> None:
    assert get_harness(name).kind == "cli"


def test_an_unproven_harness_says_so() -> None:
    """A spec written from a CLI's documentation is a hypothesis. Presenting it as ready is
    how a registry stops meaning anything."""
    for name in ("codex", "cursor", "aider"):
        assert get_harness(name).verified is False
    assert get_harness("claude_code").verified is True


def test_a_missing_binary_is_a_readable_error_not_a_crash() -> None:
    from boss_engine.services.cli_harness import (
        CliHarnessSpec,
        CliHarnessUnavailable,
        GenericCliCodeAgent,
    )

    agent = GenericCliCodeAgent(
        Settings(), CliHarnessSpec(name="nope", binary="definitely-not-installed-xyz"))
    with pytest.raises(CliHarnessUnavailable, match="not installed"):
        agent.run_role("do a thing", __import__("pathlib").Path("."))


def test_a_cli_with_no_read_only_mode_is_refused_for_a_review() -> None:
    """'The reviewer cannot edit' is an invariant of the governed pipeline. A CLI that
    cannot hold it must be refused for the step, not trusted to behave."""
    from boss_engine.services.cli_harness import (
        BUILTIN_CLI_HARNESSES,
        CliHarnessUnavailable,
        GenericCliCodeAgent,
    )

    # aider, not codex: codex-cli 0.154.0 DOES have `--sandbox read-only`, which is one of
    # the guesses first contact with the binary corrected. Using it here would both assert the
    # wrong thing and spend the operator's subscription — a unit test must never make a paid
    # call, and this one took 22s doing exactly that before it was changed.
    assert BUILTIN_CLI_HARNESSES["aider"].supports_read_only() is False
    agent = GenericCliCodeAgent(Settings(), BUILTIN_CLI_HARNESSES["aider"])
    with pytest.raises(CliHarnessUnavailable, match="read-only"):
        agent.run_role("review this", __import__("pathlib").Path("."), read_only=True)


def test_codex_can_hold_the_reviewer_cannot_edit_invariant() -> None:
    """Verified against codex-cli 0.154.0: `--sandbox read-only` is a real value on a real
    flag, so Codex can run a review step without being able to change what it reviews."""
    from boss_engine.services.cli_harness import BUILTIN_CLI_HARNESSES

    codex = BUILTIN_CLI_HARNESSES["codex"]
    assert codex.supports_read_only()
    assert codex.read_only_args == ("--sandbox", "read-only")
    # The WRITE mode had to change: `workspace-write` makes codex enforce its own
    # boundary with kernel facilities a container does not grant, so it wrote nothing
    # and said "the environment blocked file operations" while exiting 0. See
    # test_codex_can_write_in_its_sandbox. The invariant this test protects — a
    # reviewer cannot edit — is the read-only assertion above, and it is untouched.
    assert codex.write_args == ("--sandbox", "danger-full-access")


def test_no_unit_test_spends_the_operators_subscription() -> None:
    """A test that invokes a signed-in CLI costs real quota and takes tens of seconds. The
    refusal paths must trip BEFORE any process starts."""
    import inspect

    from boss_engine.services import cli_harness

    src = inspect.getsource(cli_harness.GenericCliCodeAgent.run_role)
    refusal = src.index("has no read-only mode")
    launch = src.index("subprocess.run")
    assert refusal < launch, "the read-only refusal must come before the process launch"


def test_an_operator_can_correct_the_flags_without_a_release() -> None:
    """These flags are a guess until someone runs the binary."""
    from boss_engine.services.cli_harness import load_specs

    specs = load_specs(Settings(
        BOSS_CLI_HARNESSES_JSON='{"codex": {"args": ["exec", "--json"], '
                                '"read_only_args": ["--read-only"]}}'))
    assert specs["codex"].args == ("exec", "--json")
    assert specs["codex"].supports_read_only()


def test_bad_override_json_does_not_take_the_harnesses_down() -> None:
    from boss_engine.services.cli_harness import load_specs

    specs = load_specs(Settings(BOSS_CLI_HARNESSES_JSON="{not json"))
    assert "codex" in specs and specs["codex"].binary == "codex"


def test_a_cli_harness_never_invents_a_cost() -> None:
    """A subscription CLI does not report per-call spend, and a guessed number would corrupt
    the one place BOSS answers cost questions from."""
    import inspect

    from boss_engine.services import cli_harness

    src = inspect.getsource(cli_harness.GenericCliCodeAgent.run_role)
    assert "cost_usd_micros=0" in src
    assert "cost_unreported" in inspect.getsource(cli_harness.GenericCliCodeAgent)


def test_every_harness_still_declares_what_it_needs() -> None:
    for h in available_harnesses():
        assert h["requires"] and h["description"], h["name"]


# --- a customer connects their own subscription -------------------------------
#
# The point the operator made that produced this: hand-editing a spec and running a script
# proves nothing about what a CUSTOMER can do. A subscription has no credential to paste —
# a Codex or Cursor plan is an OAuth session the CLI holds — so a form asking for one would
# be asking for something that does not exist. BOSS finds the CLI they already use.

def test_detection_reports_what_is_missing_and_the_command_that_fixes_it() -> None:
    """"Not configured" sends somebody to search. A next command does not."""
    from boss_engine.services.cli_harness import CliHarnessSpec, detect

    found = detect(CliHarnessSpec(
        name="nope", binary="definitely-not-installed-xyz", signin_hint="nope login",
        install_hint="npm install -g nope"))
    assert found["installed"] is False and found["ready"] is False
    # The next step is the COMMAND to run, not a sentence about it. The sign-in command is
    # carried separately so the wizard can show both steps at once and mark off the done one.
    assert found["next_step"] == "npm install -g nope"
    assert found["signin_hint"] == "nope login"


def test_a_spec_with_no_install_command_still_says_something_useful() -> None:
    from boss_engine.services.cli_harness import CliHarnessSpec, detect

    found = detect(CliHarnessSpec(name="nope", binary="definitely-not-installed-xyz"))
    assert "install" in (found["next_step"] or "")


def test_detection_never_raises_because_both_answers_are_useful() -> None:
    """Not installed and installed-but-signed-out are different problems with different next
    steps. A probe that throws collapses them into 'error'."""
    from boss_engine.services.cli_harness import CliHarnessSpec, detect

    assert detect(CliHarnessSpec(name="x", binary="no-such-binary-zzz"))["installed"] is False


def test_a_cli_with_no_login_of_its_own_is_ready_once_installed() -> None:
    """aider reads an API key from the environment; there is nothing to sign in to, and
    reporting it as 'not authenticated' forever would be a permanent false negative."""
    from boss_engine.services.cli_harness import BUILTIN_CLI_HARNESSES

    assert BUILTIN_CLI_HARNESSES["aider"].auth_check_args == ()


def test_the_subscription_agents_know_how_to_be_signed_in() -> None:
    from boss_engine.services.cli_harness import BUILTIN_CLI_HARNESSES as B

    assert B["codex"].signin_hint == "codex login"
    assert B["codex"].auth_check_args == ("login", "status")
    assert B["codex"].entitlement_source_id == "openai_codex_chatgpt"


def test_connecting_uses_boss_own_oauth_vocabulary() -> None:
    """BOSS already names these; a ninth spelling would make the broker treat a real OAuth
    subscription as an unrecognised one."""
    import inspect

    from boss_engine.services import setup_service
    from boss_engine.services.entitlements import EntitlementBrokerService

    src = inspect.getsource(setup_service.connect_coding_agent)
    assert '"official_cli_oauth"' in src
    assert "official_cli_oauth" in EntitlementBrokerService.OFFICIAL_OAUTH_METHODS


def test_a_subscription_seat_is_not_offered_to_multi_tenant_work() -> None:
    """It is the operator's own seat, and the provider's terms say so."""
    import inspect

    from boss_engine.services import setup_service

    assert '"multi_tenant_allowed": False' in inspect.getsource(
        setup_service.connect_coding_agent)


def test_connect_refuses_rather_than_half_succeeding() -> None:
    """Signed in is not the same as working: a CLI reports 'logged in' from a token on disk,
    which says somebody signed in once — not that the session still works or the plan is
    active. The same rule verify_model_key already applies to an API key."""
    import inspect

    from boss_engine.services import setup_service

    src = inspect.getsource(setup_service.connect_coding_agent)
    assert "is not installed on this machine" in src
    assert "installed but not signed in" in src
    assert "could not complete a turn" in src


def test_the_proof_turn_cannot_edit_anything() -> None:
    """Proving a connection must not be able to change the customer's code."""
    import inspect

    from boss_engine.services import setup_service

    src = inspect.getsource(setup_service.prove_coding_agent)
    assert "read_only=read_only" in src and "supports_read_only()" in src


def test_boss_never_stores_a_subscription_credential() -> None:
    """There is nothing to store. If this ever grows a secret_ref the claim above is false."""
    import inspect

    from boss_engine.services import setup_service

    src = inspect.getsource(setup_service.connect_coding_agent)
    assert "credential_secret_ref_id" not in src
    assert "api_key" not in src


# --- a subscription on a laptop, a pipeline in a container --------------------
#
# Detection answers "is the CLI on the machine BOSS is running on". For a self-hosted install
# that is the whole answer. For a deployed one it is no answer at all: the operator's login is
# on their laptop and the pipeline runs in a container. Measured on the real deployment — the
# the private host API reported `codex installed=False` while the same check on the laptop said ready,
# which is correct and useless at the same time.
#
# So what matters for a shipped product is whether the subscription can TRAVEL.

def test_a_subscription_says_whether_it_can_reach_a_server() -> None:
    """Portability is about having ANY route to a server, not specifically a token.

    This test used to assert codex took a token on stdin, which is what
    `codex login --with-access-token` looks like it should mean. Measured against codex-cli
    0.154.0 with a real ChatGPT plan, that flag rejects access_token, id_token and
    refresh_token alike — it wants an agent identity JWT, which a consumer plan never mints —
    and CODEX_ACCESS_TOKEN in the environment makes `codex login status` error outright.
    Restoring the CLI's own auth file signs it in. So codex is portable by FILE.
    """
    from boss_engine.services.cli_harness import BUILTIN_CLI_HARNESSES as B

    assert B["codex"].auth_file_secret == "CODEX_AUTH_JSON"
    assert not B["codex"].token_env, "the token route was measured not to work for codex"
    # cursor has neither route, so it can only be used where its login lives.
    assert B["cursor"].token_env == ""
    assert B["cursor"].auth_file_secret == ""


def test_detection_reports_portability_not_just_presence() -> None:
    from boss_engine.config import Settings
    from boss_engine.services.cli_harness import detect_all

    by_name = {d["name"]: d for d in detect_all(Settings())}
    assert by_name["codex"]["portable"] is True
    assert by_name["cursor"]["portable"] is False


def test_both_coding_clis_are_in_the_image() -> None:
    """A harness the control plane cannot START is a harness that accepts work and never
    performs it — the same failure that left runs queued for weeks when git was missing."""
    from pathlib import Path

    dockerfile = (Path("Dockerfile.community") if Path("Dockerfile.community").exists()
                  else Path("Dockerfile"))
    if not dockerfile.exists():  # pragma: no cover - engine installed standalone
        pytest.skip("Dockerfile not in this checkout")
    text = dockerfile.read_text()
    assert "@anthropic-ai/claude-code" in text
    assert "@openai/codex" in text


def test_the_image_ships_no_credential() -> None:
    """The image carries the binary; the deployment supplies the token."""
    from pathlib import Path

    dockerfile = Path("Dockerfile")
    if not dockerfile.exists():  # pragma: no cover
        pytest.skip("Dockerfile not in this checkout")
    text = dockerfile.read_text()
    for secret in ("CODEX_ACCESS_TOKEN=", "CLAUDE_CODE_OAUTH_TOKEN=", "sk-"):
        assert secret not in text


def test_a_container_is_not_told_to_open_a_browser() -> None:
    """"Run codex login" is advice a container's operator cannot take."""
    from boss_engine.services.cli_harness import _is_interactive_host

    assert isinstance(_is_interactive_host(), bool)
    import inspect

    from boss_engine.services import cli_harness

    src = inspect.getsource(cli_harness._is_interactive_host)
    assert "/.dockerenv" in src and "KUBERNETES_SERVICE_HOST" in src


# --- the token that carries a subscription to a server ------------------------

def test_a_cli_with_no_token_route_says_so_rather_than_trying() -> None:
    from boss_engine.services.cli_harness import BUILTIN_CLI_HARNESSES, GenericCliCodeAgent

    agent = GenericCliCodeAgent(Settings(), BUILTIN_CLI_HARNESSES["cursor"])
    state = agent.ensure_authorised()
    assert state["authorised"] is None and "no token route" in state["reason"]


def test_an_uninstalled_cli_is_not_a_token_problem() -> None:
    """Different causes need different next steps; collapsing them into "failed" is how an
    operator ends up re-minting a token that was never the problem."""
    from boss_engine.services.cli_harness import CliHarnessSpec, GenericCliCodeAgent

    agent = GenericCliCodeAgent(Settings(), CliHarnessSpec(
        name="nope", binary="definitely-not-installed-xyz",
        token_env="NOPE_TOKEN", token_login_args=("login", "--with-access-token")))
    assert agent.ensure_authorised()["reason"] == "not installed"


def test_the_token_is_piped_not_passed_as_an_argument() -> None:
    """argv is visible in `ps` to every process on the box. A subscription credential must
    never appear there."""
    import inspect

    from boss_engine.services import cli_harness

    src = inspect.getsource(cli_harness.GenericCliCodeAgent.ensure_authorised)
    assert "input=token" in src, "the token must arrive on stdin"
    # The argv is the binary and the spec's flags, and nothing else. Asserted literally
    # rather than by searching the slice for "token" — `token_login_args` contains that
    # word, which made the first version of this test pass judgement on its own variable
    # names instead of on where the secret goes.
    assert "[self._resolve_binary(), *self.spec.token_login_args]" in src


def test_a_rejected_token_is_never_echoed_into_the_log() -> None:
    """The CLI's own failure message can quote what it was given."""
    import inspect

    from boss_engine.services import cli_harness

    src = inspect.getsource(cli_harness.GenericCliCodeAgent.ensure_authorised)
    assert "only the status is safe to keep" in src
    assert "proc.stderr" not in src


def test_the_environment_wins_over_the_stored_secret() -> None:
    """What an operator exports while debugging must not be silently overridden by a store.

    Measured on the resolver both routes now share: `_resolve_token` used to hold a second
    copy of this lookup, which read the store under the plain key only — so a credential
    stored by BOSS's own API, which namespaces what it writes, was invisible to it.
    """
    import inspect

    from boss_engine.services import cli_harness

    # ONE resolver, shared: the chat adapter shells out to the same CLIs and had no way to
    # reach the stored credential at all, which is how a connected subscription produced
    # "OPENAI_API_KEY is not configured".
    src = inspect.getsource(cli_harness.stored_credential)
    assert src.index("os.environ.get") < src.index("backend_for")
    # And both routes reach it rather than reimplementing it.
    assert "stored_credential(self.settings, name)" in inspect.getsource(
        cli_harness.GenericCliCodeAgent._resolve_secret)
    assert "_resolve_secret(self.spec.token_env)" in inspect.getsource(
        cli_harness.GenericCliCodeAgent._resolve_token)


def test_signing_in_is_attempted_once_per_agent_not_per_call() -> None:
    """detect() shells out twice; doing that before every stage would add it to every step
    of the pipeline."""
    import inspect

    from boss_engine.services import cli_harness

    src = inspect.getsource(cli_harness.GenericCliCodeAgent.run_role)
    assert "_auth_checked" in src
