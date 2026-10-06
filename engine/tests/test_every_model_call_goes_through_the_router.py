"""Every skill, executor and agent step gets its model from the project's routing.

"Video edit and publish" → step `edit` → DEADLETTERED:

    IntegrationConfigurationError: OPENAI_API_KEY is not configured

on an install with Claude Code and Codex subscriptions connected and no OpenAI key, where
readiness had just said "a model is connected". The agent loop built its model from the
deployment settings (`llm_engine` = "auto" = the OpenAI adapter) and never asked which model
the project had connected. Four executors had each been patched with their own copy of the
resolution; the agent loop and `model.generate_code` never got one.

The fix is one router (`model_routing.project_chat_adapter`) and one declared path for what only
one provider can do (`register_executor(..., requires=...)`), which readiness checks before the
run. These tests hold both, over EVERY registered executor, so the next skill cannot reopen it.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text

from boss_engine.config import Settings
from boss_engine.services import model_routing, skill_executors
from boss_engine.services.claude_cli_adapter import ClaudeCliAdapter
from boss_engine.services.openai_adapter import OpenAIAdapter

#: Building a model adapter from bare settings, or reading a provider key, outside the router.
_FORBIDDEN_CALLS = frozenset({
    "make_chat_adapter", "make_agent_chat_adapter", "make_codex_chat_adapter",
    "OpenAIAdapter", "ClaudeCliAdapter", "CodexCliAdapter", "chat_engine_for",
})
_FORBIDDEN_NAMES = frozenset({"openai_api_key", "OPENAI_API_KEY"})
#: Calls that mean "this asks a model something".
_MODEL_CALLS = frozenset({"create_chat_completion", "create_text_response",
                          "create_plain_answer"})


def _no_key_settings(**extra) -> Settings:
    return Settings().model_copy(update={
        "openai_api_key": None, "azure_openai_key": None, "llm_engine": "auto",
        "chat_llm_engine": "", "chat_model": "", **extra})


def _called_names(tree: ast.AST) -> set[str]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            out.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
    return out


def _mentions(tree: ast.AST, names: frozenset[str]) -> set[str]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in names:
            found.add(node.attr)
        elif isinstance(node, ast.Name) and node.id in names:
            found.add(node.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and node.value in names:
            found.add(node.value)
    return found


# ── The class of bug, over every executor ─────────────────────────────────────────────────────

def test_every_model_call_goes_through_the_router():
    """Each registered executor that calls a model resolves it with `project_chat_adapter`, or
    declared a provider requirement — and none builds an adapter or reads a key itself."""
    assert skill_executors._EXECUTORS, "no executors registered; the import changed"
    problems = []
    routed = []
    for skill_id, handler in sorted(skill_executors._EXECUTORS.items()):
        tree = ast.parse(textwrap.dedent(inspect.getsource(handler)))
        calls = _called_names(tree)
        bypass = (calls & _FORBIDDEN_CALLS) | _mentions(tree, _FORBIDDEN_NAMES)
        if bypass:
            problems.append(f"{skill_id} builds its own model or reads a key: {sorted(bypass)}")
        uses_router = "project_chat_adapter" in calls
        declared = bool(model_routing.declared_requirements(skill_id))
        if calls & _MODEL_CALLS and not (uses_router or declared):
            problems.append(f"{skill_id} calls a model without the project's routing")
        if declared and "provider_credential" not in calls:
            problems.append(f"{skill_id} declares a provider requirement but never reads it "
                            "through model_routing.provider_credential")
        if uses_router:
            routed.append(skill_id)
    assert not problems, "\n".join(problems)
    # The ones this bug was about, by name, so the enumeration cannot pass by finding nothing.
    for skill_id in ("model.generate_code", "model.generate_text", "meeting.extract_actions",
                     "video.plan_edit"):
        assert skill_id in routed, f"{skill_id} no longer resolves the project's model"


#: Modules allowed to build a chat adapter themselves, each for a reason that is not "this caller
#: resolves the project's model on its own". Anything else in `boss_engine` must ask the router.
_ADAPTER_EXEMPT = {
    # The router itself.
    "services/model_routing.py": "the router",
    # The adapter factories: they define what the router builds.
    "services/claude_cli_adapter.py": "adapter factory",
    "services/codex_cli_adapter.py": "adapter factory",
    "services/openai_adapter.py": "adapter factory",
    # The project's model DECISION, now a thin reader of model_routing.choose.
    "services/setup_service.py": "defines chat_engine_for as a wrapper over model_routing.choose",
    # The build lanes: NativeCodeAgent is constructed by make_code_agent with settings already
    # bound to the project's lane credential (_bind_lane_credentials), the build-side router.
    "services/code_agent.py": "build lane agent, bound per project by make_code_agent",
    # Typed decisions spend through their own governed path (typed_decision_spend: reservation,
    # gateway lease, receipt) on the decision model the step names — not the chat model.
    "services/typed_decisions.py": "governed decision model, own reservation/lease/receipt",
}


def _package_files():
    import pathlib

    import boss_engine

    root = pathlib.Path(boss_engine.__file__).parent
    for path in sorted(root.rglob("*.py")):
        yield path.relative_to(root).as_posix(), path


def test_nothing_in_boss_engine_builds_a_chat_adapter_outside_the_router():
    """The whole package, not a list of modules: the sixth pasted copy (meeting_service) and the
    bare `make_chat_adapter(settings)` calls in PRD decomposition, the intent classifier, the PR
    reviewer, the software factory and the engine agent all passed an allow-list of five."""
    found = []
    for rel, path in _package_files():
        if rel in _ADAPTER_EXEMPT:
            continue
        bypass = _called_names(ast.parse(path.read_text())) & _FORBIDDEN_CALLS
        if bypass:
            found.append(f"{rel}: {sorted(bypass)}")
    assert not found, ("these build a model adapter outside model_routing — call "
                       "model_routing.project_chat_adapter instead:\n" + "\n".join(found))


def test_the_exemptions_are_still_needed():
    """An exemption nobody needs is a hole the next caller walks through."""
    files = dict(_package_files())
    for rel in _ADAPTER_EXEMPT:
        if rel == "services/model_routing.py":
            continue
        tree = ast.parse(files[rel].read_text())
        defined = {n.name for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        assert (_called_names(tree) | defined) & _FORBIDDEN_CALLS, f"{rel} no longer needs it"


def test_no_agent_tool_or_executor_reads_a_provider_key_itself():
    """generate_image read OPENAI_API_KEY from the environment, so a key stored through the
    console was invisible to it. Tools (the chat agent's and MCP's catalogue), the agent and the
    executors read a provider key only through model_routing.provider_credential."""
    found = []
    for rel, path in _package_files():
        if not (rel.startswith(("tools/", "agent/")) or rel == "services/skill_executors.py"):
            continue
        hits = _mentions(ast.parse(path.read_text()), _FORBIDDEN_NAMES)
        if hits:
            found.append(f"{rel}: {sorted(hits)}")
    assert not found, found


# ── The router, against a project's connected sources ─────────────────────────────────────────

@pytest.fixture()
def sources(entitlement_tables):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as conn:
        entitlement_tables(conn)
    with engine.begin() as conn:
        yield conn


def _connect(conn, source_id):
    conn.execute(text(
        "INSERT INTO entitlement_sources (project_id, source_id, provider,"
        " connection_status, credential_secret_ref_id, updated_at)"
        " VALUES ('p', :s, 'x', 'connected', NULL, :at)"),
        {"s": source_id, "at": datetime(2026, 10, 3, tzinfo=timezone.utc)})


def test_a_subscription_only_project_routes_to_the_subscription(sources):
    _connect(sources, "anthropic_claude_code")
    _connect(sources, "openai_codex_chatgpt")
    adapter = model_routing.project_chat_adapter(sources, _no_key_settings(), "p")
    assert isinstance(adapter, ClaudeCliAdapter), type(adapter).__name__


def test_a_key_only_install_is_left_on_its_key(sources):
    """The router must not move an install that has always run on an API key."""
    adapter = model_routing.project_chat_adapter(
        sources, _no_key_settings(openai_api_key="sk-test-not-real"), "p")
    assert isinstance(adapter, OpenAIAdapter)


def test_the_console_choice_wins(sources):
    from boss_engine.services.codex_cli_adapter import CodexCliAdapter

    _connect(sources, "anthropic_claude_code")
    sources.execute(text("INSERT INTO project_settings (project_id, key, value) "
                         "VALUES ('p', 'chat_llm_engine', 'codex')"))
    adapter = model_routing.project_chat_adapter(sources, _no_key_settings(), "p")
    assert isinstance(adapter, CodexCliAdapter), type(adapter).__name__


def test_the_agent_loop_takes_its_model_from_the_router(monkeypatch):
    """Behaviour, not source: `_drive_run` asks the router with the RUN's project."""
    from boss_engine.services import agent_loop

    asked = {}

    class _Stop(Exception):
        pass

    def router(connection, settings, project_id, purpose=None):
        asked["project"] = project_id
        raise _Stop

    monkeypatch.setattr(agent_loop, "project_chat_adapter", router)
    monkeypatch.setattr(agent_loop, "load_agent_run", lambda _c, _r: {
        "project_id": "proj-x", "task_id": None, "messages": [], "transcript": [],
        "usage": None, "finalize_ctx": {}, "step": 0})
    with pytest.raises(_Stop):
        agent_loop._drive_run(None, _no_key_settings(), "run-1", 1)
    assert asked == {"project": "proj-x"}


