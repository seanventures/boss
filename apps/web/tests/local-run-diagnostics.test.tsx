// @vitest-environment happy-dom
import React, { act } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ShareRunButton, ShareRunSheet } from "../app/improve/share-run";
import { $, click, mount, rerender, settle, unmount } from "./_helpers/live-render";
import { posts, stubFetch } from "./_helpers/cockpit-render";
import { cockpit } from "./_helpers/factory-fixtures";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }) }));
vi.mock("../app/factory/legacy-build-parts", async (original) => ({ ...(await original<object>()), DeploymentLine: () => null }));
vi.mock("../app/deploy-ui", () => ({ LiveDeployment: () => null }));

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let files: Blob[];
let anchors: { filename: string; href: string }[];
let reads: URL[];
let answer: (url: URL, n: number) => Promise<Response> | Response;

beforeEach(() => {
  files = []; anchors = []; reads = [];
  answer = () => Response.json({ bundle: "build failed\nerror: [SECRET] [HOST]", masked: { SECRET: 1, HOST: 1 } });
  vi.spyOn(URL, "createObjectURL").mockImplementation((blob) => { files.push(blob as Blob); return "blob:owned-report"; });
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    anchors.push({ filename: this.download, href: this.href });
  });
  vi.stubGlobal("fetch", vi.fn(async (input: string, init?: RequestInit) => {
    expect(init?.method ?? "GET").toBe("GET");
    const url = new URL(String(input), "http://owned-fixture.invalid");
    reads.push(url);
    return answer(url, reads.length);
  }));
});

afterEach(() => { unmount(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

const sheet = (unitId = "crun_owned", projectId = "p_owned", actor = "owner") =>
  <ShareRunSheet projectId={projectId} actor={actor} unitKind="build" unitId={unitId} localOnly onClose={() => {}} />;

async function inspect() { await click($("[data-testid=diagnostic-inspected]")); }
async function edit(value: string) {
  await act(async () => {
    const textarea = $("[data-testid=share-run-bundle]") as HTMLTextAreaElement;
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(textarea, value);
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await settle();
}

it("prepares a scoped local report without collector reads, writes, or Send", async () => {
  await mount(sheet());
  expect(reads).toHaveLength(1);
  expect(Object.fromEntries(reads[0].searchParams)).toEqual({ view: "share_bundle", project_id: "p_owned", unit_kind: "build", unit_id: "crun_owned", actor: "owner" });
  expect($("[data-testid=share-run-send]")).toBeNull();
  expect($("[data-testid=share-run-note]")).toBeNull();
  expect(($("[data-testid=share-run-bundle]") as HTMLTextAreaElement).value).toContain("[SECRET]");
  expect(($("[data-testid=diagnostic-download]") as HTMLButtonElement).disabled).toBe(true);
  expect(files).toHaveLength(0);
});

it("saves exactly the inspected edited UTF-8 snapshot after a fresh access check", async () => {
  await mount(sheet());
  await inspect();
  await edit("bounded report · user removed private details\n[SECRET]");
  expect(($("[data-testid=diagnostic-inspected]") as HTMLInputElement).checked).toBe(false);
  await inspect();
  await click($("[data-testid=diagnostic-download]"));
  expect(reads).toHaveLength(2);
  expect(reads.every((url) => url.searchParams.get("view") === "share_bundle")).toBe(true);
  expect(files).toHaveLength(1);
  expect(files[0].type).toBe("text/plain;charset=utf-8");
  expect(await files[0].text()).toBe("bounded report · user removed private details\n[SECRET]");
  expect(anchors).toEqual([{ filename: "boss-run-diagnostics.txt", href: "blob:owned-report" }]);
  unmount();
  expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:owned-report");
});

it("refuses local save when permission is revoked after the preview", async () => {
  answer = (_url, n) => n === 1 ? Response.json({ bundle: "redacted report", masked: {} }) : Response.json({ error: "DENIED" }, { status: 403 });
  await mount(sheet()); await inspect(); await click($("[data-testid=diagnostic-download]"));
  expect(files).toHaveLength(0);
  expect($("[role=alert]")?.getAttribute("title")).toBe("You can't download this run");
});

it("does not display or export a late report from another run, project or actor", async () => {
  let finishOld!: (response: Response) => void;
  answer = (_url, n) => n === 1 ? new Promise((resolve) => { finishOld = resolve; }) : Response.json({ bundle: "new scoped report", masked: {} });
  await mount(sheet());
  await rerender(sheet("crun_other", "p_other", "another-owner"));
  await act(async () => finishOld(Response.json({ bundle: "old private report", masked: {} })));
  await settle();
  expect(($("[data-testid=share-run-bundle]") as HTMLTextAreaElement).value).toBe("new scoped report");
  expect(document.body.textContent).not.toContain("old private report");
  expect(($("[data-testid=diagnostic-download]") as HTMLButtonElement).disabled).toBe(true);
  expect(files).toHaveLength(0);
});

it("cancels an in-flight download check when the scope changes", async () => {
  let finishCheck!: (response: Response) => void;
  answer = (_url, n) => n === 2 ? new Promise((resolve) => { finishCheck = resolve; }) : Response.json({ bundle: "scoped report", masked: {} });
  await mount(sheet()); await inspect(); await click($("[data-testid=diagnostic-download]"));
  await rerender(sheet("crun_other"));
  await act(async () => finishCheck(Response.json({ bundle: "old report", masked: {} })));
  await settle();
  expect(files).toHaveLength(0);
  expect(($("[data-testid=diagnostic-download]") as HTMLButtonElement).disabled).toBe(true);
});

it.each([null, "x".repeat(400_001)])("refuses malformed or oversized preview data", async (bundle) => {
  answer = () => Response.json({ bundle, masked: {} });
  await mount(sheet());
  expect($("[data-testid=share-run-bundle]")).toBeNull();
  expect($("[role=alert]")?.textContent).toBe("Report unavailable");
  expect(files).toHaveLength(0);
});

it("offers the same local-only control for a workflow run", async () => {
  await mount(<ShareRunButton projectId="p_owned" actor="owner" unitKind="workflow_run" unitId="wfr_owned" />);
  await click($("[data-testid=diagnostic-open]"));
  expect(reads[0].searchParams.get("unit_kind")).toBe("workflow_run");
  expect(reads[0].searchParams.get("unit_id")).toBe("wfr_owned");
  expect($("[data-testid=share-run-send]")).toBeNull();
});

it("opens the actual Factory menu's diagnostic report for this build only", async () => {
  const { Cockpit } = await import("../app/factory/cockpit");
  const requests: URL[] = [];
  stubFetch(cockpit(), (input) => {
    if (!input.includes("/api/improve")) return undefined;
    const url = new URL(input, "http://owned-fixture.invalid"); requests.push(url);
    return { bundle: "redacted build report", masked: {} };
  });
  await mount(<Cockpit projectId="p1" runId="crun_inv2" actor="ceo" onClose={() => {}} onOpenBuild={() => {}} />);
  await click($("[data-testid=fx-more]"));
  await click($("[data-testid=fx-menu-diagnostics]"));
  expect($("[role=dialog]")?.getAttribute("aria-label")).toBe("Download diagnostics");
  expect(requests).toHaveLength(1);
  expect(Object.fromEntries(requests[0].searchParams)).toEqual({ view: "share_bundle", project_id: "p1", unit_kind: "build", unit_id: "crun_inv2", actor: "ceo" });
  expect(posts).toHaveLength(0);
  expect($("[data-testid=share-run-send]")).toBeNull();
});
