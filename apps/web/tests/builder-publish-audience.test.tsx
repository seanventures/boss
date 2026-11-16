// @vitest-environment happy-dom
/**
 * The visitor plate is not the bench preview (BOSSAPP-1243).
 * Seam: PublishAudience and visitorDoor. Pending has no visitor link.
 * An approved link is never the live preview URL.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PublishAudience, visitorDoor, type VisitorPlate } from "../app/builder/publish-audience";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const PREVIEW = { id: "prev_bench", url: "http://127.0.0.1:4311/web/", title: "Harbor menu" };
const VISITOR = "/v1/visitors/vsnap_harbor";

let root: Root | null = null;
let host: HTMLElement | null = null;

async function settle() {
  for (let i = 0; i < 8; i += 1) {
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  }
}

async function render(node: React.ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => { root!.render(node); });
  await settle();
}

function $(sel: string) {
  return document.querySelector<HTMLElement>(sel);
}

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  document.body.innerHTML = "";
});

function plate(over: Partial<VisitorPlate> = {}): VisitorPlate {
  return {
    id: "vsnap_harbor",
    audience: "customers",
    approval_state: "pending",
    visitor_url: null,
    preview_url: PREVIEW.url,
    snapshot: { title: "Harbor menu", revision: 3 },
    is_live_preview: false,
    kind: "visitor_snapshot",
    ...over,
  };
}

describe("visitorDoor", () => {
  it("drops a pending plate and any url that is the live preview", () => {
    expect(visitorDoor(plate())).toBeNull();
    expect(visitorDoor(plate({ approval_state: "pending", visitor_url: VISITOR }))).toBeNull();
    expect(visitorDoor(plate({
      approval_state: "approved", visitor_url: PREVIEW.url,
    }))).toBeNull();
    expect(visitorDoor(plate({
      approval_state: "approved", visitor_url: VISITOR, is_live_preview: true,
    }))).toBeNull();
    expect(visitorDoor(plate({ approval_state: "approved", visitor_url: VISITOR }))).toBe(VISITOR);
  });
});

describe("PublishAudience", () => {
  it("keeps the bench preview off the visitor door while the plate is pending", async () => {
    await render(<PublishAudience preview={PREVIEW} plate={plate()} error={null} onPublish={() => {}} />);
    expect($('[data-testid="bench-preview"]')?.textContent).toContain(PREVIEW.url);
    expect($('[data-testid="bench-note"]')?.textContent).toBe("Not the visitor door.");
    expect($('[data-testid="approval-state"]')?.textContent).toBe("Pending");
    expect($('[data-testid="visitor-door"]')?.textContent).toContain("No visitor URL");
    expect($('[data-testid="visitor-door"] a')).toBeNull();
    expect($('[data-testid="wax-seal"]')?.getAttribute("data-state")).toBe("held");
    const hrefs = [...document.querySelectorAll("a")].map((a) => a.getAttribute("href"));
    expect(hrefs).not.toContain(PREVIEW.url);
  });

  it("opens a visitor link only after approval, and that link is not the preview", async () => {
    await render(<PublishAudience
      preview={PREVIEW}
      plate={plate({ approval_state: "approved", visitor_url: VISITOR })}
      error={null}
      onPublish={() => {}}
    />);
    const link = $('[data-testid="visitor-door"] a');
    expect($('[data-testid="approval-state"]')?.textContent).toBe("Approved");
    expect(link?.getAttribute("href")).toBe(VISITOR);
    expect(link?.getAttribute("href")).not.toBe(PREVIEW.url);
    expect($('[data-testid="wax-seal"]')?.getAttribute("data-state")).toBe("open");
    expect($('[data-testid="bench-preview"]')?.textContent).toContain(PREVIEW.url);
  });

  it("seals without the approval mark as a pending publish", async () => {
    const onPublish = vi.fn();
    await render(<PublishAudience preview={PREVIEW} plate={null} error={null} onPublish={onPublish} />);
    const audience = $('[data-testid="audience"]') as HTMLSelectElement;
    await act(async () => {
      audience.value = "staff";
      audience.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await act(async () => {
      $('[data-testid="publish-plate"]')!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(onPublish).toHaveBeenCalledWith({ audience: "staff", approved: false });
    const approved = $('[data-testid="approved"]') as HTMLInputElement;
    await act(async () => { approved.click(); });
    await act(async () => {
      $('[data-testid="publish-plate"]')!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    });
    expect(onPublish).toHaveBeenLastCalledWith({ audience: "staff", approved: true });
  });

  it("puts a long sentence in a paragraph and keeps tips to fourteen words", async () => {
    const error = "This plate could not be sealed because the audience is not one of the four visitor groups.";
    await render(<PublishAudience preview={PREVIEW} plate={plate()} error={error} onPublish={() => {}} />);
    const copy = $('[data-testid="plate-copy"]');
    expect(copy?.tagName).toBe("P");
    const sentences = (copy?.textContent ?? "").split(/[.!?]/).map((s) => s.trim()).filter(Boolean);
    expect(sentences.some((s) => s.split(/\s+/).length > 6)).toBe(true);
    const failure = $('[data-testid="publish-error"]');
    expect(failure?.tagName).toBe("P");
    expect(failure?.textContent).toBe(error);
    for (const el of document.querySelectorAll("[title]")) {
      const words = (el.getAttribute("title") ?? "").trim().split(/\s+/).filter(Boolean);
      expect(words.length).toBeLessThanOrEqual(14);
    }
    expect($('[data-testid="publish-plate"]')?.textContent).toBe("Seal this plate");
  });
});