# ── A stored key: readiness and the router give ONE answer ────────────────────────────────────

_STORED_KEY = "sk-stored-in-the-console-not-real"


@pytest.fixture()
def project(sources, monkeypatch, setup_tables):
    """`sources` plus the GitHub tables readiness reads, and a secret store that answers."""
    setup_tables(sources)
    from boss_engine.services import credential_resolve, secret_backends

    def resolve(_connection, ref_id, _settings):
        assert ref_id == "sr_stored", ref_id
        return _STORED_KEY

    monkeypatch.setattr(secret_backends, "resolve_secret_ref", resolve)
    monkeypatch.setattr(credential_resolve, "resolve_secret_ref", resolve)
    return sources


def _store_key(conn, provider: str, *, base_url: str = "", model: str = "") -> None:
    """What POST /v1/setup/model-key records: a `setup-<provider>` source and its SecretRef."""
    import json

    from boss_engine.services.setup_service import PROVIDERS

    conn.execute(text(
        "INSERT INTO entitlement_sources (project_id, source_id, provider, connection_status,"
        " credential_secret_ref_id, updated_at, supported_models, connection_metadata)"
        " VALUES ('p', :s, :prov, 'declared', 'sr_stored', :at, :models, :meta)"),
        {"s": f"setup-{provider}", "prov": provider,
         "at": datetime(2026, 10, 3, tzinfo=timezone.utc),
         "models": json.dumps([model or PROVIDERS[provider].get("example_model") or "m"]),
         "meta": json.dumps({"base_url": base_url or PROVIDERS[provider].get("base_url") or ""})})
    conn.execute(text("INSERT INTO secret_refs (id, project_id, provider, status, updated_at)"
                      " VALUES ('sr_stored', 'p', :prov, 'active', :at)"),
                 {"prov": provider, "at": datetime(2026, 10, 3, tzinfo=timezone.utc)})


