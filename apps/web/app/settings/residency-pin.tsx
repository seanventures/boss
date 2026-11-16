"use client";

/**
 * Where one built app is allowed to run (BOSSAPP-1255).
 *
 * A customs seal, not a dashboard: the geography is a stamp, and the
 * control-plane host is a locked line that this screen never rewrites.
 * Geography labels are compared locally. No cloud region is contacted.
 */
import React, { useRef, useState } from "react";

export const GEOGRAPHIES = [
  { id: "eu", label: "European Union" },
  { id: "uk", label: "United Kingdom" },
  { id: "us", label: "United States" },
  { id: "apac", label: "Asia Pacific" },
] as const;

export type GeographyId = (typeof GEOGRAPHIES)[number]["id"];

const CLOUD_REGION = /^[a-z]{2}(?:-[a-z0-9]+){1,3}-\d[a-z]?$/;

export type Admit = {
  allowed: boolean;
  path: "today" | "pinned" | "refused";
  reason: string | null;
  appPath: string;
  controlPlaneHost: string;
  cloudContacted: false;
  targetGeography: string;
};

export function admitDeploy(input: {
  geography: string | null;
  targetGeography: string;
  appPath: string;
  controlPlaneHost: string;
}): Admit {
  const base = {
    appPath: input.appPath,
    controlPlaneHost: input.controlPlaneHost,
    cloudContacted: false as const,
    targetGeography: input.targetGeography,
  };
  if (!input.geography) {
    return { ...base, allowed: true, path: "today", reason: null };
  }
  const target = input.targetGeography.trim().toLowerCase();
  if (CLOUD_REGION.test(target)) {
    return {
      ...base,
      allowed: false,
      path: "refused",
      reason: "That label is a cloud region. It was not contacted.",
    };
  }
  if (target !== input.geography) {
    return { ...base, allowed: false, path: "refused", reason: "Target geography is not the pin." };
  }
  return { ...base, allowed: true, path: "pinned", reason: null };
}

export type ResidencyPinProps = {
  projectId?: string;
  actor?: string;
  appKey?: string;
  appName?: string;
  geography?: string | null;
  controlPlaneHost: string;
  appPath?: string;
  onPin?: (geography: GeographyId) => void;
  onClear?: () => void;
};

const CSS = `
.rp {
  --ink: #1B2A4A;
  --paper: #E7EEF2;
  --sheet: #F4F8F9;
  --teal: #1F6F6A;
  --brass: #C4A35A;
  --refusal: #C23B22;
  --fog: #5C6B78;
  color: var(--ink);
  background: var(--paper);
  font-family: "Public Sans", "Avenir Next", "Segoe UI", sans-serif;
  padding: 28px 22px 36px;
}
.rp-sheet {
  max-width: 880px;
  margin: 0 auto;
  background: var(--sheet);
  border-left: 8px solid var(--brass);
  padding: 28px 28px 32px;
}
.rp-kicker {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  font-size: 12px;
  color: var(--fog);
  margin: 0 0 8px;
}
.rp h1 {
  font-family: Besley, "Iowan Old Style", Palatino, serif;
  font-weight: 650;
  font-size: 40px;
  line-height: 1.05;
  margin: 0 0 12px;
}
.rp-thesis {
  max-width: 38rem;
  font-size: 17px;
  line-height: 1.45;
  margin: 0 0 22px;
}
.rp-manifest {
  display: flex;
  gap: 28px;
  align-items: center;
}
.rp-seal {
  width: 148px;
  height: 148px;
  flex: 0 0 148px;
  border-radius: 50%;
  border: 3px dashed var(--teal);
  display: grid;
  place-items: center;
  text-align: center;
  font-family: Besley, "Iowan Old Style", Palatino, serif;
  font-size: 22px;
  letter-spacing: 0.08em;
  color: var(--teal);
  transform: rotate(-8deg);
  transition: transform 180ms ease, background 180ms ease, color 180ms ease;
}
.rp-seal[data-pinned="true"] {
  background: var(--teal);
  color: var(--sheet);
  border-style: solid;
  transform: rotate(6deg);
}
.rp-lines { flex: 1; min-width: 0; }
.rp-line {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  border-top: 1px solid #C5D0D6;
  padding: 10px 0;
}
.rp-line b {
  font-weight: 600;
}
.rp-host {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 14px;
}
.rp-lock {
  color: var(--brass);
  margin-right: 6px;
}
.rp-geos {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin: 18px 0 8px;
}
.rp-geo, .rp-act {
  font: inherit;
  color: var(--ink);
  background: transparent;
  border: 1px solid var(--ink);
  padding: 8px 12px;
  cursor: pointer;
}
.rp-geo[data-id="eu"] {
  font-family: Besley, Palatino, serif;
  font-size: 18px;
  padding: 10px 16px;
  border-width: 2px;
}
.rp-geo[aria-pressed="true"] {
  background: var(--ink);
  color: var(--sheet);
}
.rp-acts { display: flex; gap: 8px; margin-top: 8px; }
.rp-act[data-kind="pin"] { background: var(--brass); border-color: var(--brass); }
.rp-probe {
  margin-top: 22px;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: end;
}
.rp-probe label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; }
.rp-probe input {
  font: inherit;
  border: 1px solid var(--ink);
  background: var(--sheet);
  padding: 8px 10px;
  min-width: 180px;
  color: var(--ink);
}
.rp-verdict { margin: 12px 0 0; }
.rp-verdict[data-allowed="false"] { color: var(--refusal); }
.rp-cloud { color: var(--fog); font-size: 13px; }
button:focus-visible, input:focus-visible {
  outline: 2px solid #1B2A4A;
  outline-offset: 2px;
}
@media (max-width: 640px) {
  .rp-manifest { flex-direction: column; align-items: flex-start; }
  .rp h1 { font-size: 32px; }
}
@media (prefers-reduced-motion: reduce) {
  .rp-seal { transition: none; }
}
`;

