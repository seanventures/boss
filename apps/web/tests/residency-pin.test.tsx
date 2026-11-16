// @vitest-environment happy-dom
/**
 * An admin pins where a built app runs. The control-plane host string on this
 * screen does not change, and a cloud region is never described as contacted.
 */
import React, { useState } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { $, cleanup, click, render } from "./_helpers/kit-render";

const HOST = "plane.example:8000";
const TODAY = "ssh://deploy@198.51.100.10/ledger";

type Geography = "eu" | "uk" | "us" | "apac" | null;

function Screen({ initial = null as Geography }: { initial?: Geography }) {
  const [geography, setGeography] = useState<Geography>(initial);
  return (
    <ResidencyPin
      appKey="ledger"
      appName="Ledger"
      geography={geography}
      controlPlaneHost={HOST}
      appPath={TODAY}
      onPin={(next) => setGeography(next)}
      onClear={() => setGeography(null)}
    />
  );
}

const { ResidencyPin, admitDeploy } = await import("../app/settings/residency-pin");

afterEach(() => cleanup());

function words(value: string | null | undefined): string[] {
  return (value ?? "").trim().split(/\s+/).filter(Boolean);
}

describe("admitDeploy", () => {
  it("keeps today's path when the pin is unset", () => {
    const out = admitDeploy({
      geography: null,
      targetGeography: "us",
      appPath: TODAY,
      controlPlaneHost: HOST,
    });
    expect(out.allowed).toBe(true);
    expect(out.path).toBe("today");
    expect(out.appPath).toBe(TODAY);
    expect(out.controlPlaneHost).toBe(HOST);
    expect(out.cloudContacted).toBe(false);
  });

  it("refuses a target that is not the pin and does not contact a region", () => {
    const refused = admitDeploy({
      geography: "eu",
      targetGeography: "us",
      appPath: TODAY,
      controlPlaneHost: HOST,
    });
    expect(refused.allowed).toBe(false);
    expect(refused.path).toBe("refused");
    expect(refused.appPath).toBe(TODAY);
    expect(refused.controlPlaneHost).toBe(HOST);
    expect(refused.cloudContacted).toBe(false);
    expect(refused.reason?.toLowerCase()).not.toContain("contacted");
    const region = admitDeploy({
      geography: "eu",
      targetGeography: "eu-west-1",
      appPath: TODAY,
      controlPlaneHost: HOST,
    });
    expect(region.allowed).toBe(false);
    expect(region.cloudContacted).toBe(false);
    expect(region.reason?.toLowerCase()).toContain("not contacted");
    expect(region.controlPlaneHost).toBe(HOST);
  });
});

describe("the residency screen", () => {
  it("shows an unset pin, the EU choice, and the same control-plane host", async () => {
    await render(<Screen />);
    expect($('[data-testid="residency-seal"]')?.textContent).toContain("UNPINNED");
    expect($('[data-testid="residency-control-host"]')?.textContent).toBe(HOST);
    expect($('[data-testid="residency-path"]')?.textContent).toBe(TODAY);
    expect($('[data-testid="residency-cloud"]')?.getAttribute("data-cloud-contacted")).toBe("false");
    expect($('[data-testid="residency-geography-eu"]')).toBeTruthy();
    const style = $("style")?.textContent ?? "";
    expect(style).toContain("Besley");
    expect(style).toContain("#1B2A4A");
    expect(style).not.toContain("Inter");
    expect(style.toLowerCase()).not.toContain("#f4f1ea");
    expect(style).not.toContain("#7C3AED");
    expect(style).toContain("prefers-reduced-motion");
  });

  it("pins the EU without rewriting the host or today's path", async () => {
    await render(<Screen />);
    await click('[data-testid="residency-geography-eu"]');
    await click('[data-testid="residency-pin-button"]');
    expect($('[data-testid="residency-seal"]')?.textContent).toContain("EU");
    expect($('[data-testid="residency-control-host"]')?.textContent).toBe(HOST);
    expect($('[data-testid="residency-path"]')?.textContent).toBe(TODAY);
    expect($('[data-testid="residency-cloud"]')?.getAttribute("data-cloud-contacted")).toBe("false");
  });

  it("refuses a deploy outside the pin and allows one inside it", async () => {
    await render(<Screen initial="eu" />);
    const target = $('[data-testid="residency-target"]') as HTMLInputElement;
    target.value = "us";
    target.dispatchEvent(new Event("input", { bubbles: true }));
    await click('[data-testid="residency-check"]');
    const refused = $('[data-testid="residency-verdict"]');
    expect(refused?.getAttribute("data-path")).toBe("refused");
    expect(refused?.getAttribute("data-allowed")).toBe("false");
    expect($('[data-testid="residency-control-host"]')?.textContent).toBe(HOST);
    expect($('[data-testid="residency-path"]')?.textContent).toBe(TODAY);
    expect(refused?.textContent?.toLowerCase()).not.toContain("eu-west-1");
    target.value = "eu";
    target.dispatchEvent(new Event("input", { bubbles: true }));
    await click('[data-testid="residency-check"]');
    expect($('[data-testid="residency-verdict"]')?.getAttribute("data-path")).toBe("pinned");
    expect($('[data-testid="residency-control-host"]')?.textContent).toBe(HOST);
  });

  it("clears the pin and keeps today's path", async () => {
    await render(<Screen initial="eu" />);
    await click('[data-testid="residency-clear"]');
    expect($('[data-testid="residency-seal"]')?.textContent).toContain("UNPINNED");
    const target = $('[data-testid="residency-target"]') as HTMLInputElement;
    target.value = "us";
    target.dispatchEvent(new Event("input", { bubbles: true }));
    await click('[data-testid="residency-check"]');
    expect($('[data-testid="residency-verdict"]')?.getAttribute("data-path")).toBe("today");
    expect($('[data-testid="residency-control-host"]')?.textContent).toBe(HOST);
    expect($('[data-testid="residency-path"]')?.textContent).toBe(TODAY);
  });

  it("puts a long sentence in a paragraph and keeps tooltips short", async () => {
    await render(<Screen />);
    const copies = [...document.querySelectorAll<HTMLElement>("[data-copy]")];
    expect(copies.length).toBeGreaterThan(0);
    for (const el of copies) {
      if (words(el.textContent).length > 6) expect(el.tagName).toBe("P");
    }
    const thesis = $('[data-testid="residency-thesis"]');
    expect(thesis?.tagName).toBe("P");
    expect(words(thesis?.textContent).length).toBeGreaterThan(6);
    for (const el of document.querySelectorAll<HTMLElement>("[title]")) {
      expect(words(el.getAttribute("title")).length).toBeLessThanOrEqual(14);
    }
  });
});