def _readiness_says_connected(conn, settings) -> bool:
    from boss_engine.services import workflow_readiness as wr

    return wr.check(conn, settings, "p", "model")[0]


def test_a_stored_openai_key_with_no_env_key_is_what_the_step_runs_on(project):
    """THE REVIEW'S REPRO: only a `setup-openai` key row. Readiness said "a model is connected";
    the adapter was `OpenAIAdapter(settings)` with no connection, read the empty env key, and the
    step died with "OPENAI_API_KEY is not configured"."""
    _store_key(project, "openai")
    settings = _no_key_settings()
    assert _readiness_says_connected(project, settings)
    adapter = model_routing.project_chat_adapter(project, settings, "p")
    assert isinstance(adapter, OpenAIAdapter), type(adapter).__name__
    assert adapter._resolved_api_key() == _STORED_KEY


@pytest.mark.parametrize("provider,base_url", [
    ("openrouter", "https://openrouter.ai/api/v1"),
    ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/"),
    ("xai", "https://api.x.ai/v1"),
])
def test_a_stored_key_for_another_provider_reaches_that_provider(project, provider, base_url):
    """Gemini, OpenRouter and xAI fell outside the three engines the factory knew, and got the
    OpenAI adapter on OpenAI's URL with no key at all."""
    _store_key(project, provider, model=f"{provider}-model")
    settings = _no_key_settings()
    assert _readiness_says_connected(project, settings)
    adapter = model_routing.project_chat_adapter(project, settings, "p")
    assert isinstance(adapter, OpenAIAdapter), type(adapter).__name__
    assert adapter._resolved_api_key() == _STORED_KEY
    assert adapter._resolved_base_url() == base_url
    assert adapter.settings.openai_model == f"{provider}-model"


