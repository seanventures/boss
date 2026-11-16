/**
 * The deploy row names who approved. The face stays; the name is words beside it, never an id
 * parked only in a title.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

import { Approver } from "../app/infra/deploy-row";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLElement | null = null;

async function render(node: React.ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => { root!.render(node); });
  return host;
}

afterEach(() => {
  act(() => root?.unmount());
  host?.remove();
  root = null;
  host = null;
  document.body.innerHTML = "";
});

const who = () => document.querySelector(".inf-dep-who");

describe("the deploy row names who approved", () => {
  it("names Ada", async () => {
    const el = await render(<Approver approver={{ actor: "Ada" }} />);
    expect(who()?.textContent).toContain("Ada");
    expect(el.querySelector(".ag-avatar")).not.toBeNull();
  });

  it("does not print a user id", async () => {
    const id = "usr_kdzhawzy7mxmp5hpb3bqlgqojs";
    const el = await render(<Approver approver={{ actor: id }} />);
    expect(who()?.textContent ?? "").not.toContain(id);
    expect(el.innerHTML).not.toContain(id);
    expect(el.querySelector(".ag-avatar")).not.toBeNull();
  });

  it("says you when the approver is the person reading the row", async () => {
    const id = "usr_kdzhawzy7mxmp5hpb3bqlgqojs";
    const el = await render(<Approver approver={{ actor: id }} viewer={id} />);
    expect(who()?.textContent).toContain("you");
    expect(el.innerHTML).not.toContain(id);
  });

  it("names the role when no actor was recorded", async () => {
    const el = await render(<Approver approver={{ roleKey: "deployment_manager" }} />);
    expect(who()?.textContent).toContain("Deployment Manager");
    expect(el.querySelector(".ag-avatar")).not.toBeNull();
  });

  it("renders no name when nobody approved", async () => {
    const el = await render(<Approver approver={null} />);
    expect(who()?.textContent ?? "").toBe("");
    expect(el.textContent ?? "").not.toMatch(/Ada|Deployment Manager|usr_|agt_/);
    expect(el.querySelector(".ag-avatar")).toBeNull();
  });
});
