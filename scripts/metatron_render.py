"""Metatron's rich rendering (BOSSAPP-761): what the terminal draws, and nothing else.

Every function here takes a payload the client already fetched (metatron_tui.MetatronClient)
and returns a Rich renderable. Nothing here calls the API, chooses an action, or decides what a
reply means: a choice is drawn with the number the client will send through its own `pick`, and
the order of choices is the client's order (`MetatronClient.choices`).

BOSS's colours: teal and blue for agents, amber for "you decide", green for done, red for
errors. NO_COLOR (or `make_console(color=False)`) keeps the layout and emits no escape
sequences at all.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sys
import threading
import time
from typing import Any, Iterable, Iterator, Mapping, Optional, TextIO

from rich import box
from rich.console import Console, Group, RenderableType
from rich.json import JSON
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

TEAL = "#14b8a6"
BLUE = "#3b82f6"
AMBER = "#f59e0b"
GREEN = "#22c55e"
RED = "#ef4444"

THEME = Theme({
    "boss.agent": f"bold {TEAL}",
    "boss.blue": BLUE,
    "boss.hex": TEAL,
    "boss.logo": f"bold {BLUE}",
    "boss.decide": f"bold {AMBER}",
    "boss.decide.border": AMBER,
    "boss.ok": GREEN,
    "boss.err": f"bold {RED}",
    "boss.err.border": RED,
    "boss.dim": "grey58",
    "boss.you": "bold",
    "boss.add": f"{GREEN}",
    "boss.del": f"{RED}",
    "boss.hunk": f"{BLUE}",
    "boss.lineno": "grey46",
})

#: Agent name badges: teals and blues, stable per agent key.
BADGE_COLOURS = (TEAL, BLUE, "#06b6d4", "#6366f1", "#0ea5e9", "#10b981", "#2dd4bf", "#60a5fa")

DONE = {"done", "succeeded", "success", "completed", "complete", "ok", "passed", "approved",
        "skipped", "merged"}
FAILED = {"failed", "error", "errored", "rejected", "cancelled", "canceled", "dead", "blocked",
          "timed_out"}
RUNNING = {"running", "in_progress", "active", "leased", "started", "working",
           "waiting_for_decision", "awaiting_approval", "paused"}


def make_console(file: TextIO, *, width: Optional[int] = None, color: Optional[bool] = None,
                 env: Optional[Mapping[str, str]] = None) -> Console:
    """A console for one stream. color=None follows the environment: NO_COLOR (any value, per
    no-color.org) means no escape sequences at all; otherwise Rich's terminal detection. A
    fixed width is a fixed render (snapshots, the demo cast)."""
    source = os.environ if env is None else env
    if color is None:
        color = not source.get("NO_COLOR")
    options: dict[str, Any] = {"file": file, "theme": THEME, "highlight": False, "emoji": False,
                               "legacy_windows": False}
    if width is not None:
        options.update(width=width, force_terminal=bool(color), force_interactive=False)
    if color:
        options["color_system"] = "truecolor" if width is not None else "auto"
    else:
        options.update(color_system=None, no_color=True)
    return Console(**options)


# ── The banner ───────────────────────────────────────────────────────────────────────────────

HEXAGON = (
    "    ▁▁▁▁▁▁▁    ",
    "   ╱       ╲   ",
    "  ╱  ┏━━━╮  ╲  ",
    " ▕   ┣━━━┫   ▏ ",
    "  ╲  ┗━━━╯  ╱  ",
    "   ╲▁▁▁▁▁▁▁╱   ",
)
WORDMARK = (
    "██████╗  ██████╗ ███████╗███████╗",
    "██╔══██╗██╔═══██╗██╔════╝██╔════╝",
    "██████╔╝██║   ██║███████╗███████╗",
    "██╔══██╗██║   ██║╚════██║╚════██║",
    "██████╔╝╚██████╔╝███████║███████║",
    "╚═════╝  ╚═════╝ ╚══════╝╚══════╝",
)


def logo(width: int = 80) -> Text:
    """The hexagon "B" and, when there is room, the BOSS wordmark beside it."""
    out = Text()
    wide = width >= len(HEXAGON[0]) + 3 + len(WORDMARK[0])
    for index, row in enumerate(HEXAGON):
        for char in row:
            out.append(char, style="boss.logo" if char in "┏━╮┣┫┗╯" else "boss.hex")
        if wide:
            out.append("   ")
            out.append(WORDMARK[index], style="boss.logo")
        elif index == 3:
            out.append("  BOSS", style="boss.logo")
        out.append("\n")
    out.append("Metatron", style="boss.agent")
    out.append(" · the BOSS terminal", style="boss.dim")
    return out


def check_mark(ok: Optional[bool]) -> Text:
    if ok is True:
        return Text("✓", style="boss.ok")
    if ok is False:
        return Text("✗", style="boss.err")
    return Text("–", style="boss.dim")


def status_table(status: Mapping[str, Any]) -> Table:
    table = Table.grid(padding=(0, 1))
    table.add_column(width=1)
    table.add_column(no_wrap=True)
    table.add_column(overflow="fold")
    for item in status.get("checks") or []:
        detail = Text(str(item.get("detail") or ""), style="boss.dim" if item.get("ok") else "")
        if item.get("fix"):
            detail.append("\n")
            detail.append("fix ", style="boss.decide")
            detail.append_text(_inline_code(str(item["fix"])))
        table.add_row(check_mark(item.get("ok")), Text(str(item.get("label"))), detail)
    return table


def status_panel(status: Mapping[str, Any]) -> Panel:
    return Panel(status_table(status), title="Connection", title_align="left",
                 border_style="boss.hex", box=box.ROUNDED, padding=(0, 1))


def whats_new_table(news: Mapping[str, Any]) -> Group:
    lines: list[RenderableType] = [Text(str(news.get("release") or "Release notes"), style="bold")]
    for item in news.get("items") or []:
        line = Text("• ", style="boss.hex")
        line.append(str(item))
        lines.append(line)
    if news.get("source"):
        lines.append(Text(str(news["source"]), style="boss.dim"))
    return Group(*lines)


def banner(*, api_url: str, project_id: str, status: Optional[Mapping[str, Any]],
           news: Optional[Mapping[str, Any]], width: int = 80) -> Group:
    """The startup screen: the logo, then one welcome panel with what's new and the
    connection checklist (side by side when the terminal is wide enough)."""
    parts: list[RenderableType] = [logo(width), Text("")]
    body = Table.grid(padding=(0, 3), expand=True)
    welcome = Text()
    welcome.append("Welcome. ", style="bold")
    welcome.append("Type to talk to BOSS — the same agent, thread and actions as the console's "
                   "Ask BOSS. ")
    welcome.append("/", style="boss.agent")
    welcome.append(" for commands.")
    sections: list[RenderableType] = [welcome]
    if news:
        sections += [Text(""), Text("What's new", style="boss.agent"), whats_new_table(news)]
    right: list[RenderableType] = []
    if status:
        right = [Text("Connection", style="boss.agent"), status_table(status)]
    if right and width >= 100:
        body.add_column(ratio=1)
        body.add_column(ratio=1)
        body.add_row(Group(*sections), Group(*right))
    else:
        body.add_column()
        body.add_row(Group(*sections))
        if right:
            body.add_row(Text(""))
            body.add_row(Group(*right))
    subtitle = Text(f"{project_id} · {api_url}", style="boss.dim")
    parts.append(Panel(body, border_style="boss.hex", box=box.ROUNDED, padding=(0, 1),
                       subtitle=subtitle, subtitle_align="right"))
    return Group(*parts)


# ── Small pieces ─────────────────────────────────────────────────────────────────────────────

def badge_colour(key: str) -> str:
    digest = hashlib.sha256((key or "boss").encode()).digest()
    return BADGE_COLOURS[digest[0] % len(BADGE_COLOURS)]


def agent_badge(name: str, key: str = "") -> Text:
    """A coloured name badge: ● QA Lead."""
    colour = TEAL if not key else badge_colour(key)
    text = Text("● ", style=colour)
    text.append(name or "BOSS", style=f"bold {colour}")
    return text


def _inline_code(text: str) -> Text:
    """`code` spans in a one-line fix, drawn in the agent colour."""
    out = Text()
    for index, part in enumerate(re.split(r"`([^`]+)`", text)):
        out.append(part, style="boss.agent" if index % 2 else "")
    return out


def _short_args(args: Any, limit: int = 60) -> str:
    if not isinstance(args, dict) or not args:
        return ""
    shown = ", ".join(f"{k}={json.dumps(v, default=str, ensure_ascii=False)}" for k, v in args.items())
    return shown if len(shown) <= limit else shown[: limit - 1] + "…"


def tool_line(name: str, status: str, *, ok: Optional[bool] = True, detail: str = "",
              args: Any = None, expanded: bool = False) -> Text:
    """● action — status, compact; `expanded` adds the arguments."""
    style = "boss.ok" if ok else ("boss.err" if ok is False else "boss.dim")
    line = Text("● ", style=style)
    line.append(name, style="bold")
    line.append(" — ", style="boss.dim")
    line.append(status, style=style)
    if detail:
        line.append(f"  {detail}", style="boss.dim")
    if expanded and isinstance(args, dict) and args:
        line.append("\n    ")
        line.append(json.dumps(args, default=str, ensure_ascii=False), style="boss.dim")
    elif args:
        short = _short_args(args)
        if short:
            line.append(f"  ({short})", style="boss.dim")
    return line


def tool_lines(tool_calls: Iterable[Mapping[str, Any]], actions: Iterable[Mapping[str, Any]],
               *, expanded: bool = False) -> list[Text]:
    lines: list[Text] = []
    for call in tool_calls:
        ok = bool(call.get("ok", True))
        error = str(call.get("error") or "")
        lines.append(tool_line(str(call.get("tool") or call.get("name") or "tool"),
                               "ok" if ok else "failed", ok=ok, detail=error,
                               args=call.get("args") if expanded else None, expanded=expanded))
    for action in actions:
        detail = " ".join(f"{k} {action[k]}" for k in ("run_id", "task_id", "meeting_id", "plan_id")
                          if action.get(k))
        lines.append(tool_line(str(action.get("kind") or "action"), "done", ok=True,
                               detail=detail, args=action.get("args") if expanded else None,
                               expanded=expanded))
    return lines


def progress_bar(done: int, total: int, width: int = 10) -> Text:
    filled = round(width * done / total) if total else 0
    bar = Text("▰" * filled, style="boss.ok")
    bar.append("▱" * (width - filled), style="boss.dim")
    bar.append(f" {done}/{total}", style="boss.dim")
    return bar


def _state_of(item: Mapping[str, Any]) -> str:
    if item.get("done") is True:
        return "done"
    return str(item.get("status") or item.get("state") or "").lower()


def checklist(title: str, items: list[Mapping[str, Any]], *, numbered: bool = True) -> Group:
    """Plans and tasks: ☑ done, ▶ running, ☒ failed, ☐ waiting, with a progress bar."""
    done = sum(1 for i in items if _state_of(i) in DONE)
    header = Text(title, style="bold")
    header.append("  ")
    header.append_text(progress_bar(done, len(items)))
    rows: list[RenderableType] = [header]
    for index, item in enumerate(items, 1):
        state = _state_of(item)
        if state in DONE:
            mark, style = "☑", "boss.ok"
        elif state in FAILED:
            mark, style = "☒", "boss.err"
        elif state in RUNNING:
            mark, style = "▶", "boss.agent"
        else:
            mark, style = "☐", "boss.dim"
        line = Text(f"  {mark} ", style=style)
        if numbered:
            line.append(f"{index}. ", style="boss.dim")
        label = str(item.get("title") or item.get("label") or item.get("name")
                    or item.get("step_id") or f"Step {index}")
        line.append(label, style="" if state not in DONE else "boss.dim")
        after = item.get("depends_on")
        if isinstance(after, list) and after:
            line.append(f"  after {', '.join(str(int(i) + 1) for i in after if str(i).isdigit())}",
                        style="boss.dim")
        rows.append(line)
    return Group(*rows)


def step_progress(steps: list[Mapping[str, Any]]) -> Text:
    """A workflow run on one line: ✓ spec › ✓ build › ● test › ○ deploy  2/4."""
    line = Text()
    done = 0
    for index, step in enumerate(steps):
        state = _state_of(step)
        if state in DONE:
            mark, style = "✓", "boss.ok"
            done += 1
        elif state in FAILED:
            mark, style = "✗", "boss.err"
        elif state in RUNNING:
            mark, style = "●", "boss.agent"
        else:
            mark, style = "○", "boss.dim"
        if index:
            line.append(" › ", style="boss.dim")
        line.append(f"{mark} ", style=style)
        line.append(str(step.get("step_id") or step.get("name") or step.get("title") or "step"),
                    style=style if style != "boss.dim" else "")
    line.append("  ")
    line.append_text(progress_bar(done, len(steps), width=8))
    return line


# ── Diffs ────────────────────────────────────────────────────────────────────────────────────

HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def looks_like_diff(text: str) -> bool:
    return bool(re.search(r"^@@ -\d+", text, re.M)) or text.startswith(("--- ", "diff --git"))


def diff(text: str) -> Table:
    """A unified diff with old and new line numbers: additions green, removals red."""
    table = Table(box=None, show_header=False, padding=(0, 1), pad_edge=False, expand=False)
    table.add_column(justify="right", style="boss.lineno", no_wrap=True, min_width=3)
    table.add_column(justify="right", style="boss.lineno", no_wrap=True, min_width=3)
    table.add_column(overflow="fold", no_wrap=False)
    old = new = 0
    for raw in text.rstrip("\n").splitlines():
        hunk = HUNK.match(raw)
        if hunk:
            old, new = int(hunk.group(1)), int(hunk.group(2))
            table.add_row("", "", Text(raw, style="boss.hunk"))
        elif raw.startswith(("---", "+++", "diff --git", "index ")):
            table.add_row("", "", Text(raw, style="bold"))
        elif raw.startswith("+"):
            table.add_row("", str(new), Text(raw, style="boss.add"))
            new += 1
        elif raw.startswith("-"):
            table.add_row(str(old), "", Text(raw, style="boss.del"))
            old += 1
        elif raw.startswith("\\"):
            table.add_row("", "", Text(raw, style="boss.dim"))
        else:
            table.add_row(str(old) if old else "", str(new) if new else "", Text(raw))
            old, new = (old + 1 if old else 0), (new + 1 if new else 0)
    return table


class CodeBlock(Markdown):
    """A fenced block (Rich pads code itself, so no extra blank line goes before it)."""


FENCE = re.compile(r"^```[ \t]*([\w+-]*)[^\n]*\n(.*?)^```[ \t]*$", re.S | re.M)


def markdown_blocks(reply: str) -> list[RenderableType]:
    """A reply as Markdown, block by block (paragraph granularity, for progressive reveal):
    fenced code is syntax-highlighted by Rich's Markdown, a ```diff fence becomes the
    numbered diff."""
    blocks: list[RenderableType] = []
    position = 0
    for match in FENCE.finditer(reply):
        blocks += _prose_blocks(reply[position:match.start()])
        language, body = match.group(1).lower(), match.group(2)
        if language in {"diff", "patch"} or (not language and looks_like_diff(body)):
            blocks.append(Padding(diff(body), (0, 0, 0, 2)))
        else:
            blocks.append(CodeBlock(match.group(0), code_theme="monokai"))
        position = match.end()
    blocks += _prose_blocks(reply[position:])
    return blocks


def _prose_blocks(text: str) -> list[RenderableType]:
    out: list[RenderableType] = []
    for chunk in re.split(r"\n\s*\n", text.strip("\n")):
        if chunk.strip():
            out.append(Markdown(chunk, code_theme="monokai"))
    return out


# ── An agent turn ────────────────────────────────────────────────────────────────────────────

def choices_card(choices: list[Mapping[str, Any]]) -> Panel:
    """The offers, numbered as /pick sends them. Amber: this is yours to decide."""
    table = Table.grid(padding=(0, 1))
    table.add_column(justify="right", style="boss.decide", no_wrap=True)
    table.add_column(overflow="fold")
    for number, choice in enumerate(choices, 1):
        label = Text(str(choice.get("label") or choice.get("tool") or choice.get("message") or "choice"),
                     style="bold")
        if choice.get("kind") == "action" and choice.get("tool"):
            label.append(f"  {choice['tool']}", style="boss.dim")
        why = str(choice.get("why") or "").strip()
        if why:
            label.append(f"\n{why}", style="boss.dim")
        table.add_row(f"{number}", label)
    footer = Text("Nothing has run yet. Type a number, or /pick to choose with the arrows.",
                  style="boss.dim")
    return Panel(Group(table, Text(""), footer), title=Text("You decide", style="boss.decide"),
                 title_align="left", border_style="boss.decide.border", box=box.ROUNDED,
                 padding=(0, 1))


def confirmation_card(pending: Mapping[str, Any]) -> Panel:
    summary = str(pending.get("summary") or pending.get("title") or pending.get("plan_id"))
    phases = [p for p in pending.get("phases") or [] if isinstance(p, dict)]
    parts: list[RenderableType] = [Text(summary, style="bold")]
    if phases:
        parts += [Text(""), checklist("Plan", phases)]
    footer = Text("/confirm", style="boss.decide")
    footer.append(" to go ahead, or say what to change.", style="boss.dim")
    parts += [Text(""), footer]
    return Panel(Group(*parts), title=Text("Waiting for your go-ahead", style="boss.decide"),
                 title_align="left", border_style="boss.decide.border", box=box.ROUNDED,
                 padding=(0, 1))


def turn_header(agent_name: str = "BOSS", agent_key: str = "", *, recovered: bool = False) -> Text:
    header = agent_badge(agent_name, agent_key)
    if recovered:
        header.append("  (read back from the thread after the connection dropped)", style="boss.dim")
    return header


def turn_parts(payload: Mapping[str, Any], choices: list[Mapping[str, Any]], *,
               agent_name: str = "BOSS", agent_key: str = "") -> list[RenderableType]:
    """An Ask BOSS reply, in reading order: who, the words, what it read and did, the plan
    waiting on you, the choices."""
    parts: list[RenderableType] = [turn_header(agent_name, agent_key,
                                               recovered=bool(payload.get("recovered_from_thread")))]
    reply = str(payload.get("reply") or "").strip()
    blocks = markdown_blocks(reply) if reply else []
    for index, block in enumerate(blocks):
        following = blocks[index + 1] if index + 1 < len(blocks) else None
        gap = 0 if following is None or isinstance(following, CodeBlock) or isinstance(block, CodeBlock) else 1
        parts.append(Padding(block, (0, 0, gap, 2)))
    calls = [c for c in payload.get("tool_calls") or [] if isinstance(c, dict)]
    done = [a for a in payload.get("actions") or [] if isinstance(a, dict)]
    lines = tool_lines(calls, done)
    if lines:
        hint = Text("  /expand or Ctrl-O shows the arguments", style="boss.dim") if calls else None
        parts.append(Padding(Group(*lines, *([hint] if hint else [])), (0, 0, 0, 2)))
    pending = payload.get("pending_confirmation")
    if isinstance(pending, dict) and pending.get("plan_id"):
        parts.append(confirmation_card(pending))
    if choices:
        parts.append(choices_card(choices))
    return parts


def turn(payload: Mapping[str, Any], choices: list[Mapping[str, Any]], **kwargs: Any) -> Group:
    return Group(*turn_parts(payload, choices, **kwargs))


def expanded(payload: Mapping[str, Any]) -> Group:
    calls = [c for c in payload.get("tool_calls") or [] if isinstance(c, dict)]
    done = [a for a in payload.get("actions") or [] if isinstance(a, dict)]
    return Group(Text("What the last reply read and did", style="bold"),
                 *tool_lines(calls, done, expanded=True))


def action_result(payload: Mapping[str, Any]) -> RenderableType:
    """What pressing an offer did (AgentActionRunResponse)."""
    tool = str(payload.get("tool") or "action")
    if payload.get("ok") is False or payload.get("error"):
        return tool_line(tool, "did not run", ok=False, detail=str(payload.get("error") or "refused"))
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    parts: list[RenderableType] = [tool_line(tool, "done", ok=True)]
    said = result.get("reply") or result.get("summary") or result.get("note") or result.get("message")
    if said:
        parts.append(Padding(Markdown(str(said)), (0, 0, 0, 2)))
    steps = result.get("steps")
    if isinstance(steps, list) and steps and all(isinstance(s, dict) for s in steps):
        parts.append(Padding(step_progress(steps), (0, 0, 0, 2)))
    for key in ("diff", "patch"):
        if isinstance(result.get(key), str) and result[key].strip():
            parts.append(Padding(diff(result[key]), (0, 0, 0, 2)))
    return Group(*parts)


# ── Tables and cards ─────────────────────────────────────────────────────────────────────────

def _table(title: str, *columns: str) -> Table:
    table = Table(title=title, title_justify="left", title_style="bold", box=box.SIMPLE_HEAD,
                  header_style="boss.dim", pad_edge=False, expand=False)
    for column in columns:
        table.add_column(column, overflow="fold")
    return table


def status_text(status: Any) -> Text:
    value = str(status or "?")
    lowered = value.lower()
    style = ("boss.ok" if lowered in DONE else "boss.err" if lowered in FAILED
             else "boss.decide" if lowered in {"pending", "delegated", "waiting_for_decision",
                                               "awaiting_approval", "firing"}
             else "boss.agent" if lowered in RUNNING else "")
    return Text(value, style=style)


def tasks_table(payload: Mapping[str, Any], page: int) -> RenderableType:
    items = [t for t in payload.get("items") or [] if isinstance(t, dict)]
    table = _table(f"Tasks in {payload.get('project_id') or 'this project'} · page {page}",
                   "ID", "Status", "Title")
    for task in items:
        table.add_row(Text(str(task.get("id") or "?"), style="boss.dim"),
                      status_text(task.get("status")), str(task.get("title") or "Untitled"))
    return Group(table if items else Text("No tasks on this page.", style="boss.dim"),
                 Text("/task ID for details · /task create KEY TITLE adds work", style="boss.dim"))


def decisions_table(payload: Mapping[str, Any]) -> RenderableType:
    items = [d for d in payload.get("items") or [] if isinstance(d, dict)]
    table = _table(f"{str(payload.get('view') or 'open').title()} decisions in {payload.get('project_id')} "
                   f"· {payload.get('total_count', len(items))} total · page {payload.get('page', 1)}/"
                   f"{payload.get('pages', 1)}", "ID", "Status", "Risk", "Question")
    for decision in items:
        table.add_row(Text(str(decision.get("id") or "?"), style="boss.dim"),
                      status_text(decision.get("status")), str(decision.get("risk_tier", "?")),
                      str(decision.get("question") or "Untitled"))
    return Group(table if items else Text("No decisions on this page.", style="boss.dim"),
                 Text("/decision ID opens the card", style="boss.dim"))


def decision_card(payload: Mapping[str, Any]) -> Panel:
    """One decision, as a card: the question, the facts, the options numbered, and the two
    commands that act on it (the API checks the reviewer and policy)."""
    rows = Table.grid(padding=(0, 1))
    rows.add_column(style="boss.dim", no_wrap=True)
    rows.add_column(overflow="fold")
    for field in ("status", "risk_tier", "task_id", "deadline_at", "reason", "default_action",
                  "approved_by", "delegated_to", "resolution_note"):
        if payload.get(field) is not None:
            value = payload[field]
            rows.add_row(field.replace("_", " "),
                         status_text(value) if field == "status" else Text(str(value)))
    parts: list[RenderableType] = [Text(str(payload.get("question") or "Untitled"), style="bold"),
                                   Text(""), rows]
    recommendation = payload.get("recommendation")
    if recommendation:
        parts += [Text(""), Text("Recommendation", style="boss.agent"),
                  Text(recommendation if isinstance(recommendation, str)
                       else json.dumps(recommendation, ensure_ascii=False, default=str))]
    options = payload.get("options")
    if isinstance(options, list) and options:
        parts += [Text(""), Text("Options", style="boss.agent")]
        for number, option in enumerate(options, 1):
            label = option.get("label") or option.get("title") or option.get("id") \
                if isinstance(option, dict) else option
            line = Text(f"  {number}  ", style="boss.decide")
            line.append(str(label))
            parts.append(line)
    if payload.get("cost_summary"):
        parts += [Text(""), Text("Cost", style="boss.agent"),
                  Text(json.dumps(payload["cost_summary"], ensure_ascii=False, default=str))]
    decision_id = str(payload.get("id") or "ID")
    open_states = {"pending", "delegated"}
    if str(payload.get("status") or "").lower() in open_states:
        act = Text("\n/decision approve ", style="boss.decide")
        act.append(f"{decision_id} NOTE", style="boss.dim")
        act.append("   /decision reject ", style="boss.decide")
        act.append(f"{decision_id} NOTE", style="boss.dim")
        parts.append(act)
    return Panel(Group(*parts), title=Text(f"Decision {decision_id}", style="boss.decide"),
                 title_align="left", border_style="boss.decide.border", box=box.ROUNDED,
                 padding=(0, 1))


def projects_table(payload: Mapping[str, Any]) -> RenderableType:
    table = _table("Projects", "", "ID", "Name", "Where")
    for project in payload.get("items") or []:
        if not isinstance(project, dict):
            continue
        active = project.get("id") == payload.get("active_project_id")
        where = " / ".join(str(v) for v in (project.get("org_name"), project.get("workspace_name")) if v)
        table.add_row(Text("●" if active else "", style="boss.agent"),
                      Text(str(project.get("id")), style="bold" if active else ""),
                      str(project.get("name") or project.get("slug") or ""), Text(where, style="boss.dim"))
    return Group(table, Text("/project ID switches (or /project for the picker)", style="boss.dim"))


def agents_table(payload: Mapping[str, Any]) -> RenderableType:
    if "items" not in payload:
        line = agent_badge(str(payload.get("name") or "BOSS"), str(payload.get("active_role") or ""))
        line.append(f"  {payload.get('note') or ''}", style="boss.dim")
        return line
    table = _table(f"Agents in {payload.get('project_id')}", "Agent", "Key", "Mission")
    active = payload.get("active_role")
    for agent in payload["items"]:
        key = str(agent.get("role_key"))
        badge = agent_badge(str(agent.get("name") or key), key)
        if key == active:
            badge.append("  (talking)", style="boss.dim")
        if agent.get("lifecycle") == "paused":
            badge.append("  paused", style="boss.decide")
        table.add_row(badge, Text(key, style="boss.dim"), str(agent.get("mission") or ""))
    return Group(table, Text("/agent KEY (or /agent for the picker) · /agent auto for BOSS",
                             style="boss.dim"))


def models_view(payload: Mapping[str, Any]) -> RenderableType:
    resolved = payload.get("resolved") or {}
    head = Text("Chat model  ", style="bold")
    head.append(str(resolved.get("label") or resolved.get("model") or "not resolved"), style="boss.agent")
    if not payload.get("choice"):
        head.append("  Auto", style="boss.dim")
    parts: list[RenderableType] = [head]
    if resolved.get("why"):
        parts.append(Text(str(resolved["why"]), style="boss.dim"))
    options = payload.get("options") or []
    if options:
        table = _table("", "Choose with", "Model", "")
        for option in options:
            spec = str(option.get("engine")) + (f":{option['model']}" if option.get("model") else "")
            table.add_row(Text(spec, style="boss.agent"), str(option.get("label") or ""),
                          Text("metered" if option.get("metered") else "", style="boss.decide"))
        parts.append(table)
    parts.append(Text("/model ENGINE[:MODEL] (or /model for the picker) · /model auto", style="boss.dim"))
    return Group(*parts)


def runs_table(payload: Mapping[str, Any]) -> RenderableType:
    items = [r for r in payload.get("items") or [] if isinstance(r, dict)]
    table = _table(f"Workflow runs in {payload.get('project_id')}", "Run", "Status", "Workflow", "Started")
    for run in items:
        table.add_row(Text(str(run.get("id")), style="boss.dim"), status_text(run.get("status")),
                      str(run.get("workflow_name") or run.get("workflow_key") or ""),
                      Text(str(run.get("created_at") or "")[:16].replace("T", " "), style="boss.dim"))
    return Group(table if items else Text("No runs.", style="boss.dim"),
                 Text("/run ID shows its steps · /follow ID watches them", style="boss.dim"))


def run_view(payload: Mapping[str, Any]) -> RenderableType:
    steps = [s for s in payload.get("steps") or [] if isinstance(s, dict)]
    head = Text(f"Run {payload.get('id')}  ", style="bold")
    head.append(str(payload.get("workflow_name") or payload.get("workflow_key") or ""))
    head.append("  ")
    head.append_text(status_text(payload.get("status")))
    parts: list[RenderableType] = [head]
    if steps:
        parts.append(Padding(step_progress(steps), (0, 0, 0, 2)))
        parts.append(Padding(checklist("Steps", steps), (0, 0, 0, 2)))
    for field in ("blocked_reason", "error"):
        if payload.get(field):
            parts.append(Text(f"  {field.replace('_', ' ')}: {payload[field]}",
                              style="boss.err" if field == "error" else "boss.decide"))
    return Group(*parts)


SEV_STYLE = {"sev1": "boss.err", "sev2": "boss.err", "sev3": "boss.decide", "sev4": "boss.dim",
             "critical": "boss.err", "warning": "boss.decide", "info": "boss.dim"}


def alerts_table(payload: Mapping[str, Any]) -> RenderableType:
    alerts = [a for a in payload.get("alerts") or [] if isinstance(a, dict)]
    counts = payload.get("counts") or {}
    title = (f"Alerts in {payload.get('project_id')} · {counts.get('firing', 0)} firing · "
             f"{counts.get('pending', 0)} pending · {counts.get('needs_attention', 0)} need attention")
    table = _table(title, "Sev", "State", "App", "Alert")
    for alert in alerts:
        sev = str(alert.get("sev") or alert.get("severity") or "?")
        table.add_row(Text(sev.upper(), style=SEV_STYLE.get(sev.lower(), "")),
                      status_text(alert.get("state_word") or alert.get("state")),
                      str(alert.get("app") or ""),
                      str(alert.get("summary") or alert.get("alertname") or alert.get("label") or ""))
    return table if alerts else Group(Text(title, style="bold"), Text("No alerts.", style="boss.dim"))


def api_menu(payload: Mapping[str, Any]) -> RenderableType:
    """The chooser /api draws: the API's domains, or one numbered listing of operations."""
    if payload.get("view") == "domains":
        table = _table("The API on this install", "Domain", "Routes", "")
        for domain in payload.get("domains") or []:
            table.add_row(Text(str(domain.get("id")), style="boss.agent"),
                          str(domain.get("route_count")), str(domain.get("title") or ""))
        return Group(table, Text(f"project {payload.get('active_project_id')}", style="boss.dim"),
                     Text(str(payload.get("hint") or ""), style="boss.dim"))
    title = (f"{payload.get('count')} operations for {payload.get('search')!r} · page "
             f"{payload.get('page')}/{payload.get('pages')}")
    table = _table(title, "#", "Method", "Path", "Summary")
    for operation in payload.get("operations") or []:
        summary = str(operation.get("summary") or "")
        if not operation.get("terminal_invokable"):
            summary += f"  [{operation.get('limitation') or 'not addressable by /op'}]"
        table.add_row(Text(str(operation.get("index") or ""), style="boss.decide"),
                      str(operation.get("method")),
                      Text(str(operation.get("path")), style="boss.agent"), summary)
    hint = Text(str(payload.get("hint") or ""), style="boss.dim")
    if payload.get("next_page_command"):
        hint = Text(f"{payload['next_page_command']} · ", style="boss.dim").append_text(hint)
    return Group(table, hint)


