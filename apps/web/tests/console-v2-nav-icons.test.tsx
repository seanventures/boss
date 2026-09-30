import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { NavIcon } from "../components/v2/NavIcon";
import { V2_NAV } from "../components/v2/Shell";

describe("nav icons", () => {
  it("every nav item has one", () => {
    // A nav where some items have icons and some do not looks like the iconless ones failed to
    // load. All or none, and the operator asked for all.
    const missing = V2_NAV.filter(
      (item) => renderToStaticMarkup(<NavIcon id={item.id} />) === "",
    ).map((i) => i.id);
    expect(missing).toEqual([]);
  });

  it("draws nothing at all for an id it has no icon for", () => {
    // Rather than an empty 16px box, which reads as a broken image.
    expect(renderToStaticMarkup(<NavIcon id="not-a-screen" />)).toBe("");
  });

  it("inherits its colour so it dims and brightens with its label", () => {
    // Otherwise the active state needs a second set of rules that will drift.
    const svg = renderToStaticMarkup(<NavIcon id="hive" />);
    expect(svg).toContain('stroke="currentColor"');
    expect(svg).toContain('fill="none"');
  });

  it("is hidden from screen readers, because the label is right next to it", () => {
    expect(renderToStaticMarkup(<NavIcon id="hive" />)).toContain('aria-hidden="true"');
  });

  it("gives each screen a distinct glyph", () => {
    // Two identical icons are worse than none: they say the screens are the same kind of thing.
    const drawn = V2_NAV.map((i) => renderToStaticMarkup(<NavIcon id={i.id} />));
    expect(new Set(drawn).size).toBe(V2_NAV.length);
  });
});