@pytest.mark.parametrize("provider", ["openai", "openrouter", "gemini", "xai", "custom",
                                      "anthropic"])
def test_readiness_and_the_router_agree_for_every_key_the_console_can_store(project, provider):
    """Every API-key provider the setup screen offers: if readiness says connected, the router
    builds an adapter that holds a credential — never the keyless fallback."""
    from boss_engine.services.setup_service import PROVIDERS

    assert PROVIDERS[provider]["kind"] == "api_key"
    _store_key(project, provider, base_url=("http://models.example:4000/v1"
                                            if provider == "custom" else ""))
    settings = _no_key_settings()
    choice = model_routing.choose(project, settings, "p")
    assert _readiness_says_connected(project, settings) == choice.runnable is True
    adapter = model_routing.project_chat_adapter(project, settings, "p")
    if isinstance(adapter, OpenAIAdapter):
        assert adapter._resolved_api_key() == _STORED_KEY, provider
    else:
        assert isinstance(adapter, ClaudeCliAdapter), type(adapter).__name__


def test_nothing_connected_is_not_connected_for_either(project):
    settings = _no_key_settings()
    assert not _readiness_says_connected(project, settings)
    assert not model_routing.choose(project, settings, "p").runnable


def test_a_connected_source_no_chat_adapter_can_drive_is_not_a_connected_model(project):
    """A Cursor subscription was counted as "a model" while nothing could answer through it."""
    _connect(project, "cursor_subscription")
    project.execute(text("UPDATE entitlement_sources SET source_id='cursor' "
                         "WHERE source_id='cursor_subscription'"))
    assert not model_routing.choose(project, _no_key_settings(), "p").runnable
    assert not _readiness_says_connected(project, _no_key_settings())


# ── Capacity and spend ─────────────────────────────────────────────────────────────────────────

def test_an_exhausted_subscription_is_passed_over_for_one_with_capacity(sources):
    from boss_engine.services.codex_cli_adapter import CodexCliAdapter

    _connect(sources, "anthropic_claude_code")
    _connect(sources, "openai_codex_chatgpt")
    sources.execute(text(
        "UPDATE entitlement_sources SET observed_capacity = :c "
        "WHERE source_id = 'anthropic_claude_code'"),
        {"c": '{"exhausted": true, "retry_at": "2999-01-01T00:00:00+00:00"}'})
    choice = model_routing.choose(sources, _no_key_settings(), "p")
    assert choice.engine == "codex", choice
    adapter = model_routing.project_chat_adapter(sources, _no_key_settings(), "p")
    assert isinstance(adapter, CodexCliAdapter), type(adapter).__name__


def test_a_subscription_never_falls_back_to_a_paid_key_unasked(sources):
    """`make_agent_chat_adapter` wraps Claude in FallingBackAdapter(claude, OpenAI) whenever a key
    exists, so a CLI hiccup moved agent turns onto the metered key with no reservation."""
    sources.execute(text("INSERT INTO project_settings (project_id, key, value) "
                         "VALUES ('p', 'chat_llm_engine', 'claude_code')"))
    adapter = model_routing.project_chat_adapter(
        sources, _no_key_settings(openai_api_key="sk-test-not-real"), "p")
    assert type(adapter) is ClaudeCliAdapter, type(adapter).__name__