def api_card(payload: Mapping[str, Any], *, plain: str = "") -> RenderableType:
    """One operation, picked: what it takes, its path template, and the lines to copy."""
    heading = Text(f"{payload.get('method')} {payload.get('path')}", style="bold")
    if payload.get("operation_id"):
        heading.append(f"\n{payload['operation_id']}", style="boss.dim")
    parts: list[RenderableType] = [heading]
    for key in ("summary", "description"):
        if payload.get(key):
            parts.append(Text(str(payload[key]).strip()))
    fields = Table.grid(padding=(0, 2))
    fields.add_column(style="boss.agent", no_wrap=True)
    fields.add_column(overflow="fold")
    for parameter in payload.get("parameter_fields") or []:
        fields.add_row(f"{parameter.get('in')} {parameter.get('name')}",
                       ", ".join(v for v in (str(parameter.get("type") or ""),
                                             "required" if parameter.get("required") else "optional") if v))
    for field in payload.get("body_fields") or []:
        detail = [str(field.get("type") or ""), "required" if field.get("required") else "optional"]
        if field.get("enum"):
            detail.append(f"choices={field['enum']}")
        if field.get("description"):
            detail.append(str(field["description"]))
        fields.add_row(f"body {field.get('name')}", ", ".join(d for d in detail if d))
    if fields.row_count:
        parts += [Text(""), fields]
    if payload.get("auto_filled_body_fields"):
        parts.append(Text("The selected project is inserted when the body omits project_id.",
                          style="boss.dim"))
    if not payload.get("terminal_invokable"):
        parts.append(Text(f"Limit: {payload.get('limitation')}", style="boss.decide"))
    if payload.get("path_template"):
        parts += [Text(""), Text("Path template", style="boss.dim"),
                  Text(f"{payload.get('method')} {payload['path_template']}", style="boss.agent")]
    lines = [line for line in (payload.get("run_command"), payload.get("example"),
                               payload.get("api_example")) if line]
    if lines:
        parts.append(Text(""))
        parts.append(Text("Copy one of these and replace every <value>", style="boss.dim"))
        parts += [_inline_code(line) for line in lines]
    parts += [Text(""), Text(str(payload.get("access_note") or ""), style="boss.dim")]
    title = Text(f"[{payload['index']}] operation", style="boss.agent") if payload.get("index") else None
    return Panel(Group(*parts), title=title, title_align="left", border_style="boss.hex",
                 box=box.ROUNDED, padding=(0, 1))


