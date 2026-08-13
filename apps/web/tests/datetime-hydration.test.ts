import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import React from "react";

import {
  formatAgeCompact,
  formatDate,
  formatDateTime,
  formatDateTimeShort,
  formatRelativeTime,
  formatTime,
} from "../lib/datetime";
import { MissionTaskBoard } from "../app/mission-operations-ui";
import { PolicyEvalList } from "../app/safety-ui";
import { CICDPipelinesPanel } from "../app/cicd-pipelines-ui";
import { GitHubOperations } from "../app/github-operations-ui";
import { emptyGitHubOperationsFeed } from "../lib/github-operations";
import type { TaskFeedItem } from "../lib/tasks";
import type { ReceiptFeedItem } from "../lib/receipts";
import type { OpsQueueJobItem } from "@boss/api-client";
import { readdirSync, readFileSync } from "node:fs";
import { join, relative, resolve } from "node:path";

/**
 * BOSS-346: React #418 (HTML mismatch) was driven by locale/TZ-dependent date
 * strings and Date.now()-based relative labels in SSR'd client islands.
 * These helpers must emit identical text on Node (SSR) regardless of process TZ.
 */

describe("datetime helpers are hydration-safe", () => {
  const iso = "2026-08-12T01:25:15.648620+00:00";

  it("formatDateTime is stable under TZ=UTC and TZ=Asia/Tokyo", () => {
    const previous = process.env.TZ;
    try {
      process.env.TZ = "UTC";
      const utc = formatDateTime(iso);
      process.env.TZ = "Asia/Tokyo";
      const tokyo = formatDateTime(iso);
      process.env.TZ = "America/Los_Angeles";
      const la = formatDateTime(iso);
      expect(utc).toBe(tokyo);
      expect(utc).toBe(la);
      expect(utc).toBe("Aug 12, 2026, 1:25 AM UTC");
    } finally {
      if (previous === undefined) delete process.env.TZ;
      else process.env.TZ = previous;
    }
  });

  it("formatDate / formatTime / formatDateTimeShort are UTC and deterministic", () => {
    expect(formatDate(iso)).toBe("Aug 12, 2026");
    expect(formatTime(iso)).toBe("01:25:15 AM UTC");
    expect(formatDateTimeShort(iso)).toBe("Aug 12, 1:25 AM UTC");
    expect(formatDateTime(undefined)).toBe("Unknown time");
    expect(formatDate(null)).toBe("Never");
  });

  it("formatRelativeTime uses explicit nowMs (no hidden Date.now during pure call)", () => {
    const now = Date.parse("2026-08-12T01:40:15.648Z"); // +15m
    expect(formatRelativeTime(iso, now)).toBe("15m ago");
    expect(formatRelativeTime(iso, now, { style: "short" })).toBe("15m");
    const later = Date.parse("2026-08-12T02:30:15.648Z"); // +65m
    expect(formatRelativeTime(iso, later)).toBe("1h ago");
    expect(formatAgeCompact(iso, later)).toBe("1h 5m");
    expect(formatRelativeTime(undefined, now)).toBe("not yet");
  });

  it("ClientRelativeTime SSRs absolute UTC then upgrades after mount", () => {
    const src = readFileSync(
      resolve(__dirname, "../app/client-relative-time.tsx"),
      "utf8",
    );
    expect(src).toContain("formatDateTime(iso");
    expect(src).toContain("useState(absolute)");
    expect(src).toContain("formatRelativeTime(iso, now");
    // Must not call Date.now during the initial render path (only inside useEffect).
    const withoutEffect = src.replace(/useEffect\([\s\S]*?\n  \}, \[[^\]]*\]\);/, "");
    expect(withoutEffect).not.toContain("Date.now()");
  });

  it("MissionTaskBoard SSR markup embeds the same UTC timestamp the client will paint", () => {
    const tasks: TaskFeedItem[] = [
      {
        id: "tsk_abc123",
        title: "Hydration canary",
        status: "in_progress",
        ownerRoleId: "pm",
        dependencyTaskIds: [],
        priority: "high",
        riskTier: 1,
        updatedAt: iso,
      },
    ];
    const jobs: OpsQueueJobItem[] = [];
    const receipts: ReceiptFeedItem[] = [];
    const html = renderToStaticMarkup(
      React.createElement(MissionTaskBoard, { tasks, jobs, receipts }),
    );
    expect(html).toContain(`dateTime="${iso}"`);
    expect(html).toContain("Aug 12, 2026, 1:25 AM UTC");
    // Must not emit a bare locale-only short form that would diverge by TZ.
    expect(html).not.toMatch(/>\s*Aug 12, 2026, 1:25 AM\s*</);
  });

  it("named BOSS-346 areas avoid locale DateTimeFormat / raw Date.now labels", () => {
    const files = [
      "work-orchestration-ui.tsx",
      "code-tasks-ui.tsx",
      "shepherd-ui.tsx",
      "reports-ui.tsx",
      "pipeline-ui.tsx",
      "org-gate.tsx",
      "page.tsx",
      "client-relative-time.tsx",
    ];
    for (const file of files) {
      const src = readFileSync(resolve(__dirname, `../app/${file}`), "utf8");
      expect(src).not.toMatch(/toLocaleDateString\s*\(/);
      expect(src).not.toMatch(/toLocaleTimeString\s*\(/);
      expect(src).not.toMatch(/new Intl\.DateTimeFormat\s*\(/);
      if (file === "code-tasks-ui.tsx") {
        // Live phase duration is mount-gated (useState null + useEffect).
        expect(src).toContain("setCurrentPhaseMs");
        expect(src).not.toContain("suppressHydrationWarning");
      }
      if (file === "page.tsx") {
        expect(src).toContain("ClientRelativeTime");
        expect(src).not.toMatch(/function waitedLabel/);
      }
      if (file === "work-orchestration-ui.tsx" || file === "reports-ui.tsx") {
        expect(src).toMatch(/ClientRelativeTime|formatDate/);
      }
    }
  });

  it("residual client islands use UTC helpers, pinned locales, and no raw ISO slices", () => {
    const appRoot = resolve(__dirname, "../app");
    const allowDateNow = new Set([
      "client-relative-time.tsx",
      "code-tasks-ui.tsx",
      "client-forms.tsx",
      "actions.ts",
      "api/debug-api/route.ts",
      "page.tsx",
    ]);
    const files: string[] = [];
    const walk = (dir: string) => {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const full = join(dir, entry.name);
        if (entry.isDirectory()) {
          if (entry.name === "node_modules") continue;
          walk(full);
          continue;
        }
        if (/\.(ts|tsx)$/.test(entry.name)) files.push(full);
      }
    };
    walk(appRoot);

    const offenders: string[] = [];
    for (const file of files) {
      const rel = relative(appRoot, file).replaceAll("\\", "/");
      const src = readFileSync(file, "utf8");
      const hits: string[] = [];
      if (/toLocaleDateString\s*\(/.test(src)) hits.push("toLocaleDateString");
      if (/toLocaleTimeString\s*\(/.test(src)) hits.push("toLocaleTimeString");
      if (/new Intl\.DateTimeFormat\s*\(/.test(src)) hits.push("Intl.DateTimeFormat");
      if (/\.toLocaleString\s*\(\s*\)/.test(src)) hits.push("toLocaleString()");
      if (/\.toLocaleString\s*\(\s*(?!["']en-US)/.test(src)) hits.push("toLocaleString(non-en-US)");
      if (/\.slice\(\s*0\s*,\s*19\s*\)/.test(src)) hits.push("iso.slice(0, 19)");
      if (/\.slice\(\s*11\s*,\s*19\s*\)/.test(src)) hits.push("iso.slice(11, 19)");
      if (/toISOString\(\)\s*\.replace\(\s*["']T["']/.test(src)) hits.push("toISOString().replace(T)");
      if (
        !allowDateNow.has(rel) &&
        /Date\.now\s*\(/.test(src) &&
        !/useEffect\([\s\S]*Date\.now\(\)[\s\S]*\}\s*,/.test(src)
      ) {
        hits.push("Date.now() in render");
      }
      if (hits.length) offenders.push(`${rel}: ${hits.join(", ")}`);
    }
    expect(offenders).toEqual([]);
  });

  it("residual Safety / CI/CD / GitHub SSR markup emits UTC helper text", () => {
    const policyHtml = renderToStaticMarkup(
      React.createElement(PolicyEvalList, {
        items: [
          {
            id: "pol_1",
            boundary: "budget",
            action: "reserve",
            decision: "allow",
            reason: null,
            createdAt: iso,
          },
        ],
      }),
    );
    expect(policyHtml).toContain("Aug 12, 2026, 1:25 AM UTC");
    expect(policyHtml).not.toContain("2026-08-12T01:25:15");
    expect(policyHtml).not.toContain("2026-08-12 01:25:15");

    const cicdHtml = renderToStaticMarkup(
      React.createElement(CICDPipelinesPanel, {
        projectId: "boss-dogfood",
        initialRuns: [
          {
            id: "run_1",
            project_id: "boss-dogfood",
            vendor: "github_actions",
            status: "succeeded",
            recorded_at: iso,
            source: "webhook",
          },
        ],
      }),
    );
    expect(cicdHtml).toContain("Aug 12, 2026, 1:25 AM UTC");
    expect(cicdHtml).not.toMatch(/2026-08-12[T ]01:25:15/);

    const feed = {
      ...emptyGitHubOperationsFeed(undefined, "boss-dogfood"),
      apiReachable: true,
      summary: {
        ...emptyGitHubOperationsFeed(undefined, "boss-dogfood").summary,
        lastObservedAt: iso,
      },
    };
    const githubHtml = renderToStaticMarkup(
      React.createElement(GitHubOperations, {
        feed,
        actor: "ceo",
        canManage: false,
      }),
    );
    expect(githubHtml).toContain("Aug 12, 2026, 1:25 AM UTC");
    expect(githubHtml).not.toContain("2026-08-12 01:25 UTC");
  });
});