def test_a_refused_turn_is_settled_and_explained():
    from boss_engine.services.model_failures import explain, settled_failure

    refused = model_routing.ModelSpendRefused(
        f"{model_routing.ModelSpendRefused.MARKER}: Budget cap exceeded. Decision created. "
        "(budget cap; decision dec_1)")
    said = settled_failure(refused)
    assert said and said.startswith("The budget refused this work."), said
    assert explain(refused)["kind"] == "budget_refused"


# ── One place records a step failure; every kind of step is classified there ──────────────────

@pytest.mark.parametrize("raised", [
    # The agent loop, raw.
    "IntegrationConfigurationError: OPENAI_API_KEY is not configured.",
    # typed_decisions' generic wrapper.
    "typed decision failed: OPENAI_API_KEY is not configured.",
    # A skill or tool that declared a provider requirement.
    "test.make_picture needs a provider capability this project has not connected: "
    "An OpenAI key for image generation. Connect an OpenAI key for image generation in Repo "
    "and model. A subscription or another provider's key does not do image generation.",
])
def test_a_failure_no_retry_can_fix_is_settled_whatever_raised_it(raised):
    from boss_engine.services.model_failures import settled_failure

    said = settled_failure(raised)
    assert said is not None, raised
    assert said.startswith(("No connected model can run this work. Connect a model",
                            "This step needs an OpenAI key for image generation")), said
    assert settled_failure(said) == said, "explained twice"


@pytest.mark.parametrize("raised", [
    "connection reset by peer", "no eligible model source right now (rate window)",
    "HTTP 401 from the deploy target",
])
def test_and_a_failure_a_retry_can_fix_is_left_retryable(raised):
    from boss_engine.services.model_failures import settled_failure

    assert settled_failure(raised) is None


# ── generate_image reads its key the declared way ──────────────────────────────────────────────

def _image_tool():
    from boss_engine.tools.builtin import build_default_registry

    return {s.name: s for s in build_default_registry().specs()}["generate_image"]


class _Ctx:
    def __init__(self, conn, settings):
        self._conn, self.settings = conn, settings

    def begin(self):
        import contextlib

        @contextlib.contextmanager
        def _cm():
            yield self._conn
        return _cm()


def test_generate_image_is_a_declared_requirement_readiness_can_see():
    assert "openai_images" in model_routing.TOOL_REQUIREMENTS["generate_image"]
    from boss_engine.services.workflow_readiness import tool_provider_needs

    assert tool_provider_needs("generate_image") == {"provider:openai_images"}


def test_generate_image_without_a_key_says_the_fix(project):
    settings = _no_key_settings(boss_edition="enterprise")
    out = _image_tool().handler(_Ctx(project, settings), project_id="p", actor="t",
                                args={"prompt": "a hero image", "path": "hero.png"})
    assert out["ok"] is False
    assert out["needs"] == "provider:openai_images"
    assert "Connect an OpenAI key for image generation in Repo and model" in out["error"]


def test_generate_image_uses_a_key_stored_through_the_console(project, monkeypatch, tmp_path):
    """It read only OPENAI_API_KEY from the environment, so a console key was invisible."""
    import urllib.request

    _store_key(project, "openai")
    monkeypatch.setenv("BOSS_IMAGE_DIR", str(tmp_path))
    sent = {}

    class _Reply:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            import base64
            import json

            return json.dumps({"data": [{"b64_json": base64.b64encode(b"png").decode()}]}).encode()

    def urlopen(request, timeout=0):
        sent["auth"] = request.headers.get("Authorization")
        sent["url"] = request.full_url
        return _Reply()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    out = _image_tool().handler(_Ctx(project, _no_key_settings(boss_edition="enterprise")),
                                project_id="p", actor="t",
                                args={"prompt": "a hero image", "path": "hero.png"})
    assert out["ok"] is True, out
    assert sent["auth"] == f"Bearer {_STORED_KEY}"
    assert sent["url"].startswith("https://api.openai.com/v1/images/generations")


# The reported step itself, end to end through the start and tick endpoints, is in
# apps/api/tests/test_an_agent_step_runs_on_the_connected_subscription.py.


# ── What only one provider can do: declared, checked before start, explained on failure ──────

