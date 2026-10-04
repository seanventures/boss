"use client";

/**
 * PLACEHOLDER (stream S3). The real `StationLine` is stream S2's (app/factory/station-line.tsx on
 * fx/s2): at integration S2's file replaces this one. Same props, so the cockpit mounts either.
 * Kept to the minimum the cockpit needs to render and be tested: labelled bays, roving tabindex,
 * a bay press and a face press.
 */
import React, { useRef } from "react";

import type { Station, StationKey } from "../../lib/factory-floor";
import { StationFaceView, spanOf } from "./cockpit-util";

export type StationLineProps = {
  stations: Station[];
  density: "row" | "header";
  onBay?: (key: StationKey) => void;
  onFace?: (actId: string, anchor?: HTMLElement) => void;
  still?: boolean;
};

export function StationLine({ stations, density, onBay, onFace, still }: StationLineProps) {
  const list = useRef<HTMLDivElement>(null);
  const move = (e: React.KeyboardEvent, i: number) => {
    const bays = [...(list.current?.querySelectorAll<HTMLElement>("[data-bay]") ?? [])];
    let next = i;
    if (e.key === "ArrowRight") next = Math.min(bays.length - 1, i + 1);
    else if (e.key === "ArrowLeft") next = Math.max(0, i - 1);
    else if (e.key === "Enter" && e.shiftKey) {
      const s = stations[i];
      if (s.actId && onFace) { e.preventDefault(); onFace(s.actId, e.currentTarget as HTMLElement); }
      return;
    } else return;
    e.preventDefault();
    bays[next]?.focus();
  };
  return (
    <div ref={list} className="fx-line" data-density={density} role="list" aria-label="Stations" data-testid="fx-line">
      {stations.map((s, i) => (
        <div key={s.key} role="listitem" className="fx-bay" data-key={s.key} data-status={s.status}
             aria-label={[s.label, s.word].filter(Boolean).join(", ")}>
          <button type="button" className="fx-bay-btn" data-bay tabIndex={i === 0 ? 0 : -1}
                  data-testid={`fx-bay-${s.key}`} title={s.edgeLabel || undefined}
                  onKeyDown={(e) => move(e, i)} onClick={() => onBay?.(s.key)}>
            <span className="fx-bay-label">{s.label}</span>
          </button>
          {s.actId && onFace ? (
            <button type="button" className="fx-bay-face" data-testid={`fx-bay-face-${s.key}`}
                    aria-label={`${s.label}: who`} onClick={(e) => onFace(s.actId as string, e.currentTarget)}>
              <StationFaceView station={s} size={density === "header" ? 32 : 24} still={still} />
            </button>
          ) : <StationFaceView station={s} size={density === "header" ? 32 : 24} still={still} />}
          {s.seats.length > 1 ? <span className="fx-bay-seats" title="Review seats">×{s.seats.length}</span> : null}
          <span className="fx-bay-word">{[s.word, spanOf(s.startedAt, s.endedAt)].filter(Boolean).join(" · ")}</span>
          {s.links.map((l) => (
            <a key={l.label} className="fx-bay-link" href={l.url} target="_blank" rel="noreferrer">{l.label}</a>
          ))}
        </div>
      ))}
    </div>
  );
}
