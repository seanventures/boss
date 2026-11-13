"use client";

/**
 * An extension's console panel (boss.extension/v1 ui.slot.register, BOSSAPP-861), in a SANDBOXED
 * IFRAME it cannot climb out of.
 *
 *   <iframe sandbox="allow-scripts" srcdoc={the slot's document}>
 *
 * No `allow-same-origin`: the frame is an opaque origin, so it cannot touch this page's DOM,
 * cookies or storage, and any request it made would carry no session (its CSP also says
 * connect-src 'none'). The document comes from BOSS (GET /v1/ext/{id}/ui/{slot}), which read it
 * from the installed bundle after the trust gate passed and put its bridge + CSP first in <head>.
 *
 * THE postMessage API is exactly two messages, and only from THAT frame's own window:
 *   resize      {bossExt: 1, type: "resize", height}     → the frame's height (40-2000 px)
 *   getContext  {bossExt: 1, type: "getContext", id}      → {type: "context", id, context}
 * Anything else is ignored. The context is the API's answer for the slot — the extension's own
 * `ui:` state in this project, never anything of BOSS's.
 */
import React, { useEffect, useRef, useState } from "react";

import { frameContextUrl, frameUrl, useExtensionSlots, type ExtensionSlot, type SlotType } from "../lib/extensions-view";

export const FRAME_MIN = 40;
export const FRAME_MAX = 2000;
export const clampHeight = (h: unknown): number =>
  Math.max(FRAME_MIN, Math.min(FRAME_MAX, Math.round(Number(h) || 0)));

export function ExtensionFrame({ slot, projectId }: { slot: ExtensionSlot; projectId: string }) {
  const ref = useRef<HTMLIFrameElement | null>(null);
  const [doc, setDoc] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [height, setHeight] = useState(120);

  useEffect(() => {
    let live = true;
    setDoc(null);
    setError(null);
    fetch(frameUrl(slot.extensionId, slot.name, projectId), { cache: "no-store" })
      .then(async (r) => {
        if (!live) return;
        if (!r.ok) {
          const body = await r.json().catch(() => ({})) as { detail?: string; error?: string };
          setError(body.detail || body.error || `The panel did not load (${r.status}).`);
          return;
        }
        setDoc(await r.text());
      })
      .catch(() => { if (live) setError("The panel did not load."); });
    return () => { live = false; };
  }, [slot.extensionId, slot.name, projectId]);

  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      const frame = ref.current?.contentWindow;
      if (!frame || e.source !== frame) return;           // only this frame, never another window
      const d = (e.data ?? {}) as { bossExt?: unknown; type?: unknown; height?: unknown; id?: unknown };
      if (d.bossExt !== 1) return;
      if (d.type === "resize") {
        setHeight(clampHeight(d.height));
        return;
      }
      if (d.type === "getContext") {
        const id = typeof d.id === "number" ? d.id : 0;
        fetch(frameContextUrl(slot.extensionId, slot.name, projectId), { cache: "no-store" })
          .then((r) => (r.ok ? r.json() : { context: null }))
          .catch(() => ({ context: null }))
          .then((body: { context?: unknown }) => {
            // An opaque-origin frame has no origin to name: "*" goes to that one window only.
            frame.postMessage({ bossExt: 1, type: "context", id, context: body?.context ?? null }, "*");
          });
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [slot.extensionId, slot.name, projectId]);

  if (error) return <div className="inf-notice" data-testid="ext-frame-error">{error}</div>;
  if (doc === null) return <div className="inf-faint" data-testid="ext-frame-loading">Loading…</div>;
  return (
    <iframe ref={ref} data-testid={`ext-frame-${slot.extensionId}-${slot.name}`} title={slot.title}
            sandbox="allow-scripts" referrerPolicy="no-referrer" srcDoc={doc}
            style={{ width: "100%", height, border: 0, display: "block" }} />
  );
}

/** Every enabled, trusted extension's panels of one type, each in its own frame. */
export function ExtensionSlots({ type, projectId, only, cardClass, titleClass }: {
  type: SlotType; projectId: string; only?: string; cardClass?: string; titleClass?: string;
}) {
  const slots = useExtensionSlots(type, projectId);
  const rows = (slots.data ?? []).filter((s) => !only || s.extensionId === only);
  if (!rows.length) return null;
  return (
    <div className="ext-slots" data-testid={`ext-slots-${type}`}>
      {rows.map((s) => (
        <section key={`${s.extensionId}:${s.name}`} className={cardClass ?? "ext-slot"}
                 data-testid={`ext-slot-${s.extensionId}-${s.name}`} aria-label={s.title}>
          <h3 className={titleClass ?? "ext-slot-title"} title={`From the ${s.extensionId} extension`}>{s.title}</h3>
          <ExtensionFrame slot={s} projectId={projectId} />
        </section>
      ))}
    </div>
  );
}
