// @vitest-environment happy-dom
/**
 * ⌘K (wireframe 11): the palette's project records, grouped like the wireframe — Work (PR #3 ·
 * Visual refresh finish, needs you, "↵ drawer · ⌘↵ GitHub"; Run; Build), Deploys & agents
 * (Staging · 203.0.113.20:8004 open ↗; Ask Helm about "pr 3"), Do (New work N; Switch project P) —
 * scoped to the open project with a removable tag, ↑/↓ + ↵ over the drawn list, ⌘↵ to GitHub,
 * and the empty / error states. Real JumpResults, fetch stubbed at the boundary.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { JumpResults, parseJumpRows } from "../app/projects/jump";
import { $$, mountAs, stubFetch, tid, unmount } from "./_helpers/multirepo-fixtures";

const P = "helpdesk-ai-demo";
const ITEMS = [
  { label: "PR #3 · Visual refresh finish", kind: "pr", needs_you: true, hint: "↵ drawer · ⌘↵ GitHub",
    external_url: "https://github.com/acme/helpdesk-ai-demo/pull/3", href: `?project_id=${P}&area=project&tab=work&drawer=build%3Acrun_3` },
  { label: "Run · Visual refresh finish", kind: "run", hint: "Workflows", href: `?project_id=${P}&area=project&tab=work&drawer=run%3Awfr_43` },
  { label: "Build · Visual refresh finish", kind: "build", hint: "Code Tasks", href: `?project_id=${P}&area=code_tasks&view=apps&build=crun_3` },
  { label: "Staging · 203.0.113.20:8004", kind: "deploy", hint: "open ↗", external_url: "http://203.0.113.20:8004", href: `?project_id=${P}&area=infra&view=deploys&deploy=sdep_1` },
  { label: "Helm", kind: "captain", hint: "captain", role_key: "engineering_captain", href: `?project_id=${P}&area=project&drawer=captain` },
];
const wait = (ms = 220) => act(async () => { await new Promise((r) => setTimeout(r, ms)); });

function Palette({ q, done = () => {} }: { q: string; done?: () => void }) {
  return (
    <div className="bx-palette"><input value={q} readOnly />
      <div className="bx-palette-list">
        <button type="button" className="bx-palette-item" data-testid="area-row">Command Center</button>
        <JumpResults query={q} projectId={P} projects={[{ id: P, name: "HelpDesk AI Demo" }, { id: "hello", name: "Hello World" }]} onDone={done} />
      </div>
    </div>
  );
}

afterEach(() => unmount());

describe("⌘K reads like screen 11", () => {
  it("groups Work, Deploys & agents and Do, with the wireframe's labels, tags and hints", async () => {
    const calls = stubFetch((c) => (c.path === "/v1/jump" ? { body: { items: ITEMS } } : undefined));
    await mountAs(<Palette q="pr 3" />);
    await wait();
    expect(calls.find((c) => c.path === "/v1/jump")!.search).toBe(`?q=pr+3&project_id=${P}`);
    expect($$(".pxj-lbl").map((l) => l.textContent)).toEqual(["Work", "Deploys & agents", "Do"]);
    const pr = tid("palette-jump-pr")!;
    expect(pr.querySelector(".pxj-t")!.textContent).toBe("PR #3 · Visual refresh finish");
    expect(pr.querySelector(".pxj-tag")!.textContent).toBe("needs you");
    expect(pr.querySelector(".pxj-k")!.textContent).toBe("↵ drawer · ⌘↵ GitHub");
    expect(tid("palette-jump-run")!.querySelector(".pxj-k")!.textContent).toBe("Workflows");
    expect(tid("palette-jump-build")!.querySelector(".pxj-k")!.textContent).toBe("Code Tasks");
    expect(tid("palette-jump-deploy")!.querySelector(".pxj-k")!.textContent).toBe("open ↗");
    expect(tid("palette-jump-ask")!.textContent).toContain("Ask Helm about “pr 3”");
    expect(tid("palette-jump-new-work")!.textContent).toContain("New work in this project");
    expect(tid("palette-jump-new-work")!.querySelector("kbd")!.textContent).toBe("N");
    expect(tid("palette-jump-switch")!.querySelector("kbd")!.textContent).toBe("P");
    // The scope tag and esc sit in the input row.
    expect(document.querySelector(".bx-palette [data-testid=palette-jump-scope]")!.textContent).toBe("HelpDesk AI Demo");
    expect(document.querySelector(".bx-palette .pxj-kbd")!.textContent).toBe("esc");
  });

  it("the scope tag's ✕ searches every project", async () => {
    const calls = stubFetch((c) => (c.path === "/v1/jump" ? { body: { items: ITEMS } } : undefined));
    await mountAs(<Palette q="deploy" />);
    await wait();
    await act(async () => { (tid("palette-jump-unscope") as HTMLButtonElement).click(); });
    await wait();
    expect(calls.filter((c) => c.path === "/v1/jump").pop()!.search).toBe("?q=deploy");
    expect(tid("palette-jump-scope")).toBeNull();
  });

  it("↓ moves the selection over the drawn list; ↵ opens it; ⌘↵ opens the PR on GitHub", async () => {
    stubFetch((c) => (c.path === "/v1/jump" ? { body: { items: ITEMS } } : undefined));
    const opened = vi.fn();
    vi.stubGlobal("open", opened);
    const done = vi.fn();
    await mountAs(<Palette q="pr 3" done={done} />);
    await wait();
    const key = (k: string, meta = false) => act(async () => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: k, metaKey: meta, bubbles: true, cancelable: true }));
    });
    await key("ArrowDown");
    const rows = $$(".bx-palette-item");
    expect(rows.find((r) => r.classList.contains("active"))!.getAttribute("data-testid")).toBe("area-row");
    await key("ArrowDown");
    expect(tid("palette-jump-pr")!.classList.contains("active")).toBe(true);
    await key("Enter", true);
    expect(opened).toHaveBeenCalledWith("https://github.com/acme/helpdesk-ai-demo/pull/3", "_blank", "noopener,noreferrer");
    expect(done).toHaveBeenCalled();
  });

  it("P on a chosen row lists the other projects", async () => {
    stubFetch((c) => (c.path === "/v1/jump" ? { body: { items: ITEMS } } : undefined));
    await mountAs(<Palette q="pr 3" />);
    await wait();
    await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true })); });
    await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "p", bubbles: true, cancelable: true })); });
    expect(tid("palette-jump-to-hello")!.textContent).toContain("Hello World");
  });

  it("no match offers to ask the captain; a failed search says so", async () => {
    stubFetch((c) => (c.path === "/v1/jump" ? (c.search.includes("zzz") ? { body: { items: [] } } : { status: 500, body: {} }) : undefined));
    await mountAs(<Palette q="zzz" />);
    await wait();
    expect(tid("palette-jump-empty")!.textContent).toBe("No matches · Ask Helm");
    unmount();
    stubFetch(() => ({ status: 500, body: {} }));
    await mountAs(<Palette q="boom" />);
    await wait();
    expect(tid("palette-jump-error")!.textContent).toBe("Search unavailable");
  });

  it("parses only rows it can open", () => {
    expect(parseJumpRows({ items: [{ label: "x", kind: "pr" }, { label: "y", kind: "pr", external_url: "https://g/1" }] }).map((r) => r.label)).toEqual(["y"]);
  });
});