def ops_table(payload: Mapping[str, Any]) -> RenderableType:
    if "domains" in payload:
        table = _table(f"API domains · project {payload.get('active_project_id')}", "Domain", "Routes", "")
        for domain in payload["domains"]:
            table.add_row(Text(str(domain.get("id")), style="boss.agent"), str(domain.get("route_count")),
                          str(domain.get("title") or ""))
        return Group(table, Text(str(payload.get("hint") or ""), style="boss.dim"))
    table = _table(f"{payload.get('count')} matches for {payload.get('search')!r} · page "
                   f"{payload.get('page')}/{payload.get('pages')}", "#", "Operation", "Method", "Path", "Summary")
    for operation in payload.get("operations") or []:
        alias = operation.get("operation_id") or f"/api {operation['method']} {operation['path']}"
        summary = str(operation.get("summary") or "")
        if not operation.get("terminal_invokable"):
            summary += f"  [{operation.get('limitation') or 'not addressable by /op'}]"
        table.add_row(Text(str(operation.get("index") or ""), style="boss.decide"),
                      Text(str(alias), style="boss.agent"), str(operation.get("method")),
                      str(operation.get("path")), summary)
    hint = "/api NUMBER for the card, or /describe OPERATION_ID before /op"
    if payload.get("page", 1) < payload.get("pages", 1):
        hint = f"Next: /ops {payload.get('search')} --page {payload.get('page', 1) + 1} · " + hint
    return Group(table, Text(hint, style="boss.dim"))


