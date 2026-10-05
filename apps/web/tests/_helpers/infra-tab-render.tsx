/**
 * Mount one Infra tab the way infra-home does — URL state through useUrlState/writeUrl — so a
 * tab's own drawers, filters and sheets run for real against mockInfraFetch().
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";

import { readInfraUrl } from "../../lib/infra-view";
import type { InfraTabProps } from "../../app/infra/infra-home";
import { useUrlState, writeUrl } from "../../app/infra/kit";

export function TabHarness({ Tab, query = "" }: { Tab: (p: InfraTabProps) => React.ReactElement | null; query?: string }) {
  const [url, reread] = useUrlState(readInfraUrl);
  const go = (patch: Record<string, string | null>, mode: "push" | "replace" = "push") => { writeUrl(patch, mode); reread(); };
  return <Tab projectId="project_1" actor="ceo" orgId={null} url={url} go={go} query={query} />;
}

export type Mounted = { host: HTMLDivElement; root: Root; unmount: () => Promise<void> };

export async function mount(el: React.ReactElement, search = ""): Promise<Mounted> {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  window.history.replaceState(null, "", `/${search}`);
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  await act(async () => { root.render(el); });
  return { host, root, unmount: async () => { await act(async () => root.unmount()); host.remove(); } };
}

/** Let fetches resolve and React settle until `until()` holds. */
export async function settle(until: () => boolean, what: string, scope: () => string = () => document.body.textContent ?? ""): Promise<void> {
  for (let i = 0; i < 60; i += 1) {
    if (until()) return;
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  }
  throw new Error(`${what} did not happen: ${scope().slice(0, 600)}`);
}

export async function press(el: Element | null | undefined, what = "element"): Promise<void> {
  if (!el) throw new Error(`missing ${what}`);
  await act(async () => { (el as HTMLElement).click(); });
}

export const byTestId = (id: string, scope: ParentNode = document): HTMLElement | null => scope.querySelector(`[data-testid="${id}"]`);
export const allByTestId = (id: string, scope: ParentNode = document): HTMLElement[] => [...scope.querySelectorAll<HTMLElement>(`[data-testid="${id}"]`)];
