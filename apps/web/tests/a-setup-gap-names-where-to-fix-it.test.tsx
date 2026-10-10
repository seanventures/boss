// @vitest-environment happy-dom
/**
 * NO DEAD END: a step whose ability is not set up says where it is set up. "Needs Deploy set up"
 * carries a link that opens Infra › Fleet with the App target sheet (the Host sheet first when there
 * is no machine yet), and Infra opens the sheet the link names.
 */
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it } from "vitest";

import { FieldError } from "../app/workflows/editor/inspector/block-settings";
import { addFromUrl } from "../app/infra/fleet";
import { readInfraUrl } from "../lib/infra-view";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("a setup gap names where to fix it", () => {
  it("the step's problem carries its fix as a link", async () => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    await act(async () => {
      root.render(<FieldError field="ability" problems={[{ step_id: "deploy", field: "ability", message: "Needs Deploy set up",
        severity: "warning", fix_label: "Set up where it deploys", fix_href: "/?area=infra&view=fleet&add=target" }]} />);
    });
    const a = host.querySelector('[data-testid="wf-problem-fix"]')!;
    expect(a.textContent).toBe("Set up where it deploys");
    expect(a.getAttribute("href")).toBe("/?area=infra&view=fleet&add=target");
    expect(a.getAttribute("target")).toBe("_blank");
    act(() => root.unmount());
  });

  it("Infra reads ?add= and opens the right sheet", () => {
    expect(readInfraUrl("?area=infra&view=fleet&add=target").add).toBe("target");
    expect(addFromUrl("target", 2)).toBe("target");
    expect(addFromUrl("target", 0)).toBe("host");
    expect(addFromUrl("host", 0)).toBe("host");
    expect(addFromUrl("nonsense", 3)).toBeNull();
    expect(addFromUrl(null, 3)).toBeNull();
  });
});
