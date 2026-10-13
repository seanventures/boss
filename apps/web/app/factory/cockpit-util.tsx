"use client";

/**
 * Small pieces the cockpit's parts share: a station's face (from the server's `face`, never a
 * guess), clock and duration words, and the act popover a face press opens.
 */
import { Hourglass } from "@phosphor-icons/react";
import { createPortal } from "react-dom";
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";

import type { AvatarState } from "../../lib/agent-world-types";
import type { Station, StationStatus } from "../../lib/factory-floor";
import { ActCard } from "../agents/act-card";
import { fetchAct, type ActDetail } from "../agents/run-acts";
import { nameOf, RunFace } from "../agents/run-face";
import { consoleHref } from "../url-intent";
import type { AreaId } from "../../lib/area-ids";
import { useIdentityVersion } from "../../lib/use-agent-identities";

/**
 * A link to another console area that KEEPS WHO YOU ARE (`org=`). The cockpit built these from
 * scratch (`?area=activity&run=…`), which dropped the org and landed the operator on the
 * organisation picker — the fault url-intent.ts's consoleHref exists to prevent. The area is
 * typed: a research origin linked to `area=discoveries`, an area the console does not have.
 */
export function areaHref(params: { area: AreaId } & Record<string, string | null | undefined>): string {
  const search = typeof window === "undefined" ? "" : window.location.search;
  return consoleHref(search, params);
}

/** A station's status as the face's state. Waiting and pending never move (motion would lie). */
export function faceState(status: StationStatus): AvatarState {
  switch (status) {
    case "working": return "working";
    case "needs_you": return "needs_you";
    case "done": return "done";
    case "stuck": return "stuck";
    case "failed": return "failed";
    case "stopped": return "stopped";
    default: return "idle";
  }
}

export const BOSS_TIP = "BOSS machinery. No agent at this station.";

/** Who stands at a station, exactly as the server said: an agent, a hollow next face, BOSS, You, or nobody. */
export function StationFaceView({ station, size = 24, still }: { station: Station; size?: 16 | 20 | 24 | 32 | 48; still?: boolean }) {
  useIdentityVersion(); // names and faces re-resolve when the install's identities load
  const state = still && (station.status === "working" || station.status === "needs_you") ? "idle" : faceState(station.status);
  if (station.status === "waiting") {
    return (
      <span className="fx-face-wait" data-face="waiting" title={station.tip || "Waiting"}>
        <Hourglass size={Math.max(12, size - 8)} weight="bold" aria-hidden />
      </span>
    );
  }
  if (station.face === "agent" && station.roleKey) {
    return <span className="fx-face" data-face="agent" data-role={station.roleKey}><RunFace roleKey={station.roleKey} size={size} state={state} /></span>;
  }
  if (station.face === "hollow" && station.hollowRole) {
    const n = nameOf(station.hollowRole).name;
    return (
      <span className="fx-face" data-face="hollow" data-role={station.hollowRole} data-hollow
            title={station.tip || `Next: ${n} · ${station.label}`}>
        <RunFace roleKey={station.hollowRole} size={size} state="idle" />
      </span>
    );
  }
  if (station.face === "boss") {
    return <span className="fx-face" data-face="boss" title={BOSS_TIP}><RunFace roleKey={null} variant="boss" size={size} state={state} /></span>;
  }
  if (station.face === "you") {
    return <span className="fx-face" data-face="you" title="You"><RunFace roleKey={null} variant="you" size={size} state={state} /></span>;
  }
  return <span className="fx-face-dot" data-face="none" title={station.tip || `Next: ${station.label}`} aria-hidden />;
}

/** `09:12` — absolute, with the ISO time as its title (spec §7 Audit). */
export function clockOf(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/** `9m`, `41s`, `2h 4m` between two instants (or until `now`). Empty when unknown. */
export function spanOf(fromIso?: string, toIso?: string | null, now?: number | null): string {
  const a = fromIso ? Date.parse(fromIso) : NaN;
  const b = toIso ? Date.parse(toIso) : now ?? NaN;
  if (!Number.isFinite(a) || !Number.isFinite(b)) return "";
  return secondsWord(Math.max(0, Math.round((b - a) / 1000)));
}

export function secondsWord(s: number): string {
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

export function money(usd: number): string {
  return `$${usd.toFixed(usd >= 10 ? 0 : 2)}`;
}

/** Cost only when there is some to say (walk D16): a spend that rounds to `$0.00` says nothing. */
export function costWord(usd: number): string {
  return usd > 0 && money(usd) !== "$0.00" && money(usd) !== "$0" ? money(usd) : "";
}

export function kilo(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(n >= 10_000 ? 0 : 1)}k` : String(n);
}

/** Who said it: an agent's name, else BOSS. */
export function whoName(roleKey: string | null): string {
  return roleKey ? nameOf(roleKey).name : "BOSS";
}

/**
 * The Act card for one piece of work, opened from a face anywhere in the cockpit. Read once from
 * `/v1/acts/{id}` (the same record the Act card shows everywhere), fixed-positioned so a sticky
 * Line or a scroll box cannot clip it. Escape and a click elsewhere close it.
 */
export function ActPopover({ projectId, actId, viewer, anchor, onClose }: {
  projectId: string; actId: string; viewer: string; anchor: HTMLElement | null; onClose: () => void;
}) {
  const [act, setAct] = useState<ActDetail | null>(null);
  const [failed, setFailed] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const [at, setAt] = useState<{ left: number; top: number } | null>(null);
  useEffect(() => {
    let alive = true;
    void fetchAct(projectId, actId).then((d) => { if (alive) { setAct(d); setFailed(!d); } });
    return () => { alive = false; };
  }, [projectId, actId]);
  useLayoutEffect(() => {
    if (!box.current) return;
    const r = anchor?.getBoundingClientRect();
    const vw = window.innerWidth || 1024;
    const vh = window.innerHeight || 768;
    const w = box.current.offsetWidth || 320;
    const h = box.current.offsetHeight || 280;
    const left = r ? Math.max(16, Math.min(vw - w - 16, r.left + r.width / 2 - w / 2)) : 16;
    // Beside the face: below it, or above when below would run off the screen.
    const below = r ? r.bottom + 8 : 80;
    const top = r && below + h > vh - 8 && r.top - h - 8 > 8 ? r.top - h - 8 : below;
    setAt({ left: Math.round(left), top: Math.round(top) });
  }, [anchor, act]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { e.stopPropagation(); onClose(); } };
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (!box.current?.contains(t) && !anchor?.contains(t)) onClose();
    };
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("mousedown", onDown);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      document.removeEventListener("mousedown", onDown);
    };
  }, [anchor, onClose]);
  // Portalled: placed against the viewport, not a filtered or transformed ancestor (walk D19).
  return createPortal(
    <div ref={box} role="dialog" aria-label="Who did this" className="ag-act-pop fx-act-pop" data-testid="fx-act-pop"
         data-act-id={actId} style={at ? { left: at.left, top: at.top } : { visibility: "hidden" }}>
      {act ? <ActCard act={act} viewer={viewer} onClose={onClose} />
        : <span className="fx-muted">{failed ? "Not found" : "Loading"}</span>}
    </div>, document.body,
  );
}
