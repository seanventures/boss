// @vitest-environment happy-dom
/**
 * Who can's Undo (tester K2). After Apply recommended, `Undo last` restored an OLD snapshot of one
 * ability's holders — silently taking Email from Juno, Pilot and Strata — and toasted "Saved".
 * An action is now recorded as what it added and removed; Undo reverts exactly that against the
 * holders as they are now, and says what it undid.
 */
import React from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { $, cleanup, click, render } from "./_helpers/kit-render";
import { ability, clone, installKitApi, KITS } from "./_helpers/kit-api";
import type { Ability } from "../lib/kit-types";
import type { KitWithCrew } from "../lib/kit-api";

const { WhoCan, undone } = await import("../app/skills/who-can");

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function toasts(): string[] {
  const seen: string[] = [];
  window.addEventListener("boss:toast", (e) => seen.push((e as CustomEvent<{ message: string }>).detail.message));
  return seen;
}

describe("undone()", () => {
  it("removes what the action added and re-adds what it removed, nothing else", () => {
    expect(undone(["a", "b", "c"], { ability: "x", added: ["c"], removed: [] })).toEqual(["a", "b"]);
    expect(undone(["a"], { ability: "x", added: [], removed: ["b"] })).toEqual(["a", "b"]);
    expect(undone(["a", "z"], { ability: "x", added: ["c"], removed: [] })).toEqual(["a", "z"]);
  });
});

describe("Undo after Apply recommended", () => {
  it("takes back only the grants Apply recommended made, against the holders now", async () => {
    const before = clone(KITS.fresh);
    // Email is held by three agents before; Apply recommended gives it to pm too.
    ability(before, "email").holders = ["release_manager", "backend_engineer", "program_manager"];
    const after = clone(before);
    ability(after, "email").holders = [...ability(before, "email").holders, "pm"];
    const calls = installKitApi(before, (_url, body) => {
      if (body?.action === "recommended") return { granted: [{ role_key: "pm", ability: "email" }], skipped: [] };
      return undefined;
    });
    const said = toasts();

    function Host() {
      const [kit, setKit] = React.useState<KitWithCrew>(before);
      return (
        <WhoCan kit={kit} crew={kit.crew ?? []} projectId="p1" actor="ceo" focusRole={null}
                onChanged={(a: Ability) => setKit((k) => ({ ...k, abilities: k.abilities.map((x) => (x.key === a.key ? a : x)) }))}
                onReload={() => setKit(after)} onOpenAbility={() => {}} />
      );
    }
    await render(<Host />);
    await click('[data-testid="kit-who-apply"]');
    expect($('[data-testid="kit-who-changes"]')!.textContent).toBe("1 change");
    await click('[data-testid="kit-who-undo"]');

    const puts = calls.filter((c) => c.body?.action === "holders");
    expect(puts).toHaveLength(1);
    expect(puts[0].body).toMatchObject({ ability: "email" });
    expect(new Set(puts[0].body!.roles as string[])).toEqual(new Set(["release_manager", "backend_engineer", "program_manager"]));
    expect(said).toContain("Undid Apply recommended");
    expect(said).not.toContain("Saved");
    expect($('[data-testid="kit-who-changes"]')).toBeNull();
  });
});

describe("columns past the edge are announced (K18)", () => {
  it("a matrix wider than its frame shows a fade and More →, which scrolls", async () => {
    const kit = clone(KITS.fresh);
    installKitApi(kit);
    const proto = HTMLElement.prototype as unknown as Record<string, unknown>;
    const saved = { sw: Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollWidth"),
                    cw: Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientWidth") };
    Object.defineProperty(proto, "scrollWidth", { configurable: true, get() { return this.classList?.contains("kit-matrix-wrap") ? 1300 : 0; } });
    Object.defineProperty(proto, "clientWidth", { configurable: true, get() { return this.classList?.contains("kit-matrix-wrap") ? 1000 : 0; } });
    try {
      await render(<WhoCan kit={kit} crew={kit.crew ?? []} projectId="p1" actor="ceo" focusRole={null}
                           onChanged={() => {}} onReload={() => {}} onOpenAbility={() => {}} />);
      expect($('.kit-matrix-frame')!.dataset.more).toBe("true");
      const wrap = $('[data-testid="kit-matrix-wrap"]') as HTMLElement & { scrollBy: (o: unknown) => void };
      const by = vi.fn();
      wrap.scrollBy = by;
      await click('[data-testid="kit-matrix-more"]');
      expect(by).toHaveBeenCalled();
    } finally {
      if (saved.sw) Object.defineProperty(HTMLElement.prototype, "scrollWidth", saved.sw); else delete proto.scrollWidth;
      if (saved.cw) Object.defineProperty(HTMLElement.prototype, "clientWidth", saved.cw); else delete proto.clientWidth;
    }
  });

  it("the columns are compact enough for twelve at 1440", async () => {
    const { readFileSync } = await import("node:fs");
    const { resolve } = await import("node:path");
    const css = readFileSync(resolve(__dirname, "../app/skills/kit.css"), "utf8");
    // rem at the console's 19px root (layout-scale.css), as px at 1440.
    const width = 19 * Number(css.match(/\.kit-matrix thead th \{[^}]*width: ([\d.]+)rem/)![1]);
    const first = 19 * Number(css.match(/\.kit-matrix tbody th \{[^}]*min-width: ([\d.]+)rem/)![1]);
    // 1440 minus the console's sidebar (~244) and the panel's padding (~80).
    expect(12 * width + first).toBeLessThanOrEqual(1440 - 244 - 80);
  });
});
