// @vitest-environment happy-dom
/**
 * AN ALERT ROW READS ONE WAY, AND LEADS TO ITS RULE (operator, Oct 18):
 *   - a rule row's SEV / Sample / Repo-managed chips wrap in their own cell, never overprint;
 *   - one state per alert row: its status chip. The incident link names the incident, not its
 *     status ("Pending" beside "Resolved"), and a pending alert says "pending N", not "open N";
 *   - each alert names the rule it came from (AppLatencyP95 vs SampleLatencyP95) and opens it:
 *     Respond automatically, Triage, Silence, Edit, from the Alerts page and the app drawer.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FIRING_ALERT, NOISE_ALERT, allAlertsWire, historyWire, ruleWire, rulesWire } from "./_helpers/alerts-fixtures";
import { $, click, settle, unmount } from "./_helpers/live-render";
import { openObs, params, text } from "./_helpers/obs-render";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: vi.fn(), push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => { unmount(); vi.unstubAllGlobals(); });

const css = readFileSync(join(process.cwd(), "app/observability/obs.css"), "utf8");
const rule = (sel: string) => css.match(new RegExp(`(?:^|\\n)${sel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*\\{([^}]*)\\}`))?.[1] ?? "";

/** A flap: fired at 11:00, cleared, pending again since 11:58, still tied to its resolved incident. */
const REPENDED = {
  ...FIRING_ALERT, id: "alrt_pend", rule_id: "arule_sample", kind: "custom", label: "SampleLatencyP95", alertname: "SampleLatencyP95",
  state: "pending", state_word: "Pending", fired_at: "2026-10-04T11:00:00.000Z", active_at: "2026-10-04T11:58:00.000Z",
  duration_s: undefined, incident: { id: "inc_old", title: "api (prod): p95 latency", status: "resolved", word: "Resolved" },
  status: { key: "pending", word: "Pending", tone: "grey", tip: null }, needs_attention: false,
};
const SAMPLE_RULE = ruleWire("latency_p95", { id: "arule_sample", kind: "custom", label: "SampleLatencyP95", alert: "SampleLatencyP95",
  condition: "Custom PromQL", sample: true, severity: "info", sev: "sev4", managed_by: "code", editable: "promql" });

