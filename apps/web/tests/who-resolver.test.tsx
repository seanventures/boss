// @vitest-environment happy-dom
/**
 * <Who> (BOSSAPP-1040): attribution is a name wherever BOSS knows one, and the id itself when it
 * does not — NEVER a blank, never an invented person. Breaks covered: a usr_ id the directory
 * names must read as that name (id in the tooltip); one the directory cannot name must still be
 * shown as the id; "person:ceo" reads as the owner; an agent party reads as the agent's name.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { primePeople, resolveWho, Who } from "../app/who";
import { $, cleanup, render } from "./_helpers/kit-render";

afterEach(() => { cleanup(); primePeople(null); vi.unstubAllGlobals(); });

describe("resolveWho", () => {
  it("names what it can and refuses to guess the rest", () => {
    const dir = new Map([["usr_wsw5h4abcdef", "Ada Lovelace"]]);
    expect(resolveWho("usr_wsw5h4abcdef", dir)).toMatchObject({ name: "Ada Lovelace", kind: "person" });
    expect(resolveWho("person:usr_wsw5h4abcdef", dir)).toMatchObject({ name: "Ada Lovelace", kind: "person" });
    expect(resolveWho("usr_nobodyknows1", dir)).toMatchObject({ name: null, kind: "person" });
    expect(resolveWho("person:ceo", dir)).toMatchObject({ name: "the owner" });
    expect(resolveWho("agent:qa", dir).kind).toBe("agent");
    expect(resolveWho("agent:qa", dir).name).toBeTruthy();
    expect(resolveWho("", dir)).toMatchObject({ name: "someone" });
  });
});

describe("<Who>", () => {
  it("falls back to the id when no name resolves: shown, never blank", async () => {
    primePeople({}); // the directory loaded and holds nothing
    await render(<p>by <Who id="usr_wsw5h4abcdef" testId="who-under-test" /></p>);
    const el = $('[data-testid="who-under-test"]')!;
    expect(el.textContent).toBe("usr_wsw5h4abcdef");
    expect(el.textContent).not.toBe("");
  });

  it("shows the directory's name with the id in the tooltip", async () => {
    primePeople({ usr_wsw5h4abcdef: "Ada Lovelace" });
    await render(<p>by <Who id="usr_wsw5h4abcdef" testId="who-under-test" /></p>);
    const el = $('[data-testid="who-under-test"]')!;
    expect(el.textContent).toContain("Ada Lovelace");
    expect(el.getAttribute("title")).toBe("usr_wsw5h4abcdef");
  });

  it("draws the named party's initial, and none for an id nothing names", async () => {
    primePeople({ usr_wsw5h4abcdef: "Ada Lovelace" });
    await render(<p><Who id="usr_wsw5h4abcdef" testId="who-named" /> <Who id="usr_nobodyknows1" testId="who-raw" /></p>);
    expect($('[data-testid="who-named"] .idn-face')?.textContent).toBe("AL");
    expect($('[data-testid="who-raw"] .idn-face')).toBeNull();
    expect($('[data-testid="who-raw"]')?.textContent).toBe("usr_nobodyknows1");
  });

  it("reads the directory once for the whole page and names the person when it lands", async () => {
    let reads = 0;
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (String(url).includes("/v1/identity/users")) {
        reads += 1;
        return new Response(JSON.stringify({ users: [{ id: "usr_wsw5h4abcdef", display_name: "Ada Lovelace" }] }),
                            { status: 200, headers: { "Content-Type": "application/json" } });
      }
      return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
    }));
    await render(
      <p>
        <Who id="usr_wsw5h4abcdef" testId="who-a" /> and <Who id="usr_wsw5h4abcdef" testId="who-b" />
      </p>,
    );
    expect(reads).toBe(1);
    expect($('[data-testid="who-a"]')?.textContent).toContain("Ada Lovelace");
    expect($('[data-testid="who-b"]')?.textContent).toContain("Ada Lovelace");
  });
});
