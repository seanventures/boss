/**
 * Mount Infra against the shared fixtures: the real polls, parsers and components run, `fetch`
 * is mockInfraFetch() (ETags, 304s, recorded writes). `mountInfra` mounts the whole home (every
 * tab, NEEDS YOU, the header) — what the parity test opens; `mountFleet` mounts only the Fleet
 * tab and the header's `+ Add` / `Ask BOSS`, for tests that should not depend on the other tabs.
 *
 * Callers set `// @vitest-environment happy-dom` and mock `next/navigation` if a component asks.
 */
import React, { act, useCallback } from "react";
import { createRoot, type Root } from "react-dom/client";

import { readInfraUrl, useFleet } from "../../lib/infra-view";
import { mockInfraFetch, type Route, type Variant } from "./infra-fixtures";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | null = null;
let host: HTMLElement | null = null;
let uninstall: (() => void) | null = null;

export const PROJECT = "project_1";
export const ACTOR = "ceo";

export async function settle(rounds = 12): Promise<void> {
  for (let i = 0; i < rounds; i += 1) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
}

/** Point the console URL at `?area=infra&<search>`. */
export function setUrl(search: string): void {
  const q = search.replace(/^\?/, "");
  window.history.replaceState(null, "", `/?area=infra${q ? `&${q}` : ""}`);
}

async function mountNode(node: React.ReactElement): Promise<void> {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await act(async () => root!.render(node));
  await settle();
}

export type Mounted = ReturnType<typeof mockInfraFetch>;

export async function mountInfra(search: string, opts: { variant?: Variant; routes?: Route[] } = {}): Promise<Mounted> {
  setUrl(search);
  const mock = mockInfraFetch(opts);
  uninstall = mock.install();
  const { InfraHome } = await import("../../app/infra/infra-home");
  await mountNode(<InfraHome projectId={PROJECT} actor={ACTOR} orgId={null} />);
  return mock;
}

export async function mountFleet(search: string, opts: { variant?: Variant; routes?: Route[] } = {}): Promise<Mounted> {
  setUrl(search);
  const mock = mockInfraFetch(opts);
  uninstall = mock.install();
  const { FleetTab, InfraAddMenu, InfraAskBoss } = await import("../../app/infra/fleet");
  const { useUrlState, writeUrl } = await import("../../app/infra/kit");
  function Harness() {
    const fleet = useFleet(PROJECT, ACTOR);
    const [url, reread] = useUrlState(readInfraUrl);
    const go = useCallback((patch: Record<string, string | null>, mode: "push" | "replace" = "push") => {
      writeUrl(patch, mode);
      reread();
    }, [reread]);
    const props = { projectId: PROJECT, actor: ACTOR, orgId: null, url, go, query: url.q };
    return (
      <div className="inf" data-testid="infra">
        <header><InfraAddMenu {...props} fleet={fleet} /><InfraAskBoss {...props} fleet={fleet} /></header>
        <FleetTab {...props} fleet={fleet} />
      </div>
    );
  }
  await mountNode(<Harness />);
  return mock;
}

export function unmountInfra(): void {
  act(() => root?.unmount());
  host?.remove();
  root = null; host = null;
  uninstall?.(); uninstall = null;
  document.body.innerHTML = "";
}

export const $ = (sel: string) => document.querySelector<HTMLElement>(sel);
export const $$ = (sel: string) => [...document.querySelectorAll<HTMLElement>(sel)];
export const byTestId = (id: string) => $(`[data-testid="${id}"]`);

export async function click(el: Element | null | string): Promise<void> {
  const target = typeof el === "string" ? byTestId(el) : el;
  if (!target) throw new Error(`nothing to click: ${String(el)} in ${document.body.textContent?.slice(0, 400)}`);
  await act(async () => { (target as HTMLElement).click(); });
  await settle();
}

/** Type into an input/textarea/select the way React hears it. */
export async function type(el: Element | null | string, value: string): Promise<void> {
  const target = (typeof el === "string" ? byTestId(el) : el) as HTMLInputElement | null;
  if (!target) throw new Error(`nothing to type into: ${String(el)}`);
  const proto = target instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
    : target instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
  await act(async () => {
    setter?.call(target, value);
    target.dispatchEvent(new Event(target instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  });
  await settle(2);
}

/** Submit the form an element sits in. */
export async function submit(el: Element | null | string): Promise<void> {
  const target = typeof el === "string" ? byTestId(el) : el;
  const form = target instanceof HTMLFormElement ? target : target?.closest("form");
  if (!form) throw new Error(`no form around ${String(el)}`);
  await act(async () => { form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })); });
  await settle();
}