def thread_view(payload: Mapping[str, Any]) -> RenderableType:
    title = Text(f"Thread {payload.get('thread_id')}", style="bold")
    if payload.get("working"):
        title.append("  BOSS is working", style="boss.agent")
    parts: list[RenderableType] = [title]
    for message in payload.get("messages") or []:
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
            continue
        if message["role"] == "user":
            line = Text("you ", style="boss.you")
            line.append(str(message.get("content") or "").strip())
            parts.append(line)
        else:
            parts.append(agent_badge("BOSS"))
            parts += [Padding(b, (0, 0, 0, 2)) for b in markdown_blocks(str(message.get("content") or ""))]
    return Group(*parts)


def help_table(commands: Iterable[tuple[str, str, str]]) -> RenderableType:
    table = _table("Commands", "Command", "Arguments", "")
    for name, args, description in commands:
        table.add_row(Text(name, style="boss.agent"), Text(args, style="boss.dim"), description)
    keys = Text()
    for key, what in (("Enter", "send"), ("Esc", "cancel a turn"), ("↑↓", "history"),
                      ("Tab", "complete"), ("Ctrl-O", "expand tool calls"), ("Ctrl-D", "quit")):
        keys.append(f"{key} ", style="bold")
        keys.append(f"{what}   ", style="boss.dim")
    return Group(Text("Type a sentence to talk to BOSS; a number picks a choice.", style="bold"),
                 table, keys)


