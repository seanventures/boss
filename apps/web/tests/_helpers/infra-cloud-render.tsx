/**
 * Mount Infra › Cloud against the shared fixtures: the real CloudTab, the real poll, parsers and
 * writes, with `fetch` routed by mockInfraFetch (tests/_helpers/infra-fixtures.ts).
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import { CloudTab } from "../../app/infra/cloud";
import { useUrlState, writeUrl } from "../../app/infra/kit";
import { readInfraUrl } from "../../lib/infra-view";
import { mockInfraFetch, type Route } from "./infra-fixtures";

function Harness() {
  const [url, reread] = useUrlState(readInfraUrl);
  const go = (patch: Record<string, string | null>, mode: "push" | "replace" = "push") => { writeUrl(patch, mode); reread(); };
  return <CloudTab projectId="project_1" actor="ceo" orgId={null} url={url} go={go} query="" />;
}

export type Mounted = { host: HTMLDivElement; root: Root; api: ReturnType<typeof mockInfraFetch>; done: () => Promise<void> };

export async function mountCloud(search: string, routes: Route[] = []): Promise<Mounted> {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  window.history.replaceState(null, "", `/${search}`);
  try { window.localStorage.clear(); } catch { /* none */ }
  const api = mockInfraFetch({ routes });
  const uninstall = api.install();
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  await act(async () => { root.render(<Harness />); });
  return {
    host, root, api,
    done: async () => { await act(async () => root.unmount()); host.remove(); uninstall(); document.body.innerHTML = ""; },
  };
}

/** Let effects, fetches and re-renders run until `until()` holds. */
export async function settle(until: () => boolean, what: string): Promise<void> {
  for (let i = 0; i < 60; i += 1) {
    if (until()) return;
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  }
  throw new Error(`${what} did not happen: ${document.body.textContent?.slice(0, 600)}`);
}

export const $ = (testid: string): HTMLElement | null => document.querySelector(`[data-testid="${testid}"]`);
export const $$ = (testid: string): HTMLElement[] => [...document.querySelectorAll<HTMLElement>(`[data-testid="${testid}"]`)];

export async function press(testid: string): Promise<void> {
  const el = $(testid);
  if (!el) throw new Error(`missing ${testid}: ${document.body.textContent?.slice(0, 400)}`);
  await act(async () => { el.click(); });
}

/** Type into an input/textarea/select the way React hears it. */
export async function type(el: Element | null, value: string): Promise<void> {
  if (!el) throw new Error("no element to type into");
  const proto = Object.getPrototypeOf(el);
  const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
  await act(async () => {
    setter?.call(el, value);
    el.dispatchEvent(new Event(el.tagName === "SELECT" ? "change" : "input", { bubbles: true }));
  });
}
