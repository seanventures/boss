"use client";

/**
 * From Publish: place the app in a customer tenant, or hand over the image,
 * the Terraform stub, and an SBOM only when Publish already recorded one.
 * No cloud is called from this screen.
 */
import React, { useState } from "react";
import { apiWrite } from "../../lib/api-write";

export type PublishRecord = {
  app: string;
  imageReference: string;
  recordedSbom?: string | Record<string, unknown> | unknown[] | null;
  packages?: string[];
  publishId?: string | null;
};

export type HandoffBody = {
  app: string;
  image_reference: string;
  mode: "place" | "handover";
  tenant?: string | null;
  recorded_sbom?: unknown;
  publish_id?: string | null;
};

export type HandoffResult = {
  ok?: boolean;
  app?: string;
  mode: string;
  tenant?: string | null;
  placed?: boolean;
  cloud_called?: boolean;
  image_reference: string;
  terraform_stub: string;
  sbom?: unknown;
  sbom_recorded?: boolean;
  sbom_note?: string;
};

const CSS = `
.th {
  --th-dock: #c5d0d8;
  --th-ink: #1a2330;
  --th-ticket: #f7f8f6;
  --th-brass: #8d6a2f;
  --th-tide: #1c5e57;
  --th-seal-none: #8e2f1f;
  --th-rule: #2a3544;
  --th-display: "Barlow Condensed", "Arial Narrow", "Franklin Gothic Medium", sans-serif;
  --th-body: "Source Serif 4", "Iowan Old Style", Palatino, "Palatino Linotype", serif;
  --th-mono: "IBM Plex Mono", ui-monospace, monospace;
  box-sizing: border-box;
  min-height: 100%;
  padding: 28px 20px 48px;
  background: var(--th-dock);
  color: var(--th-ink);
  font-family: var(--th-body);
}
.th *, .th *::before, .th *::after { box-sizing: border-box; }
.th-sheet { max-width: 40rem; margin: 0 auto; }
.th-eyebrow {
  margin: 0 0 6px;
  font-family: var(--th-display);
  font-size: 0.95rem;
  font-weight: 600;
  letter-spacing: 0.16em;
  text-transform: uppercase;
}
.th-app {
  margin: 0 0 12px;
  font-family: var(--th-display);
  font-size: clamp(2.6rem, 8vw, 4.4rem);
  font-weight: 650;
  letter-spacing: -0.02em;
  line-height: 0.9;
  text-transform: uppercase;
}
.th-lead, .th-note, .th-error { margin: 0 0 16px; font-size: 1.05rem; line-height: 1.45; }
.th-error { color: var(--th-seal-none); }
.th-actions { display: flex; flex-wrap: wrap; gap: 10px; align-items: end; margin-bottom: 22px; }
.th-field { display: flex; flex-direction: column; gap: 4px; flex: 1 1 12rem; }
.th-field span, .th-k {
  font-family: var(--th-display);
  font-size: 0.82rem;
  letter-spacing: 0.14em;
  text-transform: uppercase;
}
.th-field input {
  font-family: var(--th-mono);
  font-size: 0.95rem;
  padding: 10px 12px;
  border: 1px solid var(--th-rule);
  background: var(--th-ticket);
  color: var(--th-ink);
}
.th-place, .th-hand {
  font-family: var(--th-display);
  font-size: 1.15rem;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  padding: 10px 16px;
  border: 1px solid var(--th-rule);
  background: var(--th-ink);
  color: var(--th-ticket);
  cursor: pointer;
}
.th-hand { background: var(--th-tide); border-color: var(--th-tide); }
.th-place:disabled, .th-hand:disabled { cursor: not-allowed; opacity: 0.45; }
.th-place:focus-visible, .th-hand:focus-visible, .th-field input:focus-visible {
  outline: 2px solid var(--th-tide);
  outline-offset: 3px;
}
.th-ticket {
  position: relative;
  background:
    radial-gradient(circle at left, var(--th-dock) 6px, transparent 7px) left center / 18px 22px repeat-y,
    var(--th-ticket);
  border: 1px solid var(--th-rule);
  padding: 22px 22px 22px 28px;
}
.th-seal {
  position: absolute;
  top: 18px;
  right: 16px;
  width: 4.6rem;
  height: 4.6rem;
  display: grid;
  place-items: center;
  border: 2px solid var(--th-brass);
  border-radius: 50%;
  color: var(--th-seal-none);
  font-family: var(--th-display);
  font-size: 0.95rem;
  letter-spacing: 0.08em;
  transform: rotate(-8deg);
  animation: th-stamp 280ms ease-out both;
}
.th-seal[data-held="true"] { color: var(--th-tide); }
@keyframes th-stamp {
  from { transform: rotate(-14deg) scale(1.18); opacity: 0; }
  to { transform: rotate(-8deg) scale(1); opacity: 1; }
}
.th-row { display: block; margin: 0 0 14px; padding-right: 5.2rem; }
.th-row code, .th-row pre {
  display: block;
  margin: 4px 0 0;
  font-family: var(--th-mono);
  font-size: 0.82rem;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  color: var(--th-tide);
}
.th-cloud { margin: 8px 0 0; font-family: var(--th-display); letter-spacing: 0.08em; text-transform: uppercase; }
@media (max-width: 640px) {
  .th-actions { display: block; }
  .th-place, .th-hand { width: 100%; margin-top: 8px; }
  .th-seal { position: static; margin: 0 0 12px auto; }
  .th-row { padding-right: 0; }
}
@media (prefers-reduced-motion: reduce) {
  .th-seal { animation: none; }
}
`;

