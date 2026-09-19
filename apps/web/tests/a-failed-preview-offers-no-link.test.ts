import { describe, expect, it } from "vitest";

import { operatorPreviewOpenHref } from "../lib/code-tasks";
import { previewRecordFromStepOutput } from "../lib/previews";

/**
 * A preview that never served must not be offered as a link.
 *
 * The interview-scorecard run built its app, opened PR #208, and then failed to preview it:
 * `vite build` ran at a monorepo root with no entry module, nothing listened, and the preview
 * row was written `status=failed, failure_kind=port_timeout`. The workflow step still showed
 * "Open preview", and the operator clicked through to a dead /v1/previews/<id>/web/.
 *
 * operatorPreviewOpenHref had refused failed previews all along. The step defeated it by
 * rebuilding the record by hand with a hardcoded `status: "running"`.
 */

const FAILED_STEP_OUTPUT = {
  tool: "start_preview",
  status: "awaiting",
  capability: "preview",
  preview_id: "prev_4fd4ac8467c64868bdc2",
  preview: {
    id: "prev_4fd4ac8467c64868bdc2",
    status: "failed",
    failure_kind: "port_timeout",
    error: 'app did not serve HTTP within timeout. Could not resolve entry module "index.html".',
  },
};

describe("a failed preview offers no link", () => {
  it("keeps the failure the tool recorded instead of assuming it is running", () => {
    expect(previewRecordFromStepOutput(FAILED_STEP_OUTPUT)?.status).toBe("failed");
  });

  it("produces no href for it, so no Open preview button can be rendered", () => {
    const record = previewRecordFromStepOutput(FAILED_STEP_OUTPUT);
    expect(record).not.toBeNull();
    expect(operatorPreviewOpenHref(record!)).toBeNull();
  });

  it("still links a preview that is genuinely running", () => {
    const record = previewRecordFromStepOutput({
      preview: { id: "prev_live", status: "running" },
    });
    expect(operatorPreviewOpenHref(record!)).toBe("/v1/previews/prev_live/web/");
  });

  it("prefers the API-resolved live_preview over the recorded one", () => {
    const record = previewRecordFromStepOutput({
      live_preview: { id: "prev_live", status: "expired", url: null },
      preview: { id: "prev_stale", status: "running" },
    });
    expect(record?.id).toBe("prev_live");
    expect(operatorPreviewOpenHref(record!)).toBeNull();
  });

  it("treats an unrecorded status as running, which is what it meant before", () => {
    const record = previewRecordFromStepOutput({ preview: { id: "prev_x" } });
    expect(record?.status).toBe("running");
  });
});
