import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { PreviewRec } from "@boss/api-client";

import { emptyPreviewFeed, operatorPreviewOpenHref } from "../lib/code-tasks";
import {
  PREVIEW_EXPIRED_409,
  PREVIEW_FIXTURE_BADGE,
  PREVIEW_FIXTURE_CTA,
  PREVIEW_FIXTURE_OPEN_LABEL,
  PREVIEW_HOW_TO_REAL_VIEW,
  PREVIEW_REAL_EMPTY_COPY,
  PREVIEW_ZERO_REAL_RUNNING,
  inventoryPreviews,
  isFixturePreview,
  isRealRunningPreview,
  previewFailClosedReason,
  previewOpenLabel,
} from "../lib/previews";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
}));

vi.mock("../app/actions", () => ({
  createPreviewFixtureAction: vi.fn(),
}));

vi.mock("../app/submit-button", () => ({
  SubmitButton: (props: { children: React.ReactNode; className?: string }) => (
    <button type="submit" className={props.className} data-testid="preview-fixture-cta">
      {props.children}
    </button>
  ),
}));

const { PreviewsHonesty } = await import("../app/previews-ui");

function preview(partial: Partial<PreviewRec> & Pick<PreviewRec, "id" | "status">): PreviewRec {
  return {
    url: null,
    open_url: null,
    mode: null,
    ...partial,
  };
}

const fixtureRunning = preview({
  id: "prev_fix",
  status: "running",
  mode: "fixture",
  fixture: true,
  open_url: "https://boss-api.local.sean.network/v1/previews/prev_fix/web/",
});

const realRunning = preview({
  id: "prev_app",
  status: "running",
  mode: "process",
  open_url: "https://boss-api.local.sean.network/v1/previews/prev_app/web/",
});

const expiredBound = preview({
  id: "prev_exp",
  status: "expired",
  mode: "process",
  open_url: "https://boss-api.local.sean.network/v1/previews/prev_exp/web/",
});

function html(node: React.ReactElement) {
  return renderToStaticMarkup(node);
}

describe("preview classification (fixture vs real)", () => {
  it("treats mode=fixture and fixture=true as fixtures, never real running", () => {
    expect(isFixturePreview(fixtureRunning)).toBe(true);
    expect(isRealRunningPreview(fixtureRunning)).toBe(false);
    expect(isFixturePreview(realRunning)).toBe(false);
    expect(isRealRunningPreview(realRunning)).toBe(true);
    expect(isFixturePreview({ mode: "fixture" })).toBe(true);
    expect(isFixturePreview({ fixture: true, mode: "process" })).toBe(true);
  });

  it("splits inventory so fixture running does not count as a real app View", () => {
    const inventory = inventoryPreviews([fixtureRunning, realRunning, expiredBound]);
    expect(inventory.realRunningCount).toBe(1);
    expect(inventory.fixtureCount).toBe(1);
    expect(inventory.expiredCount).toBe(1);
    expect(inventory.realRunning.map((p) => p.id)).toEqual(["prev_app"]);
    expect(inventory.fixtures.map((p) => p.id)).toEqual(["prev_fix"]);
  });

  it("uses honest fixture copy and never the generic Open preview label", () => {
    expect(previewOpenLabel(fixtureRunning)).toBe(PREVIEW_FIXTURE_OPEN_LABEL);
    expect(previewOpenLabel(realRunning)).toBe("Open preview");
    expect(PREVIEW_FIXTURE_OPEN_LABEL).toContain("not a customer app");
    expect(PREVIEW_FIXTURE_BADGE).toBe("fixture — not a sandboxed app");
    expect(PREVIEW_REAL_EMPTY_COPY).toContain("not a customer app");
    expect(PREVIEW_HOW_TO_REAL_VIEW).toContain("BOSS_MICROVM_CLAIMS_FROZEN");
    expect(PREVIEW_HOW_TO_REAL_VIEW).toContain("successful code-task preview run");
  });

  it("fail-closes expired View (409) even when a historical open_url exists", () => {
    expect(previewFailClosedReason(expiredBound)).toBe(PREVIEW_EXPIRED_409);
    expect(operatorPreviewOpenHref(expiredBound)).toBeNull();
    expect(operatorPreviewOpenHref(fixtureRunning)).toContain("/v1/previews/prev_fix/web/");
    expect(operatorPreviewOpenHref(realRunning)).toContain("/v1/previews/prev_app/web/");
  });
});