@pytest.fixture()
def image_skill():
    """A provider-specific executor, registered the declared way and removed afterwards."""
    skill_id = "test.make_picture"

    @skill_executors.register_executor(skill_id, requires="openai_images")
    def _make_picture(context, inputs):
        key = model_routing.provider_credential(context, skill_id, "openai_images")
        return {"ok": bool(key)}

    yield skill_id
    skill_executors._EXECUTORS.pop(skill_id, None)
    model_routing._DECLARED.pop(skill_id, None)


def test_an_undeclared_requirement_cannot_be_registered():
    with pytest.raises(ValueError, match="PROVIDER_REQUIREMENTS"):
        skill_executors.register_executor("test.bad", lambda c, i: {}, requires="dall_e_9")
    assert "test.bad" not in skill_executors._EXECUTORS


def test_a_provider_specific_skill_without_its_key_is_refused_by_readiness(sources, image_skill):
    from boss_engine.services import workflow_readiness as wr

    _connect(sources, "anthropic_claude_code")  # a model IS connected; images still are not
    spec = {"entry_step": "draw", "steps": [
        {"id": "draw", "kind": "capability", "captain": "designer",
         "binding": {"agent_work": True, "skills": [image_skill],
                     "inputs": {"instructions": "draw a hero image"}},
         "next": []}]}
    needs = {r["key"] for r in wr.requirements(spec)}
    assert "provider:openai_images" in needs, needs

    ok, detail = wr.check(sources, _no_key_settings(), "p", "provider:openai_images")
    assert not ok and "OpenAI" in detail
    need = wr.need_for("provider:openai_images")
    assert need.fix.startswith("Connect an OpenAI key for image generation in Repo and model")

    ok, _ = wr.check(sources, _no_key_settings(openai_api_key="sk-test-not-real"), "p",
                     "provider:openai_images")
    assert ok


def test_an_unscoped_agent_step_is_checked_for_what_its_agent_holds(sources, image_skill):
    """The video step names its agent and scopes nothing; the agent's GRANT is its reach."""
    from boss_engine.services import workflow_readiness as wr

    sources.execute(text("CREATE TABLE role_skills (project_id TEXT, role_key TEXT, "
                         "skill_id TEXT)"))
    sources.execute(text("INSERT INTO role_skills VALUES ('p', 'designer', :s), "
                         "('p', 'designer', 'video.trim')"), {"s": image_skill})
    spec = {"entry_step": "draw", "steps": [
        {"id": "draw", "kind": "capability", "captain": "designer",
         "binding": {"agent_work": True, "inputs": {"instructions": "x"}}, "next": []}]}
    keys = {r["key"] for r in wr.requirements(spec, connection=sources, project_id="p")}
    assert "provider:openai_images" in keys, keys
    # And an agent holding only routed skills needs nothing beyond the model.
    sources.execute(text("DELETE FROM role_skills WHERE skill_id = :s"), {"s": image_skill})
    keys = {r["key"] for r in wr.requirements(spec, connection=sources, project_id="p")}
    assert not {k for k in keys if k.startswith("provider:")}, keys


def test_and_the_step_failure_says_the_same_fix(image_skill):
    from boss_engine.services.model_failures import explain

    class Ctx:
        settings = _no_key_settings()
        connection = None
        project_id = "p"

    handler = skill_executors._EXECUTORS[image_skill]
    with pytest.raises(model_routing.ProviderRequirementMissing) as caught:
        handler(Ctx(), {})
    said = explain(caught.value)
    assert said["kind"] == "provider_requirement"
    assert said["action"].startswith(
        "Connect an OpenAI key for image generation in Repo and model")
    assert said["retry"] is False


def test_a_key_read_without_declaring_it_is_refused():
    class Ctx:
        settings = _no_key_settings(openai_api_key="sk-test-not-real")
        connection = None
        project_id = "p"

    with pytest.raises(ValueError, match="without declaring"):
        model_routing.provider_credential(Ctx(), "model.generate_text", "openai_images")


def test_the_old_sentence_is_explained_as_no_model_not_as_openai():
    from boss_engine.services.model_failures import explain

    said = explain("IntegrationConfigurationError: OPENAI_API_KEY is not configured.")
    assert said["kind"] == "no_model"
    assert "Connect a model" in said["action"]
