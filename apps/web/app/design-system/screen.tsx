"use client";

/**
 * Workspace enamel (BOSSAPP-1254). One spec — name, colors, type, spacing —
 * painted on a street blade. A new app in this workspace reads that spec.
 */
import React, { useEffect, useState } from "react";
import { apiWrite } from "../../lib/api-write";
import { GatedButton } from "../../components/gated";

const CSS = `
@import url("https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700&family=IBM+Plex+Mono:wght@400;500&family=Public+Sans:wght@400;600&display=swap");

.ds-screen {
  --night: #102833;
  --aluminum: #C9D0D4;
  --paper: #F4F7F8;
  --soot: #1C1915;
  --caution: #E3A008;
  background: var(--night);
  color: var(--paper);
  font-family: "Public Sans", "Avenir Next", sans-serif;
  min-height: 100%;
  padding: 36px 32px 72px;
}
.ds-screen h1, .ds-blade-name, .ds-screen button {
  font-family: "Barlow Condensed", "Arial Narrow", sans-serif;
}
.ds-eyebrow {
  margin: 0 0 8px;
  font-size: 13px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: #D5DDE2;
}
.ds-screen h1 {
  margin: 0 0 12px;
  font-size: 68px;
  font-weight: 700;
  line-height: 0.88;
  letter-spacing: 0.01em;
  text-transform: uppercase;
}
.ds-intro, .ds-unread, .ds-app-read, .ds-note {
  max-width: 38rem;
  margin: 0;
  font-size: 16px;
  line-height: 1.45;
}
.ds-blade {
  position: relative;
  margin: 28px 0 8px;
  padding: 26px 48px 18px 78px;
  background: #C9D0D4;
  color: #1C1915;
  border-radius: 10px 4px 4px 10px;
  min-height: 128px;
}
.ds-blade::before, .ds-blade::after {
  content: "";
  position: absolute;
  right: 18px;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #102833;
}
.ds-blade::before { top: 16px; }
.ds-blade::after { bottom: 16px; }
.ds-cut {
  position: absolute;
  left: 0;
  top: 0;
  bottom: 0;
  width: 34px;
  transform-origin: top center;
  animation: ds-cut-in 0.45s ease-out;
  clip-path: polygon(0 0, 100% 0, 62% 100%, 0 100%);
}
.ds-blade-name {
  margin: 0;
  font-size: 64px;
  font-weight: 700;
  line-height: 0.9;
  letter-spacing: 0.02em;
  text-transform: uppercase;
}
.ds-reed {
  position: relative;
  height: 36px;
  margin-top: 18px;
  border-top: 2px solid #1C1915;
}
.ds-tick {
  position: absolute;
  top: 0;
  width: 2px;
  height: 14px;
  background: #1C1915;
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 11px;
}
.ds-tick span {
  position: absolute;
  top: 16px;
  left: -8px;
}
.ds-band {
  display: grid;
  grid-template-columns: 120px 1fr;
  gap: 12px 28px;
  align-items: end;
  padding: 20px 0;
  border-top: 1px solid rgba(244, 247, 248, 0.22);
}
.ds-band h2 {
  margin: 0;
  font-family: "Barlow Condensed", "Arial Narrow", sans-serif;
  font-size: 28px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.ds-wells, .ds-faces, .ds-steps {
  display: flex;
  flex-wrap: wrap;
  gap: 22px;
}
.ds-well, .ds-face, .ds-step {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 140px;
}
.ds-well span, .ds-face span, .ds-step span, .ds-name span, .ds-app span {
  font-size: 12px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: #D5DDE2;
}
.ds-swatch {
  display: block;
  height: 28px;
  border-radius: 2px;
  border: 1px solid rgba(244, 247, 248, 0.35);
}
.ds-screen input {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-size: 14px;
  color: #F4F7F8;
  background: transparent;
  border: none;
  border-bottom: 1px solid rgba(244, 247, 248, 0.5);
  padding: 6px 0;
  width: 100%;
}
.ds-screen button {
  font-size: 22px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  background: #E3A008;
  color: #1C1915;
  border: none;
  border-radius: 2px;
  padding: 8px 16px;
  cursor: pointer;
}
.ds-create {
  background: transparent;
  color: #F4F7F8;
  box-shadow: inset 0 -2px 0 #E3A008;
}
.ds-actions, .ds-new {
  display: flex;
  flex-wrap: wrap;
  gap: 18px 28px;
  align-items: flex-end;
  margin-top: 8px;
}
.ds-name, .ds-app { min-width: 220px; flex: 1; }
.ds-alert { color: #F4D6CF; }
.ds-dot {
  display: inline-block;
  width: 14px;
  height: 14px;
  margin-left: 8px;
  border-radius: 2px;
  vertical-align: -1px;
}
.ds-screen input:focus-visible, .ds-screen button:focus-visible {
  outline: 2px solid #E3A008;
  outline-offset: 3px;
}
@keyframes ds-cut-in {
  from { transform: scaleY(0); }
  to { transform: scaleY(1); }
}
@media (prefers-reduced-motion: reduce) {
  .ds-cut { animation: none; }
}
@media (max-width: 720px) {
  .ds-screen { padding: 24px 16px 48px; }
  .ds-screen h1, .ds-blade-name { font-size: 42px; }
  .ds-band { grid-template-columns: 1fr; }
  .ds-blade { padding: 22px 36px 16px 56px; }
}
`;

