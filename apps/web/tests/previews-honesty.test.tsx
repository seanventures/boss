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
  // The preview row now carries the approve gate. These honesty tests are about what the row
  // SAYS, not about approving, so the gate resolves to nothing and the control renders null —
  // which is also the correct real-world behaviour for a preview with no merge decision.
  approvePreviewAction: vi.fn(),
  previewGateAction: vi.fn(async () => ({ ok: false })),
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
  open_url: "https://boss-api.local.example.test/v1/previews/prev_fix/web/",
});

const realRunning = preview({
  id: "prev_app",
  status: "running",
  mode: "process",
  open_url: "https://boss-api.local.example.test/v1/previews/prev_app/web/",
});

const expiredBound = preview({
  id: "prev_exp",
  status: "expired",
  mode: "process",
  open_url: "https://boss-api.local.example.test/v1/previews/prev_exp/web/",
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

  it("does NOT put the operator fixture panel on the customer's Factory page", () => {
    // It described itself accurately — "Operator Views — fixture HTML is not a customer app",
    // "Create fixture View (not a customer app)" — and that is the point: a harness for
    // proving the View gateway without a microVM does not belong on the page a customer opens
    // to look at their apps. On goldeneye it had become 47 expired rows, each repeating
    // "cannot approve: preview status is 'expired'", under a heading about fixtures.
    //
    // Preview itself is untouched. Open preview, the status chip, Startup logs and Rebuild
    // preview are on the app's own detail bar and always were — that is where an action about
    // an app belongs, next to the app.
    const root = join(dirname(fileURLToPath(import.meta.url)), "..");
    const page = readFileSync(join(root, "app/page.tsx"), "utf8");
    expect(page).not.toContain("<PreviewsPanel");
    expect(page).not.toContain("fixture HTML is not a customer app");

    // The endpoint stays, because the harness is still worth having — just not there.
    const actions = readFileSync(join(root, "app/actions.ts"), "utf8");
    expect(actions).toContain("/v1/previews/fixture");
    expect(actions).toContain("createPreviewFixtureAction");
  });

  it("moves the one action that would otherwise have been lost", () => {
    // "Approve & ship" existed ONLY in that panel. Removing the section without moving it
    // would have deleted the merge-and-deploy control from the console.
    const root = join(dirname(fileURLToPath(import.meta.url)), "..");
    const ui = readFileSync(join(root, "app/code-tasks-ui.tsx"), "utf8");
    expect(ui).toContain('import { ApprovePreview } from "./previews-ui"');
    expect(ui).toContain("<ApprovePreview preview={live} compact />");
    // Beside the PR it merges, not in a second place further down the page.
    const bar = ui.slice(ui.indexOf('data-testid="ct-detail-actionbar"'),
                         ui.indexOf('data-testid="ct-detail-ask"'));
    expect(bar).toContain("ApprovePreview");
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

describe("the actions moved without moving their prose", () => {
it("does not drag a paragraph into the button bar with it", () => {
    // The effect line names every app the merge would deploy. On a full-width panel that reads
    // fine; in an action bar it is a sentence hundreds of pixels long that pushed everything
    // after it off the right of the screen — which is what happened when it was first moved.
    const root = join(dirname(fileURLToPath(import.meta.url)), "..");
    const previews = readFileSync(join(root, "app/previews-ui.tsx"), "utf8");
    expect(previews).toContain("if (compact) {");
    // Still knowable BEFORE the click — it moves to the tooltip, it is not dropped.
    expect(previews).toContain("title={effect}");
    expect(previews).toContain('data-testid="preview-approve-count"');
  });

  it("shows one preview per run, not one per process", () => {
    // A run has one app, so two "Open preview" buttons side by side ask the operator to choose
    // between things that are the same. A rebuild that leaves the old process running produced
    // exactly that.
    const root = join(dirname(fileURLToPath(import.meta.url)), "..");
    const ui = readFileSync(join(root, "app/code-tasks-ui.tsx"), "utf8");
    expect(ui).toContain("const shown = livePreviews.slice(0, 1);");
    expect(ui).toContain(".slice(0, 1)}");
  });

  it("puts Retry at the top with the other actions", () => {
    const root = join(dirname(fileURLToPath(import.meta.url)), "..");
    const ui = readFileSync(join(root, "app/code-tasks-ui.tsx"), "utf8");
    const bar = ui.slice(ui.indexOf('data-testid="ct-detail-actionbar"'),
                         ui.indexOf('data-testid="ct-detail-ask"'));
    expect(bar).toContain("<RetryControls runId={active.id} compact");
    // Every state the lower one covered, so removing that one loses nothing — including
    // `completed`, where a retry is how a shipped run clears a remaining ✗.
    expect(bar).toContain('active.status !== "running" && active.status !== "queued"');
  });

  it("does not then repeat preview and retry further down the same page", () => {
    // Two controls that do the same thing, in two places, and the operator has to work out
    // whether they differ. They do not.
    const root = join(dirname(fileURLToPath(import.meta.url)), "..");
    const ui = readFileSync(join(root, "app/code-tasks-ui.tsx"), "utf8");
    expect(ui).toContain('<div className="ct-block-title">Iterate</div>');
    // Exactly one mount of each in the detail page: the action bar's.
    expect(ui.match(/<PreviewControls/g)?.length).toBe(1);
    expect(ui.match(/<RetryControls/g)?.length).toBe(1);
  });

  it("stops any child forcing the bar wider than its column", () => {
    // A bar whose width depends on the longest string a child happens to render will break
    // again the next time one is added.
    const root = join(dirname(fileURLToPath(import.meta.url)), "..");
    const css = readFileSync(join(root, "app/globals.css"), "utf8");
    expect(css).toContain(".ct-detail-actionbar, .ct-detail-actions { min-width: 0; max-width: 100%; }");
    expect(css).toContain(".ct-detail-actions > * { min-width: 0; max-width: 100%; }");
  });
});

describe("a preview that is still building says so", () => {
  const CT = readFileSync(
    join(dirname(fileURLToPath(import.meta.url)), "..", "app/code-tasks-ui.tsx"), "utf8");

  it("shows the starting preview instead of hiding it", () => {
    // Until the attempt finished there was no row at all — nothing in the list, nothing to
    // poll. Now there is one from the first moment, so the filter has to admit it.
    expect(CT).toContain('p.status === "starting"');
  });

  it("does not offer a link to something that is not serving yet", () => {
    // A link that 404s is worse than no link.
    expect(CT).toContain('data-testid="preview-building"');
    expect(CT).toContain("Building preview…");
  });

  it("shows what is done and what is pending, like a deploy does", () => {
    expect(CT).toContain('data-testid="preview-steps"');
    expect(CT).toContain('{p.steps!.filter((x) => x.status === "ok").length}/{p.steps!.length} steps');
  });

  it("distinguishes done, failed, skipped and running in the ledger", () => {
    // A skipped substrate is a fact worth showing — `auto` tries three, and "could not start"
    // could not say whether one was tried or all of them.
    expect(CT).toContain('x.status === "ok" ? "✓" : x.status === "failed" ? "✗"');
    expect(CT).toContain('x.status === "skipped" ? "–" : "…"');
  });
});
