// @vitest-environment happy-dom
/**
 * One project shows a website, a mobile app, slides, and a data view.
 * Publishing one plate does not take the others off the sheet.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, $$, cleanup, click, render, type } from "./_helpers/kit-render";

const { ProjectSuite, suiteDedupeKey } = await import("../app/builder/project-suite");

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

type Surface = {
  kind: "website" | "mobile" | "slides" | "data_view";
  title: string;
  body: string;
  status: "draft" | "published";
  revision: number;
  published_revision: number | null;
};

const HELD: Surface[] = [
  { kind: "website", title: "Harbor site", body: "A public page.", status: "draft", revision: 1, published_revision: null },
  { kind: "mobile", title: "Harbor phone", body: "", status: "draft", revision: 2, published_revision: null },
  { kind: "slides", title: "Harbor deck", body: "", status: "draft", revision: 1, published_revision: null },
  { kind: "data_view", title: "Harbor numbers", body: "", status: "draft", revision: 3, published_revision: null },
];

const KINDS = ["website", "mobile", "slides", "data_view"];

function titles() {
  return KINDS.map((kind) => $(`[data-testid="suite-title-${kind}"]`)?.textContent ?? "");
}

describe("the project sheet", () => {
  it("shows all four plates a project holds", async () => {
    await render(<ProjectSuite projectId="p1" projectName="Harbor" initial={HELD} />);
    expect($$("[data-testid^=suite-plate-]").map((el) => el.getAttribute("data-kind"))).toEqual(KINDS);
    expect(titles()).toEqual(["Harbor site", "Harbor phone", "Harbor deck", "Harbor numbers"]);
    expect($('[data-testid="suite-lead"]')?.tagName).toBe("P");
    expect(($('[data-testid="suite-lead"]')?.textContent ?? "").trim().split(/\s+/).length).toBeGreaterThan(6);
    for (const button of $$("button")) {
      expect((button.textContent ?? "").trim().split(/\s+/).length).toBeLessThanOrEqual(6);
    }
    for (const tip of $$("[title]")) {
      expect((tip.getAttribute("title") ?? "").trim().split(/\s+/).length).toBeLessThanOrEqual(14);
    }
  });

  it("publishing the website does not drop the other plates", async () => {
    const onPublish = vi.fn(async (req: { mode: string; kind?: string; dedupe_key: string }) => {
      expect(req.mode).toBe("one");
      expect(req.kind).toBe("website");
      expect(req.dedupe_key).toBe(suiteDedupeKey("one", "website", HELD));
      return { surfaces: [{ ...HELD[0], status: "published" as const, published_revision: 1 }] };
    });
    await render(
      <ProjectSuite projectId="p1" projectName="Harbor" initial={HELD} onPublish={onPublish} />,
    );
    await click('[data-testid="suite-publish-website"]');
    expect(onPublish).toHaveBeenCalledTimes(1);
    expect(titles()).toEqual(["Harbor site", "Harbor phone", "Harbor deck", "Harbor numbers"]);
    expect($('[data-testid="suite-status-website"]')?.textContent).toBe("Published");
    expect($('[data-testid="suite-status-mobile"]')?.textContent).toBe("Held");
    expect($('[data-testid="suite-status-slides"]')?.textContent).toBe("Held");
    expect($('[data-testid="suite-status-data_view"]')?.textContent).toBe("Held");
    expect($$("[data-testid^=suite-plate-]")).toHaveLength(4);
  });

  it("publishing the sheet keeps every plate published", async () => {
    const onPublish = vi.fn(async (req: { mode: string; kind?: string }) => {
      expect(req.mode).toBe("together");
      expect(req.kind).toBeUndefined();
      return {
        surfaces: HELD.map((row) => ({ ...row, status: "published" as const, published_revision: row.revision })),
      };
    });
    await render(
      <ProjectSuite projectId="p1" projectName="Harbor" initial={HELD} onPublish={onPublish} />,
    );
    await click('[data-testid="suite-publish-together"]');
    expect(titles()).toEqual(["Harbor site", "Harbor phone", "Harbor deck", "Harbor numbers"]);
    for (const kind of KINDS) {
      expect($(`[data-testid="suite-status-${kind}"]`)?.textContent).toBe("Published");
    }
  });

  it("a refused publish stays on the sheet and says why", async () => {
    const onPublish = vi.fn(async () => {
      throw new Error("The sheet is missing Slides.");
    });
    await render(
      <ProjectSuite projectId="p1" projectName="Harbor" initial={HELD} onPublish={onPublish} />,
    );
    await click('[data-testid="suite-publish-together"]');
    expect($('[data-testid="suite-error"]')?.tagName).toBe("P");
    expect($('[data-testid="suite-error"]')?.textContent).toBe("The sheet is missing Slides.");
    expect(titles()).toEqual(["Harbor site", "Harbor phone", "Harbor deck", "Harbor numbers"]);
  });

  it("holding one plate does not clear the other three", async () => {
    const onHold = vi.fn(async (req: { kind: string; title: string }) => {
      expect(req.kind).toBe("website");
      expect(req.title).toBe("Harbor site");
      return { surfaces: [{ ...HELD[0], title: "Harbor site" }] };
    });
    await render(<ProjectSuite projectId="p1" projectName="Harbor" initial={[]} onHold={onHold} />);
    expect($$("[data-testid^=suite-plate-]")).toHaveLength(4);
    await type('[data-testid="suite-title-input-website"]', "Harbor site");
    await click('[data-testid="suite-hold-website"]');
    expect($('[data-testid="suite-title-website"]')?.textContent).toBe("Harbor site");
    for (const kind of ["mobile", "slides", "data_view"]) {
      expect($(`[data-testid="suite-status-${kind}"]`)?.textContent).toBe("Not held");
    }
  });

  it("loads the suite and still keeps the other plates when publish returns one", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/suite") && (!init?.method || init.method === "GET")) {
        return new Response(JSON.stringify({
          project_id: "p1", project_name: "Harbor", surfaces: HELD,
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (url.endsWith("/publish")) {
        const body = JSON.parse(String(init?.body ?? "{}"));
        expect(body.mode).toBe("one");
        expect(body.kind).toBe("website");
        return new Response(JSON.stringify({
          surfaces: [{ ...HELD[0], status: "published", published_revision: 1 }],
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      return new Response(JSON.stringify({ message: "Missing." }), { status: 500 });
    });
    vi.stubGlobal("fetch", fetchMock);
    await render(<ProjectSuite projectId="p1" />);
    expect($('[data-testid="suite-title-mobile"]')?.textContent).toBe("Harbor phone");
    await click('[data-testid="suite-publish-website"]');
    expect(titles()).toEqual(["Harbor site", "Harbor phone", "Harbor deck", "Harbor numbers"]);
    expect($('[data-testid="suite-status-data_view"]')?.textContent).toBe("Held");
  });
});
