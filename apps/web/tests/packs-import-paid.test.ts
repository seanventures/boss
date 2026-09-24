import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { PaidSurfaceGate } from "../app/edition-ui-paid";

describe("paid marketplace visibility", () => {
  it("keeps hosted marketplace hidden on Community", () => {
    const html = renderToStaticMarkup(
      React.createElement(PaidSurfaceGate, {
        editionId: "community",
        surface: "marketplace",
        title: "Hosted marketplace catalog",
        children: React.createElement(
          "article",
          { "data-testid": "hosted-marketplace-card" },
          "Sync hosted catalog",
        ),
      }),
    );
    expect(html).toContain("paid-surface-hidden-marketplace");
    expect(html).not.toContain("hosted-marketplace-card");
    expect(html).not.toContain("Sync hosted catalog");
  });
});
