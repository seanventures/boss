import type { CSSProperties } from "react";

import type { AgentOperationalState } from "../lib/roster";

/**
 * Handoff agent orb: acronym core with a state ring — running spins a conic
 * gradient, verified holds a green ring + check, blocked breathes coral + "!",
 * idle is a dashed hairline. Boundary-free so both the server-rendered
 * Command Center and client roster/board can use it.
 */
export function AgentOrb({
  label,
  state,
  size = 44,
  spinSeconds = 3,
  title,
}: {
  label: string;
  state: AgentOperationalState;
  size?: number;
  spinSeconds?: number;
  title?: string;
}) {
  return (
    <span
      className={`bp-orb ${state}`}
      style={{ "--orb-size": `${size}px`, "--orb-spin": `${spinSeconds}s` } as CSSProperties}
      role="img"
      aria-label={title ?? `${label} — ${state}`}
    >
      <span className="bp-orb-ring" aria-hidden />
      <span className="bp-orb-core" aria-hidden>{label}</span>
      {state === "verified" ? (
        <span className="bp-orb-badge ok" aria-hidden>
          <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="#06121f" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12l5 5 9-11" /></svg>
        </span>
      ) : null}
      {state === "blocked" ? (
        <span className="bp-orb-badge err" aria-hidden>!</span>
      ) : null}
    </span>
  );
}