function recordedValue(publish: PublishRecord): unknown {
  const raw = publish.recordedSbom;
  if (typeof raw === "string") return raw.trim() ? raw.trim() : null;
  if (Array.isArray(raw)) return raw.length ? raw : null;
  if (raw && typeof raw === "object") return Object.keys(raw).length ? raw : null;
  return null;
}

async function defaultRequest(body: HandoffBody): Promise<HandoffResult> {
  const response = await apiWrite("/v1/tenant-handoff", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  let data: { detail?: unknown } = {};
  try {
    data = await response.json();
  } catch {
    data = {};
  }
  if (!response.ok) {
    const detail = typeof data.detail === "string" ? data.detail : "The handoff failed.";
    throw new Error(detail);
  }
  return data as HandoffResult;
}

function sbomText(value: unknown): string {
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

export function TenantHandoff({
  publish,
  request = defaultRequest,
}: {
  publish: PublishRecord;
  request?: (body: HandoffBody) => Promise<HandoffResult>;
}) {
  const image = publish.imageReference.trim();
  const [tenant, setTenant] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(image ? null : "This publish has no image reference.");
  const [result, setResult] = useState<HandoffResult | null>(null);
  const held = result?.sbom_recorded === true && result.sbom != null;

  async function go(mode: "place" | "handover") {
    if (busy) return;
    if (!image) {
      setError("This publish has no image reference.");
      setResult(null);
      return;
    }
    if (mode === "place" && !tenant.trim()) {
      setError("Name the tenant.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const next = await request({
        app: publish.app,
        image_reference: publish.imageReference,
        mode,
        tenant: mode === "place" ? tenant.trim() : null,
        recorded_sbom: recordedValue(publish),
        publish_id: publish.publishId ?? null,
      });
      setResult(next);
    } catch (caught) {
      setResult(null);
      const message = caught instanceof Error ? caught.message : "";
      setError(message || "The handoff failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="th" data-testid="tenant-handoff">
      <style>{CSS}</style>
      <div className="th-sheet">
        <div className="th-eyebrow">From Publish</div>
        <h1 className="th-app">{publish.app || "Unnamed app"}</h1>
        <p className="th-lead">
          Place it in the customer&apos;s tenant, or hand over the image, the stub, and the SBOM on file.
        </p>
        {error ? <p className="th-error" data-testid="th-error">{error}</p> : null}
        <div className="th-actions">
          <label className="th-field">
            <span>Tenant</span>
            <input
              data-testid="th-tenant"
              value={tenant}
              title="The customer's cloud tenant."
              onChange={(event) => setTenant(event.target.value)}
              autoComplete="off"
              disabled={busy || !image}
            />
          </label>
          <button
            type="button"
            className="th-place"
            data-testid="th-place"
            title="Records the tenant. No cloud call."
            disabled={busy || !image || !tenant.trim()}
            onClick={() => void go("place")}
          >
            Place in tenant
          </button>
          <button
            type="button"
            className="th-hand"
            data-testid="th-handover"
            title="Image, stub, and recorded SBOM."
            disabled={busy || !image}
            onClick={() => void go("handover")}
          >
            {busy ? "Handing over…" : "Hand over"}
          </button>
        </div>
        {result ? (
          <article className="th-ticket" data-testid="th-result">
            <span className="th-seal" data-testid="th-seal" data-held={held ? "true" : "false"}>
              {held ? "ON FILE" : "NONE"}
            </span>
            {result.placed ? <p data-testid="th-placed">Recorded for the tenant.</p> : null}
            {result.tenant ? (
              <div className="th-row" data-testid="th-tenant-line">
                <span className="th-k">Tenant</span> {result.tenant}
              </div>
            ) : null}
            <div className="th-row">
              <span className="th-k">Image</span>
              <code data-testid="th-image">{result.image_reference}</code>
            </div>
            <div className="th-row">
              <span className="th-k">Terraform stub</span>
              <pre data-testid="th-stub">{result.terraform_stub}</pre>
            </div>
            <p className="th-note" data-testid="th-sbom-note">{result.sbom_note}</p>
            {held ? <pre data-testid="th-sbom">{sbomText(result.sbom)}</pre> : null}
            <p className="th-note">This stub names the image. It is not applied.</p>
            <p className="th-cloud" data-testid="th-cloud">No cloud was called.</p>
          </article>
        ) : null}
      </div>
    </section>
  );
}
