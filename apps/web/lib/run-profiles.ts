"use client";

/**
 * THE PROFILE A PIECE OF WORK RUNS ON (BOSSAPP-1334): lean · balanced · strict — how much checking
 * a build gets. Change it had the only choice; New work and Add a project now offer the same one,
 * opening on the project's own default (GET /v1/pipeline-profiles: its default, else the org's /
 * edition's), and send it as `profile` (the engine carries it in the run input to the build).
 */
import { useEffect, useState } from "react";

export type RunProfile = "lean" | "balanced" | "strict";

export const RUN_PROFILES: { key: RunProfile; label: string; tip: string }[] = [
  { key: "lean", label: "Lean", tip: "Fastest: one pass, tests first, minimal review" },
  { key: "balanced", label: "Balanced", tip: "Tests, an independent review and the usual gates" },
  { key: "strict", label: "Strict", tip: "Every check, the full review chain" },
];

/** A profile a person can pick, or null (a custom or internal one is never offered). */
export function asRunProfile(v: unknown): RunProfile | null {
  const s = String(v ?? "").trim().toLowerCase();
  return s === "lean" || s === "balanced" || s === "strict" ? s : null;
}

export const profileWords = (p: RunProfile | null | undefined) => RUN_PROFILES.find((x) => x.key === p)?.label ?? "Project default";

/** The project's default profile (null while it is read, or when it is not one a person picks). */
export function useDefaultProfile(projectId: string, enabled = true): RunProfile | null {
  const [p, setP] = useState<RunProfile | null>(null);
  useEffect(() => {
    if (!enabled || !projectId) return;
    let alive = true;
    void (async () => {
      try {
        const r = await fetch(`/v1/pipeline-profiles?project_id=${encodeURIComponent(projectId)}`, { cache: "no-store", credentials: "same-origin" });
        if (!r.ok) return;
        const body = (await r.json()) as { default?: unknown };
        if (alive) setP(asRunProfile(body.default));
      } catch { /* the choice still works; it opens on nothing picked */ }
    })();
    return () => { alive = false; };
  }, [projectId, enabled]);
  return p;
}