def payload_panel(payload: Any, title: str = "") -> RenderableType:
    """A captain command's result, or anything without its own view."""
    if isinstance(payload, (dict, list)):
        body: RenderableType = JSON(json.dumps(payload, default=str, ensure_ascii=False))
    else:
        body = Text(str(payload))
    return Panel(body, title=title or None, title_align="left", border_style="boss.hex",
                 box=box.ROUNDED, padding=(0, 1)) if title else body


# ── Errors ───────────────────────────────────────────────────────────────────────────────────

def _detail(body: str) -> str:
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError):
        return body
    if isinstance(parsed, dict):
        detail = parsed.get("detail", parsed)
        if isinstance(detail, dict):
            return str(detail.get("message") or detail.get("detail") or json.dumps(detail))
        if isinstance(detail, list):
            return "; ".join(str(d.get("msg") if isinstance(d, dict) else d) for d in detail)
        return str(detail)
    return body


def error_fix(status: int, detail: str, *, api_url: str = "", project_id: str = "") -> str:
    """The one-line fix for an HTTP failure. Words only; the server's own detail is shown
    above it unchanged."""
    if status == 0:
        return (f"Is BOSS running? `boss status` checks it (API {api_url or 'BOSS_API_URL'}); "
                "`make dev` starts it from the install.")
    if status == 401:
        return ("The key was refused: on the install machine `boss identity host-key` issues a "
                "new one; elsewhere make one in My account › API keys.")
    if status == 403:
        return "This key lacks the permission. An Owner can grant it in People & access."
    if status == 404:
        return f"Not found in {project_id or 'this project'}: check the ID, or `/project` to switch."
    if status == 409:
        return "BOSS refused it in the current state; the reason is above. A decision may need a person."
    if status == 422:
        return "The request was not valid: the field is named above. `/describe` lists the fields."
    if status == 429:
        return "Rate limited. Wait a moment and try again."
    if status >= 500:
        return "BOSS failed inside: `boss logs api` shows why. The console's Observability has it too."
    return "See the message above."