type Colors = { ground: string; metal: string; signal: string; ink: string };
type Faces = { display: string; body: string; utility: string };
type Steps = { tight: string; stack: string; band: string };

const EMPTY_COLORS: Colors = { ground: "", metal: "", signal: "", ink: "" };
const EMPTY_FACES: Faces = { display: "", body: "", utility: "" };
const EMPTY_STEPS: Steps = { tight: "", stack: "", band: "" };

type SystemBody = {
  name?: string;
  colors?: Partial<Colors>;
  type?: Partial<Faces>;
  spacing?: Partial<Record<keyof Steps, number | string>>;
};

type AppRead = { app: string; system: string; signal: string };

function messageOf(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "message" in body) {
    const message = (body as { message?: unknown }).message;
    if (typeof message === "string" && message.trim()) return message;
  }
  return fallback;
}

function textOf(value: unknown): string {
  return value === null || value === undefined ? "" : String(value);
}

export function DesignSystemScreen({ workspaceId = "" }: { workspaceId?: string }) {
  const [picked, setPicked] = useState("");
  const [draft, setDraft] = useState("");
  const active = workspaceId || picked;
  const [name, setName] = useState("");
  const [colors, setColors] = useState<Colors>(EMPTY_COLORS);
  const [faces, setFaces] = useState<Faces>(EMPTY_FACES);
  const [steps, setSteps] = useState<Steps>(EMPTY_STEPS);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [appName, setAppName] = useState("");
  const [appRead, setAppRead] = useState<AppRead | null>(null);

  function apply(body: SystemBody) {
    setName(textOf(body.name));
    setColors({
      ground: textOf(body.colors?.ground),
      metal: textOf(body.colors?.metal),
      signal: textOf(body.colors?.signal),
      ink: textOf(body.colors?.ink),
    });
    setFaces({
      display: textOf(body.type?.display),
      body: textOf(body.type?.body),
      utility: textOf(body.type?.utility),
    });
    setSteps({
      tight: textOf(body.spacing?.tight),
      stack: textOf(body.spacing?.stack),
      band: textOf(body.spacing?.band),
    });
  }

  useEffect(() => {
    if (!active) return;
    let gone = false;
    const url = `/v1/workspaces/${encodeURIComponent(active)}/design-system`;
    (async () => {
      try {
        const res = await fetch(url);
        if (res.status === 404) return;
        const body = await res.json().catch(() => null);
        if (gone) return;
        if (!res.ok) {
          setError(messageOf(body, "Could not load the design system."));
          return;
        }
        apply(body as SystemBody);
      } catch {
        if (!gone) setError("Could not load the design system.");
      }
    })();
    return () => { gone = true; };
  }, [active]);

  async function save() {
    setError(null);
    setSaved(null);
    const spacing = {
      tight: Number(steps.tight),
      stack: Number(steps.stack),
      band: Number(steps.band),
    };
    const missing = !name.trim()
      || Object.values(colors).some((color) => !color.trim())
      || !faces.display.trim()
      || !faces.body.trim();
    if (missing) {
      setError("Fill the name, colors, and type.");
      return;
    }
    const badStep = Object.values(spacing).some(
      (step) => !Number.isInteger(step) || step < 1 || step > 160);
    if (badStep) {
      setError("Spacing needs a whole step.");
      return;
    }
    const payload = {
      name: name.trim(),
      colors: {
        ground: colors.ground.trim(),
        metal: colors.metal.trim(),
        signal: colors.signal.trim(),
        ink: colors.ink.trim(),
      },
      type: {
        display: faces.display.trim(),
        body: faces.body.trim(),
        utility: faces.utility.trim(),
      },
      spacing,
    };
    try {
      const res = await apiWrite(
        `/v1/workspaces/${encodeURIComponent(active)}/design-system`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        },
      );
      const body = await res.json().catch(() => null);
      if (!res.ok) {
        setError(messageOf(body, "Could not save the design system."));
        return;
      }
      apply(body as SystemBody);
      setSaved("Design system saved.");
    } catch {
      setError("Could not save the design system.");
    }
  }

  async function createApp() {
    setError(null);
    const next = appName.trim();
    if (!next) {
      setError("Name the app.");
      return;
    }
    try {
      const res = await apiWrite(
        `/v1/workspaces/${encodeURIComponent(active)}/apps`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: next }),
        },
      );
      const body = await res.json().catch(() => null);
      if (!res.ok || !body || typeof body !== "object") {
        setError(messageOf(body, "Could not create the app."));
        return;
      }
      const record = body as { name?: string; design_system?: SystemBody };
      const system = record.design_system ?? {};
      setAppRead({
        app: textOf(record.name),
        system: textOf(system.name),
        signal: textOf(system.colors?.signal),
      });
    } catch {
      setError("Could not create the app.");
    }
  }

  const signal = colors.signal.trim() || "#E3A008";
  const ticks = (["tight", "stack", "band"] as const)
    .map((key) => ({ key, n: Number(steps[key]) }))
    .filter((step) => Number.isFinite(step.n) && step.n > 0);
  const span = Math.max(40, ...ticks.map((step) => step.n));

  if (!active) {
    return (
      <section className="ds-screen" aria-label="Design system">
        <style>{CSS}</style>
        <p className="ds-eyebrow">Workspace enamel</p>
        <h1>The spec new apps read</h1>
        <p className="ds-intro" data-testid="ds-intro">
          Name the workspace. Then set the spec that new apps read.
        </p>
        <div className="ds-actions">
          <label className="ds-name">
            <span>Workspace</span>
            <input
              data-testid="ds-workspace"
              value={draft}
              autoComplete="off"
              onChange={(event) => setDraft(event.target.value)}
            />
          </label>
          <button type="button" data-testid="ds-open" onClick={() => setPicked(draft.trim())}>
            Open
          </button>
        </div>
      </section>
    );
  }

  return (
    <section className="ds-screen" aria-label="Design system">
      <style>{CSS}</style>
      <p className="ds-eyebrow">Workspace enamel</p>
      <h1>The spec new apps read</h1>
      <p className="ds-intro" data-testid="ds-intro">
        Save a name, colors, type, and spacing. A new app in this workspace reads that spec.
      </p>

      <div className="ds-blade" data-testid="ds-blade" title="The name a new app reads.">
        <span className="ds-cut" style={{ background: signal }} aria-hidden="true" />
        <p className="ds-blade-name" data-testid="ds-blade-name">
          {name.trim() || "Name this spec"}
        </p>
        <div className="ds-reed" data-testid="ds-reed" title="Ticks are the spacing steps.">
          {ticks.map((step) => (
            <i key={step.key} className="ds-tick" style={{ left: `${(step.n / span) * 88}%` }}>
              <span>{step.n}</span>
            </i>
          ))}
        </div>
      </div>

      <div className="ds-band">
        <h2>Name</h2>
        <label className="ds-name">
          <span>Name</span>
          <input
            data-testid="ds-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            autoComplete="off"
          />
        </label>
      </div>

      <div className="ds-band">
        <h2>Colors</h2>
        <div className="ds-wells">
          {(["ground", "metal", "signal", "ink"] as const).map((key) => (
            <label className="ds-well" key={key}>
              <span>{key}</span>
              <i className="ds-swatch" style={{ background: colors[key] || "transparent" }} />
              <input
                data-testid={`ds-color-${key}`}
                value={colors[key]}
                spellCheck={false}
                autoComplete="off"
                onChange={(event) => {
                  const value = event.target.value;
                  setColors((prev) => ({ ...prev, [key]: value }));
                }}
              />
            </label>
          ))}
        </div>
      </div>

      <div className="ds-band">
        <h2>Type</h2>
        <div className="ds-faces">
          {(["display", "body", "utility"] as const).map((key) => (
            <label className="ds-face" key={key}>
              <span>{key}</span>
              <input
                data-testid={`ds-type-${key}`}
                value={faces[key]}
                autoComplete="off"
                onChange={(event) => {
                  const value = event.target.value;
                  setFaces((prev) => ({ ...prev, [key]: value }));
                }}
              />
            </label>
          ))}
        </div>
      </div>

      <div className="ds-band">
        <h2>Spacing</h2>
        <div className="ds-steps">
          {(["tight", "stack", "band"] as const).map((key) => (
            <label className="ds-step" key={key}>
              <span>{key}</span>
              <input
                data-testid={`ds-space-${key}`}
                value={steps[key]}
                inputMode="numeric"
                autoComplete="off"
                onChange={(event) => {
                  const value = event.target.value;
                  setSteps((prev) => ({ ...prev, [key]: value }));
                }}
              />
            </label>
          ))}
        </div>
      </div>

      {error ? <p className="ds-alert" role="alert">{error}</p> : null}
      {saved ? <p className="ds-note" data-testid="ds-saved">{saved}</p> : null}

      <div className="ds-actions">
        <GatedButton permission="org:update" type="button" data-testid="ds-save" onClick={() => { void save(); }}>
          Save design system
        </GatedButton>
      </div>

      <div className="ds-new">
        <label className="ds-app">
          <span>App name</span>
          <input
            data-testid="ds-app-name"
            value={appName}
            autoComplete="off"
            onChange={(event) => setAppName(event.target.value)}
          />
        </label>
        <GatedButton permission="project:create" type="button" className="ds-create" data-testid="ds-create" onClick={() => { void createApp(); }}>
          Create app
        </GatedButton>
        {appRead ? (
          <p className="ds-app-read" data-testid="ds-app-read">
            {`${appRead.app} reads ${appRead.system}.`}
            <i
              className="ds-dot"
              data-testid="ds-read-signal"
              data-color={appRead.signal}
              style={{ background: appRead.signal }}
            />
          </p>
        ) : (
          <p className="ds-unread" data-testid="ds-unread">No app has read this yet.</p>
        )}
      </div>
    </section>
  );
}
