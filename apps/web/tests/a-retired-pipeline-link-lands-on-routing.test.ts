/**
 * The Coding Pipeline page folded into Setup › Model routing (docs/product/MODEL_ROUTER.md):
 * a link to `?area=pipeline` lands on the routing row, keeping the org and the project.
 */
import { describe, expect, it } from "vitest";

import { resolveLegacyArea } from "../app/legacy-areas";

describe("?area=pipeline", () => {
  it("lands on Setup › Model routing", () => {
    const next = resolveLegacyArea(new URLSearchParams("org=org_boss&project_id=p&area=pipeline"));
    expect(next?.get("area")).toBe("setup");
    expect(next?.get("focus")).toBe("routing");
    expect(next?.get("org")).toBe("org_boss");
    expect(next?.get("project_id")).toBe("p");
  });
});