def error_panel(message: str, fix: str, *, title: str = "Error") -> Panel:
    body = Text(message)
    if fix:
        body.append("\n\n")
        body.append("fix ", style="boss.decide")
        body.append_text(_inline_code(fix))
    return Panel(body, title=Text(title, style="boss.err"), title_align="left",
                 border_style="boss.err.border", box=box.ROUNDED, padding=(0, 1))


def step_up_card() -> Panel:
    body = Text("This action needs you to confirm it is you (step-up).\n\n", style="bold")
    body.append("Sign in again in the console (My account), then press ", style="")
    body.append("Enter", style="boss.decide")
    body.append(" here to run the same command again, or ", style="")
    body.append("Esc", style="boss.decide")
    body.append(" to leave it.", style="")
    return Panel(body, title=Text("Confirm it's you", style="boss.decide"), title_align="left",
                 border_style="boss.decide.border", box=box.ROUNDED, padding=(0, 1))


def still_running_card(payload: Mapping[str, Any]) -> Panel:
    body = Text(str(payload.get("body") or "BOSS is still working on the server."))
    thread_id = payload.get("thread_id")
    if thread_id:
        body.append("\n\n")
        body.append(f"/thread {thread_id}", style="boss.agent")
        body.append(" shows the reply when it lands.", style="boss.dim")
    return Panel(body, title=Text("Still working", style="boss.agent"), title_align="left",
                 border_style="boss.hex", box=box.ROUNDED, padding=(0, 1))


