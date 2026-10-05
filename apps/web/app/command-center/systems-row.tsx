"use client";

/**
 * SYSTEMS (command-center spec §4.11): seven dots, one word each, the Apple status-page pattern.
 *
 * Every tone comes from an existing derivation on the server; the tooltip is the server's
 * one-line reason, and the dot links to the area that fixes it (one surface per job). Grey
 * hollow means "not set up" — never red. A red dot's word turns red and bold, so state is never
 * colour alone; a screen reader hears `Host: struggling. Memory 91% used.`
 *
 * MOTION ONLY ON A REAL CHANGE: a dot whose tone changed between polls pulses once.
 */
import React, { useEffect, useRef, useState } from "react";

import type { MotionMode, SystemDot } from "../../lib/command-center-view";

/** `?area=setup&focus=models` as a console link that keeps the org. */
export function consoleHref(query: string, orgId?: string | null): string {
  const out = new URLSearchParams();
  if (orgId) out.set("org", orgId);
  new URLSearchParams(query.replace(/^[/?]+/, "")).forEach((v, k) => out.set(k, v));
  return `/?${out.toString()}`;
}

export function SystemsRow({ systems, reachable, motion, orgId }: {
  systems: SystemDot[];
  /** BOSS could not be read: every dot is hollow grey `Unknown` (the last tones are not news). */
  reachable: boolean;
  motion: MotionMode;
  orgId?: string | null;
}) {
  const before = useRef<Map<string, string> | null>(null);
  const [pulse, setPulse] = useState<Set<string>>(new Set());
  useEffect(() => {
    const now = new Map(systems.map((s) => [s.key, s.tone]));
    const prev = before.current;
    before.current = now;
    if (!prev || motion !== "full") return undefined;
    const changed = new Set(systems.filter((s) => prev.has(s.key) && prev.get(s.key) !== s.tone).map((s) => s.key));
    if (!changed.size) return undefined;
    setPulse(changed);
    const t = window.setTimeout(() => setPulse(new Set()), 450);
    return () => window.clearTimeout(t);
  }, [systems, motion]);

  return (
    <ul className="cc-systems" data-testid="cc-systems">
      {systems.map((s) => {
        const tone = reachable ? s.tone : "grey";
        const state = reachable ? s.state : "Unknown";
        const tip = reachable ? s.tip : "Can't reach BOSS";
        return (
          // The Host dot reads view.host (run_retrospective.host_advice) today. SEAM: the
          // stability stream adds a system-health attention kind and a /v1 endpoint; when it
          // lands, the server's `systems.host` reads it and this row needs no change.
          <li key={s.key} className="cc-sys" data-tone={tone} data-pulse={pulse.has(s.key) ? "true" : undefined}>
            <a href={consoleHref(s.href, orgId)} title={tip} data-testid={`cc-dot-${s.key}`}
               aria-label={`${s.word}: ${state}. ${tip}`}>
              <span className="cc-sys-dot" aria-hidden />
              <span className="cc-sys-word">{s.word}</span>
              <span className="cc-sys-state">{state}</span>
            </a>
          </li>
        );
      })}
    </ul>
  );
}
