// @vitest-environment happy-dom
/**
 * From Publish: place the app in a customer tenant, or hand over the image,
 * the Terraform stub, and an SBOM only when one is already recorded (BOSSAPP-1259).
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { $, cleanup, click, render, type } from "./_helpers/kit-render";

const { TenantHandoff } = await import("../app/builder/tenant-handoff");

afterEach(() => cleanup());

const IMAGE = "registry.example/billing@sha256:abc123";

type Body = {
  app: string;
  image_reference: string;
  mode: "place" | "handover";
  tenant?: string | null;
  recorded_sbom?: unknown;
};

function papers(body: Body, extra: Record<string, unknown> = {}) {
  const recorded = body.recorded_sbom != null && body.recorded_sbom !== "";
  return {
    ok: true,
    app: body.app,
    mode: body.mode,
    tenant: body.mode === "place" ? body.tenant : null,
    placed: body.mode === "place",
    cloud_called: false,
    image_reference: body.image_reference,
    terraform_stub: `# Tenant handoff stub.\nimage = "${body.image_reference}"\n`,
    sbom: recorded ? body.recorded_sbom : null,
    sbom_recorded: recorded,
    sbom_note: recorded ? "SBOM on file." : "No SBOM is recorded.",
    ...extra,
  };
}

function words(text: string | null | undefined): number {
  return (text ?? "").trim().split(/\s+/).filter(Boolean).length;
}

describe("tenant handoff from Publish", () => {
  it("hands over the image and stub, and says when no SBOM is recorded", async () => {
    const calls: Body[] = [];
    await render(
      <TenantHandoff
        publish={{ app: "billing", imageReference: IMAGE, packages: ["left-pad"] }}
        request={async (body) => {
          calls.push(body);
          return papers(body);
        }}
      />,
    );
    expect(document.querySelector("p")?.textContent).toMatch(/SBOM on file/);
    expect(words($('[data-testid="th-handover"]')?.textContent)).toBeLessThanOrEqual(6);
    expect(words($('[data-testid="th-handover"]')?.getAttribute("title"))).toBeLessThanOrEqual(14);
    await click('[data-testid="th-handover"]');
    expect(calls).toHaveLength(1);
    expect(calls[0].mode).toBe("handover");
    expect(calls[0].recorded_sbom).toBeNull();
    expect(calls[0].image_reference).toBe(IMAGE);
    expect(JSON.stringify(calls[0])).not.toContain("left-pad");
    expect($('[data-testid="th-image"]')?.textContent).toContain(IMAGE);
    expect($('[data-testid="th-stub"]')?.textContent).toContain(IMAGE);
    expect($('[data-testid="th-sbom-note"]')?.textContent).toBe("No SBOM is recorded.");
    expect(words($('[data-testid="th-sbom-note"]')?.textContent)).toBeLessThanOrEqual(6);
    expect($('[data-testid="th-seal"]')?.textContent).toBe("NONE");
    expect($('[data-testid="th-cloud"]')?.textContent).toBe("No cloud was called.");
    expect(document.body.textContent).not.toContain("left-pad");
    expect(document.body.textContent).not.toContain("CycloneDX");
  });

  it("does not show an SBOM the handoff says was not recorded", async () => {
    await render(
      <TenantHandoff
        publish={{ app: "billing", imageReference: IMAGE }}
        request={async (body) => papers(body, {
          sbom_recorded: false,
          sbom_note: "No SBOM is recorded.",
          sbom: { bomFormat: "CycloneDX", components: [{ name: "invented-package" }] },
        })}
      />,
    );
    await click('[data-testid="th-handover"]');
    expect($('[data-testid="th-sbom-note"]')?.textContent).toBe("No SBOM is recorded.");
    expect($('[data-testid="th-sbom"]')).toBeNull();
    expect(document.body.textContent).not.toContain("invented-package");
    expect($('[data-testid="th-image"]')?.textContent).toContain(IMAGE);
    expect($('[data-testid="th-stub"]')?.textContent).toContain(IMAGE);
  });

  it("shows the SBOM that was already recorded", async () => {
    const calls: Body[] = [];
    await render(
      <TenantHandoff
        publish={{ app: "billing", imageReference: IMAGE, recordedSbom: "artifacts/sbom-9" }}
        request={async (body) => {
          calls.push(body);
          return papers(body);
        }}
      />,
    );
    await click('[data-testid="th-handover"]');
    expect(calls[0].recorded_sbom).toBe("artifacts/sbom-9");
    expect($('[data-testid="th-sbom"]')?.textContent).toContain("artifacts/sbom-9");
    expect($('[data-testid="th-sbom-note"]')?.textContent).toBe("SBOM on file.");
    expect($('[data-testid="th-seal"]')?.textContent).toBe("ON FILE");
    expect(document.body.textContent).not.toContain("No SBOM is recorded.");
  });

  it("places the app in the named tenant and does not claim a cloud call", async () => {
    const calls: Body[] = [];
    await render(
      <TenantHandoff
        publish={{ app: "billing", imageReference: IMAGE }}
        request={async (body) => {
          calls.push(body);
          return papers(body);
        }}
      />,
    );
    const place = $('[data-testid="th-place"]') as HTMLButtonElement;
    expect(place.disabled).toBe(true);
    expect(words(place.getAttribute("title"))).toBeLessThanOrEqual(14);
    await click('[data-testid="th-place"]');
    expect(calls).toHaveLength(0);
    await type('[data-testid="th-tenant"]', "acme-prod");
    expect((place).disabled).toBe(false);
    await click('[data-testid="th-place"]');
    expect(calls).toHaveLength(1);
    expect(calls[0].mode).toBe("place");
    expect(calls[0].tenant).toBe("acme-prod");
    expect(calls[0].recorded_sbom).toBeNull();
    expect($('[data-testid="th-tenant-line"]')?.textContent).toContain("acme-prod");
    expect($('[data-testid="th-placed"]')?.textContent).toBe("Recorded for the tenant.");
    expect($('[data-testid="th-cloud"]')?.textContent).toBe("No cloud was called.");
    expect($('[data-testid="th-image"]')?.textContent).toContain(IMAGE);
    expect($('[data-testid="th-sbom-note"]')?.textContent).toBe("No SBOM is recorded.");
  });

  it("shows a refusal and does not clear it by silence", async () => {
    await render(
      <TenantHandoff
        publish={{ app: "billing", imageReference: "" }}
        request={async () => {
          throw new Error("should not be asked");
        }}
      />,
    );
    const error = $('[data-testid="th-error"]');
    expect(error?.tagName).toBe("P");
    expect(error?.textContent).toBe("This publish has no image reference.");
    await click('[data-testid="th-handover"]');
    expect($('[data-testid="th-error"]')?.textContent).toBe("This publish has no image reference.");
    expect($('[data-testid="th-result"]')).toBeNull();
  });

  it("shows a failed handoff instead of swallowing it", async () => {
    await render(
      <TenantHandoff
        publish={{ app: "billing", imageReference: IMAGE }}
        request={async () => {
          throw new Error("The registry refused the image reference.");
        }}
      />,
    );
    await click('[data-testid="th-handover"]');
    const error = $('[data-testid="th-error"]');
    expect(error?.tagName).toBe("P");
    expect(error?.textContent).toBe("The registry refused the image reference.");
    expect($('[data-testid="th-result"]')).toBeNull();
  });

  it("a second press while the first is in flight does not send twice", async () => {
    let release: (value: ReturnType<typeof papers>) => void = () => {};
    const calls: Body[] = [];
    await render(
      <TenantHandoff
        publish={{ app: "billing", imageReference: IMAGE }}
        request={(body) => {
          calls.push(body);
          return new Promise((resolve) => {
            release = resolve;
          });
        }}
      />,
    );
    await click('[data-testid="th-handover"]');
    await click('[data-testid="th-handover"]');
    expect(calls).toHaveLength(1);
    expect(($('[data-testid="th-handover"]') as HTMLButtonElement).disabled).toBe(true);
    release(papers(calls[0]));
    await click('[data-testid="th-handover"]');
  });

  it("keeps the long explanation in a paragraph and respects reduced motion", async () => {
    await render(
      <TenantHandoff
        publish={{ app: "billing", imageReference: IMAGE }}
        request={async (body) => papers(body)}
      />,
    );
    const prose = [...document.querySelectorAll("p")].map((p) => p.textContent ?? "");
    expect(prose.some((line) => words(line) > 6)).toBe(true);
    const style = document.querySelector("style")?.textContent ?? "";
    expect(style).toContain("prefers-reduced-motion");
    expect(style).not.toMatch(/#f4f1ea/i);
    expect(style).not.toMatch(/purple/i);
  });
});
