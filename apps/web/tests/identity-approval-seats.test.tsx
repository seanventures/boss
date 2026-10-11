/**
 * Identity › Approval seats: each seat, its holders and the tiers it approves; holders change
 * through PUT /v1/identity/seats/{seat} (approval_rule:manage, step-up). A seat refusal names who
 * holds the seat and links here instead of "ask an Admin".
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { pageFor } from "../lib/identity/pages";
import { nextHolders, seatCards, seatLabel, tiersWords, type SeatsResponse } from "../lib/identity/seats";
import { pointedRefusal } from "../lib/server-action";
import { IdentityError } from "../lib/identity/api";

const DATA: SeatsResponse = {
  seats: [
    { seat_key: "ceo", principal_id: "usr_a", display_name: "Alice" },
    { seat_key: "ceo", principal_id: "usr_b", display_name: "Bob" },
  ],
  known_seats: ["cao", "ceo", "cto", "release_manager", "vp_agents"],
  tiers: { "0": [], "1": [], "2": ["ceo"], "3": ["ceo", "cao"] },
  two_person_tiers: [3],
  vacant_tiers: [],
};

describe("approval seats", () => {
  it("shows each seat with its holders and the tiers it approves, tiered seats first", () => {
    const cards = seatCards(DATA);
    expect(cards.map((c) => c.key).slice(0, 2)).toEqual(["ceo", "cao"]);
    const ceo = cards[0];
    expect(ceo.label).toBe("CEO");
    expect(ceo.tiers).toEqual([2, 3]);
    expect(ceo.holders.map((h) => h.name)).toEqual(["Alice", "Bob"]);
    expect(cards.find((c) => c.key === "cao")?.vacant).toBe(true);
    expect(cards.find((c) => c.key === "cto")?.vacant).toBe(false);
    expect(tiersWords(ceo.tiers, DATA.two_person_tiers)).toBe("Tier 2 · Tier 3 (two people)");
    expect(tiersWords([], [])).toBe("No tier");
    expect(seatLabel("vp_agents")).toBe("VP Agents");
  });

  it("adds and removes one holder without duplicates", () => {
    expect(nextHolders(["a", "b"], { add: "c" })).toEqual(["a", "b", "c"]);
    expect(nextHolders(["a", "b"], { add: "a" })).toEqual(["a", "b"]);
    expect(nextHolders(["a", "b"], { remove: "a" })).toEqual(["b"]);
  });

  it("is a declared Identity tab that reads with role_binding:read", () => {
    const page = pageFor("/identity/seats");
    expect(page?.label).toBe("Approval seats");
    expect(page && "permission" in page.access ? page.access.permission : null).toBe("role_binding:read");
    expect(page?.tab).toBe(true);
  });

  it("writes through the step-up guarded call with approval_rule:manage", () => {
    const src = readFileSync(join(process.cwd(), "app/identity/seats/seats-screen.tsx"), "utf8");
    expect(src).toContain("useGuardedCall");
    expect(src).toContain("/v1/identity/seats/${enc(card.key)}");
    expect(src).toContain('"approval_rule:manage"');
  });

  it("a seat refusal names the holders and links to the seats screen, never 'ask an Admin'", () => {
    const body = { error: "seat_required", message: "A tier-2 decision is approved by the ceo seat, held by Alice or Bob. Ask one of them to approve it, or change who holds the seat in Identity › Approval seats (/identity/seats).",
      href: "/identity/seats", href_label: "Approval seats" };
    const pointed = pointedRefusal({ status: 403, body });
    expect(pointed).toEqual({ message: body.message, href: "/identity/seats", hrefLabel: "Approval seats" });
    expect(pointedRefusal({ status: 403, body: { ...body, href: "https://evil.example" } })).toBeNull();
    expect(new IdentityError(403, "seat_required", { body }).message).toContain("Alice or Bob");
  });
});
