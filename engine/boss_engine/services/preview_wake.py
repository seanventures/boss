"""WAKE ON VISIT: a preview BOSS stopped is asleep, not gone.

THE CASE (recipe-box, 2026-11-19). Every preview of the app had been stopped by the idle reaper
("nobody opened it for 20 min"). Its link answered `preview not running (status=stopped)` in
plain text, and the only way back was build page → Outputs → Preview → Run preview.

THE RULE. A page load of a preview link that BOSS itself stopped (idle, past its time limit, its
process gone: the error starts "Stopped by BOSS:", or `expired`) starts that build's preview
again and answers a friendly "Waking up your app…" page that refreshes itself; once the new
preview serves, the old link redirects to it. A link a newer preview of the same build already
replaced redirects (or waits) the same way, so an old link never dies.

BOUNDED. The link is a capability (an unguessable id); waking it extends that capability to
"start this build's preview again", so it is kept narrow:
  * a page navigation only (never an asset or API call: those get 503 + Retry-After);
  * only what BOSS stopped — a person's own Stop stays stopped (they open it again from BOSS);
    a preview whose start FAILED is not retried by a visit (no restart loops);
  * only previews made in the last WAKE_WINDOW_DAYS, and only one start per build at a time
    (an advisory lock on the build, then "a newer preview is starting" wins);
  * only while the host has room (the admission gate reconcile uses); otherwise it waits.
Every wake is an `app_preview.woken` event by `boss:wake-on-visit`.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text

from ..db import begin

log = logging.getLogger(__name__)

WAKE_ACTOR = "boss:wake-on-visit"
WAKE_WINDOW_DAYS = 14


def wake_window_days() -> int:
    """BOSS_PREVIEW_WAKE_DAYS (default 14): how old a sleeping link may be and still wake; 0
    turns wake on visit off."""
    try:
        return int(os.environ.get("BOSS_PREVIEW_WAKE_DAYS", WAKE_WINDOW_DAYS))
    except ValueError:
        return WAKE_WINDOW_DAYS
#: Seconds between the waking page's own refreshes.
REFRESH_SECONDS = 4


def _latest_of_run(c: Any, run_id: str) -> Optional[dict[str, Any]]:
    row = c.execute(text(
        "SELECT id, status, error, created_at, mode FROM app_previews WHERE run_id = :r "
        "AND COALESCE(mode, '') <> 'fixture' ORDER BY created_at DESC LIMIT 1"),
        {"r": run_id}).mappings().first()
    return dict(row) if row else None


def wake(engine: Any, settings: Any, preview_id: str, *, page: bool,
         now: Optional[datetime] = None) -> dict[str, Any]:
    """What the gateway does with a request for `preview_id`:

    * {"do": "serve"}                       — it runs: proxy as always;
    * {"do": "redirect", "to": <id>}        — a newer preview of the same build runs;
    * {"do": "wait", "id": <id>, "woke": b} — it (or its replacement) is starting; `woke` when
                                              this very request started it;
    * {"do": "gone", "why": <words>}        — nothing to wake (unknown, a person stopped it,
                                              its start failed, too old).
    `page` is whether the request is a page navigation: only a page load wakes a preview."""
    from .preview_service import FIXTURE_MODE
    from .project_experience.your_app import sleep_state

    now = now or datetime.now(timezone.utc)
    with begin(engine) as c:
        row = c.execute(text(
            "SELECT id, run_id, project_id, status, error, created_at, mode FROM app_previews "
            "WHERE id = :id"), {"id": preview_id}).mappings().first()
    if row is None:
        return {"do": "gone", "why": "This preview does not exist."}
    if row["status"] == "running":
        return {"do": "serve"}
    if row["status"] == "verified":
        return {"do": "gone", "why": "This build has no live app: its tests ran instead."}
    if str(row.get("mode") or "") == FIXTURE_MODE or not row["run_id"]:
        return {"do": "gone", "why": "This preview is not running."}
    run_id = str(row["run_id"])
    # THE REQUESTED LINK DECIDES FIRST (security review #230): only a link BOSS itself put to
    # sleep, inside the wake window, may follow its build's newer preview or wake it. A link a
    # person stopped, one whose start failed, and one past the window are gone — never a door
    # to whatever preview the build runs next.
    mine = {"preview_id": preview_id}
    own = sleep_state(row["status"], row["error"])
    if own == "starting":
        return {"do": "wait", "id": preview_id, "woke": False}
    if own == "stopped":
        return {"do": "gone", "why": "This preview was stopped. Recreate it from BOSS: it "
                                     "starts again from the same build, with its data.", **mine}
    if own != "asleep":
        return {"do": "gone", "why": "This preview could not start. Recreate it from BOSS to "
                                     "try again from the same build.", **mine}
    days = wake_window_days()
    made = row["created_at"]
    if made is not None and made.tzinfo is None:
        made = made.replace(tzinfo=timezone.utc)
    if days <= 0 or (made is not None and now - made > timedelta(days=days)):
        return {"do": "gone", "why": "This preview link is too old to wake. Recreate it from "
                                     "BOSS: it starts again from the same build, with its "
                                     "data.", **mine}
    with begin(engine) as c:
        # ONE START PER BUILD: two tabs loading the same sleeping link start one preview.
        c.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"preview-wake:{run_id}"})
        latest = _latest_of_run(c, run_id) or dict(row)
        if latest["id"] != preview_id:
            st = sleep_state(latest["status"], latest["error"])
            if latest["status"] == "running":
                return {"do": "redirect", "to": str(latest["id"])}
            if st == "starting":
                return {"do": "wait", "id": str(latest["id"]), "woke": False}
            if st != "asleep":
                # Its replacement was stopped by a person, failed, or has no live app: an old
                # link never overrides that.
                return {"do": "gone", "why": "This preview was replaced and is not running. "
                                             "Recreate it from BOSS.", **mine}
        if not page:
            return {"do": "wait", "id": preview_id, "woke": False}
        if not _room(engine, settings):
            # NO ROOM TO WAKE IT NOW: the page keeps trying, and offers Recreate in BOSS.
            return {"do": "wait", "id": preview_id, "woke": False, "room": False, **mine}
        from .preview_service import start_preview_async

        try:
            started = start_preview_async(engine, settings, run_id, actor=WAKE_ACTOR)
        except ValueError:
            return {"do": "gone", "why": "The build behind this preview is gone."}
        try:
            from .core import emit_event

            emit_event(c, str(row["project_id"]), "app_preview.woken", WAKE_ACTOR, "app_preview",
                       str(started["id"]), {"run_id": run_id, "from_preview_id": preview_id,
                                             "reason": "opened while asleep"})
        except Exception:  # noqa: BLE001 — the wake stands without its log line
            log.debug("preview %s: woken event not written", preview_id, exc_info=True)
        return {"do": "wait", "id": str(started["id"]), "woke": True}


def _room(engine: Any, settings: Any) -> bool:
    from .preview_service import _respawn_allowed  # noqa: PLC2701 — the one admission answer

    try:
        return bool(_respawn_allowed(engine, settings))
    except Exception:  # noqa: BLE001
        return True


_STYLE = (
    "<style>:root{color-scheme:light dark}body{margin:0;min-height:100vh;display:grid;"
    "place-items:center;font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;"
    "background:#f6f7f9;color:#1d232b}.c{max-width:420px;margin:16px;padding:28px 26px;"
    "border:1px solid #e3e7ec;border-radius:14px;background:#fff;text-align:center}"
    "h1{font-size:18px;margin:14px 0 6px}p{margin:0;color:#5b6573}"
    ".s{width:28px;height:28px;margin:auto;border-radius:50%;border:3px solid #0d9488;"
    "border-right-color:transparent;animation:r 1s linear infinite}"
    "@keyframes r{to{transform:rotate(360deg)}}"
    "@media (prefers-reduced-motion:reduce){.s{animation:none}}"
    "@media (prefers-color-scheme:dark){body{background:#12161c;color:#e7ebf0}"
    ".c{background:#1b2129;border-color:#2a323d}p{color:#a8b2bf}}</style>"
)


def _recreate_link(href: str) -> str:
    import html

    return ("" if not href else
            f"<p style='margin-top:14px'><a href=\"{html.escape(href, quote=True)}\" target=_top "
            "style='color:#0d9488;font-weight:600'>Recreate preview in BOSS</a></p>")


def recreate_href(console_base: str, preview_id: str) -> str:
    """The console's door to Recreate preview: the project's Your app card, asked to offer it.
    Only the preview id rides in the link (the console's deep-link resolver finds its project
    for a person who may read it); no project or build id is shown on an unauthenticated page."""
    from urllib.parse import urlencode

    if not preview_id:
        return ""
    query = urlencode({"area": "project", "recreate_preview": preview_id})
    return f"{console_base.rstrip('/')}/?{query}"


def waking_page(*, woke: bool, seconds: int = REFRESH_SECONDS, room: bool = True,
                recreate: str = "") -> bytes:
    """The friendly page a sleeping (or starting) preview answers while it starts. It reloads
    itself (meta refresh: allowed under the preview sandbox, which has allow-scripts); no script,
    no request of its own, no credential."""
    lead = ("Waiting for room to start your app…" if not room
            else "Waking up your app…" if woke else "Starting your app…")
    said = ("This machine is short of memory right now; the page keeps trying by itself."
            if not room else ("It was asleep to save room. " if woke else "") + (
                "This takes about a minute; the page opens the app by itself when it is ready."))
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"<meta http-equiv=refresh content={int(seconds)}>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{lead}</title>{_STYLE}</head><body>"
        "<div class=c role=status aria-live=polite><div class=s aria-hidden=true></div>"
        f"<h1>{lead}</h1><p>{said}</p>{_recreate_link(recreate if not room else '')}</div>"
        "</body></html>"
    ).encode()


def gone_page(why: str, recreate: str = "") -> bytes:
    import html

    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8><title>This preview is not running"
        "</title><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<style>:root{color-scheme:light dark}body{font:15px/1.5 system-ui,sans-serif;"
        "max-width:480px;margin:48px auto;padding:0 16px}</style></head><body>"
        "<h1 style='font-size:18px'>This preview is not running</h1>"
        f"<p>{html.escape(why)}</p>{_recreate_link(recreate)}</body></html>"
    ).encode()


def sibling_location(host_header: Optional[str], scheme: str, old_id: str, new_id: str,
                     path: str, query: str, *, on_preview_origin: bool) -> str:
    """Where an old preview link sends a browser once its replacement runs. On the preview
    origin each preview is its own host (`prev-old.localhost:3001`): the replacement's host is
    the same with its label (preview_origin refuses another preview's host). Elsewhere (the
    API's gateway path), a path on the same host."""
    from urllib.parse import quote

    from .preview_service import preview_host_label

    tail = f"/v1/previews/{quote(new_id, safe='')}/web/{quote(path or '', safe='/')}"
    if query:
        tail += "?" + query
    if not on_preview_origin:
        return tail
    host = (host_header or "").strip()
    old_label, new_label = preview_host_label(old_id), preview_host_label(new_id)
    first, dot, rest = host.partition(".")
    if old_label and new_label and dot and first.lower() == old_label:
        return f"{'https' if scheme == 'https' else 'http'}://{new_label}.{rest}{tail}"
    return tail


def is_page_load(headers: Any) -> bool:
    """A browser loading a page (top-level or a frame), never a script's fetch or an asset."""
    get = (lambda k: str(headers.get(k) or "").lower())
    mode = get("sec-fetch-mode")
    if mode:
        return mode == "navigate"
    return "text/html" in get("accept")


__all__ = ["recreate_href", "REFRESH_SECONDS", "WAKE_ACTOR", "WAKE_WINDOW_DAYS", "gone_page", "is_page_load",
           "sibling_location", "wake", "waking_page"]
