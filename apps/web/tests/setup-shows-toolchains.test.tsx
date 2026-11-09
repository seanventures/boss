// @vitest-environment happy-dom
/**
 * Setup shows which build toolchains this install has: Python and Node always, Java when it is
 * turned on, and the command that turns it on when it is off.
 */
import { afterEach, describe, expect, it } from "vitest";

import { fakeApi, mountBoard, type Mounted } from "./_helpers/setup-board";
import { board } from "./_helpers/setup-fixtures";
import type { Toolchain } from "../lib/setup-types";

let mounted: Mounted | null = null;
afterEach(() => { mounted?.unmount(); mounted = null; });

const base: Toolchain[] = [
  { name: "python", label: "Python", installed: true, builtin: true, versions: {} },
  { name: "node", label: "Node", installed: true, builtin: true, versions: {} },
];

describe("Setup › Toolchains", () => {
  it("shows Java on, with its versions", async () => {
    const view = { ...board(), toolchains: [...base, { name: "java", label: "Java (Temurin JDK 21, Maven, Gradle)",
      installed: true, builtin: false, versions: { jdk: "Temurin 21.0.12.1+1", maven: "3.9.16" }, enable: null }] };
    fakeApi(view);
    mounted = await mountBoard(view);
    const java = document.body.querySelector('[data-testid="toolchain-java"]')!;
    expect(java.textContent).toBe("Java");
    expect(java.getAttribute("data-tip")).toContain("maven 3.9.16");
    expect(document.body.querySelector('[data-testid="toolchain-node"]')).toBeTruthy();
  });

  it("says how to turn Java on when it is off", async () => {
    const view = { ...board(), toolchains: [...base, { name: "java", label: "Java (Temurin JDK 21, Maven, Gradle)",
      installed: false, builtin: false, versions: {}, enable: "boss toolchains enable java" }] };
    fakeApi(view);
    mounted = await mountBoard(view);
    const java = document.body.querySelector('[data-testid="toolchain-java"]')!;
    expect(java.textContent).toBe("Java, off");
    expect(java.getAttribute("data-tip")).toContain("boss toolchains enable java");
  });

  it("is absent on an older API", async () => {
    const view = board();
    fakeApi(view);
    mounted = await mountBoard(view);
    expect(document.body.querySelector('[data-testid="su-row-toolchains"]')).toBeNull();
  });
});