describe("a rule row never overprints", () => {
  it("every cell wraps inside its own grid area; chips keep their size", () => {
    expect(rule(".obs-arule-name")).toMatch(/flex-wrap:\s*wrap/);
    expect(rule(".obs-arule-state")).toMatch(/flex-wrap:\s*wrap/);
    expect(rule(".obs-arule-acts")).toMatch(/flex-wrap:\s*wrap/);
    expect(rule(".obs-arule > *")).toMatch(/min-width:\s*0/);
    expect(css).toMatch(/\.obs-arule-name > \.obs-sev, \.obs-arule-name > \.obs-chip[^{]*\{[^}]*flex: none/);
  });
});

describe("one state per alert row", () => {
  it("a re-pended alert: one status chip, 'pending' from when it went pending, and an Incident link", async () => {
    await openObs("?view=apps&app=t_down&atab=alerts", { history: () => historyWire([REPENDED]) });
    const row = $("[data-testid='obs-alert-alrt_pend']")!;
    expect(row.querySelectorAll("[data-testid='obs-status']")).toHaveLength(1);
    expect(text(row.querySelector("[data-testid='obs-status']"))).toBe("Pending");
    expect(text(row.querySelector("[data-testid='obs-alert-duration']"))).toMatch(/^pending /);
    expect(text(row.querySelector("[data-testid='obs-alert-incident']"))).toBe("Incident");
    expect(text(row)).not.toMatch(/Resolved/);
  });

  it("a resolved alert's Incident button opens that alert's incident (BOSSAPP-620)", async () => {
    // The app's live error-rate incident is inc_1. This alert was resolved against inc_old.
    const resolved = {
      ...NOISE_ALERT, state: "resolved", state_word: "Resolved",
      incident: { id: "inc_old", title: "api (prod): yesterday's error rate", status: "resolved", word: "Resolved" },
    };
    const rec = await openObs("?view=alerts", { alerts: () => allAlertsWire([resolved]) });
    await click($("[data-testid='obs-alert-alrt_lat'] [data-testid='obs-alert-incident']")!);
    await settle();
    expect(params().get("inc")).toBe("inc_old");
    expect(params().get("app")).toBe("t_down");
    const responses = rec.calls.filter((u) => /\/incidents\/[^/]+\/response/.test(u));
    expect(responses.some((u) => u.includes("/incidents/inc_old/response"))).toBe(true);
    expect(responses.some((u) => u.includes("/incidents/inc_1/response"))).toBe(false);
  });

  it("the same button in the app's alert history opens that incident too", async () => {
    const resolved = {
      ...NOISE_ALERT, state: "resolved",
      incident: { id: "inc_old", title: "api (prod): yesterday's error rate", status: "resolved", word: "Resolved" },
    };
    const rec = await openObs("?view=apps&app=t_down&atab=alerts", { history: () => historyWire([resolved]) });
    await click($("[data-testid='obs-alert-alrt_lat'] [data-testid='obs-alert-incident']")!);
    await settle();
    expect(params().get("inc")).toBe("inc_old");
    const responses = rec.calls.filter((u) => /\/incidents\/[^/]+\/response/.test(u));
    expect(responses.some((u) => u.includes("/incidents/inc_old/response"))).toBe(true);
    expect(responses.some((u) => u.includes("/incidents/inc_1/response"))).toBe(false);
  });
});

describe("an alert leads to its rule", () => {
  it("from the app drawer: the rule's name, then Respond automatically, Triage, Silence and Edit", async () => {
    const rec = await openObs("?view=apps&app=t_down&atab=alerts", {
      history: () => historyWire([REPENDED, FIRING_ALERT]),
      rules: (id) => rulesWire(id, { rules: [ruleWire("error_rate"), SAMPLE_RULE] }),
    });
    const err = $("[data-testid='obs-alert-alrt_err']")!;
    expect(text(err.querySelector("[data-testid='obs-alert-rulename']"))).toBe("AppErrorRate");
    await click(err.querySelector("[data-testid='obs-alert-rule']")!);
    const sheet = $("[data-testid='obs-alert-rule-sheet']")!;
    expect(sheet.getAttribute("aria-label")).toBe("AppErrorRate");
    for (const id of ["respond", "triage", "silence", "edit"]) expect(sheet.querySelector(`[data-testid='obs-alert-rule-${id}']`)).not.toBeNull();
    expect(sheet.querySelector("[data-testid='obs-alert-rule-respond']")!.getAttribute("aria-checked")).toBe("true");
    await click(sheet.querySelector("[data-testid='obs-alert-rule-respond']")!);
    await settle();
    expect(rec.writes.at(-1)).toMatchObject({ url: "/v1/apps/t_down/alert-rules", method: "PUT",
      body: { rules: [{ id: "arule_error_rate", auto_respond: false }] } });
    await click($("[data-testid='obs-alert-rule-edit']")!);
    expect($("[data-testid='obs-rule-sheet']")).not.toBeNull();
  });

  it("from Observability › Alerts, for any app", async () => {
    await openObs("?view=alerts", { alerts: () => allAlertsWire([REPENDED]),
                                    rules: (id) => rulesWire(id, { rules: [ruleWire("error_rate"), SAMPLE_RULE] }) });
    await click($("[data-testid='obs-alert-alrt_pend'] [data-testid='obs-alert-rule']")!);
    const sheet = $("[data-testid='obs-alert-rule-sheet']")!;
    expect(sheet.getAttribute("aria-label")).toBe("SampleLatencyP95");
    await click(sheet.querySelector("[data-testid='obs-alert-rule-silence']")!);
    expect($("[data-testid='obs-silence-sheet']") ?? $("[role=dialog][aria-label^='Silence']")).not.toBeNull();
  });
});
