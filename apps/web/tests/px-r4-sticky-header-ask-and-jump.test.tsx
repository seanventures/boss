// @vitest-environment happy-dom
/**
 * Tester round 2 (px-r4, shell slice):
 *  X-30 / 01-15 / X-41  the project header is sticky (desktop: header + Needs-you + tabs; phone:
 *                       the compact tab bar stays), under the measured top bar;
 *  X-87                 a 16 px phone gutter on project pages;
 *  01-06 / 01-07        Code Tasks has Builds, Apps and Lab (`view=lab` lands on Lab), and the
 *                       rail tooltip says "Builds, apps and gated PRs (was Factory)" in every edition;
 *  09-01                ⌘K "Ask Helm…" opens the captain drawer (screen 09), never the dock;
 *  11-02 / 11-13        removing the scope keeps typing in the query; a closed palette forgets it.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { readFactoryUrl } from "../app/code-tasks-ui";
import { LabTab } from "../app/factory/lab-links";
import { JumpResults } from "../app/projects/jump";
import { askCaptain, askCaptainHref, useStickyHead } from "../app/projects/project-page";
import { communityNavCopy } from "../lib/edition";
import { codeOnly, cssOnly } from "./_helpers/source";
import { mountAs, stubFetch, tid, unmount } from "./_helpers/multirepo-fixtures";

const P = "helpdesk-ai-demo";
const wait = (ms = 220) => act(async () => { await new Promise((r) => setTimeout(r, ms)); });
afterEach(() => { unmount(); vi.restoreAllMocks(); window.history.replaceState(null, "", "/"); document.querySelector(".bx-topbar")?.remove(); });

describe("X-30 the project header is sticky", () => {
  const css = cssOnly("app/projects/px.css");
  it("desktop: header, Needs-you and tabs stick under the console top bar, above the content", () => {
    expect(css).toMatch(/\.px-sticky\[data-sticky\]\s*\{[^}]*position:\s*sticky;[^}]*top:\s*var\(--px-top/);
    // An opaque background, so the tab content never shows through while it scrolls under.
    expect(css).toMatch(/\.px-sticky\s*\{[^}]*background:[^}]*var\(--base\)\s*70%/);
    expect(codeOnly("app/projects/project-page.tsx")).toMatch(/className="px-sticky" data-sticky="" data-testid="px-sticky" ref=\{stickyRef\}/);
  });
  it("phone: only the compact tab bar stays (sticks at minus the header above it); mark and spacer hidden", () => {
    const phone = css.slice(css.indexOf("@media (max-width: 760px)"));
    expect(phone).toMatch(/\.px-sticky\[data-sticky\]\s*\{[^}]*top:\s*calc\(var\(--px-top, 62px\) - var\(--px-stick-off, 0px\)\)/);
    expect(phone).toMatch(/\.px-head \.px-mark/);
    expect(phone).toMatch(/\.px-head > \.px-spacer\s*\{\s*display:\s*none/);
    expect(phone).toMatch(/\.px-tabs\s*\{[^}]*overflow-x:\s*auto/);
  });
  it("measures the top bar and the header above the tabs (never a guessed constant)", async () => {
    const bar = document.createElement("div");
    bar.className = "bx-topbar";
    bar.getBoundingClientRect = () => ({ height: 62 } as DOMRect);
    document.body.appendChild(bar);
    let el: HTMLDivElement | null = null;
    function Probe() {
      const ref = useStickyHead();
      return <div ref={(n) => { el = n; if (n) { Object.defineProperty(n, "offsetHeight", { value: 180, configurable: true }); } ref(n); }}>
        <div className="px-tabs" ref={(t) => { if (t) Object.defineProperty(t, "offsetHeight", { value: 40, configurable: true }); }} /></div>;
    }
    await mountAs(<Probe />);
    expect(el!.style.getPropertyValue("--px-top")).toBe("62px");
    expect(el!.style.getPropertyValue("--px-stick-off")).toBe("140px");
  });
});

describe("X-87 the phone gutter is 16 px on project pages", () => {
  it("overrides the shell's 12 px narrow rule for the project areas", () => {
    const css = cssOnly("app/projects/px.css");
    expect(css).toMatch(/@media \(max-width: 760px\)\s*\{\s*\.bx-content:is\(\[data-active="project"\], \[data-active="projects"\]\)\s*\{\s*padding-left:\s*16px;\s*padding-right:\s*16px;/);
  });
});

describe("01-06 / 01-07 Code Tasks: Builds, Apps, Lab; 'was Factory'", () => {
  it("view=lab lands on Lab (it fell back to the Floor)", () => {
    expect(readFactoryUrl("?area=code_tasks&view=lab").view).toBe("lab");
    expect(readFactoryUrl("?area=code_tasks").view).toBe("floor");
  });
  it("the tabs read Builds, Apps, Lab (then Repositories, kept)", () => {
    const src = codeOnly("app/code-tasks-ui.tsx");
    const tabs = [...src.matchAll(/data-testid="fx-view-(\w+)"[\s\S]*?onClick=\{\(\) => setView\("\w+"\)\}>\s*(\w+)/g)].map((m) => m[2]);
    expect(tabs).toEqual(["Builds", "Apps", "Lab", "Repositories"]);
  });
  it("the Community rail keeps the tooltip's '(was Factory)'", () => {
    expect(communityNavCopy("code_tasks").sub).toBe("Builds, apps and gated PRs (was Factory)");
  });
  it("Lab lists each lab project, opening its own Code Tasks; says so when there is none", async () => {
    stubFetch((c) => (c.path === "/v1/templates/labs" ? { body: { labs: [{ project_id: "lab_1", name: "Juice Shop lab", app_name: "juice-shop" }] } } : undefined));
    await mountAs(<LabTab projectId={P} />);
    await wait(50);
    const row = tid("fx-lab-link") as HTMLAnchorElement;
    expect(row.textContent).toContain("Juice Shop lab");
    expect(new URLSearchParams(row.getAttribute("href")!.split("?")[1]).get("area")).toBe("code_tasks");
    unmount();
    stubFetch((c) => (c.path === "/v1/templates/labs" ? { body: { labs: [] } } : undefined));
    await mountAs(<LabTab projectId={P} />);
    await wait(50);
    expect(tid("fx-lab-empty")!.textContent).toMatch(/No lab projects yet/);
  });
});

describe("09-01 Ask Helm opens the captain drawer from every door", () => {
  it("on the project's page: the drawer opens in place with the question", () => {
    window.history.replaceState(null, "", `/?project_id=${P}&area=project&tab=overview`);
    askCaptain(P, { name: "Helm", role_key: "engineering_captain" }, "pr 3");
    const q = new URLSearchParams(window.location.search);
    expect(q.get("drawer")).toBe("captain");
    expect(q.get("cap_ask")).toBe("pr 3");
  });
  it("from anywhere else: the project's page opens with the drawer (not the dock)", () => {
    window.history.replaceState(null, "", "/?area=overview");
    const clicked: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) { clicked.push(this.getAttribute("href") ?? ""); });
    askCaptain(P, { name: "Helm", role_key: "engineering_captain" }, "status?");
    expect(clicked).toHaveLength(1);
    const q = new URLSearchParams(clicked[0].split("?")[1]);
    expect(q.get("area")).toBe("project");
    expect(q.get("project_id")).toBe(P);
    expect(q.get("drawer")).toBe("captain");
    expect(q.get("cap_ask")).toBe("status?");
    expect(new URLSearchParams(askCaptainHref(P).split("?")[1]).get("cap_ask")).toBeNull();
  });
  it("⌘K's Ask Helm row lands there", async () => {
    window.history.replaceState(null, "", `/?project_id=${P}&area=project`);
    stubFetch((c) => (c.path === "/v1/jump" ? { body: { items: [{ label: "Helm", kind: "captain", role_key: "engineering_captain", href: `?project_id=${P}&area=project&drawer=captain` }] } } : undefined));
    const done = vi.fn();
    await mountAs(<div className="bx-palette"><input defaultValue="pr 3" /><div className="bx-palette-list">
      <JumpResults query="pr 3" projectId={P} projects={[{ id: P, name: "HelpDesk AI Demo" }]} onDone={done} /></div></div>);
    await wait();
    await act(async () => { (tid("palette-jump-ask") as HTMLButtonElement).click(); });
    expect(done).toHaveBeenCalled();
    expect(new URLSearchParams(window.location.search).get("drawer")).toBe("captain");
    expect(new URLSearchParams(window.location.search).get("cap_ask")).toBe("pr 3");
  });
  it("no door opens the dock while a project is known", () => {
    const src = codeOnly("app/projects/project-page.tsx");
    const body = src.slice(src.indexOf("export function askCaptain("), src.indexOf("export function useStickyHead"));
    expect(body.indexOf("openDockAbout")).toBeGreaterThan(body.indexOf("if (!projectId)"));
    expect(body.match(/openDockAbout/g)).toHaveLength(1);
  });
});

describe("11-02 / 11-13 the palette input", () => {
  it("removing the scope tag keeps the focus in the query", async () => {
    stubFetch((c) => (c.path === "/v1/jump" ? { body: { items: [] } } : undefined));
    await mountAs(<div className="bx-palette"><input data-testid="q" defaultValue="pr" /><div className="bx-palette-list">
      <JumpResults query="pr" projectId={P} projects={[{ id: P, name: "HelpDesk AI Demo" }]} onDone={() => {}} /></div></div>);
    await wait();
    const unscope = tid("palette-jump-unscope") as HTMLButtonElement;
    unscope.focus();
    await act(async () => { unscope.click(); });
    expect(document.activeElement).toBe(tid("q"));
    expect(tid("palette-jump-scope")).toBeNull();
  });
  it("a closed palette forgets its query (Esc, the scrim or a chosen row)", () => {
    const src = codeOnly("app/ConsoleShell.tsx");
    expect(src).toMatch(/if \(paletteOpen\) \{[\s\S]{0,160}\}\s*setQuery\(""\);\s*return undefined;\s*\}, \[paletteOpen\]\);/);
  });
});
