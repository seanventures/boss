// @vitest-environment happy-dom
/**
 * PROJECT › SETTINGS › TOOLCHAIN (BOSSAPP-825): one flow — Build image → terminal → Capture → chip,
 * on the /v1/toolchain-images routes; and the words when the docker build runtime is off.
 */
import React, { act } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { ToolchainImageCard } from "../app/projects/toolchain-image";
import { mountAs, stubFetch, tid, unmount, type Call } from "./_helpers/multirepo-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const P = "prj_java";
const settle = async (n = 8) => { for (let i = 0; i < n; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };
const press = async (el: Element | null) => { if (!el) throw new Error("nothing to press"); await act(async () => { (el as HTMLElement).click(); }); await settle(); };

const img = (over: Record<string, unknown>) => ({
  id: "tci_1", base: "python:3.11-slim", status: "building", phase: "session", image_ref: null, digest: null,
  error: null, commands: 0, transcript: [], shell_command: "docker exec -it c0ffee sh", created_at: "", ...over,
});
const view = (items: unknown[], docker = true) => ({
  docker_runtime: docker, registry: null, default_image: "python:3.11-slim", default_base: "python:3.11-slim",
  builds_use: "python:3.11-slim", items,
});

afterEach(() => unmount());

describe("toolchain image card", () => {
  it("starts a session, runs a command, captures, and shows the chip", async () => {
    let items: unknown[] = [];
    const calls: Call[] = stubFetch((c) => {
      if (c.method === "GET" && c.path === "/v1/toolchain-images") return { body: view(items) };
      if (c.method === "POST" && c.path === "/v1/toolchain-images") { items = [img({})]; return { body: items[0] }; }
      if (c.path.endsWith("/exec")) return { body: { command: "java -version", exit_code: 0, stdout: "openjdk 21\n", stderr: "" } };
      if (c.path.endsWith("/capture")) { items = [img({ phase: "capturing" })]; return { body: items[0] }; }
      return undefined;
    });
    await mountAs(<ToolchainImageCard projectId={P} actor="owner@x.test" />);
    await settle();
    expect(tid("px-toolchain-chip")?.textContent).toContain("Default image");

    await press(tid("px-toolchain-build"));
    expect(calls.some((c) => c.method === "POST" && c.path === "/v1/toolchain-images")).toBe(true);
    expect(tid("px-toolchain-chip")?.textContent).toContain("Setup open");
    expect(tid("px-toolchain-term")).not.toBeNull();

    const input = tid("px-toolchain-cmd") as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "java -version");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await press(tid("px-toolchain-run"));
    const exec = calls.find((c) => c.path === "/v1/toolchain-images/tci_1/exec");
    expect((exec?.body as { command: string }).command).toBe("java -version");
    expect(tid("px-toolchain-term")?.textContent).toContain("openjdk 21");

    await press(tid("px-toolchain-capture"));
    expect(calls.some((c) => c.path === "/v1/toolchain-images/tci_1/capture")).toBe(true);
    expect(tid("px-toolchain-chip")?.textContent).toContain("Capturing");
    expect(tid("px-toolchain-capture")).toBeNull();
  });

  it("says how to turn the docker runtime on instead of offering Build", async () => {
    stubFetch((c) => (c.method === "GET" ? { body: view([], false) } : undefined));
    await mountAs(<ToolchainImageCard projectId={P} actor="owner@x.test" />);
    await settle();
    expect(tid("px-toolchain-off")?.textContent).toContain("BOSS_SANDBOX_COMMAND_RUNTIME=docker");
    expect(tid("px-toolchain-build")).toBeNull();
  });

  it("shows a ready image as the one builds run", async () => {
    stubFetch((c) => (c.method === "GET" ? { body: { ...view([img({ status: "ready", image_ref: "boss-toolchain/p:tci_1", digest: "sha256:ab" })]), builds_use: "boss-toolchain/p:tci_1" } } : undefined));
    await mountAs(<ToolchainImageCard projectId={P} actor="owner@x.test" />);
    await settle();
    expect(tid("px-toolchain-chip")?.textContent).toContain("Project image ready");
    expect(tid("px-toolchain-build")?.textContent).toContain("Rebuild image");
  });
});
