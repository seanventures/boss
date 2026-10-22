import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { KnowledgePromotionQueue } from "../app/knowledge-promotion-ui";

describe("knowledge promotion queue", () => {
  it("shows the fact and both decisions", () => {
    const html = renderToStaticMarkup(
      createElement(KnowledgePromotionQueue, {
        projectId: "proj",
        actorId: "ceo",
        initial: [{ id: "fact-1", node_key: "fact:a|b|c", body: "frontend checks catch review rounds" }],
      }),
    );
    expect(html).toContain("frontend checks catch review rounds");
    expect(html).toContain("Approve fact");
    expect(html).toContain("Reject fact");
    expect(html).toContain("knowledge-promotion");
  });
});
