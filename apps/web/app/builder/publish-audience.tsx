"use client";

import React, { useState } from "react";

/**
 * A letterpress counter: the bench holds the wet, in-progress preview.
 * The window holds a sealed plate. Pending plates stay in the rack.
 * Only an approved plate is the visitor door, and that door is never the bench.
 */

export type AudienceName = "public" | "customers" | "partners" | "staff";

export type VisitorPlate = {
  id: string;
  audience: string;
  approval_state: "pending" | "approved";
  visitor_url: string | null;
  preview_url: string | null;
  snapshot: Record<string, unknown>;
  is_live_preview: boolean;
  kind: "visitor_snapshot";
};

export const AUDIENCES: { value: AudienceName; label: string }[] = [
  { value: "public", label: "Public" },
  { value: "customers", label: "Customers" },
  { value: "partners", label: "Partners" },
  { value: "staff", label: "Staff" },
];

export function visitorDoor(plate: VisitorPlate | null | undefined): string | null {
  if (!plate || plate.is_live_preview) return null;
  if (plate.approval_state !== "approved") return null;
  const url = plate.visitor_url;
  if (!url || url === plate.preview_url) return null;
  return url;
}

type PreviewBench = { id: string; url: string; title: string };

type PublishInput = { audience: string; approved: boolean };

export function PublishAudience({
  preview, plate, error, onPublish,
}: {
  preview: PreviewBench;
  plate: VisitorPlate | null;
  error: string | null;
  onPublish: (input: PublishInput) => void;
}) {
  const [audience, setAudience] = useState<string>("customers");
  const [approved, setApproved] = useState(false);
  const door = visitorDoor(plate);
  const held = !door;

  return (
    <section className="vplate" aria-label="Visitor plate">
      <style>{STYLES}</style>
      <header className="vplate-top">
        <div>
          <p className="vplate-eyebrow">Visitor plate</p>
          <h1>Seal a copy</h1>
          <p data-testid="plate-copy">
            The live preview stays on the bench. Visitors only receive a plate that has been approved.
          </p>
        </div>
        <div
          className="vplate-seal"
          data-testid="wax-seal"
          data-state={held ? "held" : "open"}
          title={held
            ? "Pending plates stay off the visitor door."
            : "Approved. This is the visitor door."}
        >
          <span>{held ? "Held" : "Open"}</span>
        </div>
      </header>
      <div className="vplate-split">
        <div className="vplate-bench">
          <p className="vplate-kicker">On the bench</p>
          <h2>{preview.title}</h2>
          <p className="vplate-url" data-testid="bench-preview" title="The in-progress preview. Not a visitor URL.">
            {preview.url}
          </p>
          <p className="vplate-note" data-testid="bench-note">Not the visitor door.</p>
        </div>
        <form
          className="vplate-window"
          onSubmit={(event) => {
            event.preventDefault();
            onPublish({ audience, approved });
          }}
        >
          <p className="vplate-kicker">The window</p>
          <label htmlFor="vplate-audience">Audience</label>
          <select
            id="vplate-audience"
            data-testid="audience"
            value={audience}
            onChange={(event) => setAudience(event.target.value)}
          >
            {AUDIENCES.map((item) => (
              <option key={item.value} value={item.value}>{item.label}</option>
            ))}
          </select>
          <label className="vplate-check" htmlFor="vplate-approved">
            <input
              id="vplate-approved"
              data-testid="approved"
              type="checkbox"
              checked={approved}
              title="Pending plates stay off the visitor door."
              onChange={(event) => setApproved(event.target.checked)}
            />
            Approved
          </label>
          <button
            type="button"
            data-testid="publish-plate"
            onClick={() => onPublish({ audience, approved })}
          >
            Seal this plate
          </button>
          <p className="vplate-state" data-testid="approval-state">
            {plate?.approval_state === "approved" ? "Approved" : "Pending"}
          </p>
          <div className="vplate-door" data-testid="visitor-door">
            {door ? <a href={door}>{door}</a> : "No visitor URL"}
          </div>
          {error ? <p data-testid="publish-error">{error}</p> : null}
        </form>
      </div>
    </section>
  );
}

