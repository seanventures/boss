/** The ambient API at the fetch boundary, answered from the sample (lib/ambient-demo.ts — the
 *  API's own wire shapes), with per-test overrides and every call recorded. */
import { demoAnswer } from "../../lib/ambient-demo";
import { stubFetch, type Answer, type Call } from "../_helpers/multirepo-fixtures";

/** `override` answers first: an Answer, `null` for a 404, or undefined for the sample. */
export function fakeAmbientApi(override?: (c: Call) => Answer | null): Call[] {
  return stubFetch((c) => {
    const o = override?.(c);
    if (o === null) return undefined;
    if (o) return o;
    if (c.method !== "GET") return { body: {} };
    const body = demoAnswer(`${c.path}${c.search}`);
    return body === undefined ? undefined : { body };
  });
}

export const writes = (calls: Call[], path: string | RegExp) =>
  calls.filter((c) => c.method !== "GET" && (typeof path === "string" ? c.path === path : path.test(c.path)));