def is_step_up(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("__http_error__") and \
        payload.get("status") == 401 and "step_up_required" in str(payload.get("body") or "")


def failure(payload: Mapping[str, Any], *, api_url: str = "", project_id: str = "") -> RenderableType:
    if payload.get("__command_error__"):
        return error_panel(str(payload.get("message")), "`/help` lists every command and its arguments.")
    if "still_running" in payload or payload.get("__turn_dropped__"):
        return still_running_card(payload)
    if is_step_up(payload):
        return step_up_card()
    status = int(payload.get("status") or 0)
    detail = _detail(str(payload.get("body") or ""))
    title = "BOSS is not answering" if status == 0 else f"HTTP {status}"
    return error_panel(detail, error_fix(status, detail, api_url=api_url, project_id=project_id),
                       title=title)


# ── One result, whatever the command ─────────────────────────────────────────────────────────

def result(client: Any, command: Any, payload: Any, *, commands: Iterable[tuple[str, str, str]] = ()) -> Optional[RenderableType]:
    """The rich twin of MetatronClient.render: the same payload, drawn."""
    if payload is None:
        return None
    tui = _tui(client)
    if tui is not None and hasattr(tui, "redact_payload"):
        payload = tui.redact_payload(payload, client.config.api_key)
    kind = command.kind
    if isinstance(payload, dict) and (payload.get("__command_error__") or payload.get("__http_error__")):
        return failure(payload, api_url=client.config.api_url, project_id=client.project_id)
    if kind == "help":
        return help_table(commands)
    if kind in {"session", "confirm"} or (kind == "pick" and isinstance(payload, dict) and "reply" in payload):
        return turn(payload, client.choices(), agent_name=client.role_name or "BOSS",
                    agent_key=client.role or "")
    if kind == "pick" and isinstance(payload, dict):
        return action_result(payload)
    views = {"tasks": lambda p: tasks_table(p, command.page), "decisions": decisions_table,
             "decision": decision_card, "decision_action": decision_card, "projects": projects_table,
             "agent": agents_table, "model": models_view, "runs": runs_table, "run": run_view,
             "alerts": alerts_table, "ops": ops_table, "status": status_panel, "thread": thread_view,
             "expand": expanded, "api_menu": api_menu, "api_template": api_card}
    if kind in views and isinstance(payload, dict):
        try:
            return views[kind](payload)
        except (KeyError, TypeError, AttributeError):
            return payload_panel(payload)
    if kind in {"watch", "overwatch", "drive", "goals"} and isinstance(payload, dict):
        title = {"watch": "Watch", "overwatch": "Overwatch", "drive": "Drive", "goals": "Goals"}[kind]
        text = tui.format_payload(payload) if tui is not None else json.dumps(payload, default=str)
        return Panel(Text(text), title=Text(title, style="boss.agent"), title_align="left",
                     border_style="boss.hex", box=box.ROUNDED, padding=(0, 1))
    if kind == "task" and isinstance(payload, dict):
        return Text(client.render(command, payload))
    if kind == "describe" and isinstance(payload, dict):
        return Text(client.render(command, payload))
    if kind == "coverage":
        return Text(client.render(command, payload))
    if isinstance(payload, (dict, list)):
        return payload_panel(payload)
    return Text(str(payload))


def _tui(client: Any) -> Any:
    """The metatron_tui module the client came from (it may be __main__)."""
    return sys.modules.get(type(client).__module__)


def print_result(console: Console, client: Any, command: Any, payload: Any) -> None:
    rendered = result(client, command, payload,
                      commands=getattr(_tui(client), "SLASH_COMMANDS", ()))
    if rendered is not None:
        console.print(rendered)


@contextlib.contextmanager
def working(console: Console, *, active: bool = True, label: str = "BOSS is working") -> Iterator[None]:
    """A spinner with the elapsed seconds while a one-shot turn runs (on stderr's console)."""
    if not active or not console.is_terminal:
        yield
        return
    started = time.monotonic()
    stop = threading.Event()
    with console.status(f"{label} · 0s", spinner="dots", spinner_style="boss.agent") as status:
        def tick() -> None:
            while not stop.wait(0.5):
                status.update(f"{label} · {int(time.monotonic() - started)}s")

        thread = threading.Thread(target=tick, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
