"""Deploy notes: the worked example of boss.extension/v1 (BOSSAPP-861).

One extension, every capability:

    tools.register     add_note (tier 1), list_notes (tier 0), clear_notes (tier 2: asks a person)
    events.subscribe   deploy.* events become notes automatically
    jobs.schedule      an hourly digest counter
    webhooks.receive   `deploy`: an outside CI posts {"note": "..."}; kept install-wide
    state.read/write   the notes themselves, under `ui:notes` so the panel can show them
    ui.slot.register   a project panel listing the notes, and a settings panel
"""
from __future__ import annotations

from datetime import datetime, timezone

from boss_engine.extensions.sdk import Extension, ExtensionError

MAX_NOTES = 50


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DeployNotes(Extension):
    def _add(self, ctx, text: str, source: str) -> int:
        notes = list(ctx.state.get("ui:notes", []))
        notes.append({"text": text[:500], "source": source, "at": _now()})
        notes = notes[-MAX_NOTES:]
        ctx.state.put("ui:notes", notes)
        return len(notes)

    def on_tool(self, name, args, ctx):
        if name == "add_note":
            text = str(args.get("text") or "").strip()
            if not text:
                raise ExtensionError("a note needs text")
            return {"ok": True, "count": self._add(ctx, text, "tool")}
        if name == "list_notes":
            return {"notes": ctx.state.get("ui:notes", [])}
        if name == "clear_notes":
            ctx.state.delete("ui:notes")
            return {"ok": True}
        raise ExtensionError(f"no tool {name}")

    def on_events(self, events, ctx):
        for event in events:
            self._add(ctx, f"{event['kind']} on {event['entityType']} {event['entityId']}", "event")
        if not events:
            return {}
        ctx.state.put("seen:last", events[-1]["id"])
        return {"ack": events[-1]["id"]}

    def on_job(self, name, ctx):
        if name != "digest":
            raise ExtensionError(f"no job {name}")
        runs = int(ctx.state.get("digest:runs", 0)) + 1
        ctx.state.put("digest:runs", runs)
        ctx.state.put("digest:last", _now())
        return {"ok": True, "runs": runs}

    def on_webhook(self, name, request, ctx):
        payload = request.json() or {}
        note = str(payload.get("note") or "").strip()
        if not note:
            return {"status": 422, "body": {"error": "send {\"note\": \"...\"}"}}
        inbox = list(ctx.state.get("webhook:inbox", []))[-(MAX_NOTES - 1):]
        inbox.append({"text": note[:500], "at": _now(),
                      "from": request.headers.get("user-agent", "")[:100]})
        ctx.state.put("webhook:inbox", inbox)
        return {"status": 202, "body": {"received": True, "queued": len(inbox)}}
