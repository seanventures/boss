import { describe, expect, it } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

/**
 * The ASK button on every agent card was a pale grey pill with grey text, on a near-black card.
 *
 * Its rule declared a border, a colour, a radius and padding — and no `background`. So it fell
 * through to the browser's default control chrome, which is designed for a light page. It read
 * as a disabled control borrowed from another application, which is the precise opposite of the
 * one thing it exists to do: invite you to say something to that agent.
 *
 * This is a whole class of bug rather than one mistake. The console has no light theme, so a
 * button that forgets its background is always wrong, always in the same way, and always
 * survives review — the rule LOOKS complete. Nothing in TypeScript or the linter can see it.
 *
 * So: every styled button must get a background from one of its own classes or from an inline
 * style. Buttons that are deliberately transparent — a bare icon, a text link — are still fine;
 * they simply must not also be dressed as a control, which is what makes the default chrome
 * appear.
 */
const APP = join(process.cwd(), "app");
const CSS = readFileSync(join(APP, "globals.css"), "utf8");

function tsxFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) out.push(...tsxFiles(path));
    else if (entry.endsWith(".tsx")) out.push(path);
  }
  return out;
}

/** The declaration blocks of every non-state rule mentioning `.cls`. */
function rulesFor(cls: string): string[] {
  const out: string[] = [];
  const re = new RegExp(`(^|\\n)([^\\n{}]*\\.${cls}\\b[^\\n{}]*)\\{([^}]*)\\}`, "g");
  for (const m of CSS.matchAll(re)) {
    const selector = m[2].trim();
    // :hover/:disabled may add a background; they cannot be the only place one appears, or the
    // control is default-chromed until you point at it.
    if (/:(hover|active|focus|focus-visible|disabled)/.test(selector)) continue;
    out.push(m[3]);
  }
  return out;
}

const suppliesBackground = (cls: string) =>
  rulesFor(cls).some((body) => /\bbackground(-color)?\s*:/.test(body));

/** Dressed as a control: something a browser would otherwise chrome for us. */
const looksLikeAControl = (cls: string) =>
  rulesFor(cls).some((body) => /border-radius\s*:|padding\s*:|border\s*:/.test(body));

type Button = { file: string; classes: string[]; tag: string };

function buttons(): Button[] {
  const found: Button[] = [];
  for (const file of tsxFiles(APP)) {
    // Arrow functions in the attributes (`onClick={() => …}`) contain a `>`, so a naive
    // non-greedy match ends the tag early and misses everything after the first handler —
    // including the inline `background` that made one of these look like a real offender.
    const src = readFileSync(file, "utf8").replaceAll("=>", "=\u00BB");
    for (const m of src.matchAll(/<button\b[\s\S]*?>/g)) {
      const tag = m[0];
      const cls = tag.match(/className=(?:"([^"]+)"|\{`([^`]*)`\})/);
      if (!cls) continue;
      const classes = (cls[1] ?? cls[2] ?? "").match(/[a-zA-Z][\w-]*/g) ?? [];
      found.push({ file: file.slice(APP.length + 1), classes, tag });
    }
  }
  return found;
}

describe("no button is left to the browser's default chrome", () => {
  it("every styled button gets a background from its classes or its inline style", () => {
    const offenders = buttons().filter(({ classes, tag }) => {
      const known = classes.filter((c) => rulesFor(c).length > 0);
      if (known.length === 0) return false;                 // unstyled: not our business
      if (known.some(suppliesBackground)) return false;
      if (/background\s*:/.test(tag)) return false;          // set inline on this element
      return known.some(looksLikeAControl);
    });
    expect(
      offenders.map((o) => `${o.file}: .${o.classes.join(".")}`),
      "styled like a control with no background — these render as light-theme buttons",
    ).toEqual([]);
  });

  it("the ASK button in particular — this is the one that shipped", () => {
    const ask = rulesFor("roster-actions");
    expect(ask.length).toBeGreaterThan(0);
    expect(ask.some((body) => /\bbackground\s*:/.test(body))).toBe(true);
  });

  it("ASK reads as pressable rather than as a disabled control", () => {
    const base = rulesFor("roster-actions").join("\n");
    expect(base).toContain("cursor: pointer");
    // Grey-on-dark was the whole complaint: it took its colour from --muted, which is the
    // console's colour for text that does not matter.
    expect(base).not.toMatch(/color:\s*var\(--muted/);
  });

  it("and is visible to a keyboard, on a dark card", () => {
    // The default focus ring is a thin black outline — invisible against this background.
    const focus = CSS.slice(CSS.indexOf(".roster-actions button:focus-visible"));
    expect(focus.slice(0, 200)).toContain("outline:");
  });
});
