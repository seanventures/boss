// @vitest-environment happy-dom
/**
 * THE WIREFRAMES ARE THE BUILD. With AA_MOCK_EXPORT=1 this renders every Agents-at-work screen
 * from the real components on the sample data (lib/ambient-demo.ts) and writes
 * docs/product/wireframes/autonomous-agents/{index.html, screens/*.html, mock.css}, so the page
 * the operator reviews is exactly what ships. Without the variable it is skipped.
 *
 *   AA_MOCK_EXPORT=1 npx vitest run tests/autonomous-agents/export-wireframes.test.tsx
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

import React, { act } from "react";
import { describe, expect, it } from "vitest";

import { setConsoleLive } from "../../lib/console-live";
import { parseList } from "../../lib/ambient";
import { demoList } from "../../lib/ambient-demo";
import { $$, click, mountAs, settle, tid, unmount } from "../_helpers/multirepo-fixtures";
import { fakeAmbientApi } from "./_fixtures";

const RUN = process.env.AA_MOCK_EXPORT === "1";
const WEB = resolve(__dirname, "../..");
const OUT = resolve(WEB, "../../docs/product/wireframes/autonomous-agents");

/** The console's CSS, with relative @imports inlined. */
function css(): string {
  const files = ["app/globals.css", "app/design-system.css", "app/command-center.css", "app/command-center/cc.css",
                 "app/agents/agent-world.css", "app/govern/govern.css", "app/agent-ops/agent-ops.css"];
  const inline = (path: string): string => readFileSync(path, "utf8").replace(/@import\s+"([^"]+)";/g, (_m, rel: string) => inline(resolve(dirname(path), rel)));
  return files.map((f) => `/* ${f} */\n${inline(resolve(WEB, f))}`).join("\n");
}

type Screen = { key: string; title: string; caption: string; width: number; html: string };

function page(title: string, body: string, cssHref: string): string {
  return `<!doctype html><html lang="en" data-theme="dark"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${title}</title><link rel="stylesheet" href="${cssHref}">
<style>body{background:var(--void);color:var(--ink);font-family:var(--sans);margin:0;padding:24px 16px}.wf{max-width:1280px;margin:0 auto}
.wf h1{font-family:var(--display);font-size:24px;margin:0 0 4px}.wf-cap{color:var(--muted);margin:0 0 18px;font-size:14px}
.wf-sec{margin:0 0 40px}.wf-sec h2{font-size:16px;margin:0 0 2px}.wf-frame{border:1px solid var(--border-strong);border-radius:14px;padding:18px;background:var(--base);overflow-x:auto}
.wf-nav a{color:var(--accent-text);margin-right:12px;font-size:13px}</style></head><body><div class="wf">${body}</div></body></html>`;
}

