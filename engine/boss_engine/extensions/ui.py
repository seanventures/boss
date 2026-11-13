"""ui.slot.register: an extension's console panel, in a SANDBOXED IFRAME it cannot climb out of.

Two slot types: `project_panel` (a project's page) and `settings_panel` (the Extensions page).

    GET /v1/ext/{id}/ui/{slot}?project_id=…          the slot's document (text/html)
    GET /v1/ext/{id}/ui/{slot}/context?project_id=…  what the frame's getContext() answers

THE DOCUMENT is the bundle file the manifest declares (`spec.ui.slots[].entry`), read from the
INSTALLED copy only after the trust gate passes, with BOSS's bridge script and a CSP meta tag
inserted first in <head>. The console puts it in `<iframe sandbox="allow-scripts" srcdoc=…>`:
no `allow-same-origin`, so the frame is an OPAQUE origin — it cannot read the parent's DOM,
cookies or storage, and cannot call the API with the operator's session. The HTTP answer carries
the same lock as a header, with `sandbox allow-scripts` (so even opened directly it is an opaque
origin) and `frame-ancestors 'self'`. connect-src is 'none': the frame fetches nothing; what it
shows comes from getContext().

THE postMessage API is two messages, nothing else (the console ignores anything else, and anything
not from that frame's own window):

    {bossExt: 1, type: "resize", height}        the frame asks for a height (clamped 40-2000 px)
    {bossExt: 1, type: "getContext", id}         the console answers {type: "context", id, context}

`context` is {extensionId, version, slot, slotType, projectId, data}: `data` is the extension's
OWN state under keys starting `ui:` in that project (needs state.read; capped at 32 KiB) — never
anything of BOSS's or another extension's.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .registry import ExtensionRefused, InstalledExtension

#: The meta CSP inside the srcdoc document (sandbox and frame-ancestors are header-only).
META_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src data:; font-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'")
#: The header CSP on the HTTP answer.
HEADER_CSP = "sandbox allow-scripts; " + META_CSP + "; frame-ancestors 'self'"
MAX_DOCUMENT_BYTES = 512 * 1024
MAX_CONTEXT_DATA_BYTES = 32 * 1024

BRIDGE = """<script>(function(){var n=0,w={};function post(m){m.bossExt=1;parent.postMessage(m,"*");}
window.bossExt={resize:function(h){post({type:"resize",height:Math.round(+h||0)});},
getContext:function(){return new Promise(function(r){var id=++n;w[id]=r;post({type:"getContext",id:id});});}};
window.addEventListener("message",function(e){if(e.source!==parent)return;var d=e.data||{};
if(d.bossExt===1&&d.type==="context"&&w[d.id]){w[d.id](d.context);delete w[d.id];}});
window.addEventListener("load",function(){window.bossExt.resize(document.documentElement.scrollHeight);});
})();</script>"""


def headers() -> dict[str, str]:
    return {"Content-Security-Policy": HEADER_CSP, "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "Cross-Origin-Resource-Policy": "same-origin"}


def _slot(ext: InstalledExtension, slot: str):
    manifest = ext.manifest                                   # the trust gate
    found = manifest.slot(slot) if manifest.has("ui.slot.register") else None
    if found is None:
        raise ExtensionRefused("slot_unknown", f"{ext.extension_id} declares no slot {slot}", 404)
    return manifest, found


def document(ext: InstalledExtension, slot: str) -> str:
    """The slot's HTML with the bridge and the CSP meta first in <head>."""
    _, spec = _slot(ext, slot)
    root = ext.directory.resolve()
    path = (root / spec.entry).resolve()
    if root not in path.parents or not path.is_file():
        raise ExtensionRefused("slot_entry_missing", f"{spec.entry} is not in the bundle", 404)
    raw = path.read_bytes()
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ExtensionRefused("slot_too_large", f"a panel document is at most {MAX_DOCUMENT_BYTES} bytes")
    html = raw.decode("utf-8", errors="replace")
    inject = f'<meta http-equiv="Content-Security-Policy" content="{META_CSP}">' + BRIDGE
    m = re.search(r"<head[^>]*>", html, re.I)
    if m:
        return html[:m.end()] + inject + html[m.end():]
    return "<!doctype html><html><head>" + inject + "</head><body>" + html + "</body></html>"


def context(engine: Any, ext: InstalledExtension, slot: str, project_id: Optional[str]) -> dict[str, Any]:
    import json

    from . import state as state_store

    manifest, spec = _slot(ext, slot)
    if spec.type.value == "project_panel" and not ext.enabled_in(project_id):
        raise ExtensionRefused("extension_disabled", "the extension is not enabled in this project", 409)
    data: dict[str, Any] = {}
    if manifest.has("state.read"):
        used = 0
        with engine.begin() as c:
            for key in state_store.list_keys(c, ext.extension_id, project_id, "ui:", limit=100):
                _, value = state_store.get(c, ext.extension_id, project_id, key)
                size = len(json.dumps(value, default=str))
                if used + size > MAX_CONTEXT_DATA_BYTES:
                    break
                used += size
                data[key[3:]] = value
    return {"extensionId": ext.extension_id, "version": ext.version, "slot": spec.name,
            "slotType": spec.type.value, "title": spec.title, "projectId": project_id, "data": data}


def slots(ext: InstalledExtension, slot_type: str) -> list[dict[str, Any]]:
    try:
        manifest = ext.manifest
    except ExtensionRefused:
        return []
    if not manifest.has("ui.slot.register") or manifest.spec.ui is None:
        return []
    return [{"extensionId": ext.extension_id, "name": s.name, "title": s.title, "type": s.type.value}
            for s in manifest.spec.ui.slots if s.type.value == slot_type]
