/**
 * The facets read returns at most 40 people. When the rail is holding 40, it
 * says "Showing 40 people." Fewer than 40 stays quiet, and it never claims
 * there are more people. The person rows stay the list the server sent.
 */
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { FacetRail } from "../app/activity/facet-rail";
import type { ActivityFacets } from "../lib/activity-log";

function facets(count: number): ActivityFacets {
  return {
    levels: {}, sources: {}, statuses: {}, services: {},
    agents: [], agentsMore: 0, kinds: [], runs: [],
    actors: Array.from({ length: count }, (_, i) => ({ actor: `person-${i + 1}`, n: i + 1 })),
    total: count, exact: true,
  };
}

function markup(count: number): string {
  return renderToStaticMarkup(
    <FacetRail facets={facets(count)} tokens={[]} showVerbose={false} onTokens={() => {}} onVerbose={() => {}} />,
  );
}

function actorValues(html: string): string[] {
  return [...html.matchAll(/data-key="actor" data-value="([^"]+)"/g)].map((match) => match[1]!);
}

describe("the activity rail says when forty people are showing", () => {
  it("says it is showing 40 people when the list has 40", () => {
    const html = markup(40);
    expect(html).toContain("Showing 40 people.");
    expect(html.match(/Showing 40 people\./g)).toEqual(["Showing 40 people."]);
    expect(html).not.toContain("more people");
    expect(actorValues(html)).toEqual(Array.from({ length: 40 }, (_, i) => `person-${i + 1}`));
  });

  it("says nothing about a cap when fewer than 40 people are listed", () => {
    for (const count of [0, 1, 39]) {
      const html = markup(count);
      expect(html, String(count)).not.toContain("Showing");
      expect(html, String(count)).not.toContain("more people");
      expect(html, String(count)).not.toContain("cap");
      expect(actorValues(html), String(count)).toEqual(
        Array.from({ length: count }, (_, i) => `person-${i + 1}`),
      );
    }
  });
});
