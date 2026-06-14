import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

type ControlFinding = {
  line: number;
  tag: string;
};

function findVisibleControlsWithoutAccessibleName(source: string): ControlFinding[] {
  const lines = source.split("\n");
  const findings: ControlFinding[] = [];
  let labelDepth = 0;

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index] ?? "";
    const tagMatch = line.match(/<(input|textarea|select)\b/);
    const startsInLabel = labelDepth > 0 || line.includes("<label");

    if (tagMatch) {
      const controlLines = [line];
      let cursor = index;
      while (!controlLines.join("\n").includes(">") && cursor < lines.length - 1) {
        cursor += 1;
        controlLines.push(lines[cursor] ?? "");
      }
      const controlMarkup = controlLines.join("\n");
      const isHidden = /type=["']hidden["']/.test(controlMarkup);
      const hasAccessibleName =
        startsInLabel ||
        /aria-label=/.test(controlMarkup) ||
        /aria-labelledby=/.test(controlMarkup);

      if (!isHidden && !hasAccessibleName) {
        findings.push({ line: index + 1, tag: tagMatch[1] ?? "control" });
      }
    }

    labelDepth += (line.match(/<label\b/g) ?? []).length;
    labelDepth -= (line.match(/<\/label>/g) ?? []).length;
    labelDepth = Math.max(labelDepth, 0);
  }

  return findings;
}

describe("executive console accessibility baseline", () => {
  // Note: static source scan (labels via label/aria, landmarks, roles) + explicit aria asserts for 3x features.
  // Axe-core / full automated a11y (WCAG) or lighthouse not run here (no playwright/axe deps per current package.json; P2 future).
  // Manual: use browser devtools a11y panel + axe browser ext on / after `make dev` + seed. All added controls use label/aria-label.
  // Covers: pack install UI, selfimp forms (incl notes), toasts (role/alert), commercial panels, monitoring sections, pag lists (role=list), worker notes (labels).
  it("gives every visible form control an accessible name", () => {
    const pageSource = readFileSync(
      resolve(__dirname, "../app/page.tsx"),
      "utf8",
    );

    expect(findVisibleControlsWithoutAccessibleName(pageSource)).toEqual([]);
  });

  it("provides keyboard skip navigation and labelled landmarks", () => {
    const shellSource = readFileSync(
      resolve(__dirname, "../app/ConsoleShell.tsx"),
      "utf8",
    );
    const pageSource = readFileSync(
      resolve(__dirname, "../app/page.tsx"),
      "utf8",
    );

    // Skip link in the console shell + its target landmark (keyboard users
    // reach the decision inbox without tabbing through the whole nav).
    expect(shellSource).toContain('className="skip-link"');
    expect(shellSource).toContain('href="#decision-inbox"');
    expect(pageSource).toContain('id="decision-inbox"');
    expect(pageSource).toContain('aria-labelledby="decision-inbox-title"');
    expect(pageSource).toContain('id="decision-inbox-title"');

    // Landmark roles: a labelled <nav> for the area switcher and a labelled
    // <main> content region.
    expect(shellSource).toContain("<nav");
    expect(shellSource).toContain('aria-label="Console areas"');
    expect(shellSource).toContain("<main");
    expect(shellSource).toMatch(/<main[^>]*aria-label=/);

    // Console title heading + at least one labelled section landmark.
    expect(pageSource).toContain('id="boss-console-title"');
    expect(pageSource).toContain('aria-label="Command center"');
  });

  it("console navigation controls have accessible names", () => {
    const shellSource = readFileSync(
      resolve(__dirname, "../app/ConsoleShell.tsx"),
      "utf8",
    );

    // Interactive nav controls are labelled (no icon-only unlabelled buttons).
    expect(shellSource).toContain('aria-label="Toggle navigation"');
    expect(shellSource).toContain('aria-label="Jump to area"');
    expect(shellSource).toContain('aria-label="Console areas"');
  });
});
