// @vitest-environment happy-dom
/**
 * The System tab's service states come from the host helper's last reading (enterprise validation,
 * ENT-OBS-01). Once the helper has been silent past 10 minutes an old "running" is not a fact
 * about now: the API marks the snapshot stale and the tab greys it and says so.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { SystemTab } from "../app/observability/system";
import { parseSystem } from "../lib/obs-view";
import { systemWire } from "./_helpers/obs-fixtures";
import { $, mount, unmount } from "./_helpers/live-render";

afterEach(() => unmount());

const services = [{ name: "boss-api-1", service: "api", state: "running" }];

function tab(compose: Record<string, unknown>) {
  const data = parseSystem(systemWire({ compose }));
  const props = { projectId: "p1", actor: "owner", orgId: "o1", system: { data, loading: false, error: null } };
  return <SystemTab {...(props as unknown as React.ComponentProps<typeof SystemTab>)} />;
}

describe("the service states on the System tab", () => {
  it("parses the stale mark and defaults to fresh", () => {
    expect(parseSystem(systemWire({ compose: { at: "2026-11-26T07:01:05Z", stale: true, services } })).compose!.stale).toBe(true);
    expect(parseSystem(systemWire({ compose: { at: "2026-11-26T07:01:05Z", services } })).compose!.stale).toBe(false);
  });

  it("greys a stale snapshot and says the helper stopped reporting", async () => {
    await mount(tab({ at: "2026-11-26T07:01:05Z", stale: true, services }));
    const zone = $('[data-testid="obs-compose-services"]')!;
    expect(zone.getAttribute("data-stale")).toBe("true");
    expect(zone.textContent).toContain("Stale · last reported");
    expect($('[data-testid="obs-compose-stale"]')!.textContent).toContain("boss steward status");
  });

  it("shows a fresh snapshot plainly", async () => {
    await mount(tab({ at: "2026-11-26T07:01:05Z", stale: false, services }));
    expect($('[data-testid="obs-compose-services"]')!.getAttribute("data-stale")).toBeNull();
    expect($('[data-testid="obs-compose-stale"]')).toBeNull();
  });
});
