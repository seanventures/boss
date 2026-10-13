// @vitest-environment happy-dom
/** While a stream is selected, the other streams' counts read as what selecting them would add.
 *
 *  A deploy's log showed "6 lines" with Events 15 and Jobs 1 beside it, which read as a count
 *  that did not add up. The numbers were right (a facet counts what selecting it would show);
 *  the words were not. Also: one rating face per subject (Rate vs "Working as expected?"). */
import React from "react";
import { createRoot, type Root } from "react-dom/client";
import { act } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { FacetRail } from "../app/activity/facet-rail";
import type { ActivityFacets } from "../lib/activity-log";
import { codeOnly } from "./_helpers/source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => { act(() => root?.unmount()); host?.remove(); root = null; host = null; });

const FACETS: ActivityFacets = {
  levels: { error: 0, warn: 0, info: 6, verbose: 0 },
  sources: { event: 15, job: 1, deployment: 6, receipt: 0 },
  statuses: {}, services: {}, agents: [], agentsMore: 0, kinds: [], runs: [], total: 22, exact: true,
};

function mount(tokens: { key: string; value: string }[]) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => root!.render(<FacetRail facets={FACETS} tokens={tokens as never} showVerbose={false}
                                     onTokens={() => {}} onVerbose={() => {}} />));
}
const countOf = (src: string) =>
  host!.querySelector(`[data-testid="lx-src-${src}"] .lx-count`)?.textContent;

describe("stream counts", () => {
  it("with Deploys selected, Deploys counts its lines and the others say +N", () => {
    mount([{ key: "src", value: "deployment" }]);
    expect(countOf("deployment")).toBe("6");
    expect(countOf("event")).toBe("+15");
    expect(countOf("job")).toBe("+1");
  });

  it("with no stream selected, every count is plain", () => {
    mount([]);
    expect(countOf("event")).toBe("15");
  });
});

describe("one rating face per subject", () => {
  it("the closed button asks the subject's question, not a bare Rate", () => {
    const src = codeOnly("app/improve/rate-it.tsx");
    expect(src).toContain('{QUESTION[subject] ?? "Rate"}</button>');
  });
});
