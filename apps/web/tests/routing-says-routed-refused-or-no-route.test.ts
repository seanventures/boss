/**
 * ROUTING speaks one outcome language. The Models view sends the server's word (`Routed`,
 * `Denied`, `No route`) and the full list route a key (`selected` / `denied`); the walk found every
 * row reading "Routed", refusals included, because the row compared the word with a key.
 */
import { describe, expect, it } from "vitest";

import { outcomeOf, routeRow } from "../app/observability/routing";

describe("a routing outcome", () => {
  it.each([
    ["Routed", "selected", "Routed"], ["Denied", "denied", "Refused"], ["No route", "none", "No route"],
    ["selected", "selected", "Routed"], ["denied", "denied", "Refused"], [null, "none", "No route"],
  ])("%s → %s", (raw, key, word) => {
    expect(outcomeOf(raw as string | null)).toEqual({ key, word });
  });

  it("a list-route row with a denial reason is refused, with no source is no route", () => {
    expect(routeRow({ id: "a", denial_reason: "budget cap reached" }).outcome).toBe("denied");
    expect(routeRow({ id: "b", selected_source_id: "src_1" }).outcome).toBe("selected");
    expect(routeRow({ id: "c" }).outcome).toBe("none");
  });
});