describe("Mission Control Previews honesty", () => {
  it("shows honest empty + how-to when 0 real running, even if a fixture exists", () => {
    const markup = html(
      <PreviewsHonesty
        feed={{ ...emptyPreviewFeed(), apiReachable: true, items: [fixtureRunning] }}
        projectId="boss-dogfood"
        actor="ceo"
      />,
    );
    expect(markup).toContain(PREVIEW_REAL_EMPTY_COPY);
    expect(markup).toContain(PREVIEW_HOW_TO_REAL_VIEW);
    expect(markup).toContain(PREVIEW_ZERO_REAL_RUNNING);
    expect(markup).toContain(PREVIEW_FIXTURE_BADGE);
    expect(markup).toContain(PREVIEW_FIXTURE_OPEN_LABEL);
    expect(markup).toContain(PREVIEW_FIXTURE_CTA);
    expect(markup).not.toContain("Open preview ↗");
    expect(markup).toContain("0 real running · 1 fixture");
    expect(markup).not.toContain("View opens a live app");
  });

  it("lists a real running app separately from fixture HTML", () => {
    const markup = html(
      <PreviewsHonesty
        feed={{ ...emptyPreviewFeed(), apiReachable: true, items: [fixtureRunning, realRunning] }}
        projectId="boss-dogfood"
        actor="ceo"
      />,
    );
    expect(markup).toContain("prev_app");
    expect(markup).toContain("Open preview");
    expect(markup).toContain(PREVIEW_FIXTURE_OPEN_LABEL);
    expect(markup).not.toContain(PREVIEW_REAL_EMPTY_COPY);
    expect(markup).toContain('data-testid="preview-row-real"');
    expect(markup).toContain('data-testid="preview-row-fixture"');
  });

  it("shows 409 fail-closed copy for expired rows and does not offer Open", () => {
    const markup = html(
      <PreviewsHonesty
        feed={{ ...emptyPreviewFeed(), apiReachable: true, items: [expiredBound] }}
        projectId="boss-dogfood"
        actor="ceo"
      />,
    );
    expect(markup).toContain(PREVIEW_EXPIRED_409);
    expect(markup).toContain('data-preview-view="fail-closed"');
    expect(markup).not.toContain(`href="${expiredBound.open_url}"`);
    expect(markup).toContain(PREVIEW_ZERO_REAL_RUNNING);
    expect(markup).toContain(PREVIEW_FIXTURE_CTA);
  });

  it("wires Mission Control Previews + fixture POST without claiming a live app", () => {
    const root = join(dirname(fileURLToPath(import.meta.url)), "..");
    const page = readFileSync(join(root, "app/page.tsx"), "utf8");
    expect(page).toContain("<PreviewsPanel");
    expect(page).toContain("fixture HTML is not a customer app");
    const actions = readFileSync(join(root, "app/actions.ts"), "utf8");
    expect(actions).toContain("/v1/previews/fixture");
    expect(actions).toContain("createPreviewFixtureAction");
  });

  it("surfaces list failures instead of implying 0 running success", () => {
    const markup = html(
      <PreviewsHonesty
        feed={emptyPreviewFeed("API returned 503")}
        projectId="boss-dogfood"
        actor="ceo"
      />,
    );
    expect(markup).toContain("Preview list unavailable — API returned 503");
    expect(markup).toContain(PREVIEW_REAL_EMPTY_COPY);
  });
});
