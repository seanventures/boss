// @vitest-environment happy-dom
/**
 * BOSSAPP-1193: a BOSS volume made before the cleanup labels existed cannot be judged by the
 * Infra Agent; the Storage card names it instead of leaving it silent. An older API without the
 * `volumes` reading draws nothing and never crashes.
 */
import React from "react";
import { afterEach, describe, expect, it } from "vitest";

import { StorageCardView, relabelWords } from "../app/infra/storage-card";
import { parseSummary } from "../lib/infra-storage";
import { $, mount, unmount } from "./_helpers/live-render";

afterEach(() => unmount());

const noop = { onHost: () => {}, onClean: () => {}, onRule: () => {}, cleaning: false };
const wire = { host_id: "h", disk: { total_gb: 100, free_gb: 50 } };

describe("volumes that need a relabel on the Storage card", () => {
  it("names them from the summary's volume reading", async () => {
    const s = parseSummary({ ...wire, volumes: { reclaimable_gb: 2.5, needs_relabel: 2 } })!;
    expect(s.volumes).toEqual({ reclaimableGb: 2.5, needsRelabel: 2 });
    await mount(<StorageCardView s={s} projectId="p1" {...noop} />);
    expect($('[data-testid="sc-relabel-chip"]')!.textContent).toContain("2 unlabelled BOSS volumes — need relabel");
  });

  it("draws nothing when none need it, or the API does not send the reading", async () => {
    const none = parseSummary({ ...wire, volumes: { reclaimable_gb: 0, needs_relabel: 0 } })!;
    await mount(<StorageCardView s={none} projectId="p1" {...noop} />);
    expect($('[data-testid="sc-relabel"]')).toBeNull();
    unmount();
    const old = parseSummary(wire)!;
    expect(old.volumes).toBeNull();
    await mount(<StorageCardView s={old} projectId="p1" {...noop} />);
    expect($('[data-testid="sc-relabel"]')).toBeNull();
    expect(parseSummary({ ...wire, volumes: { needs_relabel: "x" } })!.volumes).toEqual({ reclaimableGb: 0, needsRelabel: 0 });
    expect(relabelWords(1)).toBe("1 unlabelled BOSS volume — needs relabel");
  });
});
