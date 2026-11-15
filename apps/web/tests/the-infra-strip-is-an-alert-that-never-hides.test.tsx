// @vitest-environment happy-dom
/**
 * S1 · The storage strip (BOSSAPP-1156): an alert on every page while the incident is open,
 * flashing until a person acknowledges it, then one quiet status line that STAYS until the
 * incident ends. There is no close. The flash is one slow pulse that stops with reduced motion.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InfraStripBanner, InfraStripBannerView, stripState } from "../app/infra/infra-strip-banner";
import { openIncident } from "../lib/infra-storage";
import { demoIncident } from "../lib/infra-storage-demo";
import { codeOnly } from "./_helpers/source";
import { $, $$, click, mount, settle, unmount } from "./_helpers/live-render";

afterEach(() => { unmount(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

const incident = openIncident(demoIncident("1"))!;

describe("the storage strip", () => {
  it("is an assertive alert that says the cause, the progress and the ETA, with Acknowledge only", async () => {
    await mount(<InfraStripBannerView incident={incident} actor="alice" acked={false} onAcknowledge={() => {}} busy={false} />);
    const strip = $('[data-testid="infra-strip"]')!;
    expect(strip.getAttribute("role")).toBe("alert");
    expect(strip.getAttribute("aria-live")).toBe("assertive");
    expect(strip.dataset.state).toBe("flashing");
    expect($('[data-testid="infra-strip-head"]')!.textContent).toBe("Storage critical on laptop-boss · Infra Agent is cleaning");
    expect($('[data-testid="infra-strip-sub"]')!.textContent).toContain("14 runs paused at a checkpoint");
    expect($('[data-testid="infra-strip-meta"]')!.textContent).toMatch(/GB freed/);
    expect($('[data-testid="infra-strip-meta"]')!.textContent).toMatch(/~\d+ min left/);
    expect($('[data-testid="infra-strip-meta"]')!.textContent).toContain("step 3 of 6");
    // The one dismissing control is Acknowledge; nothing closes or hides it.
    const buttons = $$('[data-testid="infra-strip"] button').map((b) => b.textContent?.trim());
    expect(buttons).toEqual(["Acknowledge"]);
    expect(strip.querySelector('[aria-label*="lose" i], [title*="lose" i], [aria-label*="dismiss" i]')).toBeNull();
    expect($('[data-testid="infra-strip-watch"]')!.getAttribute("href")).toContain("area=overview");
    // The pulse is on the dot, the strip's own class carries the ground pulse.
    expect(strip.querySelector(".ia-pulse")).not.toBeNull();
  });

  it("shrinks to a quiet status line after Acknowledge and stays until the incident resolves", async () => {
    await mount(<InfraStripBannerView incident={{ ...incident, acknowledgedBy: "Alice", acknowledgedAt: "2026-11-14T18:04:00Z" }} actor="alice" acked={false} onAcknowledge={() => {}} busy={false} />);
    const strip = $('[data-testid="infra-strip"]')!;
    expect(strip.getAttribute("role")).toBe("status");
    expect(strip.dataset.state).toBe("acknowledged");
    expect($('[data-testid="infra-strip-acked"]')!.textContent).toMatch(/acknowledged by Alice/);
    expect(strip.querySelector(".ia-pulse")).toBeNull();
    expect($$('[data-testid="infra-strip"] button')).toHaveLength(0);
    // Resolved: gone. Any other status: shown.
    expect(stripState({ ...incident, status: "resolved" }, true)).toBe("hidden");
    expect(stripState(incident, false)).toBe("flashing");
    expect(stripState(incident, true)).toBe("acknowledged");
    expect(stripState({ ...incident, status: "asking" }, false)).toBe("flashing");
    expect(stripState(null, false)).toBe("hidden");
  });

  it("live: reads /incidents/current, posts the acknowledgement with the actor, and keeps the strip", async () => {
    const calls: { url: string; method: string; body: string | null }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      calls.push({ url, method: init?.method ?? "GET", body: typeof init?.body === "string" ? init.body : null });
      if (url.includes("/acknowledge")) return new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "content-type": "application/json" } });
      if (url.includes("/incidents/current")) {
        return new Response(JSON.stringify({ incidents: [{ id: "si_9", host_id: "boss-1", host_label: "boss-1", status: "critical", free_pct_now: 11, hold: { active: true, paused_runs: 3 }, freed_gb: 4, eta_seconds: 120,
          flow: [{ key: "detected", type: "signal", label: "Detected", badges: ["11 % free"], state: "done", at: "2026-11-14T18:02:00Z", detail: "11 % free" }] }], hold: { active: true, paused_runs: 3 } }),
          { status: 200, headers: { "content-type": "application/json", etag: "e1" } });
      }
      return new Response("{}", { status: 404 });
    }));
    await mount(<InfraStripBanner projectId="p1" actor="alice" />);
    for (let i = 0; i < 5 && !$('[data-testid="infra-strip"]'); i += 1) await settle();
    expect($('[data-testid="infra-strip"]')).not.toBeNull();
    expect($('[data-testid="infra-strip-head"]')!.textContent).toContain("boss-1");
    await click($('[data-testid="infra-strip-ack"]'));
    const ack = calls.find((c) => c.url.includes("/v1/infra/storage/incidents/si_9/acknowledge"));
    expect(ack?.method).toBe("POST");
    expect(JSON.parse(ack!.body!)).toEqual({});
    expect($('[data-testid="infra-strip"]')!.getAttribute("role")).toBe("status");
    expect($('[data-testid="infra-strip-acked"]')!.textContent).toContain("alice");
  });

  it("renders nothing on a healthy host, and nothing on an API without the router yet (404)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "Not Found" }), { status: 404 })));
    await mount(<InfraStripBanner projectId="p1" actor="alice" />);
    await settle();
    expect($('[data-testid="infra-strip"]')).toBeNull();
    expect(document.body.textContent).not.toMatch(/404|Not Found/);
  });
});

describe("the strip's place and motion", () => {
  it("is mounted by the shell on every area, above the notice slot and the identity banners", () => {
    const shell = codeOnly(join(__dirname, "../app/ConsoleShell.tsx"));
    const strip = shell.indexOf("<InfraStripBanner");
    expect(strip).toBeGreaterThan(0);
    expect(strip).toBeLessThan(shell.indexOf("id={STRIP_NOTICE_SLOT}"));
    expect(strip).toBeLessThan(shell.indexOf("<IdentityBanners"));
  });

  it("pulses at 0.5 Hz (a 2 s cycle, far under WCAG 2.3.1's 3 Hz) and not at all with reduced motion", () => {
    const css = readFileSync(join(__dirname, "../app/infra/infra-storage.css"), "utf8");
    expect(css).toMatch(/@keyframes ia-pulse/);
    expect(css).toMatch(/\.ia-pulse\s*\{\s*animation:\s*ia-pulse\s+2s/);
    const reduced = css.slice(css.indexOf("@media (prefers-reduced-motion: reduce)"));
    expect(reduced).toMatch(/\.ia-pulse,\s*\.ia-loud\s*\{\s*animation:\s*none;/);
  });
});
