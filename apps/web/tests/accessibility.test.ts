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
  it("gives every visible form control an accessible name", () => {
    const pageSource = readFileSync(
      resolve(__dirname, "../app/page.tsx"),
      "utf8",
    );

    expect(findVisibleControlsWithoutAccessibleName(pageSource)).toEqual([]);
  });

  it("provides keyboard skip navigation and a labelled main landmark", () => {
    const pageSource = readFileSync(
      resolve(__dirname, "../app/page.tsx"),
      "utf8",
    );

    expect(pageSource).toContain('className="skip-link"');
    expect(pageSource).toContain('aria-labelledby="boss-console-title"');
    expect(pageSource).toContain('id="decision-inbox"');
    expect(pageSource).toContain("tabIndex={-1}");
  });
});
