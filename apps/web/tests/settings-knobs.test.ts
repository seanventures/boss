import { describe, expect, it } from "vitest";

import { codeOnly } from "./_helpers/source";

/**
 * How hard BOSS runs lived entirely in environment variables, so changing a number meant an
 * ops action and a container restart. In practice nobody changed them and the defaults became
 * the product.
 */
const KNOBS = codeOnly("app/settings-knobs-ui.tsx");
const SHELL = codeOnly("app/ConsoleShell.tsx");
const STORE = codeOnly("../../engine/boss_engine/services/project_settings.py");
const CSS = codeOnly("app/globals.css");

describe("the area is called Settings", () => {
  it("is labelled for what it governs, not just spend", () => {
    expect(SHELL).toContain('id: "budget", label: "Settings"');
  });

  it("keeps the area id, because ids are routes and bookmarks", () => {
    expect(SHELL).toContain('id: "budget"');
  });
});

describe("a knob says where its value came from", () => {
  it("shows the source beside every knob", () => {
    // "runs three agents" and "nobody chose, so it runs the deployment default" are different
    // facts, and only one tells you whether turning this dial does anything.
    expect(KNOBS).toContain('data-source={k.source}');
    expect(CSS).toContain('.settings-source[data-source="project"]');
  });

  it("offers a way back to the default", () => {
    expect(KNOBS).toContain("Use the default");
  });

  it("hands the knob back rather than storing the current default", () => {
    // Storing it would freeze the project against a later change to the deployment.
    expect(STORE).toContain("def clear_one");
    expect(STORE).toContain("DELETE FROM project_settings");
  });
});

describe("a knob cannot be set to something harmful", () => {
  it("refuses a key it does not know", () => {
    // A settings table that accepts anything is a place where typos live forever.
    expect(STORE).toContain("class UnknownKnob");
    expect(STORE).toContain("is not a setting BOSS knows about");
  });

  it("bounds every value, in words", () => {
    // "between 1 and 16" is actionable; "invalid" is not. A concurrency of 400 is an outage.
    expect(STORE).toContain("has to be between");
    expect(STORE).toContain('"min": 1, "max": 16');
  });
});

describe("the knob is read by the thing it governs", () => {
  it("concurrency is looked up per project at run time", () => {
    // A knob that changes nothing is worse than no knob.
    const GOV = codeOnly("../../engine/boss_engine/services/governed_code_task_service.py");
    expect(GOV).toContain('_effective_setting(');
    expect(GOV).toContain('"code_agent_max_concurrency"');
  });

  it("falls back to the environment when the project has said nothing", () => {
    // The environment was the only answer until this existed, and remains a complete one.
    expect(STORE).toContain("def effective");
    expect(STORE).toContain("return fallback if row is None else row");
  });
});

describe("a step can carry what it needs to KNOW", () => {
  const DW = codeOnly("app/delivery-workflows-ui.tsx");
  const SVC = codeOnly("../../engine/boss_engine/services/workflow_custom_steps.py");

  it("asks for it when the step is defined", () => {
    // A binding says WHICH skill or tool runs; it says nothing about the standard the step is
    // meant to hold to — house style, the clauses that matter, the doc explaining the system
    // it touches. That lived in the head of whoever defined the step.
    expect(DW).toContain('data-testid="dw-step-knowledge"');
    expect(DW).toContain("knowledge: knowledge.trim()");
  });

  it("hands it to the thing that actually runs", () => {
    // Stored and unused would be worse than not asking.
    expect(SVC).toContain('inputs = {**inputs, "knowledge": knowledge.strip()}');
  });

  it("passes nothing when a step has none", () => {
    // A step defined before this existed must behave exactly as it did.
    expect(SVC).toContain("if knowledge.strip():");
  });
});

describe("the starter shelf", () => {
  const HERO = codeOnly("app/ask-boss-hero.tsx");
  const CSS_I = codeOnly("app/globals.css");

  it("offers a fresh set, not the ones already built", () => {
    // All sixteen of the previous ideas had been run.
    for (const gone of ["Flaky-test detector", "On-call rota with escalation",
                        "CSV-to-chart explorer", "Waitlist with referral",
                        "Dependency freshness report", "Shift swap for clinics"]) {
      expect(HERO).not.toContain(gone);
    }
    for (const fresh of ["Incident timeline builder", "Subscription audit",
                         "Interview scorecard", "Decision log"]) {
      expect(HERO).toContain(fresh);
    }
  });

  it("gives every card a mark and an accent from ONE palette", () => {
    // Sixteen identical grey cards is a list pretending to be a gallery — but sixteen cards
    // each on its own random hue is worse, and that is what shipped: hue values of 4, 152,
    // 268, 196, 28, 340 and 12, points scattered around the colour circle with nothing tying
    // them together. A row of them read as arbitrary because it WAS arbitrary. The accent is
    // now drawn from a fixed short palette, so the strip reads as one product.
    expect(HERO).toContain("const GLYPHS");
    // `askboss-idea-mark`, not `askboss-idea-art`: the latter was an earlier design's icon
    // tile, and only this test still referred to it — which is the failure mode of a test that
    // greps source. It passed for months while asserting a class the component had stopped
    // rendering.
    expect(HERO).toContain('className="askboss-idea-mark"');
    expect(CSS_I).toContain(".askboss-idea-mark {");
    // `ACCENTS` became `DOMAIN_GRADIENTS`: the accent per card is now a three-stop gradient
    // chosen by DOMAIN, not a single tone from a list. Same property being asserted — one
    // palette, no per-card random hue.
    expect(HERO).toContain("const DOMAIN_GRADIENTS");
    expect(HERO).not.toMatch(/hue: \d+/);
    expect(CSS_I).not.toContain("--idea-hue");
  });

  it("draws the app, not a coloured rectangle", () => {
    // Three versions of this shipped as decoration: a gradient wash, then a wash with a
    // texture, then a random stock PHOTOGRAPH — an iceberg over "Docs freshness board". A
    // starter card should show what the thing would look like, which means a screenshot of a
    // user interface: window chrome, a sidebar, a header, and content laid out to match the
    // idea. Opaque surfaces, because the previous drawing was white at 6% opacity over a
    // saturated gradient and had no edge to hold.
    expect(HERO).toContain("const MOCKUPS");
    expect(HERO).toContain("const UI = {");
    expect(HERO).toMatch(/fill=\{UI\.(panel|row|chrome)\}/);
    // And it is DRAWN, never fetched: this console is tailnet-only, so a card built on a CDN
    // is a broken image the day the CDN is unreachable.
    expect(HERO).not.toContain("picsum");
    expect(HERO).not.toMatch(/<img[^>]+https?:/);
  });

  it("draws the mark inline rather than fetching it", () => {
    // The console must render with no network beyond its own origin, and a broken <img> is
    // worse than no picture.
    expect(HERO).toContain('<path d={GLYPHS[i.glyph]} />');
    expect(HERO).not.toMatch(/<img[^>]+https?:/);
  });

  it("still only drafts — a click never spends money", () => {
    // Clicking a suggestion used to fire a governed build immediately.
    expect(HERO).toContain("onClick={() => draftIdea(i)}");
  });
});

