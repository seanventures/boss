/**
 * Open Observability at a URL with the wire fixtures behind fetch (obs-fixtures.ts), the way the
 * console does: `?area=observability&<url>`. Returns the fetch recorder.
 */
import React from "react";

import { ObsHome } from "../../app/observability/obs-home";
import { mockObsFetch, type ObsBodies } from "./obs-fixtures";
import { mount, settle } from "./live-render";

export async function openObs(url: string, bodies: ObsBodies = {}) {
  const rec = mockObsFetch(bodies);
  window.history.replaceState(null, "", `/?area=observability&${url.replace(/^\?/, "")}`);
  await mount(<ObsHome projectId="p1" actor="ceo" />);
  await settle();
  return rec;
}

export const params = () => new URLSearchParams(window.location.search);
export const text = (el: Element | null) => (el?.textContent ?? "").replace(/\s+/g, " ").trim();