describe.skipIf(!RUN)("export the Agents-at-work wireframes", () => {
  it("renders every screen from the real components and writes the mock", async () => {
    // The static page has no browser to measure the frame: give the canvas the width a laptop
    // console gives it (1200px, 820px beside an open drawer) so each chart is drawn fitted.
    const observers = new Set<() => void>();
    (globalThis as { ResizeObserver?: unknown }).ResizeObserver = class {
      cb: () => void; constructor(cb: () => void) { this.cb = cb; }
      observe() { observers.add(this.cb); this.cb(); } disconnect() { observers.delete(this.cb); } };
    const resized = () => act(async () => { for (const cb of observers) cb(); });
    Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, get(this: HTMLElement) {
      if (!this.classList.contains("aa-canvas-frame")) return 0;
      return document.querySelector('[data-testid="aa-drawer"]') ? 820 : 1200;
    } });
    const screens: Screen[] = [];
    const grab = (key: string, title: string, caption: string, width = 1280) => {
      const host = document.body.firstElementChild as HTMLElement;
      screens.push({ key, title, caption, width, html: host.innerHTML });
    };
    const area = async (search: string) => {
      fakeAmbientApi();
      setConsoleLive(false);
      window.history.replaceState({}, "", `/${search}`);
      const { AgentOpsHome } = await import("../../app/agent-ops/agent-ops-home");
      await mountAs(<AgentOpsHome projectId="p1" actor="ceo" />, "owner");
      await settle(); await settle();
    };

    await area("?area=agent_ops&aat=resolved_by_agents");
    grab("01-list", "Agents at work — the list", "Scorecard as pictures, five outcome tabs, each item a small diagram with badges.");
    unmount();

    await area("?area=agent_ops&aat=running");
    grab("02-list-running", "Running tab", "Live items pulse on their newest node.");
    unmount();

    await area("?area=agent_ops&item=work_order_api");
    grab("03-flow-incident", "Flow chart — the Order API incident (§11.0 mock)",
         "Alert → Chief of Staff → SRE lead → investigator & engineer → squad vote (2 approve · 1 red dissent) → rollback decision → run → recovered.");
    await click($$('[data-testid="aa-node"]').find((n) => n.dataset.type === "decision")!);
    await resized();
    await settle();
    grab("04-flow-drawer", "A node's drawer — the only place text lives",
         "The decision: question, chosen vs rejected, confidence, undo, cost, why, each vote as a face, the dissent's reason, evidence; Override… for a person.");
    unmount();

    await area("?area=agent_ops&item=work_payments");
    grab("05-flow-needs-you", "Flow chart — Needs you", "Production rollback always needs a person: the amber node pulses and carries its one button, Answer.");
    unmount();

    await area("?area=agent_ops&item=work_dep");
    grab("06-flow-overridden", "Flow chart — overridden", "A person's decision overrides the agent's: the red arc points at the row it overrode.");
    unmount();

    await area("?area=agent_ops&aav=roadmap");
    grab("07-roadmap", "Recommended roadmap", "Ranked cards: kind, value/effort/risk dots, evidence chips, the squad's faces; Accept (ticket/epic/PRD) · Not now · Reject with a reason.");
    unmount();

    fakeAmbientApi();
    setConsoleLive(false);
    const { AutonomyPanel } = await import("../../app/agent-ops/autonomy-panel");
    await mountAs(<AutonomyPanel projectId="p1" />, "owner");
    await settle(); await settle();
    grab("08-autonomy", "Govern › Autonomy — autonomy & safety",
         "The stop switch, today's agent budget, and the class × level matrix with each class's track record; BOSS's earned promotion is one Raise.");
    await click(tid("aa-kill-btn"));
    grab("09-kill-confirm", "Stop all agent work — the confirmation", "One confirmation; operators may stop, only owners resume or stop every project.");
    unmount();

    const { AgentsAtWorkCard, AmbientNeedRows } = await import("../../app/command-center/agents-at-work");
    const v = parseList(demoList("all"));
    const today = new Date().toISOString();
    const view = { ...v, items: v.items.map((i) => ({ ...i, resolved_at: i.resolved_at ? today : null })) };
    await mountAs(
      <div className="cc" style={{ display: "grid", gap: 14 }}>
        <section className="cc-card cc-needs cc-sec"><header className="cc-card-h"><h2>Needs you</h2></header>
          <div className="cc-card-body"><div className="cc-needs-list"><AmbientNeedRows view={view} listedDecisionIds={new Set()} /></div></div></section>
        <AgentsAtWorkCard view={view} />
      </div>, "owner");
    grab("10-command-center", "Command Center — Needs you row and the Agents at work card",
         "One queue: an agent item waiting on you is a row with its diagram; the card counts need you · running · resolved today.", 1000);
    unmount();

    mkdirSync(resolve(OUT, "screens"), { recursive: true });
    writeFileSync(resolve(OUT, "mock.css"), css());
    for (const s of screens) {
      writeFileSync(resolve(OUT, "screens", `${s.key}.html`),
        page(s.title, `<section class="wf-sec" style="max-width:${s.width}px"><div class="wf-frame">${s.html}</div></section>`, "../mock.css"));
    }
    const nav = screens.map((s) => `<a href="#${s.key}">${s.title.split(" — ")[0]}</a>`).join("");
    const body = `<h1>Agents at work — wireframes</h1>
<p class="wf-cap">Rendered from the real console components on the spec'd sample data (lib/ambient-demo.ts). Diagram first; text only in a drawer. PNGs in screens/.</p>
<p class="wf-nav">${nav}</p>
${screens.map((s) => `<section class="wf-sec" id="${s.key}"><h2>${s.title}</h2><p class="wf-cap">${s.caption}</p><div class="wf-frame" style="max-width:${s.width}px">${s.html}</div></section>`).join("\n")}`;
    writeFileSync(resolve(OUT, "index.html"), page("Agents at work wireframes", body, "mock.css"));
    expect(screens.length).toBe(10);
  });
});