describe("the starter strip is a gallery, not one image recoloured", () => {
  const HERO3 = codeOnly("app/ask-boss-hero.tsx");
  const CSS3 = codeOnly("app/globals.css");

  it("paints with saturated colour, the way the app that worked did", () => {
    // "Living Colors" has NO image files: its look is CSS gradients over four vivid tokens
    // (330 81% 58%, 262 83% 63%, 24 95% 58%, 172 66% 42%) — saturation 66–95%, lightness
    // 42–63%. These cards were #161c2e / #1d2438 — saturation ~25%, lightness 12–20% — a
    // near-black window with a few coloured pixels, which is what "the images are so poor"
    // meant.
    expect(HERO3).toContain("DOMAIN_GRADIENTS");
    const stops = [...HERO3.matchAll(/hsl\((\d+) (\d+)% (\d+)%\)/g)];
    expect(stops.length).toBeGreaterThan(8);
    // Every stop vivid: nothing washed out, nothing near-black.
    for (const [, , sat, light] of stops) {
      expect(Number(sat)).toBeGreaterThanOrEqual(60);
      expect(Number(light)).toBeGreaterThanOrEqual(40);
      expect(Number(light)).toBeLessThanOrEqual(70);
    }
  });

  it("draws the layout in white over the colour, at opacities that hold an edge", () => {
    // The first attempt at colour failed on 6–14% opacity shapes, which read as weather.
    const opacities = [...HERO3.matchAll(/rgba\(255,255,255,\.(\d+)\)/g)]
      .map((m) => Number(m[1]));
    expect(opacities.length).toBeGreaterThan(4);
    expect(Math.max(...opacities)).toBeGreaterThanOrEqual(90);
  });

  it("shows a supplied photograph only once it has loaded", () => {
    // onError alone leaves a broken-image icon wherever the handler has not run — a server
    // render, or the moment before the request 404s. Sixteen broken icons over the artwork is
    // worse than no photographs at all.
    expect(HERO3).toContain('style={{ display: "none" }}');
    expect(HERO3).toContain('onLoad={(e) => { e.currentTarget.style.display = "block"; }}');
  });

  it("gives every idea its own drawing", () => {
    // The previous set had five layouts mapped BY GLYPH, so seven of the sixteen ideas showed
    // the identical rows-with-avatars picture and three more shared a second one: three
    // adjacent cards differing only in accent colour.
    const ideas = [...HERO3.matchAll(/title: "([^"]+)", glyph:/g)].map((m) => m[1]);
    const mapped = [...HERO3.matchAll(/^\s+"([^"]+)": "(\w+)",$/gm)]
      .filter(([, key]) => ideas.includes(key));
    expect(mapped.length).toBe(ideas.length);
    const layouts = new Set(mapped.map(([, , layout]) => layout));
    expect(layouts.size).toBe(ideas.length);
  });

  it("maps by title, since mapping by glyph is what collapsed them", () => {
    expect(HERO3).toContain("MOCKUP_FOR[i.title]");
  });

  it("does not force the hover peek into the layout", () => {
    // `.askboss-idea > * { position: relative }` overrode the peek's absolute positioning and
    // put 150px of invisible, zero-opacity text into every card — the empty two-thirds under
    // every title.
    expect(CSS3).toContain(".askboss-idea > *:not(.askboss-idea-peek)");
  });
});

describe("supplied photographs replace the drawings", () => {
  const HERO4 = codeOnly("app/ask-boss-hero.tsx");

  it("serves them from this app, not a CDN", () => {
    // The console is tailnet-only: an image fetched from the internet is a broken card the
    // day that host is unreachable, which is why the seeded-Picsum attempt was removed.
    expect(HERO4).toContain("`/starters/${slugFor(title)}.jpg`");
    expect(HERO4).not.toMatch(/<img[^>]+https?:/);
  });

  it("falls back to the drawing when a file is absent", () => {
    // Which is what lets them be added one at a time: none present, all sixteen present, or
    // anything between, and the strip is complete either way.
    expect(HERO4).toContain('e.currentTarget.style.display = "none"');
  });

  it("derives the file name from the title, so the two cannot drift", () => {
    expect(HERO4).toContain("function slugFor");
  });
});
