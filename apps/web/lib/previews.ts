import type { PreviewRec } from "@boss/api-client";

import { operatorPreviewOpenHref, type PreviewFeed } from "./code-tasks";

export const PREVIEW_FIXTURE_MODE = "fixture";

/** Same badge the fixture HTML serves. Never imply this is a customer app. */
export const PREVIEW_FIXTURE_BADGE = "fixture — not a sandboxed app";

export const PREVIEW_FIXTURE_OPEN_LABEL = "Open fixture View (not a customer app)";

export const PREVIEW_REAL_OPEN_LABEL = "Open preview";

export const PREVIEW_VERIFIED_OPEN_LABEL = "View verified preview";

export const PREVIEW_FIXTURE_CTA = "Create fixture View (not a customer app)";

export const PREVIEW_ZERO_REAL_RUNNING = "0 real running app Views";

export const PREVIEW_REAL_EMPTY_COPY =
  "0 real running app Views. Fixture HTML is not a customer app.";

export const PREVIEW_HOW_TO_REAL_VIEW =
  "A real View requires sandbox claims unfrozen (BOSS_MICROVM_CLAIMS_FROZEN=false) and a successful code-task preview run. Do not treat a fixture View as a sandboxed app.";

export const PREVIEW_EXPIRED_409 =
  "preview not running (status=expired) — View returns 409";

export function isFixturePreview(preview: {
  mode?: string | null;
  fixture?: boolean | null;
}): boolean {
  return preview.fixture === true || (preview.mode || "").toLowerCase() === PREVIEW_FIXTURE_MODE;
}

export function isRealRunningPreview(preview: PreviewRec): boolean {
  return (preview.status || "").toLowerCase() === "running" && !isFixturePreview(preview);
}

export function previewFailClosedReason(
  preview: Pick<PreviewRec, "status">,
): string | null {
  const status = (preview.status || "").toLowerCase();
  if (status === "expired") return PREVIEW_EXPIRED_409;
  if (status === "stopped") return "preview not running (status=stopped) — View returns 409";
  if (status === "failed") return "preview not running (status=failed) — View returns 409";
  return null;
}

export function previewOpenLabel(preview: PreviewRec): string {
  if (isFixturePreview(preview)) return PREVIEW_FIXTURE_OPEN_LABEL;
  if ((preview.status || "").toLowerCase() === "verified") return PREVIEW_VERIFIED_OPEN_LABEL;
  return PREVIEW_REAL_OPEN_LABEL;
}

export type PreviewInventory = {
  realRunning: PreviewRec[];
  fixtures: PreviewRec[];
  expired: PreviewRec[];
  stopped: PreviewRec[];
  failed: PreviewRec[];
  other: PreviewRec[];
  realRunningCount: number;
  fixtureCount: number;
  expiredCount: number;
};

export function inventoryPreviews(items: PreviewRec[]): PreviewInventory {
  const realRunning: PreviewRec[] = [];
  const fixtures: PreviewRec[] = [];
  const expired: PreviewRec[] = [];
  const stopped: PreviewRec[] = [];
  const failed: PreviewRec[] = [];
  const other: PreviewRec[] = [];
  for (const item of items) {
    if (isFixturePreview(item)) {
      fixtures.push(item);
      continue;
    }
    const status = (item.status || "").toLowerCase();
    if (status === "running") realRunning.push(item);
    else if (status === "expired") expired.push(item);
    else if (status === "stopped") stopped.push(item);
    else if (status === "failed") failed.push(item);
    else other.push(item);
  }
  return {
    realRunning,
    fixtures,
    expired,
    stopped,
    failed,
    other,
    realRunningCount: realRunning.length,
    fixtureCount: fixtures.length,
    expiredCount: expired.length,
  };
}

export function previewOperatorHref(preview: PreviewRec): string | null {
  if (previewFailClosedReason(preview)) return null;
  return operatorPreviewOpenHref(preview);
}

/**
 * The preview record a workflow step recorded, as a thing with a REAL status.
 *
 * A step carries its preview in one of two shapes: `live_preview`, resolved per-step by the
 * API, or the nested `{preview: {...}}` that the start_preview tool wrote. The caller used to
 * build the second one by hand and hardcode `status: "running"`, which quietly defeated
 * operatorPreviewOpenHref's refusal of expired/stopped/failed previews: a preview that died
 * with "Could not resolve entry module index.html" still rendered a confident "Open preview"
 * button, and the operator opened a dead URL.
 *
 * Exported and pure so it can be tested as itself. The console's own tests are source greps,
 * which pass whether or not this rule is right.
 */
export function previewRecordFromStepOutput(
  output: Record<string, unknown>,
): { id: string; status: string; url: string | null } | null {
  const live = output.live_preview as
    { id: string; status: string; url: string | null } | undefined;
  if (live) return live;
  const nested = output.preview as
    { id?: string; url?: string; status?: string } | undefined;
  if (!nested?.id) return null;
  // Absent means "not recorded yet", which is the only case that may assume it is running.
  return { id: nested.id, status: String(nested.status || "running"), url: nested.url ?? null };
}

export type { PreviewFeed };
