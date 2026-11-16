// @vitest-environment happy-dom
/**
 * People in the window are a facet. A blank actor is not one.
 * The rail lists them, and choosing one writes the actor token the bar already uses.
 * Typing `actor:` suggests those same people, once each.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FacetRail } from "../app/activity/facet-rail";
import { QueryBar } from "../app/activity/query-bar";
import { mapActivityFacets, type ActivityFacets } from "../lib/activity-log";
import type { LogToken } from "../lib/log-url";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLElement | null = null;
afterEach(() => { act(() => root?.unmount()); host?.remove(); root = null; host = null; });

const FACETS: ActivityFacets = {
  levels: {}, sources: {}, statuses: {}, services: {},
  agents: [], agentsMore: 0, kinds: [], runs: [],
  actors: [{ actor: "sam", n: 2 }, { actor: "riley", n: 1 }],
  total: 3, exact: true,
};

function mount(node: React.ReactElement) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => { root!.render(node); });
}

const actorRows = () => [...host!.querySelectorAll<HTMLElement>('[data-key="actor"]')];

describe("people on the activity rail", () => {
  it("copies each person and skips a blank name", () => {
    const facets = mapActivityFacets({
      levels: {}, sources: {}, statuses: {}, services: {},
      actors: [
        { actor: "sam", n: 2 },
        { actor: "   ", n: 9 },
        { actor: "", n: 4 },
        "boss",
        { actor: "riley", n: 1 },
        { n: 7 },
      ],
      total: 3, exact: true,
    });
    expect(facets!.actors).toEqual([{ actor: "sam", n: 2 }, { actor: "riley", n: 1 }]);
    expect(mapActivityFacets({})!.actors).toEqual([]);
  });

  it("lists each person, and a click writes the actor token", () => {
    const onTokens = vi.fn();
    mount(<FacetRail facets={FACETS} tokens={[]} showVerbose={false} onTokens={onTokens} onVerbose={() => {}} />);
    const rows = actorRows();
    expect(rows.map((row) => row.getAttribute("data-value"))).toEqual(["sam", "riley"]);
    expect(rows[0].querySelector(".lx-count")?.textContent).toBe("2");
    act(() => { rows[0].querySelector("button")!.click(); });
    expect(onTokens).toHaveBeenCalledWith([{ key: "actor", value: "sam" }]);
  });

  it("choosing another person replaces the actor token", () => {
    const onTokens = vi.fn();
    const tokens: LogToken[] = [{ key: "actor", value: "sam" }];
    mount(<FacetRail facets={FACETS} tokens={tokens} showVerbose={false} onTokens={onTokens} onVerbose={() => {}} />);
    const rows = actorRows();
    expect(rows.map((row) => row.getAttribute("data-value"))).toEqual(["sam", "riley"]);
    expect(rows[0].querySelector("button")!.getAttribute("aria-pressed")).toBe("true");
    act(() => { rows[1].querySelector("button")!.click(); });
    expect(onTokens).toHaveBeenCalledWith([{ key: "actor", value: "riley" }]);
  });

  it("actor: suggests each person once", () => {
    const onAdd = vi.fn();
    mount(<QueryBar tokens={[]} q="" labels={{}} names={{}} facets={FACETS} error=""
                     onTokens={() => {}} onQuery={() => {}} onAdd={onAdd} />);
    const input = host!.querySelector("[data-testid=lx-query-input]") as HTMLInputElement;
    act(() => {
      input.focus();
      input.dispatchEvent(new FocusEvent("focus"));
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "actor:");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const offered = [...host!.querySelectorAll<HTMLButtonElement>("[data-testid=lx-suggest] button")];
    expect(offered.map((button) => button.querySelector("[data-user-text]")?.textContent)).toEqual(["sam", "riley"]);
    expect(offered.map((button) => button.querySelector(".lx-count")?.textContent)).toEqual(["2", "1"]);
    act(() => { offered[0].dispatchEvent(new MouseEvent("mousedown", { bubbles: true })); });
    expect(onAdd).toHaveBeenCalledTimes(1);
    expect(onAdd).toHaveBeenCalledWith({ key: "actor", value: "sam" });
  });
});
