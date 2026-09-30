import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { ShepherdConsole } from "../app/shepherd-ui";
import { codeOnly } from "./_helpers/source";

/**
 * A single endpoint having a bad minute must not take the whole console down.
 *
 * The page loads its feeds with `.catch(() => null)` per feed, and the comment there says
 * plainly: "one dead panel must never take the console with it". That guarantee was false.
 * These actions RETURN `{error: "overview 500"}` instead of throwing, so the catch never fires
 * and the error object arrives at the panel dressed as data. It is truthy, so the "still
 * loading" guard waves it through, and the first property read off the payload throws:
 *
 *     Cannot read properties of undefined (reading 'productive')
 *
 * The whole console went white because one feed answered badly.
 */
const SHEPHERD = codeOnly("app/shepherd-ui.tsx");
const DELIVERY = codeOnly("app/delivery-ui.tsx");
const ACTIONS = codeOnly("app/actions.ts");

// This suite compiles JSX with the classic runtime, which needs React in scope at call time.
(globalThis as Record<string, unknown>).React = React;

/** Render the panel with a payload, the way the page does. */
const render = (initial: unknown) =>
  renderToStaticMarkup(<ShepherdConsole initial={initial as never} />);

describe("the shepherd panel", () => {
  it("recognises an error payload instead of rendering it as data", () => {
    expect(SHEPHERD).toContain("function readOverview");
    expect(SHEPHERD).toContain("if (record.error) return { data: null, error: String(record.error) }");
  });

  it("treats a payload without the field the panel is about as unusable", () => {
    // Not an error, not data. Rendering it would put "productive $0.00" on screen, which is a
    // claim about the world from a panel whose entire job is saying whether spend is wasted.
    expect(SHEPHERD).toContain("came back without a spend summary");
  });

  it("uses the same reader for the refresh as for the first paint", () => {
    // The old `d as Overview` cast in load() was the other half of the bug: a refresh could
    // admit a shape the first render had rejected.
    expect(SHEPHERD).toContain("readOverview(await getShepherdOverviewAction())");
    expect(SHEPHERD).not.toContain("setData(d as Overview)");
  });

  it("still cannot throw on the spend read", () => {
    // Belt as well as braces: the reader guarantees `spend` exists, and this line is the one
    // that took the console down.
    expect(SHEPHERD).toContain("data.spend?.productive ?? 0");
    expect(SHEPHERD).toContain("data.spend?.unproductive ?? 0");
  });
});

describe("the delivery panel", () => {
  it("checks for an error payload before reading fields", () => {
    expect(DELIVERY).toContain("Delivery: {data.error}");
  });

  it("never maps targets that might not be there", () => {
    // Two of the three reads already defended themselves; the third did not, and a record with
    // `configured: true` and no targets is what a half-written one looks like.
    expect(DELIVERY).not.toMatch(/\{data\.targets\.map\(/);
    expect((DELIVERY.match(/\(data\.targets \|\| \[\]\)/g) ?? []).length).toBeGreaterThanOrEqual(3);
  });
});

describe("the actions these panels read", () => {
  it("still report failure as a value rather than throwing", () => {
    // Documenting the shape, not endorsing it: as long as they return `{error}`, every consumer
    // is responsible for recognising it, and this file is where that expectation is recorded.
    expect(ACTIONS).toContain("return { error: `overview ${res.status}` }");
  });
});

describe("the exact payloads, rendered", () => {
  it("shows the reason instead of throwing on the one that crashed", () => {
    // `{error: "overview 500"}` is what the action returns for any non-2xx, and what the page
    // passed straight into this panel.
    expect(render({ error: "overview 500" })).toContain("overview 500");
  });

  it("says a payload is unusable rather than inventing zeros", () => {
    const html = render({ ledger: [], escalations: [] });
    expect(html).toContain("without a spend summary");
    expect(html).not.toContain("$0.00");
  });

  it("renders a good payload unchanged", () => {
    // The guard must not cost the panel its actual job.
    const html = render({
      class_names: {}, reasoning: [], ledger: [], escalations: [], calibration: [],
      spend: { productive: 5, unproductive: 1 },
    });
    expect(html).toContain("$5.00 productive");
  });

  it("still says it is loading when there is genuinely nothing yet", () => {
    // null and "arrived broken" are different states and must not collapse into one.
    expect(render(null)).toContain("Loading shepherd…");
  });
});