function words(value: string): number {
  return value.trim().split(/\s+/).filter(Boolean).length;
}

export function ResidencyPin({
  projectId,
  actor,
  appKey = "app",
  appName = "This app",
  geography = null,
  controlPlaneHost,
  appPath = "",
  onPin,
  onClear,
}: ResidencyPinProps) {
  const controlled = onPin !== undefined;
  const [owned, setOwned] = useState<string | null>(geography);
  const [choice, setChoice] = useState<GeographyId>(
    (geography as GeographyId | null) ?? "eu",
  );
  const [verdict, setVerdict] = useState<Admit | null>(null);
  const [error, setError] = useState<string | null>(null);
  const targetRef = useRef<HTMLInputElement>(null);
  const shown = controlled ? geography : owned;
  const host = controlPlaneHost;

  async function persist(next: GeographyId | null) {
    if (!projectId || !actor) {
      setOwned(next);
      return;
    }
    const path = `/v1/projects/${encodeURIComponent(projectId)}/residency-pins/${encodeURIComponent(appKey)}`;
    const response = await fetch(path, {
      method: next ? "PUT" : "DELETE",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(
        next
          ? { actor, geography: next, control_plane_host: host }
          : { actor },
      ),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => null);
      const message = typeof body?.detail === "string" ? body.detail : "The pin was refused.";
      setError(message);
      return;
    }
    setError(null);
    setOwned(next);
  }

  function pinHere() {
    if (!GEOGRAPHIES.some((item) => item.id === choice)) return;
    if (controlled) {
      onPin?.(choice);
      return;
    }
    void persist(choice);
  }

  function clear() {
    if (controlled) {
      onClear?.();
      return;
    }
    void persist(null);
  }

  function check() {
    setVerdict(admitDeploy({
      geography: shown ?? null,
      targetGeography: targetRef.current?.value ?? "",
      appPath,
      controlPlaneHost: host,
    }));
  }

  const reason = verdict?.reason ?? (verdict ? "Deploy allowed" : "Not checked");

  return (
    <section className="rp" data-testid="residency-pin">
      <style>{CSS}</style>
      <div className="rp-sheet">
        <p className="rp-kicker" data-copy>{appName}</p>
        <h1 data-copy>Where it runs</h1>
        <p className="rp-thesis" data-testid="residency-thesis" data-copy>
          The pin is the only place a deploy may land. The control plane stays where it is.
        </p>
        <div className="rp-manifest">
          <div
            className="rp-seal"
            data-testid="residency-seal"
            data-pinned={shown ? "true" : "false"}
            data-copy
          >
            {shown ? shown.toUpperCase() : "UNPINNED"}
          </div>
          <div className="rp-lines">
            <div className="rp-line">
              <b data-copy>Control plane</b>
              <span className="rp-host" title="Host string does not move.">
                <span className="rp-lock" aria-hidden="true">⌘</span>
                <span data-testid="residency-control-host" data-copy>{host}</span>
              </span>
            </div>
            <div className="rp-line">
              <b data-copy>Today&apos;s path</b>
              <span className="rp-host" data-testid="residency-path" data-copy>{appPath}</span>
            </div>
            <div className="rp-line">
              <span className="rp-cloud" data-testid="residency-cloud" data-cloud-contacted="false" data-copy>
                No cloud call.
              </span>
            </div>
          </div>
        </div>
        <div className="rp-geos" role="group" aria-label="Geography">
          {GEOGRAPHIES.map((item) => (
            <button
              key={item.id}
              type="button"
              className="rp-geo"
              data-id={item.id}
              data-testid={`residency-geography-${item.id}`}
              data-copy
              aria-pressed={choice === item.id}
              onClick={() => setChoice(item.id)}
            >
              {item.id === "eu" ? "EU" : item.label}
            </button>
          ))}
        </div>
        <div className="rp-acts">
          <button type="button" className="rp-act" data-kind="pin" data-testid="residency-pin-button" data-copy onClick={pinHere}>
            Pin here
          </button>
          <button type="button" className="rp-act" data-testid="residency-clear" data-copy onClick={clear}>
            Clear pin
          </button>
        </div>
        <div className="rp-probe">
          <label>
            <span data-copy>Target</span>
            <input id="rp-target" data-testid="residency-target" ref={targetRef} aria-label="Target" />
          </label>
          <button type="button" className="rp-act" data-testid="residency-check" data-copy onClick={check}>
            Check deploy
          </button>
        </div>
        <div
          className="rp-verdict"
          data-testid="residency-verdict"
          data-path={verdict?.path ?? ""}
          data-allowed={verdict ? String(verdict.allowed) : ""}
        >
          {words(reason) > 6 ? <p data-copy>{reason}</p> : <span data-copy>{reason}</span>}
        </div>
        {error ? (words(error) > 6 ? <p data-copy>{error}</p> : <span data-copy>{error}</span>) : null}
      </div>
    </section>
  );
}
