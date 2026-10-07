// @vitest-environment happy-dom
/**
 * IMPACT COUNTS AND FALLBACKS READ AS WORDS (round 2, R2-WEB; after FX-C's P1-11/12/13).
 *
 * The role impact preview printed "+0 −0" for every count that did not change, and the overview
 * fell back to a raw audit outcome code when it had no word for it. Every delta on an identity
 * screen is drawn by <Delta> (lib/identity/words.ts deltaParts), and an unmapped code is
 * humanised through the words helper, never printed as the `?? x.code` fallback.
 * tests/identity-shows-no-raw-codes.test.tsx covers the rest of the class (codes, ids, events, keys).
 */
import React, { act } from "react";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { createRoot } from "react-dom/client";
import { describe, expect, it } from "vitest";

import { Delta } from "../components/identity/kit";
import { deltaParts, outcomeWords } from "../lib/identity/words";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function text(node: React.ReactElement): string {
  const host = document.createElement("div");
  const root = createRoot(host);
  act(() => root.render(node));
  const out = host.textContent ?? "";
  act(() => root.unmount());
  return out;
}

describe("a count's change", () => {
  it("never prints a zero side", () => {
    expect(deltaParts(0, 0)).toEqual({ gained: null, lost: null, none: true });
    expect(deltaParts(3, 0)).toEqual({ gained: "+3", lost: null, none: false });
    expect(deltaParts(0, 2)).toEqual({ gained: null, lost: "−2", none: false });
    expect(deltaParts(3, 2)).toEqual({ gained: "+3", lost: "−2", none: false });
  });
  it("<Delta> reads 'no change', '+3', '−2' or '+3 −2'", () => {
    expect(text(<Delta gained={0} lost={0} />)).toBe("no change");
    expect(text(<Delta gained={3} lost={0} />)).toBe("+3");
    expect(text(<Delta gained={0} lost={2} />)).toBe("−2");
    expect(text(<Delta gained={3} lost={2} />)).toBe("+3 −2");
  });
  it("an unknown audit outcome is words, not the code", () => {
    expect(outcomeWords("ok")).toBe("done");
    expect(outcomeWords("partially_applied")).toBe("partially applied");
  });
});

describe("in source", () => {
  const ROOTS = ["app/identity", "app/account", "components/identity"];
  const files = (dir: string): string[] => readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? files(p) : /\.tsx$/.test(n) ? [p] : [];
  });
  const lines = ROOTS.flatMap((r) => files(join(process.cwd(), r)))
    .flatMap((f) => readFileSync(f, "utf8").split("\n").map((l, i) => ({ at: `${f.replace(process.cwd() + "/", "")}:${i + 1}`, l })));

  it("a gained/lost count is drawn only by <Delta>", () => {
    const bad = lines.filter(({ at, l }) => !at.startsWith("components/identity/kit.tsx")
      && (/idn-(plus|minus)\b/.test(l) || /[>\s][+−]\{[\w.?]+\.(gained|lost)\b/.test(l)));
    expect(bad.map((b) => b.at)).toEqual([]);
  });

  it("no JSX falls back to a raw code (`?? x.code`, `?? x.outcome`, `?? x.error`, `?? x.event_type`)", () => {
    const bad = lines.filter(({ l }) => /\?\?\s*[\w.?]+\.(code|outcome|error|event_type|status)\s*\}/.test(l)
      && !/title=|data-|aria-|key=|href=|className=/.test(l.split("??")[0].slice(-40)));
    expect(bad.map((b) => `${b.at} ${b.l.trim().slice(0, 90)}`)).toEqual([]);
  });
});
