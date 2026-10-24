"""Metatron's rich terminal (BOSSAPP-761): snapshots of what it draws, and its keyboard.

Snapshots are plain text at 80 columns with colour off (the layout is the contract; colours are
checked separately). Regenerate after an intended change with
METATRON_UPDATE_SNAPSHOTS=1 pytest scripts/tests/test_metatron_rich.py.

Input tests drive prompt_toolkit through its pipe input: completion, the pickers, and a choice
taken from the last reply, which must reach the console's button path unchanged.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("rich")
pytest.importorskip("prompt_toolkit")

from prompt_toolkit.application import create_app_session  # noqa: E402
from prompt_toolkit.completion import CompleteEvent  # noqa: E402
from prompt_toolkit.document import Document  # noqa: E402
from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.output import DummyOutput  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_metatron_tui import AGENT_TURN, SECRET, FakeOpener, _config, tui  # noqa: E402

render = tui.sibling("metatron_render")
shell_module = tui.sibling("metatron_shell")
SNAPSHOTS = HERE / "snapshots" / "metatron"
ANSI = re.compile(r"\x1b\[")


def draw(renderable: Any, *, width: int = 80, color: bool = False) -> str:
    buffer = io.StringIO()
    render.make_console(buffer, width=width, color=color).print(renderable)
    return "\n".join(line.rstrip() for line in buffer.getvalue().splitlines()) + "\n"


def snapshot(name: str, text: str) -> None:
    path = SNAPSHOTS / f"{name}.txt"
    if os.environ.get("METATRON_UPDATE_SNAPSHOTS") or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert text == path.read_text(encoding="utf-8"), f"snapshot {name} changed; see {path}"
    assert SECRET not in text


STATUS = tui.build_status(
    "http://127.0.0.1:8000", "boss-dogfood",
    health={"status": "ok", "database_connected": True},
    me={"display_name": "Host tools", "kind": "service_account"},
    projects={"items": [{"id": "boss-dogfood", "name": "BOSS dogfood"}]},
    setup={"models": [{"label": "Claude (subscription)", "pill": {"tone": "green"}}],
           "jobs": [{"job": "chat", "resolved": {"label": "Claude Sonnet"}}],
           "embedding_model": None,
           "repository": {"connection": {"auth": "app", "account_login": "example-org"}, "repos": []},
           "budget": {"period": "day", "spent_usd": 1.84, "cap_usd": 25, "state": "ok"}})
NEWS = {"release": "BOSS Community 1.0.0-rc.10.9 candidate",
        "items": ["Monitoring and alerting", "Agents in operations"],
        "source": "docs/community/RELEASE_NOTES_v1.0-rc10.9.md"}

REPLY = {
    **AGENT_TURN,
    "reply": "One build is **waiting** on a failed step.\n\nThe fix, already saved:\n\n"
             "```python\ndef push(repo):\n    return repo.push(auth=\"github_app\")\n```",
    "tool_calls": [{"tool": "describe_workflow_run", "args": {"run_id": "wr_42"}, "ok": True, "error": ""},
                   {"tool": "test_github_access", "args": {"repo": "example-org/helpdesk"}, "ok": False,
                    "error": "403: Resource not accessible by integration"}],
    "actions": [{"kind": "start_workflow", "run_id": "wr_9"}],
}

DIFF = ("--- a/boss.deploy.yaml\n+++ b/boss.deploy.yaml\n@@ -12,4 +12,4 @@ steps:\n"
        "   - id: build\n-    push: token\n+    push: github_app\n     retries: 2\n")


def _client(responses=None):
    return tui.MetatronClient(_config(api_url="http://127.0.0.1:8000"), urlopen=FakeOpener(responses or []))


# ── Snapshots ────────────────────────────────────────────────────────────────────────────────

def test_banner_shows_the_logo_whats_new_and_the_checklist_with_fixes():
    text = draw(render.banner(api_url="http://127.0.0.1:8000", project_id="boss-dogfood",
                              status=STATUS, news=NEWS, width=80))
    snapshot("banner", text)
    assert "██████╗" in text and "┣━━━┫" in text
    assert "What's new" in text and "Monitoring and alerting" in text
    assert "✓ API reachable" in text and "✗ Embedding model" in text
    assert "fix Console: Setup › Repo and models › Embedding model." in text


def test_a_streamed_reply_is_markdown_block_by_block_with_highlighted_code():
    client = _client()
    client.last_turn = REPLY
    parts = render.turn_parts(REPLY, client.choices())
    # Who, two paragraphs and the code block arrive as separate pieces (the progressive reveal).
    assert len(parts) >= 5
    text = draw(render.turn(REPLY, client.choices()))
    snapshot("streamed_reply", text)
    assert "● BOSS" in text and "def push(repo):" in text
    coloured = draw(render.turn(REPLY, client.choices()), color=True)
    assert ANSI.search(coloured)


def test_tool_and_action_calls_are_compact_lines_that_expand():
    text = draw(render.Group(*render.tool_lines(REPLY["tool_calls"], REPLY["actions"])))
    snapshot("action_lines", text)
    assert "● describe_workflow_run — ok" in text
    assert "● test_github_access — failed  403: Resource not accessible by integration" in text
    assert "● start_workflow — done  run_id wr_9" in text
    assert "wr_42" not in text  # arguments only when expanded
    assert '{"run_id": "wr_42"}' in draw(render.expanded(REPLY))
    pressed = draw(render.action_result({"ok": True, "tool": "retry_workflow_step",
                                         "result": {"summary": "Retried **build**.",
                                                    "steps": [{"step_id": "spec", "status": "succeeded"},
                                                              {"step_id": "build", "status": "running"}]}}))
    assert "● retry_workflow_step — done" in pressed and "✓ spec › ● build" in pressed


def test_offers_and_decisions_are_amber_cards_with_numbered_choices():
    client = _client()
    client.last_turn = AGENT_TURN
    choices = render.choices_card(client.choices())
    decision = render.decision_card({
        "id": "dec_7", "question": "Deploy HelpDesk AI to production?", "status": "pending",
        "risk_tier": 2, "reason": "First production release", "recommendation": "Approve after the smoke test",
        "options": [{"label": "Approve"}, {"label": "Reject"}]})
    text = draw(choices) + draw(decision)
    snapshot("decision_card", text)
    assert "You decide" in text and "1 Start the build  start_workflow" in text
    assert "2 Investigate further" in text and "Nothing has run yet" in text
    assert "/decision approve dec_7 NOTE" in text
    assert "#f59e0b" not in text
    coloured = draw(choices, color=True)
    assert "\x1b[38;2;245;158;11m" in coloured  # amber, BOSS's "you decide"


def test_plans_are_live_checklists_and_runs_a_step_line():
    pending = {"plan_id": "plan_7", "summary": "Add a support inbox",
               "phases": [{"title": "Spec", "status": "done"}, {"title": "Build", "status": "running",
                                                                 "depends_on": [0]},
                          {"title": "Test", "depends_on": [1]}]}
    run = {"id": "wr_42", "workflow_name": "HelpDesk AI build", "status": "running",
           "steps": [{"step_id": "spec", "status": "succeeded"}, {"step_id": "build", "status": "running"},
                     {"step_id": "deploy", "status": "pending"}]}
    text = draw(render.confirmation_card(pending)) + draw(render.run_view(run))
    snapshot("checklist", text)
    assert "☑ 1. Spec" in text and "▶ 2. Build  after 1" in text and "☐ 3. Test" in text
    assert "1/3" in text and "✓ spec › ● build › ○ deploy" in text
    assert "/confirm to go ahead" in text


def test_diffs_have_line_numbers_in_green_and_red():
    text = draw(render.diff(DIFF))
    snapshot("diff", text)
    assert re.search(r"13\s+-    push: token", text)
    assert re.search(r"13\s+\+    push: github_app", text)
    assert re.search(r"12\s+12\s+- id: build", text)
    coloured = draw(render.diff(DIFF), color=True)
    assert "\x1b[38;2;34;197;94m" in coloured and "\x1b[38;2;239;68;68m" in coloured
    # A ```diff fence in a reply is drawn the same way.
    fenced = draw(render.Group(*render.markdown_blocks(f"Change:\n\n```diff\n{DIFF}```")))
    assert re.search(r"13\s+\+    push: github_app", fenced)


def test_lists_are_tables():
    tasks = {"project_id": "boss-dogfood", "items": [
        {"id": "task_1", "status": "running", "title": "Plan the community launch"},
        {"id": "task_2", "status": "succeeded", "title": "Ship the docs"}]}
    decisions = {"project_id": "boss-dogfood", "view": "open", "page": 1, "pages": 1, "total_count": 1,
                 "items": [{"id": "dec_7", "status": "pending", "risk_tier": 2,
                            "question": "Deploy HelpDesk AI to production?"}]}
    alerts = {"project_id": "boss-dogfood", "counts": {"firing": 1, "pending": 0, "needs_attention": 1},
              "alerts": [{"sev": "sev2", "state_word": "Firing", "app": "helpdesk",
                          "summary": "p95 latency above 800 ms"}]}
    text = draw(render.tasks_table(tasks, 1)) + draw(render.decisions_table(decisions)) \
        + draw(render.alerts_table(alerts))
    snapshot("tables", text)
    assert re.search(r"task_1\s+running\s+Plan the community launch", text)
    assert re.search(r"dec_7\s+pending\s+2\s+Deploy HelpDesk AI", text)
    assert re.search(r"SEV2\s+Firing\s+helpdesk\s+p95 latency", text)


def test_errors_are_red_panels_with_the_fix():
    unreachable = render.failure({"__http_error__": True, "status": 0,
                                  "body": "unreachable: request failed before an HTTP response (URLError)"},
                                 api_url="http://127.0.0.1:8000")
    forbidden = render.failure({"__http_error__": True, "status": 403,
                                "body": json.dumps({"detail": "agent_chat is not allowed for this key"})})
    step_up = render.failure({"__http_error__": True, "status": 401, "body": '{"detail":"step_up_required"}'})
    still = render.failure({"__http_error__": True, "status": 0, "still_running": True, "thread_id": "athr_7",
                            "body": "BOSS is still working on this turn on the server (thread athr_7)"})
    text = draw(unreachable) + draw(forbidden) + draw(step_up) + draw(still)
    snapshot("error_panel", text)
    assert "BOSS is not answering" in text and "boss status" in text
    assert "HTTP 403" in text and "agent_chat is not allowed" in text and "People & access" in text
    assert "Confirm it's you" in text and "Enter" in text
    assert "Still working" in text and "/thread athr_7" in text
    assert "\x1b[38;2;239;68;68m" in draw(unreachable, color=True)


def test_no_color_and_a_non_terminal_are_plain_text(monkeypatch):
    buffer = io.StringIO()
    console = render.make_console(buffer, env={"NO_COLOR": "1"})
    console.print(render.turn(REPLY, []))
    console.print(render.diff(DIFF))
    assert "One build is waiting" in buffer.getvalue()
    assert "\x1b" not in buffer.getvalue()
    snapshot("no_color", draw(render.turn({"reply": "Plain **and** readable."}, [])))
    # Not a terminal: the rich terminal is never chosen; the output is the plain REPL's.
    assert tui.rich_terminal(io.StringIO()) is False
    assert tui.rich_terminal(io.StringIO(), plain=True) is False
    monkeypatch.setenv("TERM", "dumb")

    class Tty(io.StringIO):
        def isatty(self):
            return True
    assert tui.rich_terminal(Tty(), need_input=False) is False


def test_json_output_is_unchanged_even_on_a_rich_terminal(monkeypatch, capsys):
    opener = FakeOpener([(200, AGENT_TURN)])
    monkeypatch.setattr(tui, "urlopen", opener)
    monkeypatch.setenv("BOSS_API_URL", "https://boss-api.test")
    monkeypatch.setenv("BOSS_API_KEY", SECRET)
    monkeypatch.setenv("BOSS_PROJECT_ID", "boss-dogfood")
    monkeypatch.setattr(tui, "rich_terminal", lambda *a, **k: True)
    assert tui.main(["--print", "set up three projects", "--json"]) == 0
    out = capsys.readouterr().out
    assert out == json.dumps(AGENT_TURN, default=str) + "\n"


def test_whats_new_reads_the_newest_release_notes(tmp_path):
    (tmp_path / "RELEASE_NOTES_v1.0-rc9.md").write_text("# Old\n### Ignore me\n")
    (tmp_path / "RELEASE_NOTES_v1.0-rc10.2.md").write_text(
        "# BOSS Community rc10.2\n\n## What changed\n\n### Pickers\n- x\n### Tables\n")
    news = tui.whats_new([tmp_path])
    assert news["release"] == "BOSS Community rc10.2"
    assert news["items"] == ["Pickers", "Tables"]
    assert tui.whats_new([tmp_path / "missing"]) is None


# ── Keyboard ─────────────────────────────────────────────────────────────────────────────────

def test_slash_completion_offers_every_command_with_its_description():
    completer = shell_module.SlashCompleter(tui.SLASH_COMMANDS, lambda c: [("qa_lead", "QA Lead")]
                                            if c == "/agent" else [])
    found = list(completer.get_completions(Document("/ag"), CompleteEvent()))
    assert [c.text for c in found] == ["/agent"]
    assert "Agent Studio" in found[0].display_meta_text
    every = {c.text for c in completer.get_completions(Document("/"), CompleteEvent())}
    assert every == {name for name, _a, _d in tui.SLASH_COMMANDS}
    values = list(completer.get_completions(Document("/agent q"), CompleteEvent()))
    assert [v.text for v in values] == ["qa_lead"]
    assert not list(completer.get_completions(Document("hello /ag"), CompleteEvent()))


def test_tab_completes_a_command_in_the_prompt():
    async def go():
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                session = shell_module.PromptSession(
                    completer=shell_module.SlashCompleter(tui.SLASH_COMMANDS), input=pipe,
                    output=DummyOutput())
                answer = asyncio.ensure_future(session.prompt_async())
                pipe.send_text("/overw")
                for _ in range(200):  # completions are computed off the input thread
                    await asyncio.sleep(0.01)
                    if session.default_buffer.complete_state is not None:
                        break
                pipe.send_text("\t")
                for _ in range(200):
                    await asyncio.sleep(0.01)
                    if session.default_buffer.text == "/overwatch":
                        break
                pipe.send_text("\r")
                return await answer
    assert asyncio.run(go()) == "/overwatch"


def _run_picker(items, keys: str):
    async def go():
        with create_pipe_input() as pipe:
            pipe.send_text(keys)
            return await shell_module.pick(items, title="Pick", input=pipe, output=DummyOutput())
    return asyncio.run(go())


def test_the_picker_moves_filters_and_chooses():
    items = [shell_module.PickItem("auto", "BOSS"),
             shell_module.PickItem("engineering_captain", "Engineering Captain", "ships builds"),
             shell_module.PickItem("qa_lead", "QA Lead", "proves features work")]
    assert _run_picker(items, "\x1b[B\r").value == "engineering_captain"
    assert _run_picker(items, "qa\r").value == "qa_lead"
    assert _run_picker(items, "proves\r").value == "qa_lead"
    assert _run_picker(items, "\x03") is None


def _shell(responses, **kwargs):
    opener = FakeOpener(responses)
    client = tui.MetatronClient(_config(api_url="http://127.0.0.1:8000"), urlopen=opener)
    buffer = io.StringIO()
    console = render.make_console(buffer, width=80, color=False)
    return opener, client, buffer, console


def test_choosing_a_decision_in_the_picker_presses_the_consoles_button():
    opener, client, buffer, console = _shell([
        (200, {"thread_id": "athr_7"}), (200, AGENT_TURN),
        (200, {"ok": True, "tool": "start_workflow", "result": {"ok": True}})])

    async def go():
        with create_pipe_input() as pipe:
            shell = shell_module.Shell(tui, client, console=console, input=pipe, output=DummyOutput(),
                                       reveal_delay=0, progress_every=60)
            await shell.handle_line("set up three projects")
            await shell.job
            pipe.send_text("\r")  # the first choice, as drawn
            await shell.handle_line("/pick")
            await shell.job
    asyncio.run(go())
    paths = [call["url"].split("8000")[1] for call in opener.calls]
    # The thread first (as the console does), so progress can be read; then the turn; then the
    # press, through the console's button path.
    assert paths == ["/v1/agent/threads/ensure", "/v1/metatron/session", "/v1/engine/agent/action"]
    press = opener.calls[-1]["body"]
    assert press["tool"] == "start_workflow" and press["args"] == {"template": "build_app"}
    assert press["action_id"] == "pa_1"
    out = buffer.getvalue()
    assert "You decide" in out and "● start_workflow — done" in out
    assert SECRET not in out


def test_a_typed_number_picks_and_a_line_typed_during_a_turn_waits_its_turn():
    release = threading.Event()
    calls: list[str] = []
    responses = {"/v1/metatron/session": AGENT_TURN,
                 "/v1/tasks": {"project_id": "boss-dogfood", "items": []}}

    def slow_opener(request, timeout=None):
        path = request.full_url.split("8000", 1)[1].split("?")[0]
        calls.append(path)
        if path == "/v1/metatron/session":
            release.wait(5)
        from test_metatron_tui import FakeResponse
        return FakeResponse(200, responses.get(path, {"thread_id": "athr_7"}))

    client = tui.MetatronClient(_config(api_url="http://127.0.0.1:8000"), urlopen=slow_opener)
    client.thread_id = "athr_7"
    buffer = io.StringIO()
    console = render.make_console(buffer, width=80, color=False)

    async def go():
        shell = shell_module.Shell(tui, client, console=console, input=None, output=DummyOutput(),
                                   reveal_delay=0, progress_every=60)
        await shell.handle_line("is anything stuck?")
        assert shell.job is not None
        await shell.handle_line("/tasks")  # typed while BOSS works: queued, not sent
        assert list(shell.queue) == ["/tasks"] and "/v1/tasks" not in calls
        assert "type to queue a message" in "".join(t for _s, t in shell.toolbar())
        release.set()
        await shell.job
        for _ in range(50):
            if shell.job is None and not shell.queue and "/v1/tasks" in calls:
                break
            await asyncio.sleep(0.02)
        if shell.job is not None:
            await shell.job
    asyncio.run(go())
    assert calls.index("/v1/metatron/session") < calls.index("/v1/tasks")
    assert "queued (1): /tasks" in buffer.getvalue()


def test_escape_cancels_the_wait_not_the_server_turn():
    release = threading.Event()

    def slow_opener(request, timeout=None):
        release.wait(5)
        from test_metatron_tui import FakeResponse
        return FakeResponse(200, AGENT_TURN)

    client = tui.MetatronClient(_config(api_url="http://127.0.0.1:8000"), urlopen=slow_opener)
    client.thread_id = "athr_7"
    buffer = io.StringIO()
    console = render.make_console(buffer, width=80, color=False)

    async def go():
        shell = shell_module.Shell(tui, client, console=console, input=None, output=DummyOutput(),
                                   reveal_delay=0, progress_every=60)
        await shell.handle_line("deploy it")
        job = shell.job
        shell.cancel()
        assert shell.job is None
        release.set()
        await job
    asyncio.run(go())
    out = buffer.getvalue()
    assert "Cancelled here" in out and "/thread" in out
    assert "can't create projects" not in out  # the abandoned reply is not drawn


def test_the_bar_shows_project_agent_model_and_budget():
    client = _client()
    client.role, client.role_name = "qa_lead", "QA Lead"
    shell = shell_module.Shell(tui, client, console=render.make_console(io.StringIO(), width=80),
                               output=DummyOutput())
    shell._absorb_status(STATUS)
    bar = "".join(text for _style, text in shell.toolbar())
    assert "boss-dogfood" in bar and "QA Lead" in bar and "Claude Sonnet" in bar
    assert "$1.84 of $25.00 today" in bar
    assert "Esc" in bar and "↑↓ history" in bar and "Tab complete" in bar and "/ commands" in bar


def test_history_never_keeps_a_credential(tmp_path):
    client = _client()
    history = shell_module.make_history(tui, client, tmp_path / "metatron_history")
    history.store_string("is anything stuck?")
    history.store_string('/api POST /v1/entitlements/sources/openai_api/credential {"value":"sk-live-1"}')
    history.store_string(f"remember {SECRET}")
    saved = (tmp_path / "metatron_history").read_text()
    assert "is anything stuck?" in saved
    assert "sk-live-1" not in saved and SECRET not in saved
    assert oct((tmp_path / "metatron_history").stat().st_mode & 0o777) == "0o600"


def test_the_api_chooser_is_a_table_and_the_pick_is_a_card():
    from test_metatron_tui import _catalog, _openapi  # noqa: PLC0415 — fixtures live with the client tests

    client = tui.MetatronClient(_config(api_url="http://127.0.0.1:8000"),
                                urlopen=FakeOpener([(200, _catalog()), (200, _openapi())]))
    domains = client.dispatch(tui.parse_line("/api"))
    listing = client.dispatch(tui.parse_line("/api *"))
    card = client.dispatch(tui.parse_line("/api 3"))
    text = draw(render.api_menu(domains)) + draw(render.api_menu(listing)) + draw(render.api_card(card))
    snapshot("api-chooser", text)
    assert re.search(r"core\s+4\s+Core work", text)
    assert re.search(r"1\s+GET\s+/v1/tasks", text)
    assert re.search(r"3\s+GET\s+/v1/tasks/\{task_id\}", text)
    assert "/v1/tasks/<task_id>?project_id=boss-dogfood" in text
    assert "/api run 3" in text and "Copy one of these" in text
    assert SECRET not in text


def test_a_bare_api_picks_a_domain_then_an_operation_and_shows_its_card():
    from test_metatron_tui import _catalog, _openapi  # noqa: PLC0415

    opener = FakeOpener([(200, _catalog()), (200, _openapi())])
    client = tui.MetatronClient(_config(api_url="http://127.0.0.1:8000"), urlopen=opener)
    buffer = io.StringIO()
    console = render.make_console(buffer, width=80, color=False)

    async def go():
        with create_pipe_input() as pipe:
            shell = shell_module.Shell(tui, client, console=console, input=pipe,
                                       output=DummyOutput(), reveal_delay=0, progress_every=60)
            pipe.send_text("core\r")        # the domain picker, filtered
            pipe.send_text("\x1b[B\x1b[B\r")  # the operation picker: third row
            await shell.handle_line("/api")
            if shell.job is not None:
                await shell.job
    asyncio.run(go())
    out = buffer.getvalue()
    assert "GET /v1/tasks/{task_id}" in out
    assert "/v1/tasks/<task_id>?project_id=boss-dogfood" in out
    assert client._menu_search == "core"
    # Browsing and picking read the catalog and the schema once; no call is made for the pick.
    assert [call["url"].split("8000")[1] for call in opener.calls] == [
        "/v1/meta/api-catalog", "/openapi.json"]
    assert SECRET not in out


def test_completion_after_api_offers_its_words_and_the_domains_it_has_seen():
    shell = shell_module.Shell(tui, _client(), console=render.make_console(io.StringIO(), width=80),
                               output=DummyOutput())
    shell.cache["/api"] = [("core", "Core work"), ("delivery", "Software factory")]
    completer = shell_module.SlashCompleter(tui.SLASH_COMMANDS, shell.values)
    assert [c.text for c in completer.get_completions(Document("/api r"), CompleteEvent())] == ["run"]
    assert [c.text for c in completer.get_completions(Document("/api c"), CompleteEvent())] == ["core"]
    assert {c.text for c in completer.get_completions(Document("/api "), CompleteEvent())} == {
        "run", "*", "core", "delivery"}
