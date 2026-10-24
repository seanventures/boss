"""Metatron's interactive shell (BOSSAPP-761): an inline transcript with a bottom bar.

prompt_toolkit owns the keyboard: the input line, the status bar under it, slash-command
completion with one-line descriptions, history, and the pickers for /agent, /project, /model
and /pick. Rich (metatron_render) draws the transcript above it. The terminal's scrollback is
the transcript; nothing takes over the screen.

THIN BY CONSTRUCTION. Every request is a method of metatron_tui.MetatronClient, run as it is in
the plain REPL: a sentence is one Ask BOSS turn (/v1/metatron/session → agent_turn), a choice
is the console's button press (/v1/engine/agent/action) with the tool and arguments that were
drawn. The shell decides nothing about what BOSS said; it only lets the person keep typing
while a turn runs (lines queue, in order) and shows what the server reports meanwhile.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

TEAL = "#14b8a6"
BLUE = "#3b82f6"
AMBER = "#f59e0b"
RED = "#ef4444"
GREEN = "#22c55e"

STYLE = Style.from_dict({
    "prompt.mark": f"{TEAL} bold",
    "prompt.agent": f"{TEAL} bold",
    "bottom-toolbar": "noreverse bg:default #9ca3af",
    "bar.project": f"{BLUE} bold",
    "bar.agent": f"{TEAL} bold",
    "bar.model": "#d1d5db",
    "bar.sep": "#6b7280",
    "bar.budget.ok": GREEN,
    "bar.budget.near": AMBER,
    "bar.budget.spent": RED,
    "bar.key": "#e5e7eb bold",
    "bar.hint": "#6b7280",
    "bar.busy": f"{TEAL} bold",
    "bar.queue": AMBER,
    "picker.title": f"{AMBER} bold",
    "picker.filter": f"{TEAL}",
    "picker.selected": f"bg:{TEAL} #000000 bold",
    "picker.meta": "#9ca3af",
    "picker.hint": "#6b7280",
    "completion-menu.completion": "bg:#1f2937 #e5e7eb",
    "completion-menu.completion.current": f"bg:{TEAL} #000000",
    "completion-menu.meta.completion": "bg:#111827 #9ca3af",
    "completion-menu.meta.completion.current": f"bg:{BLUE} #000000",
})

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
#: Lines that run at once even while a turn is in flight (they read nothing from it).
LOCAL_KINDS = {"help", "clear", "quit", "empty"}
TERMINAL_RUN_STATES = {"succeeded", "failed", "cancelled", "canceled", "completed", "dead",
                       "rejected", "compensated"}


# ── Completion ───────────────────────────────────────────────────────────────────────────────

class SlashCompleter(Completer):
    """`/` lists every command with its one line; after a command, its known values (the
    agents, projects and models the shell has read, or a fixed set of words)."""

    def __init__(self, commands: Iterable[tuple[str, str, str]],
                 values: Optional[Callable[[str], list[tuple[str, str]]]] = None):
        self.commands = list(commands)
        self.values = values or (lambda _command: [])

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        if " " not in text:
            typed = text.lower()
            for name, args, description in self.commands:
                if name.startswith(typed):
                    shown = f"{name} {args}".rstrip()
                    yield Completion(name, start_position=-len(text), display=shown,
                                     display_meta=description)
            return
        command, _, rest = text.partition(" ")
        if " " in rest.strip() and command.lower() not in {"/decision"}:
            return
        word = rest.split(" ")[-1]
        for value, meta in self.values(command.lower()):
            if value.lower().startswith(word.lower()):
                yield Completion(value, start_position=-len(word), display_meta=meta)


# ── Pickers ──────────────────────────────────────────────────────────────────────────────────

@dataclass
class PickItem:
    value: Any
    label: str
    meta: str = ""


def build_picker(items: list[PickItem], *, title: str, input=None, output=None,
                 initial: int = 0) -> Application:
    """A filterable list: type to filter, ↑↓ to move, Enter to choose, Esc to leave."""
    query = Buffer(multiline=False)
    state = {"index": max(0, min(initial, len(items) - 1))}

    def visible() -> list[PickItem]:
        needle = query.text.strip().lower()
        return [item for item in items
                if not needle or needle in f"{item.label} {item.meta}".lower()]

    def clamp() -> None:
        shown = visible()
        state["index"] = max(0, min(state["index"], len(shown) - 1))

    query.on_text_changed += lambda _buffer: state.update(index=0)

    def rows():
        shown = visible()
        if not shown:
            return [("class:picker.meta", "  nothing matches")]
        start = max(0, min(state["index"] - 4, len(shown) - 10))
        out: list[tuple[str, str]] = []
        for offset, item in enumerate(shown[start:start + 10]):
            index = start + offset
            selected = index == state["index"]
            style = "class:picker.selected" if selected else ""
            out.append((style, f" {'❯' if selected else ' '} {item.label} "))
            if item.meta:
                out.append(("class:picker.meta", f"  {item.meta}"))
            out.append(("", "\n"))
        if len(shown) > 10:
            out.append(("class:picker.meta", f"   {len(shown)} in all; type to filter\n"))
        return out[:-1] if out and out[-1] == ("", "\n") else out

    keys = KeyBindings()

    @keys.add("up")
    @keys.add("c-p")
    def _up(event) -> None:
        state["index"] = max(0, state["index"] - 1)

    @keys.add("down")
    @keys.add("c-n")
    @keys.add("tab")
    def _down(event) -> None:
        state["index"] = min(len(visible()) - 1, state["index"] + 1)

    @keys.add("enter")
    def _choose(event) -> None:
        clamp()
        shown = visible()
        event.app.exit(result=shown[state["index"]] if shown else None)

    @keys.add("escape", eager=True)
    @keys.add("c-c")
    @keys.add("c-d")
    def _leave(event) -> None:
        event.app.exit(result=None)

    layout = Layout(HSplit([
        Window(FormattedTextControl([("class:picker.title", title)]), height=1),
        Window(BufferControl(query), height=1,
               get_line_prefix=lambda *_: [("class:picker.filter", "  filter › ")]),
        Window(FormattedTextControl(rows), height=Dimension(min=1, max=11)),
        Window(FormattedTextControl([("class:picker.hint",
                                      "  ↑↓ move · Enter choose · Esc leave · type to filter")]),
               height=1),
    ]), focused_element=query)
    return Application(layout=layout, key_bindings=keys, style=STYLE, full_screen=False,
                       erase_when_done=True, input=input, output=output, mouse_support=False)


async def pick(items: list[PickItem], *, title: str, input=None, output=None,
               initial: int = 0) -> Optional[PickItem]:
    if not items:
        return None
    return await build_picker(items, title=title, input=input, output=output,
                              initial=initial).run_async()


# ── History that never keeps a credential ────────────────────────────────────────────────────

class SafeHistory(FileHistory):
    """↑↓ history across sessions, minus any line that carries a credential (an /op or /api
    body with a secret field, or the key itself)."""

    def __init__(self, path: str, keep: Callable[[str], bool]):
        super().__init__(path)
        self._keep = keep

    def store_string(self, string: str) -> None:
        if self._keep(string):
            super().store_string(string)


def history_path(env: Optional[dict[str, str]] = None) -> Path:
    source = os.environ if env is None else env
    base = source.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "boss" / "metatron_history"


def make_history(tui: Any, client: Any, path: Optional[Path] = None):
    def keep(line: str) -> bool:
        if client.config.api_key and client.config.api_key in line:
            return False
        command = tui.parse_line(line)
        if command.kind in {"api", "op"} and tui._request_secrets(command.body, command.path):
            return False
        return True

    target = path or history_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.touch(mode=0o600, exist_ok=True)
        os.chmod(target, 0o600)
        return SafeHistory(str(target), keep)
    except OSError:
        return InMemoryHistory()


# ── The shell ────────────────────────────────────────────────────────────────────────────────

class Shell:
    def __init__(self, tui: Any, client: Any, *, console=None, input=None, output=None,
                 history=None, reveal_delay: float = 0.03, progress_every: float = 1.5,
                 follow_every: float = 3.0):
        self.tui = tui
        self.client = client
        self.render = tui.sibling("metatron_render")
        self.console = console or self.render.make_console(None)
        self.input = input
        self.output = output
        self.reveal_delay = reveal_delay
        self.progress_every = progress_every
        self.follow_every = follow_every
        self.queue: deque[str] = deque()
        self.job: Optional[asyncio.Task] = None
        self.job_started = 0.0
        self.job_label = ""
        self.progress_label = ""
        self.cancelled: set[int] = set()
        self.job_serial = 0
        self.shown_turn: Optional[dict[str, Any]] = None
        self.retry_command = None
        self.follows: dict[str, str] = {}
        self.context: dict[str, Any] = {"project": client.project_id, "agent": "BOSS",
                                        "model": "", "budget": None}
        self.cache: dict[str, list[tuple[str, str]]] = {"/agent": [], "/project": [], "/model": [],
                                                        "/api": []}
        self.done = False
        #: The prompt line stays in the scrollback on a real terminal; tests and the demo
        #: cast, which have no prompt line, echo it.
        self.echo_input = False
        keys = KeyBindings()
        busy = Condition(lambda: self.job is not None)

        @keys.add("escape", filter=busy, eager=True)
        def _cancel(event) -> None:
            self.cancel()

        @keys.add("escape", filter=Condition(lambda: self.job is None and self.retry_command is not None),
                  eager=True)
        def _leave_retry(event) -> None:
            self.retry_command = None

        @keys.add("c-o")
        def _expand(event) -> None:
            self._spawn(self.handle_line("/expand"))

        self.session = PromptSession(
            message=self.prompt_message, bottom_toolbar=self.toolbar,
            completer=SlashCompleter(tui.SLASH_COMMANDS, self.values),
            complete_while_typing=True, history=history or InMemoryHistory(),
            key_bindings=keys, style=STYLE, refresh_interval=0.12, input=input, output=output,
            reserve_space_for_menu=6,
        )

    # ── Drawing ──────────────────────────────────────────────────────────────────────────────

    def print(self, renderable: Any) -> None:
        if renderable is not None:
            self.console.print(renderable)

    def prompt_message(self):
        if self.client.role:
            return FormattedText([("class:prompt.agent", f"{self.client.role_name or self.client.role} "),
                                  ("class:prompt.mark", "❯ ")])
        return FormattedText([("class:prompt.mark", "❯ ")])

    def _budget(self) -> list[tuple[str, str]]:
        budget = self.context.get("budget")
        if not isinstance(budget, dict):
            return []
        period = {"day": "today", "month": "this month"}.get(str(budget.get("period")), "")
        state = str(budget.get("state") or "ok")
        try:
            words = f"${float(budget.get('spent_usd') or 0):.2f} of ${float(budget.get('cap_usd') or 0):.2f} {period}".rstrip()
        except (TypeError, ValueError):
            return []
        return [("class:bar.sep", " · "), (f"class:bar.budget.{state}", words)]

    def toolbar(self):
        agent = self.client.role_name or self.client.role or "BOSS"
        bar: list[tuple[str, str]] = [
            ("class:bar.sep", " ⬡ "), ("class:bar.project", self.client.project_id),
            ("class:bar.sep", " · "), ("class:bar.agent", agent),
        ]
        if self.context.get("model"):
            bar += [("class:bar.sep", " · "), ("class:bar.model", str(self.context["model"]))]
        bar += self._budget()
        for run_id, line in list(self.follows.items())[:2]:
            bar += [("class:bar.sep", " · "), ("class:bar.model", f"{run_id} {line}")]
        bar.append(("", "\n"))
        if self.job is not None:
            spin = SPINNER[int(time.monotonic() * 10) % len(SPINNER)]
            elapsed = int(time.monotonic() - self.job_started)
            bar += [("class:bar.busy", f" {spin} {self.job_label} · {elapsed}s")]
            if self.progress_label:
                label = self.progress_label
                bar += [("class:bar.hint", f" · {label if len(label) <= 40 else label[:39] + '…'}")]
            bar += [("class:bar.hint", " · type to queue a message")]
            if self.queue:
                bar += [("class:bar.queue", f" ({len(self.queue)} queued)")]
            bar += [("class:bar.sep", " · "), ("class:bar.key", "Esc"), ("class:bar.hint", " cancels")]
        elif self.retry_command is not None:
            bar += [("class:bar.key", " Enter"), ("class:bar.hint", " runs it again once you have signed in · "),
                    ("class:bar.key", "Esc"), ("class:bar.hint", " leaves it")]
        else:
            for key, what in (("Enter", "send"), ("Esc", "cancel"), ("↑↓", "history"),
                              ("Tab", "complete"), ("/", "commands"), ("Ctrl-D", "quit")):
                bar += [("class:bar.key", f" {key}"), ("class:bar.hint", f" {what} ")]
        return FormattedText(bar)

    def values(self, command: str) -> list[tuple[str, str]]:
        fixed = {"/api": [("run", "invoke the row you picked"), ("*", "every operation")],
                 "/decisions": [("open", "pending and delegated"), ("all", "every status")],
                 "/decision": [("approve", "approve with a note"), ("reject", "reject with a note")],
                 "/alerts": [(s, "") for s in self.tui.ALERT_STATES],
                 "/drive": [("auto", "apply validated moves"), ("go", "same as auto")]}
        if command in {"/agent", "/agents"}:
            return [("auto", "back to BOSS")] + self.cache["/agent"]
        if command in {"/model", "/models"}:
            return [("auto", "let the router choose")] + self.cache["/model"]
        if command == "/api":
            return fixed["/api"] + self.cache.get("/api", [])
        return self.cache.get(command) or fixed.get(command, [])

    # ── Startup ──────────────────────────────────────────────────────────────────────────────

    def _absorb_status(self, status: dict[str, Any]) -> None:
        context = status.get("context") or {}
        self.context.update({k: v for k, v in context.items() if v})

    def _absorb_lists(self, agents: Any, projects: Any, models: Any) -> None:
        if isinstance(agents, dict) and isinstance(agents.get("items"), list):
            self.cache["/agent"] = [(str(a["role_key"]), str(a.get("name") or "")) for a in agents["items"]]
        if isinstance(projects, dict) and isinstance(projects.get("items"), list):
            self.cache["/project"] = [(str(p.get("id")), str(p.get("name") or ""))
                                      for p in projects["items"] if isinstance(p, dict) and p.get("id")]
        if isinstance(models, dict) and isinstance(models.get("options"), list):
            self.cache["/model"] = [
                (str(o.get("engine")) + (f":{o['model']}" if o.get("model") else ""), str(o.get("label") or ""))
                for o in models["options"]]

    def welcome(self) -> None:
        """The logo, what's new, and the connection checklist, before the prompt opens."""
        with self.console.status("Connecting to BOSS…", spinner="dots", spinner_style="boss.agent") \
                if self.console.is_terminal else _null():
            status = self.client.status()
        self._absorb_status(status)
        news = self.tui.whats_new()
        self.print(self.render.banner(api_url=self.client.config.api_url,
                                      project_id=self.client.project_id, status=status,
                                      news=news, width=self.console.width))

    async def _warm_caches(self) -> None:
        loop = asyncio.get_running_loop()
        agents, projects, models = await asyncio.gather(
            loop.run_in_executor(None, self.client.agents),
            loop.run_in_executor(None, self.client.projects),
            loop.run_in_executor(None, self.client.models))
        self._absorb_lists(agents, projects, models)

    # ── Lines ────────────────────────────────────────────────────────────────────────────────

    def _spawn(self, coroutine) -> asyncio.Task:
        return asyncio.get_running_loop().create_task(coroutine)

    async def handle_line(self, line: str, *, queued: bool = False) -> bool:
        """One line from the person. False ends the session."""
        raw = (line or "").strip()
        if self.retry_command is not None:
            retry, self.retry_command = self.retry_command, None
            if not raw:
                self.start(retry)
                return True
        command = self.tui.parse_line(raw)
        if command.kind == "quit":
            return False
        if command.kind == "empty":
            return True
        if command.kind == "help":
            self.print(self.render.help_table(self.tui.SLASH_COMMANDS))
            return True
        if command.kind == "clear":
            self.console.clear()
            return True
        if self.job is not None:
            self.queue.append(raw)
            note = self.render.Text(f"  queued ({len(self.queue)}): ", style="boss.dim")
            note.append(raw)
            self.print(note)
            return True
        if queued or self.echo_input:
            self.echo(raw)
        if raw.lower() == "/pick":
            await self.pick_choice()
            return True
        if await self.picker_for(command):
            return True
        self.start(command)
        return True

    def echo(self, raw: str) -> None:
        """The person's line, kept in the transcript (the prompt line itself is erased)."""
        line = self.render.Text("❯ ", style="boss.agent")
        line.append(raw, style="boss.you")
        self.print(line)

    async def picker_for(self, command: Any) -> bool:
        """/agent, /project, /model and /pick with no argument open a picker. The chosen item
        is sent through the same client method its typed form uses."""
        loop = asyncio.get_running_loop()
        bare = not command.message
        if command.kind == "agent" and bare:
            listed = await loop.run_in_executor(None, self.client.agents)
            if not isinstance(listed, dict) or "items" not in listed:
                self.print(self.render.result(self.client, command, listed))
                return True
            self._absorb_lists(listed, None, None)
            items = [PickItem("auto", "BOSS", "the general agent; it hands work to the others")] + [
                PickItem(a["role_key"], str(a.get("name") or a["role_key"]),
                         f"{a['role_key']} · {str(a.get('mission') or '')[:60]}".rstrip(" ·"))
                for a in listed["items"]]
            chosen = await pick(items, title="Talk to which agent?", input=self.input, output=self.output)
            if chosen is not None:
                self.start(self.tui.Command(kind="agent", message=str(chosen.value)))
            return True
        if command.kind == "project" and bare:
            listed = await loop.run_in_executor(None, self.client.projects)
            if not isinstance(listed, dict) or "items" not in listed:
                self.print(self.render.result(self.client, command, listed))
                return True
            self._absorb_lists(None, listed, None)
            items = [PickItem(p.get("id"), str(p.get("id")), str(p.get("name") or ""))
                     for p in listed["items"] if isinstance(p, dict) and p.get("id")]
            current = next((i for i, item in enumerate(items) if item.value == self.client.project_id), 0)
            chosen = await pick(items, title="Which project?", input=self.input, output=self.output,
                                initial=current)
            if chosen is not None:
                self.start(self.tui.Command(kind="project", message=str(chosen.value)))
            return True
        if command.kind == "api_menu" and bare:
            # NOBODY TYPES A ROUTE THEY HAVE NOT SEEN. The domains, then that domain's
            # operations, both filterable; the chosen row is sent as /api NUMBER.
            listed = await loop.run_in_executor(None, self.client.api_menu, "")
            if not isinstance(listed, dict) or listed.get("view") != "domains":
                self.print(self.render.result(self.client, command, listed))
                return True
            self.cache["/api"] = [(str(d.get("id")), str(d.get("title") or ""))
                                  for d in listed.get("domains") or [] if d.get("id")]
            items = [PickItem(d.get("id"), str(d.get("id")),
                              f"{d.get('route_count')} routes · {str(d.get('title') or '')}".rstrip(" ·"))
                     for d in listed.get("domains") or [] if d.get("id")]
            chosen = await pick(items, title="Which part of the API?", input=self.input,
                                output=self.output)
            if chosen is None:
                return True
            rows = await loop.run_in_executor(None, self.client.api_rows, str(chosen.value))
            if not isinstance(rows, list):
                self.print(self.render.result(self.client, command, rows))
                return True
            operations = [
                PickItem(operation["index"],
                         f"{operation['index']}. {operation['method']} {operation['path']}",
                         str(operation.get("summary") or operation.get("limitation") or ""))
                for operation in rows]
            picked = await pick(operations, title=f"{chosen.value}: which operation?",
                                input=self.input, output=self.output)
            if picked is not None:
                self.start(self.tui.Command(kind="api_template", message=str(picked.value)))
            return True
        if command.kind == "model" and bare:
            listed = await loop.run_in_executor(None, self.client.models)
            if not isinstance(listed, dict) or "options" not in listed:
                self.print(self.render.result(self.client, command, listed))
                return True
            self._absorb_lists(None, None, listed)
            items = [PickItem("auto", "Auto", "the router chooses per turn")] + [
                PickItem(str(o.get("engine")) + (f":{o['model']}" if o.get("model") else ""),
                         str(o.get("label") or o.get("engine")),
                         ("metered" if o.get("metered") else "subscription or local"))
                for o in listed["options"]]
            chosen = await pick(items, title="Which model answers chat?", input=self.input,
                                output=self.output)
            if chosen is not None:
                self.start(self.tui.Command(kind="model", message=str(chosen.value)))
            return True
        return False

    async def pick_choice(self) -> None:
        """A bare /pick: the last reply's choices in a picker; the chosen number is sent
        exactly as typing it would send it (MetatronClient.pick)."""
        if self.shown_turn is not None:
            self.client.last_turn = self.shown_turn
        options = self.client.choices()
        if not options:
            self.print(self.render.error_panel("The last reply offered no choices.",
                                               "Ask BOSS something; its offers come back numbered."))
            return
        items = []
        for number, choice in enumerate(options, 1):
            label = str(choice.get("label") or choice.get("tool") or choice.get("message") or "choice")
            meta = " · ".join(v for v in (str(choice.get("tool") or "") if choice.get("kind") == "action" else "",
                                          str(choice.get("why") or "")) if v)
            items.append(PickItem(number, f"{number}. {label}", meta))
        chosen = await pick(items, title="You decide (nothing has run yet)", input=self.input,
                            output=self.output)
        if chosen is not None:
            self.start(self.tui.Command(kind="pick", page=int(chosen.value)))

    def start(self, command: Any) -> None:
        self.job_serial += 1
        serial = self.job_serial
        self.job_started = time.monotonic()
        self.job_label = "BOSS is working" if command.kind in self.tui.TURN_KINDS else "Working"
        self.progress_label = ""
        self.job = self._spawn(self._run(command, serial))

    def cancel(self) -> None:
        if self.job is None:
            return
        self.cancelled.add(self.job_serial)
        self.job = None
        self.print(self.render.Text(
            "  Cancelled here. A turn already sent may still finish on the server; "
            "/thread shows it.", style="boss.decide"))
        self._next()

    def _next(self) -> None:
        if self.queue and self.job is None:
            self._spawn(self.handle_line(self.queue.popleft(), queued=True))

    async def _run(self, command: Any, serial: int) -> None:
        loop = asyncio.get_running_loop()
        if command.kind in {"pick", "confirm", "expand"} and self.shown_turn is not None:
            # The choices are the ones on screen, never a turn the person did not see.
            self.client.last_turn = self.shown_turn
        poller = None
        if command.kind in self.tui.TURN_KINDS:
            if not self.client.thread_id:
                await loop.run_in_executor(None, self.client.ensure_thread)
            if self.client.thread_id:
                poller = self._spawn(self._poll_progress(self.client.thread_id, serial))
        try:
            payload = await loop.run_in_executor(None, self.client.dispatch, command)
            if serial not in self.cancelled:
                await self.show(command, payload)
        except Exception as error:  # noqa: BLE001 — a drawing bug must not end the session
            if serial not in self.cancelled:
                self.print(self.render.error_panel(f"Metatron could not show that ({type(error).__name__}).",
                                                   "`--plain` shows the same result as plain text."))
        finally:
            if poller is not None:
                poller.cancel()
        if serial in self.cancelled:
            return
        if self.job_serial == serial:
            self.job = None
        self._next()

    async def show(self, command: Any, payload: Any) -> None:
        render = self.render
        if command.kind in {"session", "confirm"} or (
                command.kind == "pick" and isinstance(payload, dict) and "reply" in payload):
            if isinstance(payload, dict) and not payload.get("__http_error__"):
                self.shown_turn = self.client.last_turn
                safe = self.tui.redact_payload(payload, self.client.config.api_key)
                for part in render.turn_parts(safe, self.client.choices(),
                                              agent_name=self.client.role_name or "BOSS",
                                              agent_key=self.client.role or ""):
                    self.print(part)
                    if self.reveal_delay:
                        await asyncio.sleep(self.reveal_delay)
                return
        if command.kind == "pick" and isinstance(payload, dict) and not payload.get("__http_error__"):
            self.shown_turn = self.client.last_turn
        if render.is_step_up(payload):
            self.retry_command = command
        if command.kind == "project" and isinstance(payload, dict) and not payload.get("__command_error__"):
            self.context["project"] = self.client.project_id
            self.shown_turn = None
            self._spawn(self._refresh_context())
        if command.kind == "agent" and isinstance(payload, dict) and "active_role" in payload \
                and "items" not in payload:
            self.shown_turn = None
        if command.kind == "model" and isinstance(payload, dict) and isinstance(payload.get("resolved"), dict):
            self.context["model"] = payload["resolved"].get("label") or payload["resolved"].get("model") or ""
            if isinstance(payload.get("budget"), dict):
                self.context["budget"] = payload["budget"]
        if command.kind == "status" and isinstance(payload, dict) and "checks" in payload:
            self._absorb_status(payload)
        if command.kind == "run" and command.auto and isinstance(payload, dict) and payload.get("id"):
            self._spawn(self._follow(str(payload["id"]), payload))
        self.print(render.result(self.client, command, payload, commands=self.tui.SLASH_COMMANDS))

    async def _refresh_context(self) -> None:
        loop = asyncio.get_running_loop()
        status = await loop.run_in_executor(None, self.client.status)
        self._absorb_status(status)
        await self._warm_caches()

    async def _poll_progress(self, thread_id: str, serial: int) -> None:
        """While a turn runs, what BOSS reports doing (the console's progress poll), shown
        in the bar and as dim lines in the transcript."""
        loop = asyncio.get_running_loop()
        cursor = 0
        while self.job_serial == serial and serial not in self.cancelled:
            await asyncio.sleep(self.progress_every)
            payload = await loop.run_in_executor(None, self.client.progress, thread_id, cursor)
            if not isinstance(payload, dict) or payload.get("__http_error__"):
                continue
            cursor = payload.get("cursor") or cursor
            for step in payload.get("steps") or []:
                label = str(step.get("label") or "").strip()
                if label and self.job_serial == serial and serial not in self.cancelled:
                    self.progress_label = label
                    self.print(self.render.Text(f"  › {label}", style="boss.dim"))

    async def _follow(self, run_id: str, first: dict[str, Any]) -> None:
        """/follow: the run's steps, re-read every few seconds; a line when a step changes."""
        loop = asyncio.get_running_loop()
        seen = {str(s.get("step_id")): str(s.get("status")) for s in first.get("steps") or []
                if isinstance(s, dict)}
        payload: Any = first
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            steps = [s for s in payload.get("steps") or [] if isinstance(s, dict)]
            done = sum(1 for s in steps if str(s.get("status")).lower() in self.render.DONE)
            self.follows[run_id] = f"{done}/{len(steps)} {payload.get('status')}"
            if str(payload.get("status") or "").lower() in TERMINAL_RUN_STATES:
                break
            await asyncio.sleep(self.follow_every)
            fresh = await loop.run_in_executor(None, self.client.run, run_id)
            if not isinstance(fresh, dict) or fresh.get("__http_error__") or fresh.get("__command_error__"):
                continue
            payload = fresh
            changed = [s for s in payload.get("steps") or [] if isinstance(s, dict)
                       and seen.get(str(s.get("step_id"))) != str(s.get("status"))]
            for step in changed:
                seen[str(step.get("step_id"))] = str(step.get("status"))
            if changed:
                line = self.render.Text(f"  {run_id} ", style="boss.dim")
                line.append_text(self.render.step_progress(
                    [s for s in payload.get("steps") or [] if isinstance(s, dict)]))
                self.print(line)
        self.follows.pop(run_id, None)
        self.print(self.render.run_view(payload))

    # ── The loop ─────────────────────────────────────────────────────────────────────────────

    async def loop(self) -> int:
        self._spawn(self._warm_caches())
        while not self.done:
            try:
                line = await self.session.prompt_async()
            except KeyboardInterrupt:
                if self.job is not None:
                    self.cancel()
                continue
            except EOFError:
                break
            if not await self.handle_line(line):
                break
        if self.job is not None:
            self.print(self.render.Text("  Leaving with a turn in flight; its reply lands in the "
                                        "thread (/thread next time).", style="boss.dim"))
        return 0


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def run(tui: Any, client: Any) -> int:
    """The interactive terminal on a real TTY. stdout is patched so the transcript prints
    above the prompt and its bar."""
    shell = Shell(tui, client, history=make_history(tui, client))
    try:
        shell.welcome()
        with patch_stdout(raw=True):
            return asyncio.run(shell.loop())
    except KeyboardInterrupt:
        return 0


__all__ = ["PickItem", "SafeHistory", "Shell", "SlashCompleter", "build_picker", "pick", "run"]

if __name__ == "__main__":  # pragma: no cover - run metatron_tui.py instead
    sys.exit("run scripts/metatron_tui.py")
