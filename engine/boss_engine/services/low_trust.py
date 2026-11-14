"""Low-trust containment for content that arrived from outside the operator (BOSSAPP-820).

A fetched page, an external tool result, or an untrusted document is marked. A later tool call
that would send that text to an external provider or a write tool waits for approval. The send
does not run until then. The operator's own words are never marked.

This is not prompt-injection screening: nothing here classifies what the text says. It only
remembers text that arrived from outside and refuses to forward that same text.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

#: Long enough that a row id or a status word is not a span, short enough that a pasted
#: paragraph is. The marked payload has to appear whole inside a later argument.
MIN_SPAN = 32
MAX_SPANS = 40
MAX_SPAN_CHARS = 4000

ORIGINS = ("fetched_page", "tool_result", "document")
SINKS = ("external_provider", "write")

#: Metadata on a tool result, not the outside text itself.
_SKIP_KEYS = frozenset(
    {
        "untrusted",
        "screen_warning",
        "trust_origin",
        "status",
        "receipt_id",
        "decision_id",
        "skill_id",
        "server_id",
        "origin",
        "sink",
        "tool",
        "tool_call_id",
        "ok",
        "error",
    }
)

#: Tool names whose bytes came from outside the operator's own project.
_FETCH_TOKENS = ("fetch_page", "study_site", "search_web", "fetch_link", "fetch_image",
                 # Ticket text is written outside BOSS (BOSSAPP-1047 B2): the ticket read tools,
                 # and the tickets.read skill that offers them to workflow agents.
                 "ticket_get", "ticket_search", "ticket_browse", "tickets.read")

#: Skills that send arguments to something outside BOSS, or that write.
_EXTERNAL_SKILL = (
    "mcp_tool",
    "email.send",
    "slack.post",
    "mattermost.post",
    "publish_youtube",
    "create_pr",
    "generate_text",
    "generate_code",
    "issues.create",
    "issue.ensure",
    "issue.update",
    # Ticket writes go to the team's own ticket system (BOSSAPP-1047 B2).
    "tickets.write",
)
_WRITE_SKILL = ("code.write", "docs.draft", "knowledge.propose")


class Ledger:
    """The outside text seen so far in one run. Spans are exact, so a paraphrase is not held
    and a marked payload is."""

    def __init__(self) -> None:
        self.spans: list[tuple[str, str]] = []

    def add(self, text: str, origin: str) -> None:
        if origin not in ORIGINS:
            return
        cleaned = (text or "").strip()
        if len(cleaned) < MIN_SPAN:
            return
        cleaned = cleaned[:MAX_SPAN_CHARS]
        if any(existing == cleaned for _, existing in self.spans):
            return
        if len(self.spans) >= MAX_SPANS:
            return
        self.spans.append((origin, cleaned))

    def origin_in(self, value: Any) -> Optional[str]:
        """The origin of the first remembered span that appears inside `value`, if any."""
        haystacks = _strings(value)
        for origin, text in self.spans:
            if any(text in item for item in haystacks):
                return origin
        return None

    def export(self) -> list[dict[str, str]]:
        return [{"origin": origin, "text": text} for origin, text in self.spans]

    @classmethod
    def load(cls, raw: Any) -> "Ledger":
        ledger = cls()
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    ledger.add(str(item.get("text") or ""), str(item.get("origin") or ""))
        return ledger

    def absorb_messages(self, messages: Any) -> None:
        """Recover marks from tool messages already saved on the run."""
        for message in messages or []:
            if not isinstance(message, dict) or message.get("role") != "tool":
                continue
            payload = _decode(message.get("content"))
            if not _marked(payload):
                continue
            origin = _find_origin(payload) or "tool_result"
            for text in _spans(payload):
                self.add(text, origin)


def remember(content: Any, *, origin: str, ledger: Ledger, echoed: Any = None) -> dict:
    """Mark `content` as untrusted and remember its text. Strings already in `echoed` (the
    call's own arguments, such as the URL that was fetched) are not the arrival."""
    if origin not in ORIGINS:
        raise ValueError(f"unknown outside origin: {origin}")
    if isinstance(content, dict):
        marked = {**content, "untrusted": True, "trust_origin": origin}
    else:
        marked = {"untrusted": True, "trust_origin": origin, "content": content}
    echoed_text = set(_spans(echoed))
    for text in _spans(marked):
        if text in echoed_text:
            continue
        ledger.add(text, origin)
    return marked


def blocked(args: Any, *, sink: str, ledger: Ledger, approved: bool = False) -> Optional[dict]:
    """The hold, when this call would send remembered outside text. None when it may run.
    Does not send."""
    if approved or sink not in SINKS:
        return None
    origin = ledger.origin_in(args)
    if origin is None:
        return None
    return {
        "status": "awaiting_approval",
        "untrusted": True,
        "origin": origin,
        "sink": sink,
        "sent": False,
        "message": (
            "This call carries content that arrived from outside the operator "
            f"({origin.replace('_', ' ')}). It is waiting for approval and was not sent."
        ),
    }


def release(
    args: dict,
    *,
    sink: str,
    ledger: Ledger,
    send: Callable[[dict], Any],
    approved: bool = False,
) -> dict:
    """Send `args` through `send`, unless they carry marked outside text that is not approved."""
    hold = blocked(args, sink=sink, ledger=ledger, approved=approved)
    if hold is not None:
        return hold
    return {"status": "sent", "sent": True, "result": send(args)}


def outside_origin(name: str, *, kind: str = "", mcp_tool: str = "") -> Optional[str]:
    """How this tool's result arrived, or None when it is the operator's own project data."""
    blob = f"{name} {mcp_tool}".lower()
    if any(token in blob for token in _FETCH_TOKENS):
        return "fetched_page"
    if "read_document" in blob:
        return "document"
    if kind == "mcp":
        return "tool_result"
    return None


def sink_for_skill(skill: dict) -> Optional[str]:
    """`external_provider` or `write` when invoking this skill would send or write, else None."""
    policy = skill.get("policy") or {}
    if policy.get("mcp_server_id") or str(skill.get("kind") or "") == "mcp":
        return "external_provider"
    skill_id = str(skill.get("id") or "").lower()
    if any(token in skill_id for token in _EXTERNAL_SKILL):
        return "external_provider"
    if any(token in skill_id for token in _WRITE_SKILL):
        return "write"
    return None


def _decode(content: Any) -> Any:
    if isinstance(content, str):
        try:
            return json.loads(content)
        except ValueError:
            return None
    return content


def _marked(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("untrusted") is True:
            return True
        return any(_marked(item) for item in value.values())
    if isinstance(value, list):
        return any(_marked(item) for item in value)
    return False


def _find_origin(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        origin = value.get("trust_origin")
        if origin in ORIGINS:
            return str(origin)
        for item in value.values():
            found = _find_origin(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_origin(item)
            if found:
                return found
    return None


def _spans(value: Any, *, key: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        cleaned = value.strip()
        if key not in _SKIP_KEYS and len(cleaned) >= MIN_SPAN:
            found.append(cleaned[:MAX_SPAN_CHARS])
    elif isinstance(value, dict):
        for child_key, child in value.items():
            name = str(child_key)
            if name in _SKIP_KEYS and not isinstance(child, (dict, list)):
                continue
            found.extend(_spans(child, key=name))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.extend(_spans(child, key=key))
    return found


def _strings(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for child in value.values():
            found.extend(_strings(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.extend(_strings(child))
    return found