const STYLES = `
.vplate {
  --ink: #102226;
  --harbor: #14343c;
  --glass: #d7ebe4;
  --foam: #e7f6f1;
  --walnut: #3c2a24;
  --chalk: #d5ebe3;
  --coral: #e25b3a;
  --brass: #e2b657;
  --deep: #1a3f38;
  background: var(--harbor);
  color: var(--foam);
  font-family: "Avenir Next", "Segoe UI", sans-serif;
  padding: 28px 28px 32px;
  max-width: 920px;
}
.vplate h1, .vplate h2, .vplate p { margin: 0; }
.vplate h1 {
  font-family: "Iowan Old Style", Palatino, "Palatino Linotype", "Book Antiqua", serif;
  font-size: var(--fs-page, 1.132rem);
  font-weight: 600;
  letter-spacing: -0.02em;
}
.vplate h2 {
  font-family: "Iowan Old Style", Palatino, "Palatino Linotype", serif;
  font-size: var(--fs-title, 0.842rem);
  font-weight: 600;
}
.vplate-eyebrow, .vplate-kicker {
  font-family: var(--mono, ui-monospace, monospace);
  font-size: var(--fs-label, 0.6rem);
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--brass);
}
.vplate-top {
  display: flex;
  justify-content: space-between;
  gap: 24px;
  align-items: flex-start;
}
.vplate-top p[data-testid="plate-copy"] {
  margin-top: 10px;
  max-width: 38rem;
  font-size: var(--fs-body, 0.737rem);
  line-height: 1.45;
  color: var(--glass);
}
.vplate-seal {
  width: 84px;
  height: 84px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  flex: none;
  font-family: "Iowan Old Style", Palatino, serif;
  font-size: var(--fs-title, 0.842rem);
  transform: rotate(-8deg);
}
.vplate-seal[data-state="held"] {
  border: 2px dashed var(--coral);
  color: var(--coral);
  background: transparent;
}
.vplate-seal[data-state="open"] {
  background: radial-gradient(circle at 35% 30%, #f3d48a, var(--brass) 55%, #b6842e);
  color: var(--ink);
  box-shadow: inset 0 -6px 0 rgba(60, 42, 20, 0.25);
}
.vplate-split {
  display: grid;
  grid-template-columns: 1fr 1fr;
  margin-top: 28px;
  background-image: radial-gradient(circle, var(--brass) 1px, transparent 1.4px);
  background-size: 8px 14px;
  background-position: center;
  background-repeat: repeat-y;
}
.vplate-bench, .vplate-window { padding: 22px; min-height: 280px; }
.vplate-bench { background: var(--walnut); color: var(--chalk); }
.vplate-window { background: var(--glass); color: var(--ink); }
.vplate-bench .vplate-kicker { color: #f0b429; }
.vplate-window .vplate-kicker { color: var(--deep); }
.vplate-url, .vplate-note, .vplate-state, .vplate-door {
  font-size: var(--fs-meta, 0.7rem);
}
.vplate-url {
  margin-top: 14px;
  font-family: var(--mono, ui-monospace, monospace);
  word-break: break-all;
}
.vplate-note { margin-top: 8px; color: #f0b429; }
.vplate-window label {
  display: block;
  margin-top: 14px;
  font-size: var(--fs-ui, 0.737rem);
}
.vplate-window select, .vplate-window button {
  font: inherit;
  font-size: var(--fs-ui, 0.737rem);
}
.vplate-window select {
  margin-top: 6px;
  width: 100%;
  background: var(--foam);
  color: var(--ink);
  border: 1px solid var(--deep);
  padding: 8px 10px;
}
.vplate-check {
  display: flex;
  gap: 8px;
  align-items: center;
}
.vplate-window button {
  margin-top: 18px;
  background: var(--deep);
  color: var(--foam);
  border: 0;
  padding: 10px 16px;
  cursor: pointer;
}
.vplate-state { margin-top: 16px; font-family: var(--mono, ui-monospace, monospace); letter-spacing: 0.08em; text-transform: uppercase; }
.vplate-door { margin-top: 6px; font-family: var(--mono, ui-monospace, monospace); word-break: break-all; }
.vplate-door a { color: var(--deep); }
.vplate [data-testid="publish-error"] {
  margin-top: 12px;
  color: #8d2412;
  font-size: var(--fs-body, 0.737rem);
}
.vplate button:focus-visible, .vplate select:focus-visible, .vplate a:focus-visible, .vplate input:focus-visible {
  outline: 2px solid var(--brass);
  outline-offset: 2px;
}
@media (max-width: 720px) {
  .vplate-split { grid-template-columns: 1fr; background-image: none; }
  .vplate { padding: 18px; }
}
@media (prefers-reduced-motion: reduce) {
  .vplate-seal { transform: none; }
}
`;
