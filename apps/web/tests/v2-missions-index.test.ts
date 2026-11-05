import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  redirect: (to: string) => { throw new Error(`redirect ${to}`); },
}));
vi.mock("../components/identity/route-shell", () => ({
  consolePageAllowed: async () => true,
}));
vi.mock("../components/identity/page-denied", () => ({
  PageDenied: () => null,
}));

/**
 * Bare /v2/missions is not a route. Next answers its own 404, with no console and no sign-in.
 * The list is /v2; one build is /v2/missions/[id]. The index must open the list and keep the org.
 */
describe("the /v2/missions index", () => {
  it("opens the project list and keeps the org, instead of a raw 404", async () => {
    let open: ((props: {
      searchParams?: Promise<Record<string, string | string[] | undefined>>;
    }) => Promise<unknown>) | undefined;
    try {
      open = (await import("../app/v2/missions/page")).default;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (!/Cannot find module|Failed to load url|ENOENT/.test(message)) throw error;
      open = undefined;
    }
    expect(open, "no page, so Next answers 404").toEqual(expect.any(Function));
    await expect(open!({ searchParams: Promise.resolve({ org: "org_boss", view: "x" }) })).rejects.toThrow(
      "redirect /v2?org=org_boss",
    );
  });
});
